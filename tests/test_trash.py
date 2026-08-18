"""Move-to-Trash tests — the Api.move_to_trash cleanup convenience.

It is only ever pointed at files the run LEFT UNTOUCHED (the broken / unreadable
ones under "needs a look"). It must: report an accurate trashed/failed tally,
never raise, skip files that are already gone, and delegate the actual delete to
the OS trash (recoverable) rather than unlinking anything itself.

Run:  python tests/test_trash.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc import webapp  # noqa: E402


def test_move_to_trash_tally_and_never_hard_deletes():
    d = Path(tempfile.mkdtemp())
    good1, good2, gone = d / "a.mkv", d / "b.mp4", d / "missing.avi"
    good1.write_text("x"); good2.write_text("y")

    trashed_paths = []
    real = webapp._os_trash
    # Stand in for the OS trash: record what was asked, and (crucially) do NOT
    # delete — the real _os_trash sends to Trash/Recycle Bin, never unlink().
    webapp._os_trash = lambda p: trashed_paths.append(Path(p))
    try:
        api = webapp.Api()
        res = api.move_to_trash([str(good1), str(good2), str(gone)])
    finally:
        webapp._os_trash = real

    assert res["trashed"] == 2, res
    assert len(res["failed"]) == 1 and res["failed"][0]["path"] == str(gone), res
    assert {p.name for p in trashed_paths} == {"a.mkv", "b.mp4"}, trashed_paths
    # The files still exist on disk — we never hard-deleted; the OS trash is mocked.
    assert good1.exists() and good2.exists()
    print("  ok  move_to_trash counts trashed vs failed, skips missing, no hard delete")


def test_move_to_trash_reports_per_file_error_and_never_raises():
    d = Path(tempfile.mkdtemp())
    f1, f2 = d / "ok.mkv", d / "locked.mkv"
    f1.write_text("x"); f2.write_text("y")

    real = webapp._os_trash

    def flaky(p):
        if Path(p).name == "locked.mkv":
            raise OSError("permission denied")
    webapp._os_trash = flaky
    try:
        api = webapp.Api()
        res = api.move_to_trash([str(f1), str(f2)])
    finally:
        webapp._os_trash = real

    assert res["trashed"] == 1, res
    assert len(res["failed"]) == 1 and "permission" in res["failed"][0]["error"], res
    print("  ok  a per-file trash error is reported, not raised")


def test_move_to_trash_accepts_a_bare_string():
    d = Path(tempfile.mkdtemp())
    f = d / "one.mp4"; f.write_text("x")
    real = webapp._os_trash
    webapp._os_trash = lambda p: None
    try:
        res = webapp.Api().move_to_trash(str(f))     # a single path, not a list
    finally:
        webapp._os_trash = real
    assert res["trashed"] == 1 and res["failed"] == [], res
    print("  ok  move_to_trash accepts a single path string")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} trash test(s) done.")


if __name__ == "__main__":
    _run_all()
