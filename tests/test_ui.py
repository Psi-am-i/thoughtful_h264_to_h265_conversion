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

import pytest

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
