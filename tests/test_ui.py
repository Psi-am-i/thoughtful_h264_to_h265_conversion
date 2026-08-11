"""Drive the real Advanced-settings UI in a DOM and check it does what it says.

The page is loaded exactly as shipped, then the controls are operated the way a
person operates them — type in the box, click the toggle — and the state handed
to the engine is read back. Pairs with tests/test_advanced_labels.py, which takes
that state and proves the engine then behaves as the label promised.

Needs node + jsdom (`cd tests/ui && npm install jsdom`); skipped without them, so
a plain `pytest tests/` on a machine with no node still passes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import pytest
except ModuleNotFoundError:                      # standalone: no pytest available
    class _Stub:                                 # just enough to define the tests
        class mark:
            @staticmethod
            def skipif(cond, reason=""):
                def deco(fn):
                    fn.__skip__ = cond
                    return fn
                return deco
    pytest = _Stub()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_UI = Path(__file__).resolve().parent / "ui"


def _have_jsdom() -> bool:
    if not shutil.which("node"):
        return False
    r = subprocess.run(["node", "-e", "require('jsdom')"], cwd=_UI,
                       capture_output=True, stdin=subprocess.DEVNULL)
    return r.returncode == 0


@pytest.mark.skipif(not _have_jsdom(), reason="node + jsdom not installed (see tests/ui/README)")
def test_advanced_settings_ui():
    r = subprocess.run(["node", "drive.js"], cwd=_UI, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=180)
    assert r.stdout.strip(), f"harness produced nothing:\n{r.stderr[:2000]}"
    out = json.loads(r.stdout)
    assert "fatal" not in out, f"{out['fatal']}: {out.get('errors')}"
    failed = [x for x in out["results"] if not x["ok"]]
    assert not failed, "UI controls not behaving as labelled:\n" + "\n".join(
        f"  {x['name']}: got {x['got']!r}, want {x['want']!r}" for x in failed)
    assert len(out["results"]) >= 40, "harness ran fewer checks than expected"


@pytest.mark.skipif(not _have_jsdom(), reason="node + jsdom not installed (see tests/ui/README)")
def test_parallel_progress_rows():
    """With jobs>1 every file in flight must stay visible.

    A single curName meant the workers overwrote each other: one file showed as
    processing and the rest vanished, while the Stop button was already saying
    "files in flight" (plural).
    """
    r = subprocess.run(["node", "flight.js"], cwd=_UI, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=120)
    assert r.stdout.strip(), f"harness produced nothing:\n{r.stderr[:2000]}"
    failed = [x for x in json.loads(r.stdout) if not x["ok"]]
    assert not failed, "parallel progress broken:\n" + "\n".join(
        f"  {x['n']}: got {x['g']!r}, want {x['e']!r}" for x in failed)


@pytest.mark.skipif(not _have_jsdom(), reason="node + jsdom not installed (see tests/ui/README)")
def test_step_navigation_does_not_strand_you():
    """Going back to change an answer must not disable the question you came from.

    Reported from use: jumping from an unanswered DESTINATION back to QUALITY left
    destination neither done nor current, so it disabled itself and the only way
    forward was re-confirming every question in between.
    """
    r = subprocess.run(["node", "nav.js"], cwd=_UI, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=120)
    assert r.stdout.strip(), f"harness produced nothing:\n{r.stderr[:2000]}"
    failed = [x for x in json.loads(r.stdout) if not x["ok"]]
    assert not failed, "step navigation broken:\n" + "\n".join(
        f"  {x['n']}: got {x['g']!r}, want {x['e']!r}" for x in failed)


def _run_all():
    # Ask the question directly. When pytest IS installed the decorator is
    # pytest's own and leaves no attribute behind, so relying on the marker ran
    # the harness on a machine with no jsdom and failed the build.
    if not _have_jsdom():
        print("  (node/jsdom not installed — UI harness skipped)")
        return
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} UI test(s) passed.")


if __name__ == "__main__":
    _run_all()
