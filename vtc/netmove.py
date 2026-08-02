"""Resilient cross-volume move for placing finished encodes on a (often network)
output share.

Why this exists: the output library is commonly an SMB/NFS mount. `shutil.move`
across volumes does one opaque, blocking byte copy — and if the share stalls or
vanishes mid-copy, that copy can block *uninterruptibly* for hours and then fail,
which used to hang the whole run and freeze the UI (see the S02E01 incident).

What this gives instead:
  * same-volume moves stay an instant atomic rename (no copy at all);
  * cross-volume moves copy in chunks to a ``.vtcpart`` sibling, tracking byte
    progress, so a WATCHDOG can tell "still going" from "stuck";
  * if the destination volume is missing/stale we WAIT for it (polling), telling
    the caller so it can tell the user, and resume automatically once it's back;
  * the finished copy is swapped into place with an atomic ``os.replace``.

The hard limit, honestly stated: a thread already blocked inside a hung network
syscall cannot be force-killed from Python. So `abort` is honoured between chunks
(a merely-slow share) and before a copy starts, but a FULLY hung mount can only be
escaped by ending the process — which is why the app persists a resumable session.
"""

from __future__ import annotations

import errno
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

# Notify events sent to the caller (→ the UI banner). Two states only.
STUCK = "stuck"     # destination volume is missing or not making progress
BACK = "back"       # it responded again; the move is proceeding

NotifyCB = Callable[[str, str], None]   # (event, path) — event in {STUCK, BACK}
AbortCB = Callable[[], bool]            # True -> give up now (between chunks only)

# Copy 8 MiB at a time and flush each chunk, so a hung share blocks on a WRITE we
# can see (progress timestamp stops advancing) rather than deep inside a buffer.
_CHUNK = 8 * 1024 * 1024
# No byte progress for this long on an in-flight copy -> report the volume "stuck".
# 60s is deliberately tolerant: a slow-but-alive share (or a big flush pausing over a
# congested link) that resumes within the minute is never flagged, so the banner means
# a real stall, not a hiccup. It still clears the instant bytes move again.
_STALL_S = 60.0
# How often the watchdog wakes to check progress / re-probe a missing volume.
_POLL_S = 2.0
# A reachability probe (stat + tiny write) must answer within this or the volume is
# treated as unreachable — a stale mount makes the probe itself hang.
_REACH_TIMEOUT_S = 5.0


class Aborted(Exception):
    """Raised inside the copy when `abort` asks to give up between chunks."""


def _reachable(path: Path, timeout: float = _REACH_TIMEOUT_S) -> bool:
    """True if the volume under `path` answers a stat + tiny write within `timeout`.

    The whole probe runs in a throwaway daemon thread and we wait on it with a
    timeout, because a stale network mount makes even ``os.stat`` block forever — so
    it must never run on the caller's thread. Inside, we climb to the nearest EXISTING
    ancestor before probing: a not-yet-created destination subfolder must read as
    "volume up" (we'll mkdir it), not "unreachable". A leaked probe thread on a hung
    mount is harmless (daemon; it unblocks if the mount recovers).
    """
    result: dict = {"ok": False}

    def _probe() -> None:
        try:
            d = path
            for _ in range(64):                 # climb to an existing dir (mount root at worst)
                if d.is_dir():
                    break
                if d.parent == d:
                    break
                d = d.parent
            os.stat(d)
            # A stat can be served from cache while writes still fail, so actually
            # touch the volume. Unique name so parallel workers never collide.
            probe = d / f".vtc_reach.{os.getpid()}.{threading.get_ident()}"
            with open(probe, "wb") as fh:
                fh.write(b"")
            try:
                probe.unlink()
            except OSError:
                pass
            result["ok"] = True
        except OSError:
            result["ok"] = False

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout)
    # Still running -> the probe itself hung -> unreachable.
    return result["ok"] if not t.is_alive() else False


def _wait_for_volume(path: Path, notify: NotifyCB | None, abort: AbortCB | None,
                     reachable: Callable[[Path], bool], poll: float) -> bool:
    """Block until `path`'s volume is reachable (or `abort`). Returns False if the
    caller aborted while waiting. Emits STUCK once when it starts waiting and BACK
    when the volume returns, so the UI can raise and clear a banner."""
    if reachable(path):
        return True
    if notify:
        notify(STUCK, str(path))
    while not reachable(path):
        if abort and abort():
            return False
        time.sleep(poll)
    if notify:
        notify(BACK, str(path))
    return True


def _copy_chunked(src: Path, part: Path, state: dict, abort: AbortCB | None) -> None:
    """Copy src -> part in chunks, updating state['copied']/state['ts'] as it goes.
    Runs on its own thread; a hung share blocks it inside ``write`` while the
    watchdog (another thread) keeps reading `state`. Records the outcome in
    state['err'] / state['done'] rather than raising to the caller's thread."""
    try:
        with open(src, "rb") as r, open(part, "wb") as w:
            while True:
                if abort and abort():
                    raise Aborted()
                buf = r.read(_CHUNK)
                if not buf:
                    break
                w.write(buf)
                w.flush()                       # surface a stall on THIS write
                state["copied"] += len(buf)
                state["ts"] = time.monotonic()
        state["done"] = True
    except BaseException as e:                  # noqa: BLE001 — reported via state
        state["err"] = e


def robust_move(src: Path, dst: Path, *, notify: NotifyCB | None = None,
                abort: AbortCB | None = None, stall: float = _STALL_S,
                poll: float = _POLL_S, reachable: Callable[[Path], bool] | None = None,
                clock: Callable[[], float] = time.monotonic) -> None:
    """Move `src` onto `dst`, surviving a stalled/missing destination volume.

    Same volume: an instant atomic rename. Cross volume: a chunked, watched copy
    to a ``.vtcpart`` sibling then an atomic swap. `notify(event, path)` is called
    with STUCK/BACK around any wait so the caller can inform the user; `abort`, if
    given, lets a *not-fully-hung* copy bail between chunks.

    Raises OSError on a genuine copy failure (and leaves the original in place —
    nothing is deleted until the destination file is safely swapped in). `Aborted`
    is raised if the caller aborts mid-copy.
    """
    src, dst = Path(src), Path(dst)
    reach = reachable or _reachable

    # Confirm the destination volume answers BEFORE touching it: a stale mount hangs
    # os.stat/os.replace themselves, so even the fast-path rename below could block
    # forever. Waiting here (bounded probe + poll) means a missing/stale share raises a
    # banner and the move resumes once it's back, rather than hanging the run.
    if not _wait_for_volume(dst.parent, notify, abort, reach, poll):
        raise Aborted()
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Fast path: same filesystem -> rename is atomic and instant, no bytes moved.
    try:
        os.replace(src, dst)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:              # a real error, not "different volume"
            raise

    part = dst.with_name(dst.name + ".vtcpart")
    try:
        part.unlink()                           # discard a stale part from a prior try
    except OSError:
        pass

    state = {"copied": 0, "ts": clock(), "done": False, "err": None}
    worker = threading.Thread(
        target=_copy_chunked, args=(src, part, state, abort), daemon=True)
    worker.start()

    # Watchdog: while the copy runs, watch its progress timestamp. Flag STUCK after
    # `stall` seconds of no progress, clear it (BACK) as soon as bytes move again.
    stuck = False
    while worker.is_alive():
        worker.join(poll)
        idle = clock() - state["ts"]
        if not stuck and idle >= stall:
            stuck = True
            if notify:
                notify(STUCK, str(dst))
        elif stuck and idle < stall:
            stuck = False
            if notify:
                notify(BACK, str(dst))

    if state["err"] is not None:
        try:
            part.unlink()
        except OSError:
            pass
        raise state["err"]
    if stuck and notify:                        # finished right after a stall
        notify(BACK, str(dst))

    os.replace(part, dst)                        # atomic swap into final name
    try:
        shutil.copystat(src, dst)               # best-effort mode/mtime carry-over
    except OSError:
        pass
    src.unlink()                                # it was a MOVE: drop the source temp
