"""netmove tests — resilient cross-volume placement.

Covers the pieces that keep a stalled/missing output share from hanging the run:
the chunked copy, the wait-for-volume loop with STUCK/BACK notifications, and the
same-volume fast path vs. the cross-volume copy+swap.

Run:  python tests/test_netmove.py   |   pytest tests/test_netmove.py
"""
from __future__ import annotations

import errno
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc import netmove  # noqa: E402


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def test_robust_move_same_device_is_a_rename():
    d = _tmp()
    src, dst = d / "a", d / "b"
    src.write_bytes(b"hello")
    netmove.robust_move(src, dst)
    assert dst.read_bytes() == b"hello" and not src.exists()
    print("  ok  same-volume move -> instant rename, source gone")


def test_robust_move_cross_device_copies_and_swaps(monkeypatch):
    """Force the EXDEV (different-volume) branch: the file is copied to a .vtcpart
    sibling then atomically swapped, the source temp is removed, no part is left."""
    monkeypatch.setattr(netmove, "_CHUNK", 1024)          # exercise the copy loop
    d = _tmp()
    src, dst = d / "movie.src", d / "out" / "movie.mkv"
    data = b"D" * 4096
    src.write_bytes(data)

    real_replace = os.replace
    def fake_replace(a, b):                                # EXDEV only for the fast path
        if not str(a).endswith(".vtcpart"):
            raise OSError(errno.EXDEV, "cross-device")
        return real_replace(a, b)
    monkeypatch.setattr(netmove.os, "replace", fake_replace)

    netmove.robust_move(src, dst)
    assert dst.read_bytes() == data and not src.exists()
    assert not (dst.parent / (dst.name + ".vtcpart")).exists()
    print("  ok  cross-volume move -> chunked copy + atomic swap, no .part left")


def test_copy_chunked_moves_all_bytes(monkeypatch):
    monkeypatch.setattr(netmove, "_CHUNK", 1024)
    d = _tmp()
    src, part = d / "s", d / "p"
    data = b"X" * 4097                                     # >1 chunk, non-aligned tail
    src.write_bytes(data)
    state = {"copied": 0, "ts": 0.0, "done": False, "err": None}
    netmove._copy_chunked(src, part, state, None)
    assert state["done"] and state["err"] is None
    assert state["copied"] == len(data) and part.read_bytes() == data
    print("  ok  chunked copy moves every byte")


def test_copy_chunked_abort_is_recorded_not_raised():
    d = _tmp()
    src, part = d / "s", d / "p"
    src.write_bytes(b"Y" * 4096)
    state = {"copied": 0, "ts": 0.0, "done": False, "err": None}
    netmove._copy_chunked(src, part, state, lambda: True)  # abort before first chunk
    assert isinstance(state["err"], netmove.Aborted) and not state["done"]
    print("  ok  abort during copy -> Aborted captured, not propagated to caller thread")


def test_wait_for_volume_notifies_then_recovers():
    seq = iter([False, False, True])                       # gone, gone, back
    events: list[str] = []
    ok = netmove._wait_for_volume(
        Path("/x/y"), lambda e, p: events.append(e), None,
        reachable=lambda p: next(seq), poll=0)
    assert ok and events == [netmove.STUCK, netmove.BACK]
    print("  ok  wait-for-volume -> STUCK then BACK when the share returns")


def test_wait_for_volume_can_be_aborted():
    events: list[str] = []
    ok = netmove._wait_for_volume(
        Path("/x/y"), lambda e, p: events.append(e), lambda: True,
        reachable=lambda p: False, poll=0)
    assert ok is False and events == [netmove.STUCK]
    print("  ok  wait-for-volume -> abort while the share is down returns False")


def _run_all():
    import inspect
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fns = [fn for fn in fns if not inspect.signature(fn).parameters]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} netmove test(s) done (fixture-taking ones run under pytest).")


if __name__ == "__main__":
    _run_all()
