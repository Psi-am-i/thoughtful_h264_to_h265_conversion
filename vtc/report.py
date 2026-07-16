"""End-of-run reporting: RUN SUMMARY, SPACE SAVED, and RUN REPORT blocks.

Ported from the bash tool's end-of-run report (see the RUN SUMMARY / SPACE SAVED
/ RUN REPORT blocks in ``very_thoughtful_compression.sh``). These functions
consume a list of :class:`~vtc.result.FileResult` and RETURN formatted strings;
they never print. The CLI prints the returned text, and the GUI can render it.
"""

from __future__ import annotations

from collections import Counter

from vtc.result import FileResult, Outcome

# Wide rule / divider lines, matching the bash report's box drawing.
_RULE = "═" * 58
_DIVIDER = "  " + "─" * 56

# Friendly label per outcome, in the display order used by the bash summary.
# Mirrors the (label, key) rows in the bash RUN SUMMARY block.
_SUMMARY_ROWS: list[tuple[str, Outcome]] = [
    ("re-encoded (shrunk)", Outcome.SHRINK),
    ("transcoded (compatibility)", Outcome.TRANSCODE),
    ("remuxed into MP4 (lossless)", Outcome.REMUX),
    ("left as-is: already at tier", Outcome.SKIP_AT_TIER),
    ("left as-is: already H.265/AV1/VP9", Outcome.SKIP_MODERN),
    ("left as-is: output already existed", Outcome.SKIP_EXISTING),
    ("left as-is: saving too small", Outcome.SKIP_MIN_SAVING),
    ("left as-is: incompatible codec", Outcome.SKIP_INCOMPATIBLE),
    ("left as-is: unsupported codec", Outcome.SKIP_CODEC),
    ("skipped: already done (resume)", Outcome.RESUME),
    ("ERRORS (source untouched)", Outcome.ERROR),
]


def human_bytes(num: float) -> str:
    """Format a byte count as B/KB/MB/GB/TB (1024-based, one decimal).

    Matches the bash ``h()`` helper: values are divided by 1024 per unit and
    clamped at TB for anything huge.
    """
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.1f} {unit}"
        num /= 1024
    # Unreachable (the TB branch always returns), kept for type completeness.
    return f"{num:.1f} TB"


def run_summary(results: list[FileResult]) -> str:
    """Build the RUN SUMMARY block accounting for every scanned file.

    Reports total scanned, the changed/left/errors tally, then one line per
    NON-ZERO outcome category using friendly labels. Categories with a zero
    count are omitted so the block stays tight.
    """
    counts: Counter[Outcome] = Counter(r.outcome for r in results)
    total = len(results)
    changed = sum(n for outcome, n in counts.items() if outcome.changed)
    errors = counts.get(Outcome.ERROR, 0)
    left = total - changed - errors

    lines = [
        _RULE,
        f" RUN SUMMARY — {total} file(s) scanned",
        f"   changed {changed}    left as-is {left}    errors {errors}",
        _DIVIDER,
    ]
    for label, outcome in _SUMMARY_ROWS:
        count = counts.get(outcome, 0)
        if count:
            lines.append(f"   {label:<36s} {count:>4d}")
    lines.append(_RULE)
    return "\n".join(lines)


def space_savings(results: list[FileResult]) -> str | None:
    """Build the SPACE SAVED block, or None if nothing was changed.

    Sums source and output bytes over every ``.changed`` result and reports the
    original / new / saved totals (with the saved percentage).
    """
    changed = [r for r in results if r.outcome.changed]
    if not changed:
        return None

    src = sum(r.src_bytes for r in changed)
    out = sum(r.out_bytes for r in changed)
    saved = src - out
    pct = (saved / src * 100) if src else 0.0

    return "\n".join(
        [
            _RULE,
            f" SPACE SAVED — {len(changed)} file(s) replaced",
            f"   original:  {human_bytes(src)}",
            f"   new:       {human_bytes(out)}",
            f"   saved:     {human_bytes(saved)}  ({pct:.1f}%)",
            _RULE,
        ]
    )


def problem_report(results: list[FileResult]) -> str | None:
    """Build the RUN REPORT block listing every Note, or None if there are none.

    Each note is rendered as ``[LEVEL] message`` followed by the file path,
    mirroring the bash RUN REPORT with its NOTE/WARN/ERROR legend.
    """
    entries = [(r, note) for r in results for note in r.notes]
    if not entries:
        return None

    lines = [
        _RULE,
        f" RUN REPORT — {len(entries)} file(s) with notes, warnings or errors",
        "   NOTE  = informational (e.g. why an original was archived)",
        "   WARN  = worth a manual check",
        "   ERROR = file could not be converted; source untouched",
        _RULE,
    ]
    for result, note in entries:
        lines.append(f"  [{note.level}] {note.message}")
        lines.append(f"    {result.path}")
        lines.append("")
    lines.append(_RULE)
    return "\n".join(lines)


def render(results: list[FileResult]) -> str:
    """Join the summary, savings, and problem blocks (skipping None) with blanks."""
    blocks = [
        run_summary(results),
        space_savings(results),
        problem_report(results),
    ]
    return "\n\n".join(block for block in blocks if block is not None)
