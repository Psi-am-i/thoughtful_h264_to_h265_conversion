"""Ledger tests — resume behavior and settings-signature scoping.

Runnable two ways:  pytest tests/   |   python tests/test_ledger.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc.config import RunConfig  # noqa: E402
from vtc.ledger import Ledger  # noqa: E402
from vtc.model import Tier  # noqa: E402


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def test_add_then_has():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d)
        f = src / "clip.mkv"
        _write(f, b"hello world")
        led = Ledger(RunConfig(src=src))
        assert led.enabled is True
        key = led.key(f)
        assert led.has(key) is False
        led.add(key)
        assert led.has(key) is True


def test_size_or_mtime_change_invalidates():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d)
        f = src / "clip.mkv"
        _write(f, b"small")
        led = Ledger(RunConfig(src=src))
        key_before = led.key(f)
        led.add(key_before)
        assert led.has(key_before) is True

        # Change size (and mtime); the file's key must differ and not hit.
        _write(f, b"a much larger payload than before")
        os.utime(f, (100.0, 200.0))
        key_after = led.key(f)
        assert key_after != key_before
        assert led.has(key_after) is False


def test_signature_scopes_keys():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d)
        f = src / "clip.mkv"
        _write(f, b"contents")

        cfg_a = RunConfig(src=src, tier=Tier.EXCELLENT)
        cfg_b = RunConfig(src=src, tier=Tier.GOOD)
        led_a = Ledger(cfg_a)
        led_b = Ledger(cfg_b)

        key_a = led_a.key(f)
        key_b = led_b.key(f)
        assert key_a != key_b

        led_a.add(key_a)
        # The other-signature ledger sees the same file but must not resume it.
        assert led_a.has(key_a) is True
        assert led_b.has(key_b) is False


def test_disabled_is_noop():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d)
        f = src / "clip.mkv"
        _write(f, b"contents")
        led = Ledger(RunConfig(src=src, ledger_enabled=False))
        assert led.enabled is False
        key = led.key(f)
        assert led.has(key) is False
        led.add(key)  # harmless no-op
        assert led.has(key) is False
        # No ledger file should have been created.
        assert not (src / ".vtc_processed.log").exists()


def test_missing_file_uses_zero_stat():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d)
        missing = src / "gone.mkv"
        led = Ledger(RunConfig(src=src))
        key = led.key(missing)  # must not raise
        assert key.endswith("\t0\t0")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} ledger tests passed.")


if __name__ == "__main__":
    _run_all()
