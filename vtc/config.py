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

    # Resume ledger ("processing history")
    ledger_enabled: bool = True
    ledger_file: Path | None = None         # defaults to <src>/.vtc_processed.log

    # Ignore rules — files the scan pretends it never saw. They are filtered at
    # discovery, so an ignored file is absent from the count, the estimate, the
    # queue and the report alike; it is never probed, decided or touched.
    #   0 / empty = that rule is off.
    ignore_under_bytes: int = 0             # skip files SMALLER than this
    ignore_over_bytes: int = 0              # skip files LARGER than this
    ignore_exts: tuple[str, ...] = ()       # extensions to skip, with or without the dot
    ignore_name_contains: tuple[str, ...] = ()   # skip if the filename contains any of these

    # Tools
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    # Tunables (defaults mirror the model / bash)
    # tier_bpp: per-tier density overrides, keyed by Tier.name ("EXCELLENT" -> 0.12).
    # Empty means "use the tier's built-in bpp"; a tier absent from the dict keeps
    # its default, so one edited tier doesn't drag the others with it.
    tier_bpp: dict[str, float] = field(default_factory=dict)
    bitrate_floor_kbps: int = BITRATE_FLOOR_KBPS
    tier_over_tolerance: float = TIER_OVER_TOLERANCE
    hevc_factor_hd: float = HEVC_FACTOR_HD
    hevc_factor_4k: float = HEVC_FACTOR_4K
    hevc_factor_8k: float = HEVC_FACTOR_8K

    video_exts: tuple[str, ...] = VIDEO_EXTS

    def bpp_for(self, tier: Tier | None = None) -> float:
        """The quality density actually in force for `tier` (default: the run's).

        A user override from Advanced settings wins; anything non-positive or
        unparseable falls back to the tier's own anchored bpp.
        """
        t = tier or self.tier
        try:
            v = float(self.tier_bpp.get(t.name, 0.0))
        except (TypeError, ValueError):
            return t.bpp
        return v if v > 0 else t.bpp

    def hevc_factors(self) -> tuple[float, float, float]:
        """The (HD, 4K, 8K+) H.265 efficiency factors this run should use."""
        return (self.hevc_factor_hd, self.hevc_factor_4k, self.hevc_factor_8k)

    def ignore_reason(self, name: str, size: int | None = None) -> str | None:
        """Why this file is ignored, or None to process it.

        `name` is the filename (not the whole path — a rule matching a parent
        directory's name would ignore whole trees by accident). `size` may be
        None when it could not be stat()ed, in which case the size rules simply
        don't apply rather than guessing.
        """
        low = name.lower()
        if size is not None:
            if self.ignore_under_bytes > 0 and size < self.ignore_under_bytes:
                return "smaller than the ignore-under size"
            if self.ignore_over_bytes > 0 and size > self.ignore_over_bytes:
                return "larger than the ignore-over size"
        if self.ignore_exts:
            ext = Path(low).suffix.lstrip(".")
            for raw in self.ignore_exts:
                if ext and ext == str(raw).strip().lstrip(".").lower():
                    return f"extension .{ext} is on the ignore list"
        for frag in self.ignore_name_contains:
            f = str(frag).strip().lower()
            if f and f in low:
                return f"filename contains {frag!r}"
        return None

    @property
    def has_ignore_rules(self) -> bool:
        return bool(self.ignore_under_bytes or self.ignore_over_bytes
                    or self.ignore_exts or self.ignore_name_contains)

    @property
    def needs_size_to_ignore(self) -> bool:
        """True when a rule depends on file size (so the scan must stat)."""
        return bool(self.ignore_under_bytes or self.ignore_over_bytes)

    def resolved_archive_dir(self) -> Path:
        return self.archive_dir or (self.src / "originals")

    def resolved_ledger_file(self) -> Path | None:
        if not self.ledger_enabled:
            return None
        return self.ledger_file or (self.src / ".vtc_processed.log")

    def settings_signature(self) -> str:
        """Ledger signature — a change in any of these re-evaluates every file."""
        parts = [
            self.tier.name,
            self.out_codec.value,
            f"rmx{int(self.remux_to_mp4)}",
            f"xc{int(self.compat_transcode)}",
            self.output_mode.value,
        ]
        # A retuned tier means a different target for every file, so history from
        # the old density must not count as done. Appended ONLY when the tier is
        # actually overridden, so a default run still matches ledgers written
        # before per-tier bpp existed.
        bpp = self.bpp_for()
        if abs(bpp - self.tier.bpp) > 1e-9:
            parts.append(f"bpp{bpp:.5f}")
        # The encoder changes the OUTPUT, not the decision — but the ledger records
        # "this file is done", and a file done in hardware is not done in software.
        # Without this, someone who re-runs with --encoder software precisely BECAUSE
        # they want better quality gets "already done" for the entire library and no
        # explanation. Appended only for an explicit choice, so a default (AUTO) run
        # still matches ledgers written before this existed.
        if self.encoder is not Encoder.AUTO:
            parts.append(f"enc{self.encoder.value}")
        return "|".join(parts)

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
