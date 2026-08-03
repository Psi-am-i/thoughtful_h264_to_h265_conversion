"""Every subprocess spawn in the engine must carry the Windows no-console flag.

A windowed (console=False) Windows build pops a fresh console window for every
child process; the fix is CREATE_NO_WINDOW on EVERY spawn (WINDOWS-GOTCHAS.md #3).
One missed spawn brings the strobe back, and it is invisible on macOS — so guard
it structurally: walk the AST of every vtc module and assert each
`subprocess.run` / `subprocess.Popen` call spreads `**NO_WINDOW` (or passes
`creationflags` outright). Also pins the winproc contract itself.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_VTC = Path(__file__).resolve().parent.parent / "vtc"


def _spawn_calls(tree: ast.AST):
    """Yield every `subprocess.run(...)` / `subprocess.Popen(...)` Call node."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (isinstance(fn, ast.Attribute) and fn.attr in ("run", "Popen")
                and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
            yield node


def _has_no_window(call: ast.Call) -> bool:
    """True if the call spreads `**NO_WINDOW` or passes an explicit creationflags."""
    for kw in call.keywords:
        if kw.arg == "creationflags":                       # explicit flag
            return True
        if kw.arg is None and isinstance(kw.value, ast.Name) and kw.value.id == "NO_WINDOW":
            return True                                     # **NO_WINDOW spread
    return False


def test_every_subprocess_spawn_suppresses_the_console_window():
    offenders = []
    for path in sorted(_VTC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _spawn_calls(tree):
            if not _has_no_window(call):
                offenders.append(f"{path.name}:{call.lineno}")
    assert not offenders, (
        "subprocess spawn missing **NO_WINDOW (console strobes on Windows): "
        + ", ".join(offenders))


def test_found_the_spawns_at_all():
    """Guard against the walker silently matching nothing (e.g. an import rename)
    turning the assertion above into a vacuous pass."""
    total = sum(len(list(_spawn_calls(ast.parse(p.read_text(encoding="utf-8")))))
                for p in _VTC.glob("*.py"))
    assert total >= 6, f"expected to find the known subprocess spawns, found {total}"


def test_winproc_constants_are_platform_correct():
    from vtc import winproc
    if sys.platform == "win32":
        assert "creationflags" in winproc.NO_WINDOW
        assert winproc.TEXT_UTF8.get("encoding") == "utf-8"
    else:
        assert winproc.NO_WINDOW == {}          # a no-op everywhere but Windows
        assert winproc.TEXT_UTF8 == {}
    winproc.reconfigure_std_streams()           # must never raise on any platform


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} windows-subprocess tests passed.")


if __name__ == "__main__":
    _run_all()
