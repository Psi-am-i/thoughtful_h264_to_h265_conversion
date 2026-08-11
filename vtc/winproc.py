"""Windows-only subprocess hygiene, shared by the engine and the GUI shell.

Two defaults bite a windowed (``console=False``) Windows build, both invisible
on macOS/Linux (see ``Desktop/WINDOWS-GOTCHAS.md`` #3 and #4):

* A windowed app has no console of its own, so Windows spawns a fresh one for
  every child process — probing/encoding a library strobes hundreds of console
  windows. ``CREATE_NO_WINDOW`` on every spawn stops it.
* Text-mode pipes decode with the locale code page (cp1252), while ffmpeg and
  ffprobe emit UTF-8. A filename with an accent, an em dash or a macOS
  private-use codepoint then raises ``UnicodeEncodeError`` mid-encode and fails
  its own track. Forcing ``encoding='utf-8', errors='replace'`` fixes the read;
  :func:`reconfigure_std_streams` fixes the corresponding write.

Both constants are empty off Windows, so call sites stay platform-clean:
``subprocess.run(cmd, capture_output=True, text=True, **TEXT_UTF8, **NO_WINDOW)``.
"""

from __future__ import annotations

import subprocess
import sys

# Spread into EVERY subprocess.run / Popen so a windowed app never strobes
# consoles. A no-op except on Windows (and only when the flag exists).
NO_WINDOW: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW")
    else {}
)

# Spread into any subprocess that DECODES child text (capture_output / a text
# PIPE). ffmpeg/ffprobe speak UTF-8; 'replace' turns an undecodable byte into
# U+FFFD instead of raising. Empty off Windows — the POSIX locale is already
# UTF-8, so we leave the platform default untouched there.
TEXT_UTF8: dict = (
    {"encoding": "utf-8", "errors": "replace"} if sys.platform == "win32" else {}
)


def reconfigure_std_streams() -> None:
    """Force stdout/stderr to UTF-8 so *printing* a filename with an accent, an
    em dash or a macOS private-use codepoint can't raise ``UnicodeEncodeError``
    on a cp1252 console. Call once at startup, before anything can print a path.

    Safe everywhere and idempotent; a windowed frozen app may have no streams at
    all (``sys.stdout is None``), in which case there is simply nothing to do.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — detached/replaced stream; not fatal
            pass
