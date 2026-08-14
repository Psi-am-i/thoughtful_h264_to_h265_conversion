"""ffmpeg command building + execution — the encode engine.

Mirrors the ENCODE section of very_thoughtful_compression.sh:

  * hardware (VideoToolbox) vs software (libx264/libx265) selection,
  * the per-mode video-arg builder (remux copy / capped-CRF shrink /
    higher-fidelity transcode),
  * the subtitle + audio attempt matrix (embed mov_text -> without; audio
    stream-copy -> AAC fallback), and
  * sidecar .srt extraction when embedding is impossible.

Pure stdlib. Never raises: on failure `run_encode` returns
EncodeResult(ok=False, error=...).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from .config import MP4_AUDIO_CODECS, AudioPolicy, Container, Encoder, RunConfig
from .ffprobe import VTC_SIGNATURE, MediaInfo, SubtitleTrack
from . import __version__
from .model import OutCodec
from .result import EncodeResult, Mode, ProgressCB
from .winproc import NO_WINDOW, TEXT_UTF8

# ── Encoder probing ───────────────────────────────────────────────────────────

# Cache of `ffmpeg -hide_banner -encoders` output, keyed by the ffmpeg binary.
_ENCODERS_CACHE: dict[str, str] = {}


def _encoders_list(ffmpeg: str) -> str:
    """Cached `ffmpeg -hide_banner -encoders` text (empty string on failure)."""
    cached = _ENCODERS_CACHE.get(ffmpeg)
    if cached is not None:
        return cached
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            **TEXT_UTF8, **NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    _ENCODERS_CACHE[ffmpeg] = out
    return out


# Cache of "does this encoder actually WORK here" keyed by (ffmpeg, encoder).
_ENCODER_WORKS_CACHE: dict[tuple[str, str], bool] = {}


def _encoder_works(ffmpeg: str, enc: str) -> bool:
    """True only if `enc` can encode a frame here — not merely that it is listed.

    'Listed' lies: an x86_64 ffmpeg under Rosetta lists hevc_videotoolbox but
    fails every encode (-22); a Windows box lists hevc_nvenc with no NVIDIA GPU.
    So actually try a 1-frame encode to the null muxer (fast, cached) — the honest
    test of whatever hardware the user actually has.
    """
    key = (ffmpeg, enc)
    cached = _ENCODER_WORKS_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-v", "error",
             "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=1",
             "-frames:v", "1", "-c:v", enc, "-f", "null", "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30, **NO_WINDOW,
        )
        ok = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    _ENCODER_WORKS_CACHE[key] = ok
    return ok


# Hardware encoders per output codec, in preference order: VideoToolbox (Apple),
# NVENC (NVIDIA), QSV (Intel), AMF (AMD). Each is used only if listed AND it passes
# _encoder_works — otherwise the next, then software. Covers "who knows what
# hardware people have" without trusting the encoder list.
_HW_CANDIDATES = {
    OutCodec.H265: ["hevc_videotoolbox", "hevc_nvenc", "hevc_qsv", "hevc_amf"],
    OutCodec.H264: ["h264_videotoolbox", "h264_nvenc", "h264_qsv", "h264_amf"],
}


def select_hw_encoder(config: RunConfig) -> str | None:
    """The hardware encoder to use, or None for software.

    SOFTWARE -> None. HARDWARE/AUTO -> the first candidate for the codec that is
    both listed and functionally works on this machine; None if none do (a broken
    or absent hardware encoder falls back to software, never fails every file).
    """
    if config.encoder == Encoder.SOFTWARE:
        return None
    listed = _encoders_list(config.ffmpeg)
    for enc in _HW_CANDIDATES.get(config.out_codec, []):
        if f" {enc} " in listed and _encoder_works(config.ffmpeg, enc):
            return enc
    return None


def hardware_report(ffmpeg: str) -> dict:
    """What hardware encoding is actually available here, for startup/logging/UI.

    Returns {'h264': name|None, 'h265': name|None, 'available': bool}. Probes
    functionally (cached), so it reflects the real machine, not the encoder list.
    """
    listed = _encoders_list(ffmpeg)
    out: dict = {}
    for codec, key in ((OutCodec.H264, "h264"), (OutCodec.H265, "h265")):
        found = None
        for enc in _HW_CANDIDATES[codec]:
            if f" {enc} " in listed and _encoder_works(ffmpeg, enc):
                found = enc
                break
        out[key] = found
    out["available"] = bool(out["h264"] or out["h265"])
    return out


def use_hardware(config: RunConfig) -> bool:
    """Back-compat boolean: is a working hardware encoder available for this codec?"""
    return select_hw_encoder(config) is not None


# ── Video-arg builder ─────────────────────────────────────────────────────────

def _hevc_profile(info: MediaInfo) -> str:
    """main10 for a 10-bit source, else main (mirrors the pix_fmt case in bash)."""
    pix = (info.pix_fmt or "")
    if re.search(r"10(le|be)?$", pix) or "10" in pix:
        return "main10"
    return "main"


# Consecutive B-frames for the VideoToolbox encoders. 2 is the usual sweet spot:
# the gain from 0 -> 2 is the large one, and past that returns fall off while
# encoder latency grows. See the note in _hw_video_args for the measurement.
VT_B_FRAMES = 2


def _hw_video_args(info: MediaInfo, enc: str, target_kbps: int) -> list[str]:
    """`-c:v` args for a specific hardware encoder, ABR-targeting the tier bitrate.

    Hardware encoders have no true CRF, so all aim at -b:v = the tier target (which
    is what makes them converge on a re-run). Flags differ by family:
      *_videotoolbox (Apple) · *_nvenc (NVIDIA) · *_qsv (Intel) · *_amf (AMD).
    """
    b = f"{target_kbps}k"
    is265 = "hevc" in enc
    prof = _hevc_profile(info) if is265 else "high"           # main / main10 / high
    tag = ["-tag:v", "hvc1"] if is265 else ["-pix_fmt", "yuv420p"]
    if enc.endswith("videotoolbox"):
        if is265:
            # B-frames ON. This inherited "-bf 0" from the bash script with no stated
            # reason, and it was costing real quality: a B-frame predicts from both
            # sides, so it codes far cheaper than a P-frame and leaves more of a fixed
            # bitrate for detail. Measured on a blocking-prone 1080p clip at the
            # EXCELLENT target: +0.87 dB XPSNR for +1.1% size, against a measured
            # run-to-run noise floor of 0.16 dB. Every HEVC decoder handles B-frames
            # (they are Main profile), so nothing is lost in compatibility.
            #
            # NB this matters MOST on the hardware path, which has no CRF to fall back
            # on: the tier bitrate is the whole quality knob, so efficiency IS quality.
            return ["-c:v", enc, "-b:v", b, "-profile:v", prof, "-tag:v", "hvc1",
                    "-bf", str(VT_B_FRAMES), "-fps_mode", "cfr"]
        return ["-c:v", enc, "-b:v", b, "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-bf", str(VT_B_FRAMES)]
    if enc.endswith("nvenc"):
        return ["-c:v", enc, "-b:v", b, "-maxrate", b, "-preset", "p5",
                "-profile:v", prof, *tag]
    if enc.endswith(("qsv", "amf")):
        return ["-c:v", enc, "-b:v", b, "-maxrate", b, "-profile:v", prof, *tag]
    return ["-c:v", enc, "-b:v", b, "-maxrate", b, *tag]      # unknown hw: plain ABR


def build_video_args(
    config: RunConfig,
    info: MediaInfo,
    mode: Mode,
    target_kbps: int,
    hw_encoder: str | None,
) -> list[str]:
    """The `-c:v ...` argument list only (no input/output/audio/subs).

    REMUX     -> stream copy (+ hvc1 tag if the source is already HEVC).
    SHRINK    -> capped-CRF at the tuned tier ceiling (crf 20/21, preset medium).
    TRANSCODE -> higher-fidelity capped-CRF (crf 18/20, preset slow).
    `hw_encoder` (a specific *_videotoolbox/nvenc/qsv/amf name, or None for
    software) is chosen by select_hw_encoder — hardware ABR-targets -b:v instead.
    """
    if mode == Mode.REMUX:
        vargs = ["-c:v", "copy"]
        if (info.vcodec or "").lower() == "hevc":
            vargs += ["-tag:v", "hvc1"]
        return vargs

    if hw_encoder:
        return _hw_video_args(info, hw_encoder, target_kbps)

    if mode == Mode.TRANSCODE:
        crf264, crf265, preset = 18, 20, "slow"
    else:  # SHRINK
        crf264, crf265, preset = 20, 21, "medium"

    # Software is capped-CRF: the CRF sets quality and -maxrate is only a ceiling. The
    # original bug was a LOOSE ceiling (bufsize = 2x target) that let the average land
    # ~16% over target — past the convergence gate — so every re-run re-encoded the
    # same file forever, spending a generation of quality each pass.
    #
    # Fix: cap AT the tier target with a tight (~1s) VBV buffer so the average actually
    # holds near it. Not at target x 1.10 (the skip line): a software encode lands ~1-2%
    # above its own maxrate (VBV slop), so a ceiling sitting on the skip line tips just
    # over it and still won't converge. Cap at the target and the tier_over_tolerance
    # does its intended job — absorbing that slop — so the next run sees the file as
    # at-tier and leaves it alone. Verified to converge (~101% of target) even on
    # near-incompressible content; easy content still lands below via the CRF.
    maxrate = f"{target_kbps}k"
    bufsize = f"{target_kbps}k"

    if config.out_codec == OutCodec.H264:
        return ["-c:v", "libx264", "-crf", str(crf264), "-preset", preset,
                "-maxrate", maxrate, "-bufsize", bufsize,
                "-profile:v", "high", "-pix_fmt", "yuv420p"]

    profile = _hevc_profile(info)
    return ["-c:v", "libx265", "-crf", str(crf265), "-preset", preset,
            "-maxrate", maxrate, "-bufsize", bufsize,
            "-profile:v", profile, "-tag:v", "hvc1"]


# ── our own signature on the files we make ────────────────────────────────────
# Written into every output so a later run can recognise its own work and say so,
# instead of inferring it from a codec or a folder name. That inference is what
# let a second run re-encode the first run's results: source -> H.264 -> H.265,
# an entire extra generation, invisible until someone read a report closely.
#
# MP4 needs -movflags use_metadata_tags or it silently DROPS unknown keys — the
# tags appear to be written and simply are not there. Matroska takes them as they
# are. Verified both ways round, alongside +faststart.
VTC_TAG_PREFIX = "VTC_"


def _codec_label(c: OutCodec) -> str:
    return {"h265": "H.265", "h264": "H.264"}.get(c.value, c.value.upper())


def vtc_metadata(config: RunConfig, mode: Mode, target_kbps: int,
                 info: MediaInfo | None = None) -> list[str]:
    """`-metadata` arguments recording what this tool did to this file.

    Also writes a human-readable line into `comment`, but ONLY when the source
    has none — a comment often carries a release group's own notes, and we do
    not overwrite what we did not put there. Where it does get written it is
    visible in VLC, MediaInfo and Plex, and it survives tooling that drops the
    custom keys.
    """
    from datetime import date
    tags = {
        "VTC_VERSION": __version__,
        "VTC_MODE": mode.value,                     # shrink / transcode / remux
        "VTC_TIER": config.tier.name,
        "VTC_QUALITY": str(round(config.bpp_for() * 1000)),
        "VTC_CODEC": config.out_codec.value,
        "VTC_DATE": date.today().isoformat(),
    }
    if target_kbps:
        tags["VTC_TARGET_KBPS"] = str(target_kbps)
    out: list[str] = []
    for k, v in tags.items():
        out += ["-metadata", f"{k}={v}"]
    # Clear the SOURCE's per-stream statistics off our new video stream. ffmpeg
    # copies stream tags verbatim, so a re-encoded file was inheriting the old
    # BPS/NUMBER_OF_BYTES and telling every tool that read it that its video
    # weighed more than the whole file. An empty value removes the tag.
    for stale in ("BPS", "BPS-eng", "NUMBER_OF_BYTES", "NUMBER_OF_BYTES-eng",
                  "NUMBER_OF_FRAMES", "NUMBER_OF_FRAMES-eng",
                  "_STATISTICS_TAGS", "_STATISTICS_TAGS-eng",
                  "_STATISTICS_WRITING_APP", "_STATISTICS_WRITING_APP-eng",
                  "_STATISTICS_WRITING_DATE_UTC", "_STATISTICS_WRITING_DATE_UTC-eng"):
        out += ["-metadata:s:v:0", f"{stale}="]
    if info is not None and not (info.comment or "").strip():
        what = "remuxed to" if mode is Mode.REMUX else f"{mode.value} to"
        out += ["-metadata", f"comment={VTC_SIGNATURE} {__version__} — {what} "
                             f"{_codec_label(config.out_codec)} at quality "
                             f"{round(config.bpp_for() * 1000)} on {date.today().isoformat()}"]
    return out


# ── container + audio policy ──────────────────────────────────────────────────

# Subtitle codecs MP4 can carry (text, embedded as mov_text). Anything else —
# image formats (PGS/DVD) or exotic text — can't be embedded and would otherwise
# be dropped or written as a sidecar .srt.
_MP4_EMBEDDABLE_SUBS = frozenset({
    "subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "vtt", "text",
})


def _has_unembeddable_subs(info: MediaInfo) -> bool:
    """True if the file carries any subtitle MP4 cannot embed — the only case where
    'avoid sidecar .srt' has anything to do: a normal text sub embeds fine and stays
    MP4, so it never forces the container."""
    return any((s.codec or "").lower() not in _MP4_EMBEDDABLE_SUBS
               for s in info.subtitles)


def _container_decision(config: RunConfig, info: MediaInfo) -> tuple[Container, str]:
    """The output container AND the human reason for it (reason set only for MKV).

    Forced MP4/MKV is honoured. AUTO is MP4 unless something can only ride in
    Matroska: lossless (FLAC) audio, image-based subtitles you asked to keep
    (PGS/DVD — MP4 cannot hold them), or more tracks than the MKV threshold.
    """
    if config.container == Container.MP4:
        return Container.MP4, ""
    if config.container == Container.MKV:
        return Container.MKV, "you set the container to MKV"
    # "Shrink but keep the source format": stay in the source's container. Fall back
    # to Matroska for containers that can't cleanly hold a modern codec (AVI etc.) or
    # when image subtitles need it, so we still never drop a stream.
    if config.keep_source_container:
        ext = info.path.suffix.lower()
        if config.keep_image_subs and info.image_subs and ext != ".mkv":
            return Container.MKV, "image subtitles need Matroska"
        if ext in (".mp4", ".m4v", ".mov"):
            return Container.MP4, ""
        if ext == ".mkv":
            return Container.MKV, "kept as MKV"
        return Container.MKV, "kept in Matroska (source container can't hold a modern codec)"
    if config.audio_policy == AudioPolicy.FLAC:
        return Container.MKV, "FLAC (lossless) audio needs Matroska"
    if config.keep_image_subs and info.image_subs:
        codecs = ", ".join(sorted({s.codec or "unknown" for s in info.image_subs}))
        return Container.MKV, f"image subtitles ({codecs}) MP4 can't hold"
    if config.mkv_if_text_subs and _has_unembeddable_subs(info):
        return Container.MKV, "keeps a subtitle MP4 can't embed (no sidecar .srt)"
    if config.mkv_if_tracks_over and (len(info.audio) + len(info.subtitles)) > config.mkv_if_tracks_over:
        return Container.MKV, f"more than {config.mkv_if_tracks_over} tracks"
    return Container.MP4, ""


def resolve_container(config: RunConfig, info: MediaInfo) -> Container:
    """Decide the output container for this file (see `_container_decision`)."""
    return _container_decision(config, info)[0]


def container_reason(config: RunConfig, info: MediaInfo) -> str:
    """Why the output container is what it is — non-empty only when it is NOT MP4,
    so the report can explain why a file stayed MKV instead of looking like a bug."""
    container, why = _container_decision(config, info)
    return why if container == Container.MKV else ""


def _audio_bitrate(config: RunConfig, info: MediaInfo) -> int:
    return (config.audio_bitrate_multichannel if info.max_audio_channels > 2
            else config.audio_bitrate_stereo)


def _audio_attempts(config: RunConfig, info: MediaInfo, container: Container) -> list[list[str]]:
    """Ordered `-c:a ...` attempts for the chosen policy/container.

    PASSTHROUGH copies (MKV holds anything; MP4 falls back to AAC for codecs it
    can't carry). Forced AAC/AC-3/FLAC are a single attempt.
    """
    policy = config.audio_policy
    br = _audio_bitrate(config, info)
    aac = ["-c:a", "aac", "-b:a", f"{br}k"]
    if policy == AudioPolicy.AAC:
        return [aac]
    if policy == AudioPolicy.AC3:
        return [["-c:a", "ac3", "-b:a", f"{br}k"]]
    if policy == AudioPolicy.FLAC:
        return [["-c:a", "flac"]]
    # PASSTHROUGH
    if container == Container.MKV:
        return [["-c:a", "copy"]]
    # MP4: copy if every track is MP4-friendly is likely; else AAC. Try both.
    if info.audio and all(a.codec in MP4_AUDIO_CODECS for a in info.audio):
        return [["-c:a", "copy"]]
    return [["-c:a", "copy"], aac]


def _describe_audio(aargs: list[str], has_audio: bool) -> str:
    """Human summary of the `-c:a …` that actually succeeded, for the file record."""
    if not has_audio:
        return "no audio"
    codec = aargs[1] if len(aargs) > 1 else ""
    if codec == "copy":
        return "copied"
    br = aargs[3] if len(aargs) > 3 else ""
    name = {"aac": "AAC", "ac3": "AC-3", "flac": "FLAC"}.get(codec, codec.upper())
    return f"{name} {br}" if br else name


def _track_kinds(s) -> set:
    """
    Which kinds a subtitle track counts as.

    A track can be BOTH forced and hearing-impaired, so this returns a set
    rather than picking one label — otherwise a forced SDH track would be
    invisible to someone who asked for HoH. A track that is neither is 'normal'.
    """
    kinds = set()
    if getattr(s, "forced", False):
        kinds.add("forced")
    if getattr(s, "hearing_impaired", False):
        kinds.add("hoh")
    if not kinds:
        kinds.add("normal")
    return kinds


def _select_subs(config: RunConfig, subs: list) -> list:
    """
    The subtitle tracks to keep.

    Language and kind are INDEPENDENT filters applied in series, so
    "English forced subs" is expressible. The previous single `sub_mode`
    (all/forced/hoh/lang) made them mutually exclusive: choosing languages
    meant you could no longer restrict to forced, and vice versa.

    An empty language list means every language. An empty kind list means
    every kind — NOT "drop everything" — because a config that silently
    discarded all subtitles would be a nasty default to inherit.
    """
    out = list(subs)

    langs = {l.lower() for l in getattr(config, "sub_langs", ()) or ()}
    if langs:
        out = [s for s in out if (s.language or "und").lower() in langs]

    kinds = {k.lower() for k in getattr(config, "sub_kinds", ()) or ()}
    if kinds:
        out = [s for s in out if _track_kinds(s) & kinds]

    return out


# ── ffmpeg execution ──────────────────────────────────────────────────────────

_DUR_RE = re.compile(r"out_time_ms=(\d+)")
_PROGRESS_RE = re.compile(r"progress=(\w+)")


# ── live process registry (for "stop immediately") ────────────────────────────
# Every ffmpeg this process starts is registered while it runs, so an abort can
# kill them mid-encode. That is safe by construction: an encode writes to a TEMP
# file and is only moved into place after it succeeds, so a killed encode leaves
# the original untouched and the half-written temp is discarded by the caller.
_live_procs: set[subprocess.Popen] = set()
_live_lock = threading.Lock()
# Killing the running ffmpeg is not enough on its own: an encode is a LADDER of
# attempts (audio strategies, subtitles embedded then not), so a killed attempt
# just looks like a failed one and the next rung starts a fresh ffmpeg. This latch
# makes every remaining attempt fail immediately instead.
_abort = threading.Event()


def clear_abort() -> None:
    """Re-arm for a new run. Without this an aborted run poisons the next one."""
    _abort.clear()


def aborted() -> bool:
    return _abort.is_set()


class _tracked:
    """Register `proc` as killable for as long as it is running."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc

    def __enter__(self):
        with _live_lock:
            _live_procs.add(self.proc)
        return self.proc

    def __exit__(self, *exc):
        with _live_lock:
            _live_procs.discard(self.proc)
        return False


def abort_running() -> int:
    """Kill every ffmpeg started by this process. Returns how many were signalled."""
    _abort.set()
    with _live_lock:
        procs = list(_live_procs)
    n = 0
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
                n += 1
        except OSError:
            pass
    return n


def _run_ffmpeg(
    ffmpeg: str,
    args: list[str],
    label: str,
    duration: float,
    progress: ProgressCB | None,
    errsink: list[str] | None = None,
) -> bool:
    """Run one ffmpeg invocation. Returns True on exit code 0.

    When a progress callback is supplied, `-progress pipe:1` output is parsed for
    a 0..1 fraction (indeterminate/None if the source duration is unknown).
    On failure, the tail of ffmpeg's stderr is appended to `errsink` (when given)
    so the caller can report *why* it failed rather than a generic message.
    """
    if _abort.is_set():          # cancelled: don't start another attempt
        if errsink is not None:
            errsink.append("cancelled")
        return False
    if progress is None:
        # Popen rather than subprocess.run purely so the process is REGISTERED and
        # an abort can reach it — this is the path the CLI encodes on.
        try:
            proc = subprocess.Popen(
                [ffmpeg, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True, **TEXT_UTF8, **NO_WINDOW,
            )
            with _tracked(proc):
                _, err = proc.communicate()
            if proc.returncode != 0 and errsink is not None:
                tail = [ln.strip() for ln in (err or "").strip().splitlines() if ln.strip()]
                errsink.append(" / ".join(tail[-2:]) if tail else f"ffmpeg exit {proc.returncode}")
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError) as e:
            if errsink is not None:
                errsink.append(str(e))
            return False

    # Progress-wired path: stream `-progress pipe:1`. stderr goes to a temp file so
    # a failure here still reports why (the GUI runs on this path) without risking a
    # pipe-buffer deadlock while we drain stdout.
    cmd = [ffmpeg, "-progress", "pipe:1", "-nostats", *args]
    err_f = None
    try:
        err_f = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err_f,
            text=True, **TEXT_UTF8, **NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        if err_f is not None:
            err_f.close()
        if errsink is not None:
            errsink.append(str(e))
        return False

    stats: dict = {}
    with _tracked(proc):
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("fps="):
                    stats["fps"] = line[4:]
                elif line.startswith("bitrate="):
                    stats["bitrate"] = line[8:]
                elif line.startswith("speed="):
                    stats["speed"] = line[6:]
                m = _DUR_RE.search(line)
                if m and duration > 0:
                    done = int(m.group(1)) / 1_000_000.0
                    frac = max(0.0, min(1.0, done / duration))
                    progress(label, frac, dict(stats))
                elif line.startswith("progress="):
                    progress(label, None, dict(stats))
        except Exception:  # noqa: BLE001 — progress must never break the encode
            pass
        proc.wait()
    if proc.returncode != 0 and errsink is not None:
        try:
            err_f.seek(0)
            tail = [ln.strip() for ln in err_f.read().strip().splitlines() if ln.strip()]
            errsink.append(" / ".join(tail[-2:]) if tail else f"ffmpeg exit {proc.returncode}")
        except OSError:
            pass
    if err_f is not None:
        err_f.close()
    return proc.returncode == 0


def _extract_sidecars(
    ffmpeg: str,
    src: Path,
    out: Path,
    text_subs: list[SubtitleTrack],
) -> tuple[int, int]:
    """Extract each text sub to a `.srt` next to `out`.

    Naming mirrors the bash: single track -> `{stem}.{lang}.srt`; multiple ->
    `{stem}.{k}.{lang}.srt` (1-based). Returns (made, failed).
    """
    made = 0
    failed = 0
    stem = out.with_suffix("")  # strips .mp4
    single = len(text_subs) == 1
    for k, sub in enumerate(text_subs, start=1):
        lang = sub.language or "und"
        if single:
            sidecar = Path(f"{stem}.{lang}.srt")
        else:
            sidecar = Path(f"{stem}.{k}.{lang}.srt")
        ok = _run_ffmpeg(
            ffmpeg,
            ["-y", "-i", str(src), "-map", f"0:{sub.index}", "-c:s", "srt", str(sidecar)],
            label="", duration=0.0, progress=None,
        )
        if ok:
            made += 1
        else:
            try:
                sidecar.unlink()
            except OSError:
                pass
            failed += 1
    return made, failed


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def run_encode(
    config: RunConfig,
    info: MediaInfo,
    mode: Mode,
    src: Path,
    out: Path,
    target_kbps: int,
    hw_encoder: str | None,
    container: Container | None = None,
    progress: ProgressCB | None = None,
) -> EncodeResult:
    """Encode `src` to `out` per `mode` and container. Writes directly to `out`.

    MKV: one shot copying every subtitle stream (text AND image — PGS/DVD survive),
    audio per policy. MP4: the attempt matrix — embed text subs (mov_text) then
    without; audio per policy (copy -> AAC fallback on passthrough); sidecar .srt
    when embedding fails; image subs are dropped and reported. Never raises.
    """
    if container is None:
        container = resolve_container(config, info)
    ffmpeg = config.ffmpeg
    vargs = build_video_args(config, info, mode, target_kbps, hw_encoder)
    audio_attempts = _audio_attempts(config, info, container)
    # Which subtitle tracks survive (all / forced / hoh / languages), split by kind.
    kept_subs = _select_subs(config, info.subtitles)
    text_subs = [s for s in kept_subs if s.is_text]
    image_subs = [s for s in kept_subs if not s.is_text]
    all_subs_kept = len(kept_subs) == len(info.subtitles)

    encode_ok = False
    subs_embedded = False
    sidecars_made = 0
    sidecar_fail = 0
    used_aargs: list[str] = []          # the audio attempt that actually worked
    reasons: list[str] = []
    errs: list[str] = []                # ffmpeg stderr tails from failed attempts

    if container == Container.MKV:
        # Matroska carries every stream. Copy all subtitles, or just the selected
        # ones when a subtitle filter is active.
        mkv_sub_maps = (["-map", "0:s?"] if all_subs_kept
                        else [x for s in kept_subs for x in ("-map", f"0:{s.index}")])
        for aargs in audio_attempts:
            args = [
                "-y", "-i", str(src),
                "-map", "0:v:0", "-map", "0:a?", *mkv_sub_maps,
                *vargs, *aargs, "-c:s", "copy",
                *vtc_metadata(config, mode, target_kbps, info),
                str(out),
            ]
            if _run_ffmpeg(ffmpeg, args, label=src.name, duration=info.duration,
                           progress=progress, errsink=errs):
                encode_ok = True
                subs_embedded = bool(text_subs or image_subs)
                used_aargs = aargs
                break
            _unlink(out)
    else:  # MP4 — embed text subs / audio matrix / sidecar / drop image subs
        sub_attempts = ["embed", "none"] if text_subs else ["none"]
        for sub_mode in sub_attempts:
            sub_maps: list[str] = []
            sub_flags: list[str] = []
            if sub_mode == "embed":
                for sub in text_subs:
                    sub_maps += ["-map", f"0:{sub.index}"]
                sub_flags = ["-c:s", "mov_text"]
            for aargs in audio_attempts:
                args = [
                    "-y", "-i", str(src),
                    "-map", "0:v:0", "-map", "0:a?", *sub_maps,
                    *vargs, *aargs, *sub_flags,
                    *vtc_metadata(config, mode, target_kbps, info),
                    "-movflags", "+faststart+use_metadata_tags",
                    str(out),
                ]
                if _run_ffmpeg(ffmpeg, args, label=src.name, duration=info.duration,
                               progress=progress, errsink=errs):
                    encode_ok = True
                    subs_embedded = (sub_mode == "embed")
                    used_aargs = aargs
                    break
                _unlink(out)
            if encode_ok:
                break

    if not encode_ok:
        detail = f" — {errs[-1]}" if errs else ""
        return EncodeResult(ok=False, out_path=out,
                            error=f"encode failed (all audio/subtitle strategies){detail}")

    if container != Container.MKV:
        if text_subs and not subs_embedded:
            sidecars_made, sidecar_fail = _extract_sidecars(ffmpeg, src, out, text_subs)
        if image_subs:
            codecs = ", ".join(s.codec or "unknown" for s in image_subs)
            reasons.append(f"{len(image_subs)} image-based subtitle track(s) ({codecs}) "
                           f"cannot be carried into MP4")
        if sidecar_fail > 0:
            reasons.append(f"{sidecar_fail} text subtitle track(s) could not be embedded or extracted")

    try:
        out_bytes = out.stat().st_size
    except OSError:
        out_bytes = 0

    return EncodeResult(
        ok=True, out_path=out, out_bytes=out_bytes,
        subs_embedded=subs_embedded, sidecars_made=sidecars_made,
        dropped_subs_reason="; ".join(reasons),
        audio_action=_describe_audio(used_aargs, bool(info.audio)),
    )
