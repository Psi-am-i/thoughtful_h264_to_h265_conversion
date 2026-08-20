"""Move-to-Trash / permanent-delete tests.

Only ever pointed at files the run LEFT UNTOUCHED (the broken / unreadable ones
under "needs a look"). Rules:
  - Local drive: move to the OS Trash (recoverable). Never a hard delete here.
  - No-Trash drive (SMB/NAS, e.g. Beast 8TB): can't be trashed — reported as
    where:'no_trash' so the UI can ask, then delete_permanently() removes it.
  - Never raises; skips files already gone.

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

    assert res["trashed"] == 2 and res["no_trash"] == 0 and res["failed"] == 1, res
    oks = {r["path"] for r in res["results"] if r["ok"]}
    assert oks == {str(good1), str(good2)}, res
    assert {p.name for p in trashed_paths} == {"a.mkv", "b.mp4"}, trashed_paths
    assert good1.exists() and good2.exists()          # never hard-deleted (trash mocked)
    print("  ok  local: counts trashed vs failed, skips missing, no hard delete")


def test_no_trash_drive_is_flagged_not_deleted():
    """The SMB/NAS case: move_to_trash must NOT delete — it flags the file 'no_trash'
    and leaves it on disk for the UI to ask about."""
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

    assert res["trashed"] == 0 and res["no_trash"] == 1 and res["failed"] == 0, res
    row = res["results"][0]
    assert (not row["ok"]) and row["where"] == "no_trash", row
    assert f.exists(), "move_to_trash must not delete — only the confirmed delete does"
    print("  ok  no-trash drive -> flagged 'no_trash', file left intact")


def test_delete_permanently_removes_files():
    d = Path(tempfile.mkdtemp())
    f1, f2, gone = d / "x.mkv", d / "y.mp4", d / "already-gone.avi"
    f1.write_text("x"); f2.write_text("y")
    res = webapp.Api().delete_permanently([str(f1), str(f2), str(gone)])
    assert res["deleted"] == 3 and res["failed"] == [], res   # missing counts as done
    assert not f1.exists() and not f2.exists()
    print("  ok  delete_permanently removes files (and treats already-gone as done)")


def test_delete_permanently_reports_per_file_error():
    d = Path(tempfile.mkdtemp())
    f = d / "z.mkv"; f.write_text("x")
    real = os.remove
    def boom(p):
        raise OSError("permission denied")
    os.remove = boom
    try:
        res = webapp.Api().delete_permanently([str(f)])
    finally:
        os.remove = real
    assert res["deleted"] == 0 and len(res["failed"]) == 1, res
    assert "permission" in res["failed"][0]["error"], res
    assert f.exists()                                  # nothing lost on error
    print("  ok  delete_permanently reports a per-file error, keeps the file")


def test_other_trash_error_is_reported_as_failed_not_deleted():
    """A non-'no trash' error (e.g. a genuine permission problem) must NOT be treated as
    a delete case — it's reported as failed and the file is left alone."""
    d = Path(tempfile.mkdtemp())
    f = d / "weird.mp4"; f.write_text("x")
    real = webapp._os_trash
    webapp._os_trash = lambda p: (_ for _ in ()).throw(OSError("some odd trash error"))
    try:
        res = webapp.Api().move_to_trash([str(f)])
    finally:
        webapp._os_trash = real
    assert res["failed"] == 1 and res["no_trash"] == 0 and res["trashed"] == 0, res
    assert f.exists()
    print("  ok  a generic trash error is reported failed, never auto-deleted")


def test_win_unc_path_is_not_recyclable():
    """A Windows UNC / network path has no Recycle Bin, so it must be routed to the
    delete prompt rather than silently permanent-deleted. (The UNC check returns before
    any Windows-only ctypes call, so this is safe to assert on any OS.)"""
    assert webapp._win_is_recyclable(Path(r"\\nas\media\movie.mkv")) is False
    assert webapp._win_is_recyclable(Path("//nas/media/movie.mkv")) is False
    print("  ok  a Windows UNC/network path is treated as no-Recycle-Bin")


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
