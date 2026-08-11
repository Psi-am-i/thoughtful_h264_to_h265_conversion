"""ffprobe wrappers — probe a media file into plain dataclasses.

One `ffprobe -show_streams -show_format -of json` call per file, parsed into a
MediaInfo. No decisions here — just facts.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field, field
from fractions import Fraction
from pathlib import Path

from .winproc import NO_WINDOW, TEXT_UTF8

# Subtitle codecs that can live in an MP4 (as mov_text) or extract to .srt.
_TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}


@dataclass
class SubtitleTrack:
    index: int
    codec: str
    is_text: bool
    language: str = "und"
    forced: bool = False            # disposition.forced — subs for foreign-language lines only
    hearing_impaired: bool = False  # disposition.hearing_impaired — SDH / HoH captions
    default: bool = False           # disposition.default — the track players auto-select


@dataclass
class AudioTrack:
    index: int
    codec: str
    channels: int = 2
    language: str = "und"


@dataclass
class MediaInfo:
    path: Path
    ok: bool                       # False if the file could not be probed
    vcodec: str | None = None
    width: int = 0
    height: int = 0
    fps: float = 0.0
    bit_rate: int = 0              # video-stream bitrate in bps (0 if unknown)
    container_bit_rate: int = 0   # format-level bitrate in bps (fallback)
    duration: float = 0.0
    pix_fmt: str | None = None
    subtitles: list[SubtitleTrack] = field(default_factory=list)
    audio: list[AudioTrack] = field(default_factory=list)
    error: str = ""
    # VTC_* tags found on the file — present only if this tool produced it.
    vtc: dict = field(default_factory=dict)
    comment: str = ""            # the file's own comment, whoever wrote it

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def max_audio_channels(self) -> int:
        return max((a.channels for a in self.audio), default=0)

    @property
    def vtc_lossy_generation(self) -> bool:
        """True if THIS tool already spent a lossy generation on this file.

        A remux is a stream copy and costs nothing, so it does not count.
        """
        if self.vtc.get("VTC_MODE") in ("shrink", "transcode"):
            return True
        # Fall back to the comment. Custom keys need -movflags use_metadata_tags
        # in MP4, so a third-party remux can drop them while leaving the comment
        # intact — checking both makes the marker much harder to lose.
        c = (self.comment or "").lower()
        return VTC_SIGNATURE.lower() in c and ("shrink" in c or "transcode" in c)

    @property
    def vtc_summary(self) -> str:
        """Human-readable account of what we did to it, for the skip reason."""
        v = self.vtc
        if not v and self.comment and VTC_SIGNATURE.lower() in self.comment.lower():
            return self.comment
        bits = []
        if v.get("VTC_CODEC"):
            bits.append(v["VTC_CODEC"].upper().replace("H265", "H.265").replace("H264", "H.264"))
        if v.get("VTC_QUALITY"):
            bits.append(f"quality {v['VTC_QUALITY']}")
        if v.get("VTC_DATE"):
            bits.append(v["VTC_DATE"])
        return " · ".join(bits)

    @property
    def effective_bps(self) -> int:
        """Best available source bitrate: video stream, else container."""
        return self.bit_rate or self.container_bit_rate

    @property
    def text_subs(self) -> list[SubtitleTrack]:
        return [s for s in self.subtitles if s.is_text]

    @property
    def image_subs(self) -> list[SubtitleTrack]:
        return [s for s in self.subtitles if not s.is_text]


def _parse_fps(rate: str | None) -> float:
    if not rate:
        return 0.0
    try:
        return float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        return 0.0


# What we write into a file's comment when it has none. Human-readable on
# purpose: it shows up in VLC, MediaInfo and Plex, so the provenance is visible
# without any tooling.
VTC_SIGNATURE = "Very Thoughtful Compression"


def _to_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def probe(path: Path, ffprobe: str = "ffprobe") -> MediaInfo:
    """Probe `path` into a MediaInfo. Never raises — errors land in `.ok`/`.error`."""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, check=True, **TEXT_UTF8, **NO_WINDOW,
        ).stdout
        data = json.loads(out)
    except subprocess.CalledProcessError as e:
        return MediaInfo(path=path, ok=False, error=(e.stderr or "ffprobe failed").strip())
    except (json.JSONDecodeError, FileNotFoundError, OSError) as e:
        return MediaInfo(path=path, ok=False, error=str(e))

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    info = MediaInfo(path=path, ok=True)
    # Our own signature, if this file came out of an earlier run. Matroska
    # upper-cases tag keys and MP4 keeps them as written, so normalise.
    _tags = {str(k).upper(): str(v) for k, v in (fmt.get("tags") or {}).items()}
    info.vtc = {k: v for k, v in _tags.items() if k.startswith("VTC_")}
    info.comment = _tags.get("COMMENT", "")
    info.container_bit_rate = _to_int(fmt.get("bit_rate"))
    try:
        info.duration = float(fmt.get("duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        info.duration = 0.0

    for s in streams:
        kind = s.get("codec_type")
        if kind == "video" and info.vcodec is None:
            # first video stream wins (skip attached cover-art thumbnails)
            if s.get("disposition", {}).get("attached_pic"):
                continue
            info.vcodec = s.get("codec_name")
            info.width = _to_int(s.get("width"))
            info.height = _to_int(s.get("height"))
            info.fps = _parse_fps(s.get("avg_frame_rate")) or _parse_fps(s.get("r_frame_rate"))
            info.bit_rate = _to_int(s.get("bit_rate"))
            info.pix_fmt = s.get("pix_fmt")
        elif kind == "subtitle":
            codec = (s.get("codec_name") or "").lower()
            lang = (s.get("tags", {}) or {}).get("language", "und")
            disp = s.get("disposition", {}) or {}
            info.subtitles.append(SubtitleTrack(
                index=_to_int(s.get("index")),
                codec=codec,
                is_text=codec in _TEXT_SUB_CODECS,
                language=lang,
                forced=bool(disp.get("forced")),
                hearing_impaired=bool(disp.get("hearing_impaired")),
                default=bool(disp.get("default")),
            ))
        elif kind == "audio":
            lang = (s.get("tags", {}) or {}).get("language", "und")
            info.audio.append(AudioTrack(
                index=_to_int(s.get("index")),
                codec=(s.get("codec_name") or "").lower(),
                channels=_to_int(s.get("channels")) or 2,
                language=lang,
            ))
    return info
