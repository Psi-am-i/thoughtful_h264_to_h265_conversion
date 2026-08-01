"""Placement tests — the original must never be lost.

Regression for the archive-not-happening bug: an in-place re-encode whose output
lands on the SAME path as the source (h264.mp4 -> h265.mp4, same name) used to
overwrite the original BEFORE the archive step, and the archive step only ran when
the paths differed — so "Replace · archive" silently destroyed the originals.

Run:  python tests/test_placement.py   |   pytest tests/test_placement.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc.config import OutputMode, RunConfig, SourceAction  # noqa: E402
from vtc.pipeline import _place  # noqa: E402
from vtc.result import EncodeResult  # noqa: E402

_ORIG = b"ORIGINAL" * 1000
_NEW = b"NEWENCODE"


def _place_case(action: SourceAction, same_path: bool, dropped: str = ""):
    d = Path(tempfile.mkdtemp())
    src = d / "Show - S01E01 [h264].mp4"
    src.write_bytes(_ORIG)
    out = src if same_path else (d / "Show - S01E01 [h264].mkv")
    tmp = d / ".tmp.mp4"
    tmp.write_bytes(_NEW)
    cfg = RunConfig(src=d, source_action=action, output_mode=OutputMode.INPLACE)
    res = EncodeResult(ok=True, out_path=out, out_bytes=len(_NEW), dropped_subs_reason=dropped)
    _place(cfg, src, out, tmp, res)
    archived = d / "originals" / src.name
    return {
        "out_ok": out.exists() and out.read_bytes() == _NEW,
        "archived_ok": archived.exists() and archived.read_bytes() == _ORIG,
    }


def test_archive_same_path_preserves_original():
    r = _place_case(SourceAction.ARCHIVE, same_path=True)
    assert r["out_ok"], "output not written"
    assert r["archived_ok"], "original was LOST (not archived) on same-path replace"
    print("  ok  ARCHIVE same-path -> original archived, not overwritten")


def test_archive_diff_path_preserves_original():
    r = _place_case(SourceAction.ARCHIVE, same_path=False)
    assert r["out_ok"] and r["archived_ok"]
    print("  ok  ARCHIVE different-path -> original archived")


def test_delete_same_path_overwrites():
    r = _place_case(SourceAction.DELETE, same_path=True)
    assert r["out_ok"] and not r["archived_ok"]
    print("  ok  DELETE same-path -> original intentionally overwritten")


def test_delete_with_dropped_subs_archives():
    r = _place_case(SourceAction.DELETE, same_path=True, dropped="PGS subtitles")
    assert r["archived_ok"], "dropped subs must be archived even on DELETE"
    print("  ok  DELETE + dropped subs -> original archived, never silently lost")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} placement test(s) done.")


if __name__ == "__main__":
    _run_all()
