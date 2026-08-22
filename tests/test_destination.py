"""Destination options -> RunConfig wiring.

The three destination choices, plus the extras the engine already supports but the
UI now exposes: new-folder mirror-vs-flat structure, and a custom archive location.

Run:  python tests/test_destination.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc import webapp  # noqa: E402
from vtc.config import OutputMode, SourceAction  # noqa: E402

SRC = Path("/src")


def _cfg(dest, adv=None, output_dir=None):
    a = {"codec": 1, "quality": 3, "saving": 1, "encoder": 0, "dest": dest, "adv": adv or {}}
    if output_dir:
        a["outputDir"] = output_dir
    return webapp.build_config(SRC, a)


def test_new_folder_flat_vs_mirror():
    flat = _cfg(2, {"outFlat": True}, output_dir="/out")
    assert flat.output_mode is OutputMode.SEPARATE and flat.source_action is SourceAction.KEEP
    assert flat.output_dir == Path("/out") and flat.output_flat is True
    mirror = _cfg(2, {"outFlat": False}, output_dir="/out")
    assert mirror.output_flat is False, "mirror is the default structure"
    print("  ok  new folder: SEPARATE/KEEP, flat toggle honoured, mirror by default")


def test_archive_location_custom_and_default():
    custom = _cfg(0, {"archiveDir": "/arc"})
    assert custom.source_action is SourceAction.ARCHIVE
    assert custom.archive_dir == Path("/arc") and custom.resolved_archive_dir() == Path("/arc")
    default = _cfg(0, {"archiveDir": ""})
    assert default.archive_dir is None
    assert default.resolved_archive_dir() == SRC / "originals", "blank -> originals at source root"
    print("  ok  archive: custom dir honoured; blank falls back to <src>/originals")


def test_delete_is_unaffected_by_extras():
    c = _cfg(1, {"outFlat": True, "archiveDir": "/arc"})   # extras must not leak in
    assert c.source_action is SourceAction.DELETE and c.output_mode is OutputMode.INPLACE
    assert c.archive_dir is None and c.output_flat is False
    print("  ok  delete ignores archive/flat extras")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} destination test(s) done.")


if __name__ == "__main__":
    _run_all()
