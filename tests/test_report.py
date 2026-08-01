"""Report tests — RUN SUMMARY / SPACE SAVED / RUN REPORT text.

Runnable two ways:  pytest tests/   |   python tests/test_report.py

Assertions target substrings, not exact whitespace, so cosmetic layout tweaks
don't break the suite.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc.report import (  # noqa: E402
    human_bytes,
    problem_report,
    render,
    run_summary,
    space_savings,
)
from vtc.result import FileResult, Note, Outcome  # noqa: E402


def _fr(
    outcome: Outcome,
    name: str = "clip.mkv",
    src: int = 0,
    out: int = 0,
    notes: list[Note] | None = None,
) -> FileResult:
    return FileResult(
        path=Path(f"/media/{name}"),
        outcome=outcome,
        src_bytes=src,
        out_bytes=out,
        notes=notes or [],
    )


def _mixed() -> list[FileResult]:
    """Two shrinks, one remux (changed=3), three left-as-is, one error."""
    return [
        _fr(Outcome.SHRINK, "a.mkv", src=1000, out=400),
        _fr(Outcome.SHRINK, "b.mkv", src=2000, out=1000),
        _fr(Outcome.REMUX, "c.mkv", src=500, out=500),
        _fr(Outcome.SKIP_AT_TIER, "d.mp4"),
        _fr(Outcome.SKIP_MODERN, "e.mp4"),
        _fr(Outcome.RESUME, "f.mp4"),
        _fr(Outcome.ERROR, "g.avi", notes=[Note("ERROR", "encode failed")]),
    ]


def test_summary_counts():
    text = run_summary(_mixed())
    assert "RUN SUMMARY — 7 file(s) scanned" in text
    # changed = 2 shrink + 1 remux = 3; errors = 1; left = 7 - 3 - 1 = 3.
    assert "changed 3    left as-is 3    errors 1" in text


def test_summary_only_nonzero_categories():
    text = run_summary(_mixed())
    # Present (non-zero) categories:
    assert "re-encoded (shrunk)" in text
    assert "remuxed into MP4 (lossless)" in text
    assert "left as-is: already at tier" in text
    assert "skipped: already done (resume)" in text
    assert "ERRORS (source untouched)" in text
    # Absent (zero) categories must not appear:
    assert "transcoded (compatibility)" not in text
    assert "left as-is: saving too small" not in text
    assert "left as-is: unsupported codec" not in text


def test_summary_counts_render_per_category():
    text = run_summary(_mixed())
    # The two shrinks share the "re-encoded (shrunk)" row with a count of 2.
    shrink_line = next(l for l in text.splitlines() if "re-encoded (shrunk)" in l)
    assert shrink_line.strip().endswith("2")


def test_space_savings_sums():
    text = space_savings(_mixed())
    assert text is not None
    # Changed rows: src = 1000+2000+500 = 3500; out = 400+1000+500 = 1900.
    assert "3 file(s) replaced" in text
    assert "3.4 KB" in text  # original 3500 bytes -> 3.417 KB
    assert "1.9 KB" in text  # new 1900 bytes -> 1.855 KB
    # saved 1600 / 3500 = 45.7%
    assert "45.7%" in text


def test_space_savings_none_when_nothing_changed():
    results = [_fr(Outcome.SKIP_AT_TIER), _fr(Outcome.ERROR)]
    assert space_savings(results) is None


def test_problem_report_lists_notes():
    results = [
        _fr(Outcome.SHRINK, "a.mkv", src=10, out=5,
            notes=[Note("NOTE", "subtitles written as sidecar")]),
        _fr(Outcome.ERROR, "g.avi", notes=[Note("ERROR", "encode failed")]),
    ]
    text = problem_report(results)
    assert text is not None
    assert "RUN REPORT — 2 file(s)" in text
    assert "[NOTE] subtitles written as sidecar" in text
    assert "[ERROR] encode failed" in text
    assert "/media/a.mkv" in text
    assert "/media/g.avi" in text
    # Legend lines present.
    assert "WARN  = worth a manual check" in text


def test_problem_report_none_when_no_notes():
    results = [_fr(Outcome.SHRINK, src=10, out=5), _fr(Outcome.SKIP_AT_TIER)]
    assert problem_report(results) is None


def test_human_bytes():
    assert human_bytes(0) == "0.0 B"
    assert human_bytes(512) == "512.0 B"
    assert human_bytes(1024) == "1.0 KB"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(1024 ** 2) == "1.0 MB"
    assert human_bytes(1024 ** 3) == "1.0 GB"
    assert human_bytes(1024 ** 4) == "1.0 TB"


def test_render_joins_present_blocks():
    text = render(_mixed())
    assert "RUN SUMMARY" in text
    assert "SPACE SAVED" in text
    assert "RUN REPORT" in text


def test_render_skips_none_blocks():
    # No changes and no notes: only the summary block survives.
    results = [_fr(Outcome.SKIP_AT_TIER), _fr(Outcome.SKIP_MODERN)]
    text = render(results)
    assert "RUN SUMMARY" in text
    assert "SPACE SAVED" not in text
    assert "RUN REPORT" not in text


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} report tests passed.")


if __name__ == "__main__":
    _run_all()
