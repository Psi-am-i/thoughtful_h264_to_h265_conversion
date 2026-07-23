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
class FileResult:
    """One scanned file's terminal result. `report` aggregates a list of these."""
    path: Path
    outcome: Outcome
    src_bytes: int = 0          # source size (for space-savings; 0 if not replaced)
    out_bytes: int = 0          # output size (for space-savings; 0 if not replaced)
    elapsed_s: float = 0.0
    notes: list[Note] = field(default_factory=list)

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
    error: str = ""


# progress(label, fraction 0..1, stats) — fraction may be None when indeterminate;
# stats is an optional {'fps','bitrate','speed'} snapshot from ffmpeg's -progress.
ProgressCB = Callable[[str, float | None, dict | None], None]


def _noop_progress(label: str, fraction: float | None, stats: dict | None = None) -> None:  # default sink
    pass
