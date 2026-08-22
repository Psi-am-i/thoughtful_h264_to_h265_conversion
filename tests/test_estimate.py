"""estimate() must not spin forever on a folder with nothing to do.

When every file is removed by the ignore rules (or the folder has no videos),
probing finishes with an empty probe set. estimate() has to distinguish that
"done, but empty" state from "still probing" — otherwise the confirm sheet sits
on "Evaluating your library…" indefinitely (the bug this guards against).

Run:  python tests/test_estimate.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc import webapp  # noqa: E402

# H265 / Stellar / 25% saving / hardware / replace-in-place — real answer indices.
ANSWERS = {"codec": 1, "quality": 3, "saving": 1, "encoder": 0, "dest": 1}


def _api(src: Path):
    api = webapp.Api()
    api._src = src
    api._probes = []
    return api


def test_finished_but_empty_returns_measured_not_probing():
    src = Path("/tmp/vtc-empty")            # path doesn't need to exist for estimate()
    api = _api(src)
    api._probed_for = src                   # probing DONE
    api._ignored = 28
    api._total_files = 0

    e = api.estimate(ANSWERS)
    assert not e.get("error"), e            # must NOT keep saying "probing"
    assert e.get("measured") is True, e
    assert e.get("empty") is True, e
    assert e.get("ignored") == 28, e
    assert e.get("work_files") == 0, e
    print("  ok  finished-but-empty -> measured/empty with the ignored count (no spin)")


def test_still_probing_is_reported_as_probing():
    src = Path("/tmp/vtc-empty")
    api = _api(src)
    api._probed_for = None                  # NOT the current src -> genuinely still probing
    e = api.estimate(ANSWERS)
    assert e.get("error") == "probing" and e.get("measured") is False, e
    print("  ok  genuinely-still-probing still reports 'probing'")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} estimate test(s) done.")


if __name__ == "__main__":
    _run_all()
