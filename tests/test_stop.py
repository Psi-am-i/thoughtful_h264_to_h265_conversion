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

import shutil  # noqa: E402
import subprocess  # noqa: E402

from vtc import pipeline  # noqa: E402
from vtc.config import Encoder, RunConfig, SourceAction  # noqa: E402
from vtc.model import OutCodec  # noqa: E402
from vtc.result import Outcome  # noqa: E402

_HAVE_FF = shutil.which("ffmpeg") and shutil.which("ffprobe")


def _fat_clip(path: Path, secs=1):
    """A deliberately over-bitrate H.264 clip, so the tier decides to SHRINK it."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc2=size=640x360:rate=30", "-t", str(secs),
         "-c:v", "libx264", "-b:v", "12000k", "-pix_fmt", "yuv420p", str(path)],
        check=True, stdin=subprocess.DEVNULL)


def _stop_midrun(jobs: int):
    """Run real SOFTWARE encodes and request a stop from inside the run, exactly as
    the Stop button does. Returns (processed, total)."""
    d = Path(tempfile.mkdtemp())
    total = 6
    for i in range(total):
        _fat_clip(d / f"clip{i}.mp4")
    cfg = RunConfig(src=d, encoder=Encoder.SOFTWARE, out_codec=OutCodec.H264,
                    jobs=jobs, ledger_enabled=False,
                    source_action=SourceAction.ARCHIVE)
    pipeline.STOP_FILE.unlink(missing_ok=True)
    try:
        # Press Stop from the PROGRESS callback — i.e. while the first file is still
        # encoding, which is what a user actually does. Triggering it from on_result
        # instead would be a different (and unfair) test: on_result runs on the main
        # thread once a future is collected, by which time the pool worker has long
        # since picked up the next file.
        pressed = []

        def progress(label, frac, stats=None):
            if not pressed:
                pressed.append(label)
                pipeline.STOP_FILE.touch()

        results = pipeline.run(cfg, progress=progress)
        return len(results), total
    finally:
        pipeline.STOP_FILE.unlink(missing_ok=True)
        shutil.rmtree(d, ignore_errors=True)


def test_stop_midrun_software_jobs1():
    """The real complaint: does Stop work when SOFTWARE encoding? With one worker
    nothing else is in flight, so the run must end on the file that was running."""
    if not _HAVE_FF:
        print("  skip test_stop_midrun_software_jobs1 (ffmpeg/ffprobe not found)")
        return
    processed, total = _stop_midrun(jobs=1)
    assert processed == 1, f"expected exactly 1 processed before the stop, got {processed}"
    print(f"  ok  software + jobs=1: stop ended the run after 1 of {total}")


def test_stop_midrun_software_jobs2():
    """With two workers a SECOND file is already encoding when Stop is pressed, so
    it is allowed to finish — 'stop after current file' is really 'after the files
    in flight'. What must NOT happen is the queue continuing past those.
    """
    if not _HAVE_FF:
        print("  skip test_stop_midrun_software_jobs2 (ffmpeg/ffprobe not found)")
        return
    processed, total = _stop_midrun(jobs=2)
    assert processed <= 2, f"stop let {processed} files through with jobs=2 (max 2 in flight)"
    assert processed < total, "stop did not halt the queue at all"
    print(f"  ok  software + jobs=2: stop ended the run after {processed} of {total} "
          f"(in-flight files finish)")


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
