"""Resume-session tests — a run interrupted by a crash/force-quit/reboot can be
continued on next launch. The persisted intent (src + answers) round-trips, and a
session whose source folder has since vanished is ignored (and cleaned up).

Run:  python tests/test_session.py   |   pytest tests/test_session.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc import webapp  # noqa: E402


def test_session_roundtrip(monkeypatch):
    d = Path(tempfile.mkdtemp())
    sp = d / "vtc_session.json"
    monkeypatch.setattr(webapp, "_session_path", lambda: sp)
    src = d / "lib"
    src.mkdir()

    api = webapp.Api()
    api._src = src
    api._save_session({"quality": 1, "codec": 0})
    assert sp.exists(), "session not written at run start"
    assert api.pending_session() == {"src": str(src)}

    api.discard_session()
    assert not sp.exists()
    assert api.pending_session() == {}
    print("  ok  session save -> pending -> discard round-trips")


def test_pending_session_ignores_vanished_folder(monkeypatch):
    d = Path(tempfile.mkdtemp())
    sp = d / "vtc_session.json"
    monkeypatch.setattr(webapp, "_session_path", lambda: sp)
    sp.write_text(json.dumps({"src": str(d / "gone"), "answers": {}}), encoding="utf-8")

    api = webapp.Api()
    assert api.pending_session() == {}          # nothing to resume onto
    assert not sp.exists(), "a stale session pointing at a missing folder should be cleaned up"
    print("  ok  pending session with a missing source folder is ignored + cleaned")


def _run_all():
    print("(run under pytest — these use the monkeypatch fixture)")


if __name__ == "__main__":
    _run_all()
