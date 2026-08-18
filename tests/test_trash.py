"""Move-to-Trash tests — the Api.move_to_trash cleanup convenience.

It is only ever pointed at files the run LEFT UNTOUCHED (the broken / unreadable
ones under "needs a look"). It must: prefer the OS Trash, fall back to a
same-volume "VTC Trashed Files" folder when the volume has no Trash (SMB/NAS),
report accurate per-file results, never raise, skip files that are already gone,
and never hard-delete.

Run:  python tests/test_trash.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc import webapp  # noqa: E402


def test_trash_tally_and_never_hard_deletes():
    d = Path(tempfile.mkdtemp())
    good1, good2, gone = d / "a.mkv", d / "b.mp4", d / "missing.avi"
    good1.write_text("x"); good2.write_text("y")

    trashed_paths = []
    real = webapp._os_trash
    # Stand in for the OS trash: record what was asked, and (crucially) do NOT
    # delete — the real _os_trash sends to Trash/Recycle Bin, never unlink().
    webapp._os_trash = lambda p: trashed_paths.append(Path(p))
    try:
        res = webapp.Api().move_to_trash([str(good1), str(good2), str(gone)])
    finally:
        webapp._os_trash = real

    assert res["trashed"] == 2 and res["moved"] == 0 and res["failed"] == 1, res
    oks = {r["path"] for r in res["results"] if r["ok"]}
    assert oks == {str(good1), str(good2)}, res
    assert {p.name for p in trashed_paths} == {"a.mkv", "b.mp4"}, trashed_paths
    assert good1.exists() and good2.exists()          # never hard-deleted (trash mocked)
    print("  ok  counts trashed vs failed, skips missing, no hard delete")


def test_no_volume_trash_falls_back_to_same_volume_folder():
    """The SMB/NAS case (Beast 8TB): the volume has no Trash, so the file is moved
    to a recoverable 'VTC Trashed Files' folder instead of failing."""
    d = Path(tempfile.mkdtemp())
    f = d / "broken.mkv"; f.write_text("data")

    real = webapp._os_trash
    def no_trash(p):
        raise webapp._NoVolumeTrash('the volume "Beast 8TB" doesn\'t have one')
    webapp._os_trash = no_trash
    try:
        res = webapp.Api().move_to_trash([str(f)])
    finally:
        webapp._os_trash = real

    assert res["trashed"] == 0 and res["moved"] == 1 and res["failed"] == 0, res
    row = res["results"][0]
    assert row["ok"] and row["where"] == "folder", row
    dest = Path(row["dest"])
    assert dest.exists() and dest.parent.name == "VTC Trashed Files", dest
    assert not f.exists(), "source should have been moved out of the library"
    assert res["folders"] == [str(dest.parent)], res
    print("  ok  no-volume-trash -> recoverable same-volume folder")


def test_generic_trash_error_also_tries_the_folder():
    """Any trash failure (not just the tidy _NoVolumeTrash) still tries the folder
    before giving up — the user's intent is 'get this out of my library'."""
    d = Path(tempfile.mkdtemp())
    f = d / "weird.mp4"; f.write_text("x")
    real = webapp._os_trash
    webapp._os_trash = lambda p: (_ for _ in ()).throw(OSError("some odd trash error"))
    try:
        res = webapp.Api().move_to_trash([str(f)])
    finally:
        webapp._os_trash = real
    assert res["moved"] == 1 and res["failed"] == 0, res
    assert not f.exists()
    print("  ok  a generic trash error still falls back to the folder")


def test_reports_failure_when_even_the_folder_move_fails():
    d = Path(tempfile.mkdtemp())
    f = d / "stuck.mkv"; f.write_text("x")
    real_t, real_q = webapp._os_trash, webapp._quarantine
    webapp._os_trash = lambda p: (_ for _ in ()).throw(webapp._NoVolumeTrash("no trash"))
    webapp._quarantine = lambda p, base=None: (_ for _ in ()).throw(OSError("read-only volume"))
    try:
        res = webapp.Api().move_to_trash([str(f)])
    finally:
        webapp._os_trash, webapp._quarantine = real_t, real_q
    assert res["failed"] == 1 and res["trashed"] == 0 and res["moved"] == 0, res
    assert "read-only" in res["results"][0]["error"], res
    assert f.exists()                                  # nothing lost
    print("  ok  when both trash and folder fail, the row is reported, file kept")


def test_accepts_a_bare_string():
    d = Path(tempfile.mkdtemp())
    f = d / "one.mp4"; f.write_text("x")
    real = webapp._os_trash
    webapp._os_trash = lambda p: None
    try:
        res = webapp.Api().move_to_trash(str(f))       # a single path, not a list
    finally:
        webapp._os_trash = real
    assert res["trashed"] == 1 and res["failed"] == 0, res
    print("  ok  accepts a single path string")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} trash test(s) done.")


if __name__ == "__main__":
    _run_all()
