"""Resume ledger — skip files already finished under the same settings.

A completed file is recorded as one line::

    <settings-sig> TAB <abspath> TAB <size> TAB <mtime>

The settings signature (from ``RunConfig.settings_signature``) means a re-run
with the SAME tier/codec/options skips done files, while changing any of them
re-evaluates everything. Appends are atomic for short lines, so parallel
workers can share the one file. Mirrors the bash ``ledger_*`` helpers.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import RunConfig


class Ledger:
    """Persistent resume ledger built from a :class:`RunConfig`.

    Disabled (a no-op) when the config disables it or the file cannot be
    written; in that case :meth:`has` always returns ``False`` and
    :meth:`add` does nothing. Neither :meth:`has` nor :meth:`add` ever raise.
    """

    def __init__(self, config: RunConfig) -> None:
        self._signature: str = config.settings_signature()
        # Per-FILE encoder choices can't live in the run-wide signature, so they
        # ride on the individual key instead: a file done in hardware must not
        # count as done once it has been picked out for software.
        self._config = config
        path = config.resolved_ledger_file()
        self._path: Path | None = None
        if path is not None:
            # Mirror the bash "cannot write ledger -> disabled" behavior: try to
            # touch/create the file; if that fails, run as a no-op.
            try:
                with open(path, "a", encoding="utf-8"):
                    pass
                self._path = path
            except OSError:
                self._path = None

    @property
    def enabled(self) -> bool:
        """True when the ledger is active (writable and not disabled)."""
        return self._path is not None

    def key(self, path: Path) -> str:
        """Return the ledger line for ``path`` (without a trailing newline).

        Format: ``"{signature}\\t{abspath}\\t{size}\\t{mtime}"``. The abspath is
        the resolved absolute path; size and mtime come from :func:`os.stat`
        (mtime as an int). If the file no longer exists, size 0 / mtime 0 are
        used instead of raising — compute the key while the source still exists.
        """
        abspath = path.resolve()
        try:
            st = os.stat(abspath)
            size = st.st_size
            mtime = int(st.st_mtime)
        except OSError:
            size = 0
            mtime = 0
        sig = self._signature
        if self._config.forces_software(path):
            sig += "|sw"
        return f"{sig}\t{abspath}\t{size}\t{mtime}"

    def has(self, key: str) -> bool:
        """True if ``key`` is present as an exact line in the ledger file.

        Returns ``False`` when disabled or the file is missing; never raises.
        """
        if self._path is None:
            return False
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.rstrip("\n") == key:
                        return True
        except OSError:
            return False
        return False

    @property
    def path(self) -> Path | None:
        """The ledger file in use, or None when disabled."""
        return self._path

    def count(self) -> int:
        """How many files the history holds, at ANY settings signature.

        Deliberately signature-blind: the "clear history" button in the UI wipes
        the whole file, so the number beside it must count the whole file too.
        """
        if self._path is None:
            return 0
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                return sum(1 for line in fh if line.strip())
        except OSError:
            return 0

    def clear(self) -> int:
        """Empty the history. Returns how many entries were removed (0 on failure)."""
        n = self.count()
        if self._path is None:
            return 0
        try:
            with open(self._path, "w", encoding="utf-8"):
                pass
        except OSError:
            return 0
        return n

    def add(self, key: str) -> None:
        """Append ``key`` (plus a newline) to the ledger file.

        No-op when disabled. Never raises; append is atomic for short lines so
        parallel workers can safely share the one file.
        """
        if self._path is None:
            return
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(key + "\n")
        except OSError:
            pass
