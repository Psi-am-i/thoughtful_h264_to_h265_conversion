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
from pathlib import Path

from .config import MP4_AUDIO_CODECS, AudioPolicy, Container, Encoder, RunConfig
from .ffprobe import MediaInfo, SubtitleTrack
from .model import OutCodec
from .result import EncodeResult, Mode, ProgressCB

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
            stderr=subprocess.DEVNULL, timeout=30,
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
            return ["-c:v", enc, "-b:v", b, "-profile:v", prof, "-tag:v", "hvc1",
                    "-bf", "0", "-fps_mode", "cfr"]
        return ["-c:v", enc, "-b:v", b, "-profile:v", "high", "-pix_fmt", "yuv420p"]
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


# ── container + audio policy ──────────────────────────────────────────────────

def resolve_container(config: RunConfig, info: MediaInfo) -> Container:
    """Decide the output container for this file.

    Forced MP4/MKV is honoured. AUTO is MP4 unless something can only ride in
    Matroska: lossless (FLAC) audio, image-based subtitles you asked to keep
    (PGS/DVD — MP4 cannot hold them), or more tracks than the MKV threshold.
    """
    if config.container in (Container.MP4, Container.MKV):
        return config.container
    if config.audio_policy == AudioPolicy.FLAC:
        return Container.MKV
    if config.keep_image_subs and info.image_subs:
        return Container.MKV
    if config.mkv_if_tracks_over and (len(info.audio) + len(info.subtitles)) > config.mkv_if_tracks_over:
        return Container.MKV
    return Container.MP4


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


# ── ffmpeg execution ──────────────────────────────────────────────────────────

_DUR_RE = re.compile(r"out_time_ms=(\d+)")
_PROGRESS_RE = re.compile(r"progress=(\w+)")


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
    if progress is None:
        try:
            r = subprocess.run(
                [ffmpeg, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            if r.returncode != 0 and errsink is not None:
                tail = [ln.strip() for ln in (r.stderr or "").strip().splitlines() if ln.strip()]
                errsink.append(" / ".join(tail[-2:]) if tail else f"ffmpeg exit {r.returncode}")
            return r.returncode == 0
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
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        if err_f is not None:
            err_f.close()
        if errsink is not None:
            errsink.append(str(e))
        return False

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            m = _DUR_RE.search(line)
            if m and duration > 0:
                done = int(m.group(1)) / 1_000_000.0
                frac = max(0.0, min(1.0, done / duration))
                progress(label, frac)
            elif line.startswith("progress="):
                progress(label, None)
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
    text_subs = info.text_subs
    image_subs = info.image_subs

    encode_ok = False
    subs_embedded = False
    sidecars_made = 0
    sidecar_fail = 0
    reasons: list[str] = []
    errs: list[str] = []                # ffmpeg stderr tails from failed attempts

    if container == Container.MKV:
        # Matroska carries every stream — copy all subtitles, keep everything.
        for aargs in audio_attempts:
            args = [
                "-y", "-i", str(src),
                "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
                *vargs, *aargs, "-c:s", "copy",
                str(out),
            ]
            if _run_ffmpeg(ffmpeg, args, label=src.name, duration=info.duration,
                           progress=progress, errsink=errs):
                encode_ok = True
                subs_embedded = bool(text_subs or image_subs)
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
                    "-movflags", "+faststart",
                    str(out),
                ]
                if _run_ffmpeg(ffmpeg, args, label=src.name, duration=info.duration,
                               progress=progress, errsink=errs):
                    encode_ok = True
                    subs_embedded = (sub_mode == "embed")
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
    )
