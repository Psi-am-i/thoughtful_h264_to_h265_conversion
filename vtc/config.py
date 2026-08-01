"""Run configuration — the UI-agnostic settings object.

The CLI builds a RunConfig from argparse; the GUI will build the same object from
widgets. Nothing here does I/O; `pipeline` consumes it.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .model import (
    BITRATE_FLOOR_KBPS,
    HEVC_FACTOR_4K,
    HEVC_FACTOR_8K,
    HEVC_FACTOR_HD,
    TIER_OVER_TOLERANCE,
    OutCodec,
    Tier,
)

VIDEO_EXTS = ("mkv", "mp4", "mov", "avi", "webm", "m4v", "ts", "wmv", "flv")


class OutputMode(str, Enum):
    INPLACE = "inplace"        # replace the source in place
    SEPARATE = "separate"      # write to a separate folder


class SourceAction(str, Enum):
    ARCHIVE = "archive"        # move original to an archive folder
    DELETE = "delete"          # remove original after a successful replace
    KEEP = "keep"              # leave the original where it is


class Encoder(str, Enum):
    AUTO = "auto"              # hardware if available, else software
    HARDWARE = "hardware"      # VideoToolbox (macOS) / other HW where wired up
    SOFTWARE = "software"      # libx264 / libx265 capped-CRF


class AudioPolicy(str, Enum):
    PASSTHROUGH = "passthrough"   # copy tracks; convert only if the container can't hold them
    AAC = "aac"                   # re-encode all audio to AAC
    AC3 = "ac3"                   # re-encode to AC-3 (Dolby Digital) — home-theatre multichannel
    FLAC = "flac"                 # lossless — forces MKV


class Container(str, Enum):
    AUTO = "auto"   # MP4, but MKV when it must (lossless audio, image subs to keep, many tracks)
    MP4 = "mp4"
    MKV = "mkv"


# Audio codecs an MP4 container carries cleanly (others get converted on passthrough).
MP4_AUDIO_CODECS = {"aac", "ac3", "eac3", "mp3", "alac", "mp4als"}


@dataclass
class RunConfig:
    src: Path

    # Quality
    out_codec: OutCodec = OutCodec.H265
    tier: Tier = Tier.EXCELLENT
    min_saving_ratio: float = 0.75          # keep a shrink only if output <= this * source

    # Compatibility / non-MP4 policy
    remux_to_mp4: bool = True               # rehome MP4-friendly codecs into MP4 losslessly
    compat_transcode: bool = True           # transcode MP4-incompatible legacy codecs
    keep_source_container: bool = False     # shrink non-MP4 files but KEEP their container
    leave_non_mp4: bool = False             # don't touch non-MP4 containers at all

    # Subtitles: which tracks survive. Two INDEPENDENT filters, applied together,
    # so "English forced subs" is expressible (the old single sub_mode of
    # all|forced|hoh|lang made language and kind mutually exclusive).
    #   sub_langs — ISO codes to keep; empty means every language
    #   sub_kinds — any of "normal" / "forced" / "hoh"; empty means every kind
    sub_langs: tuple[str, ...] = ()
    sub_kinds: tuple[str, ...] = ()

    # Audio & container
    audio_policy: AudioPolicy = AudioPolicy.PASSTHROUGH
    audio_bitrate_stereo: int = 256         # kbps, AAC/AC-3, <=2 channels
    audio_bitrate_multichannel: int = 448   # kbps, AAC/AC-3, >2 channels
    container: Container = Container.AUTO
    keep_image_subs: bool = True            # prefer MKV over dropping PGS/DVD subtitle tracks
    mkv_if_tracks_over: int = 0             # 0 = off; force MKV when audio+sub tracks exceed this

    # Execution
    encoder: Encoder = Encoder.AUTO
    jobs: int = 1

    # Destination
    output_mode: OutputMode = OutputMode.INPLACE
    output_dir: Path | None = None
    output_flat: bool = False               # separate mode: flatten vs mirror tree
    source_action: SourceAction = SourceAction.ARCHIVE
    archive_dir: Path | None = None         # defaults to <src>/originals

    # Resume ledger
    ledger_enabled: bool = True
    ledger_file: Path | None = None         # defaults to <src>/.vtc_processed.log

    # Tools
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    # Tunables (defaults mirror the model / bash)
    bitrate_floor_kbps: int = BITRATE_FLOOR_KBPS
    tier_over_tolerance: float = TIER_OVER_TOLERANCE
    hevc_factor_hd: float = HEVC_FACTOR_HD
    hevc_factor_4k: float = HEVC_FACTOR_4K
    hevc_factor_8k: float = HEVC_FACTOR_8K

    video_exts: tuple[str, ...] = VIDEO_EXTS

    def resolved_archive_dir(self) -> Path:
        return self.archive_dir or (self.src / "originals")

    def resolved_ledger_file(self) -> Path | None:
        if not self.ledger_enabled:
            return None
        return self.ledger_file or (self.src / ".vtc_processed.log")

    def settings_signature(self) -> str:
        """Ledger signature — a change in any of these re-evaluates every file."""
        return "|".join([
            self.tier.name,
            self.out_codec.value,
            f"rmx{int(self.remux_to_mp4)}",
            f"xc{int(self.compat_transcode)}",
            self.output_mode.value,
        ])

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty = ok)."""
        errs: list[str] = []
        if not self.src.is_dir():
            errs.append(f"scan directory does not exist: {self.src}")
        if self.output_mode == OutputMode.SEPARATE and self.output_dir is None:
            errs.append("separate output mode requires output_dir")
        if self.jobs < 1:
            errs.append("jobs must be >= 1")
        if shutil.which(self.ffmpeg) is None:
            errs.append(f"ffmpeg not found: {self.ffmpeg!r}")
        if shutil.which(self.ffprobe) is None:
            errs.append(f"ffprobe not found: {self.ffprobe!r}")
        return errs
