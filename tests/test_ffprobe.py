"""ffprobe integration test — generate real clips, probe them, run the decision.

Needs ffmpeg/ffprobe on PATH; skips cleanly if absent.
Run:  pytest tests/test_ffprobe.py   |   python tests/test_ffprobe.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc.ffprobe import probe  # noqa: E402
from vtc.model import CodecCategory, OutCodec, Tier, classify_codec, over_target, target_kbps  # noqa: E402

_HAVE_FF = shutil.which("ffmpeg") and shutil.which("ffprobe")


def _make_clip(path: Path, vcodec: str, bitrate_k: int, size="1920x1080", rate=30, secs=2):
    extra = ["-tag:v", "hvc1"] if vcodec == "libx265" else []
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc2=size={size}:rate={rate}", "-t", str(secs),
         "-c:v", vcodec, "-b:v", f"{bitrate_k}k", "-pix_fmt", "yuv420p", *extra, str(path)],
        check=True, stdin=subprocess.DEVNULL,
    )


def test_probe_and_decide():
    if not _HAVE_FF:
        print("  skip test_probe_and_decide (ffmpeg/ffprobe not found)")
        return
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        fat = d / "fat.mp4"
        hevc = d / "already.mp4"
        _make_clip(fat, "libx264", 12000)
        _make_clip(hevc, "libx265", 3000)

        fi = probe(fat)
        assert fi.ok and fi.vcodec == "h264"
        assert fi.width == 1920 and fi.height == 1080
        assert 29 <= fi.fps <= 31
        assert fi.effective_bps > 8_000_000
        assert classify_codec(fi.vcodec) is CodecCategory.H264

        # Fat h264 -> H.265 EXCELLENT: over target -> encode.
        tgt = target_kbps(Tier.EXCELLENT, fi.pixels, fi.fps, OutCodec.H265)
        assert tgt == 6300
        assert over_target(fi.effective_bps / 1000, tgt) is True
        print(f"  ok  fat h264 {fi.effective_bps//1000}k -> target {tgt}k -> encode")

        hi = probe(hevc)
        assert hi.ok and classify_codec(hi.vcodec) is CodecCategory.MODERN
        print(f"  ok  already-{hi.vcodec} classified MODERN (never transcoded)")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} ffprobe test(s) done.")


if __name__ == "__main__":
    _run_all()
