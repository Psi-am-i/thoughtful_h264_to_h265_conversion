"""Stop-after-current tests.

Regression: with jobs=1 every file is submitted to the pool up front, so guarding
only the submit loop stopped nothing — the Stop button did nothing. The guard now
lives inside each work unit, so a requested stop skips every not-yet-started file.

Run:  python tests/test_stop.py   |   pytest tests/test_stop.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc import pipeline  # noqa: E402
from vtc.config import RunConfig  # noqa: E402


def test_stop_flag_skips_everything():
    d = Path(tempfile.mkdtemp())
    for i in range(4):
        (d / f"clip{i}.mp4").write_bytes(b"x")   # dummy files; skipped before any probe
    cfg = RunConfig(src=d, ledger_enabled=False)
    pipeline.STOP_FILE.unlink(missing_ok=True)
    try:
        pipeline.STOP_FILE.touch()               # stop requested before the run starts
        results = pipeline.run(cfg)
        # every file is skipped (work() returns None on STOP), so no results at all
        assert results == [], f"expected 0 processed with stop set, got {len(results)}"
        print("  ok  stop flag -> all files skipped (work-level guard)")
    finally:
        pipeline.STOP_FILE.unlink(missing_ok=True)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} stop test(s) done.")


if __name__ == "__main__":
    _run_all()
