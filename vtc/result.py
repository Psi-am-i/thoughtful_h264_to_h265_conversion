"""Shared result types and the progress-callback contract.

These are the interfaces every engine module agrees on: `encode`, `ledger`,
`report`, and `pipeline` all speak in terms of Mode / Outcome / FileResult, so
they can be built and tested independently and then composed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable


class Mode(str, Enum):
    """What we do to a file that is NOT left alone."""
    SHRINK = "shrink"          # re-encode a fat source down to the tier target
    TRANSCODE = "transcode"    # legacy/MP4-incompatible codec -> chosen codec, max fidelity
    REMUX = "remux"            # MP4-friendly codec in another container -> MP4, lossless copy


class Outcome(str, Enum):
    """Terminal outcome for a scanned file. Values match the bash outcome tags."""
    SHRINK = "shrink"
    TRANSCODE = "transcode"
    REMUX = "remux"
    SKIP_AT_TIER = "skip-at-tier"           # already at/under its tier target
    SKIP_MODERN = "skip-modern"             # already HEVC/AV1/VP9 in MP4
    SKIP_EXISTING = "skip-existing"         # output already existed
    SKIP_MIN_SAVING = "skip-min-saving"     # encoded, but saving too small -> kept original
    SKIP_INCOMPATIBLE = "skip-incompatible" # MP4-incompatible codec, transcode declined
    SKIP_CODEC = "skip-codec"               # unsupported/mezzanine codec, left untouched
    RESUME = "resume"                       # already done under these settings (ledger hit)
    ERROR = "error"                         # encode failed / empty output; source untouched

    @property
    def changed(self) -> bool:
        return self in (Outcome.SHRINK, Outcome.TRANSCODE, Outcome.REMUX)


# NOTE/WARN/ERROR lines for the problem report.
@dataclass
class Note:
    level: str   # "NOTE" | "WARN" | "ERROR"
    message: str


@dataclass
class FileDetail:
    """A structured record of exactly what happened to one file — the single source
    of truth for the report row, the log line, and the "*" detail. Populated once,
    at pipeline.process_file, where every decision (probe / mode / target /
    container / encode result) is already known. The report and log both format
    from THIS, so what's shown can never drift from what happened."""
    mode: str = ""                  # "shrink" | "transcode" | "remux"
    src_vcodec: str = ""            # source video codec, e.g. "h264"
    out_vcodec: str = ""            # output video codec ("" when unchanged, e.g. remux)
    src_ext: str = ""               # source container, e.g. ".mkv"
    out_ext: str = ""               # output container, e.g. ".mp4" / ".mkv"
    container_reason: str = ""      # WHY this container — set when output stays non-MP4
    width: int = 0
    height: int = 0
    fps: float = 0.0
    src_kbps: float = 0.0           # source video bitrate
    vid_kbps: float = 0.0           # video bitrate produced (target for a re-encode; source for a remux)
    out_kbps: float = 0.0           # actual total output bitrate (from size/duration)
    bpp: float = 0.0                # bits per pixel per frame at the video bitrate
    audio_action: str = ""          # "copied" | "AAC 256k" | "AC-3 448k" | "FLAC"
    subs_summary: str = ""          # "kept 1 image sub (MKV)" | "2 text subs embedded" | "dropped: …"

    @property
    def has_note(self) -> bool:
        """True when there's something worth flagging with a '*' — a non-MP4
        container that was kept on purpose, or a subtitle caveat."""
        return bool(self.container_reason) or self.subs_summary.startswith(("dropped", "sidecar"))

    def caption(self) -> str:
        """The one-line 'what happened' detail, shared by report and log."""
        codec = (f"{self.src_vcodec}→{self.out_vcodec}"
                 if self.out_vcodec and self.out_vcodec != self.src_vcodec else self.src_vcodec)
        bits = []
        if self.mode == "remux":
            bits.append(f"{codec} stream-copied, container unchanged" if self.src_ext == self.out_ext
                        else f"remuxed {self.src_ext}→{self.out_ext} · {codec} (copied)")
        elif self.mode == "transcode":
            bits.append(f"legacy {codec} transcoded @ {self.vid_kbps:.0f} kbps")
        else:  # shrink
            bits.append(f"{codec} shrunk to {self.vid_kbps:.0f} kbps")
        if self.bpp:
            bits.append(f"{self.bpp:.3f} bpp")
        if self.audio_action:
            bits.append(f"audio {self.audio_action}")
        if self.subs_summary:
            bits.append(self.subs_summary)
        if self.container_reason:
            bits.append(f"kept {self.out_ext[1:].upper()} — {self.container_reason}")
        return " · ".join(bits)


@dataclass
class FileResult:
    """One scanned file's terminal result. `report` aggregates a list of these."""
    path: Path
    outcome: Outcome
    src_bytes: int = 0          # source size (for space-savings; 0 if not replaced)
    out_bytes: int = 0          # output size (for space-savings; 0 if not replaced)
    elapsed_s: float = 0.0
    notes: list[Note] = field(default_factory=list)
    detail: FileDetail | None = None    # structured "what happened" (changed files)

    @property
    def saved_bytes(self) -> int:
        return max(0, self.src_bytes - self.out_bytes) if self.outcome.changed else 0


@dataclass
class EncodeResult:
    """What `encode.run_encode` returns to the pipeline."""
    ok: bool
    out_path: Path | None = None
    out_bytes: int = 0
    subs_embedded: bool = False
    sidecars_made: int = 0
    dropped_subs_reason: str = ""   # non-empty if some subs cannot survive (image subs, etc.)
    audio_action: str = ""          # what actually happened to audio: "copied"/"AAC 256k"/…
    error: str = ""


# progress(label, fraction 0..1, stats) — fraction may be None when indeterminate;
# stats is an optional {'fps','bitrate','speed'} snapshot from ffmpeg's -progress.
ProgressCB = Callable[[str, float | None, dict | None], None]


def _noop_progress(label: str, fraction: float | None, stats: dict | None = None) -> None:  # default sink
    pass
