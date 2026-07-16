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
from pathlib import Path

from .config import Encoder, RunConfig
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


def use_hardware(config: RunConfig) -> bool:
    """Resolve whether to use VideoToolbox hardware encoding.

    HARDWARE -> True, SOFTWARE -> False, AUTO -> probe the ffmpeg encoder list
    for the relevant *_videotoolbox encoder (mirrors check_videotoolbox()).
    """
    if config.encoder == Encoder.HARDWARE:
        return True
    if config.encoder == Encoder.SOFTWARE:
        return False
    enc = "h264_videotoolbox" if config.out_codec == OutCodec.H264 else "hevc_videotoolbox"
    return enc in _encoders_list(config.ffmpeg)


# ── Video-arg builder ─────────────────────────────────────────────────────────

def _hevc_profile(info: MediaInfo) -> str:
    """main10 for a 10-bit source, else main (mirrors the pix_fmt case in bash)."""
    pix = (info.pix_fmt or "")
    if re.search(r"10(le|be)?$", pix) or "10" in pix:
        return "main10"
    return "main"


def build_video_args(
    config: RunConfig,
    info: MediaInfo,
    mode: Mode,
    target_kbps: int,
    hardware: bool,
) -> list[str]:
    """The `-c:v ...` argument list only (no input/output/audio/subs).

    REMUX     -> stream copy (+ hvc1 tag if the source is already HEVC).
    SHRINK    -> capped-CRF at the tuned tier ceiling (crf 20/21, preset medium).
    TRANSCODE -> higher-fidelity capped-CRF (crf 18/20, preset slow).
    Hardware (VideoToolbox) has no true CRF -> targets -b:v instead.
    """
    if mode == Mode.REMUX:
        vargs = ["-c:v", "copy"]
        if (info.vcodec or "").lower() == "hevc":
            vargs += ["-tag:v", "hvc1"]
        return vargs

    if mode == Mode.TRANSCODE:
        crf264, crf265, preset = 18, 20, "slow"
    else:  # SHRINK
        crf264, crf265, preset = 20, 21, "medium"

    bitrate = f"{target_kbps}k"
    bufsize = f"{target_kbps * 2}k"

    if config.out_codec == OutCodec.H264:
        if hardware:
            return ["-c:v", "h264_videotoolbox", "-b:v", bitrate,
                    "-profile:v", "high", "-pix_fmt", "yuv420p"]
        return ["-c:v", "libx264", "-crf", str(crf264), "-preset", preset,
                "-maxrate", bitrate, "-bufsize", bufsize,
                "-profile:v", "high", "-pix_fmt", "yuv420p"]

    profile = _hevc_profile(info)
    if hardware:
        return ["-c:v", "hevc_videotoolbox", "-b:v", bitrate,
                "-profile:v", profile, "-tag:v", "hvc1", "-bf", "0", "-fps_mode", "cfr"]
    return ["-c:v", "libx265", "-crf", str(crf265), "-preset", preset,
            "-maxrate", bitrate, "-bufsize", bufsize,
            "-profile:v", profile, "-tag:v", "hvc1"]


# ── ffmpeg execution ──────────────────────────────────────────────────────────

_DUR_RE = re.compile(r"out_time_ms=(\d+)")
_PROGRESS_RE = re.compile(r"progress=(\w+)")


def _run_ffmpeg(
    ffmpeg: str,
    args: list[str],
    label: str,
    duration: float,
    progress: ProgressCB | None,
) -> bool:
    """Run one ffmpeg invocation. Returns True on exit code 0.

    When a progress callback is supplied, `-progress pipe:1` output is parsed for
    a 0..1 fraction (indeterminate/None if the source duration is unknown).
    """
    if progress is None:
        try:
            r = subprocess.run(
                [ffmpeg, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    # Progress-wired path: stream `-progress pipe:1`.
    cmd = [ffmpeg, "-progress", "pipe:1", "-nostats", *args]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
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


def run_encode(
    config: RunConfig,
    info: MediaInfo,
    mode: Mode,
    src: Path,
    out: Path,
    target_kbps: int,
    hardware: bool,
    progress: ProgressCB | None = None,
) -> EncodeResult:
    """Encode `src` to `out` per `mode`, reproducing the bash attempt matrix.

    Writes directly to `out` (the pipeline handles placement/replacement). Tries
    embedding text subs (mov_text) then without; within each, audio stream-copy
    then AAC fallback. Falls back to sidecar .srt extraction when embedding
    fails. Never raises.
    """
    ffmpeg = config.ffmpeg
    vargs = build_video_args(config, info, mode, target_kbps, hardware)

    text_subs = info.text_subs
    image_subs = info.image_subs

    # Attempt matrix: embed text subs first (if any), then without.
    sub_attempts = ["embed", "none"] if text_subs else ["none"]

    encode_ok = False
    subs_embedded = False

    for sub_mode in sub_attempts:
        sub_maps: list[str] = []
        sub_flags: list[str] = []
        if sub_mode == "embed":
            for sub in text_subs:
                sub_maps += ["-map", f"0:{sub.index}"]
            sub_flags = ["-c:s", "mov_text"]

        for audio_mode in ("copy", "aac"):
            if audio_mode == "copy":
                audio_flags = ["-c:a", "copy"]
            else:
                audio_flags = ["-c:a", "aac", "-b:a", "384k"]

            args = [
                "-y", "-i", str(src),
                "-map", "0:v:0", "-map", "0:a?", *sub_maps,
                *vargs,
                *audio_flags, *sub_flags,
                "-movflags", "+faststart",
                str(out),
            ]
            if _run_ffmpeg(ffmpeg, args, label=out.name,
                           duration=info.duration, progress=progress):
                encode_ok = True
                subs_embedded = (sub_mode == "embed")
                break
            # Failed attempt: clear any partial output before the next try.
            try:
                out.unlink()
            except OSError:
                pass
        if encode_ok:
            break

    if not encode_ok:
        return EncodeResult(
            ok=False,
            out_path=out,
            error="encode failed (both audio-copy and AAC fallback)",
        )

    # Sidecar fallback: embedding failed but the encode succeeded — extract each
    # text track from the SOURCE to .srt next to the output.
    sidecars_made = 0
    sidecar_fail = 0
    if text_subs and not subs_embedded:
        sidecars_made, sidecar_fail = _extract_sidecars(ffmpeg, src, out, text_subs)

    # Anything about to be irrecoverably lost? Image subs never make it into MP4;
    # text subs that failed both embed and sidecar extraction are lost too.
    reasons: list[str] = []
    if image_subs:
        codecs = ", ".join(s.codec or "unknown" for s in image_subs)
        reasons.append(
            f"{len(image_subs)} image-based subtitle track(s) ({codecs}) "
            f"cannot be carried into MP4"
        )
    if sidecar_fail > 0:
        reasons.append(
            f"{sidecar_fail} text subtitle track(s) could not be embedded or extracted"
        )
    dropped_reason = "; ".join(reasons)

    try:
        out_bytes = out.stat().st_size
    except OSError:
        out_bytes = 0

    return EncodeResult(
        ok=True,
        out_path=out,
        out_bytes=out_bytes,
        subs_embedded=subs_embedded,
        sidecars_made=sidecars_made,
        dropped_subs_reason=dropped_reason,
    )
