"""End-to-end pipeline test — scan a mixed folder, verify outcomes + placement.

Needs ffmpeg/ffprobe; skips cleanly if absent.
Run:  python3 tests/test_pipeline.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc import pipeline, report  # noqa: E402
from vtc.config import Encoder, RunConfig, SourceAction, Tier  # noqa: E402
from vtc.ffprobe import MediaInfo, probe  # noqa: E402
from vtc.model import target_kbps  # noqa: E402
from vtc.result import Mode, Outcome  # noqa: E402

_HAVE_FF = shutil.which("ffmpeg") and shutil.which("ffprobe")


def _clip(path: Path, vcodec: str, bitrate_k: int, secs=2):
    extra = ["-tag:v", "hvc1"] if vcodec == "libx265" else []
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=1920x1080:rate=30", "-t", str(secs),
         "-c:v", vcodec, "-b:v", f"{bitrate_k}k", "-pix_fmt", "yuv420p", *extra, str(path)],
        check=True, stdin=subprocess.DEVNULL,
    )


def test_pipeline_mixed_folder():
    if not _HAVE_FF:
        print("  skip test_pipeline_mixed_folder (ffmpeg/ffprobe not found)")
        return
    pipeline.STOP_FILE.unlink(missing_ok=True)  # ensure no stale stop flag
    with tempfile.TemporaryDirectory() as d:
        src = Path(d)
        _clip(src / "fat.mkv", "libx264", 12000)     # fat h264 in mkv  -> SHRINK
        _clip(src / "lean.mp4", "libx264", 3500)      # h264 mp4 below tier -> SKIP_UNDER_TIER
        _clip(src / "already.mp4", "libx265", 3000)   # hevc mp4         -> SKIP_MODERN

        cfg = RunConfig(src=src, encoder=Encoder.SOFTWARE, source_action=SourceAction.ARCHIVE)
        assert cfg.validate() == []

        results = pipeline.run(cfg)
        by_name = {r.path.name: r.outcome for r in results}
        assert by_name["fat.mkv"] is Outcome.SHRINK, by_name
        assert by_name["lean.mp4"] is Outcome.SKIP_UNDER_TIER, by_name
        assert by_name["already.mp4"] is Outcome.SKIP_MODERN, by_name

        # Placement: fat.mkv -> fat.mp4 (hevc), original archived under originals/
        assert (src / "fat.mp4").exists()
        assert probe(src / "fat.mp4").vcodec == "hevc"
        assert (src / "originals" / "fat.mkv").exists()
        assert not (src / "fat.mkv").exists()          # original moved
        # Untouched files stay put and unchanged
        assert probe(src / "lean.mp4").vcodec == "h264"
        assert probe(src / "already.mp4").vcodec == "hevc"

        # Report renders and space savings is positive
        rendered = report.render(results)
        assert "RUN SUMMARY" in rendered and "3 file(s) scanned" in rendered
        assert "SPACE SAVED" in rendered
        print("  ok  mixed folder: shrink / skip-at-tier / skip-modern + placement")

        # Ledger created; a second run resumes (no re-encode)
        assert (src / ".vtc_processed.log").exists()
        results2 = pipeline.run(cfg)
        out2 = {r.path.name: r.outcome for r in results2}
        # fat is now fat.mp4 (hevc) so it appears under that name; nothing shrinks again
        assert Outcome.SHRINK not in out2.values(), out2
        assert out2.get("lean.mp4") is Outcome.RESUME, out2
        print("  ok  second run resumes (no re-encode)")


def _h264(kbps: int) -> MediaInfo:
    return MediaInfo(path=Path("x.mp4"), ok=True, vcodec="h264",
                     width=1920, height=1080, fps=24.0, bit_rate=kbps * 1000)


def test_shrink_only_when_it_can_clear_the_savings_bar():
    """A shrink must be predicted to clear the post-encode min-saving gate.

    Files merely OVER target used to be encoded and then thrown away by the size
    gate — original safe, but the time wasted and the estimate over-promising.
    """
    cfg = RunConfig(src=Path("."), tier=Tier.GOOD)
    target = target_kbps(cfg.tier, 1920 * 1080, 24.0, cfg.out_codec)
    bar = target / cfg.min_saving_ratio          # source must be at least this fat

    # Comfortably fat: shrink.
    mode, outcome, _ = pipeline.decide(cfg, _h264(int(bar * 1.5)))
    assert mode is Mode.SHRINK and outcome is None, (mode, outcome)

    # In the old dead zone — over target, but the output could never be 25%
    # smaller. Left alone rather than encoded-then-discarded.
    mode, outcome, _ = pipeline.decide(cfg, _h264(int(bar * 0.9)))
    assert mode is None and outcome is Outcome.SKIP_AT_TIER, (mode, outcome)

    # Already at tier: unchanged behaviour.
    mode, outcome, _ = pipeline.decide(cfg, _h264(int(target * 0.9)))
    assert mode is None and outcome is Outcome.SKIP_AT_TIER, (mode, outcome)
    print("  ok  shrink decision requires a predicted saving, not just over-target")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} pipeline test(s) done.")


if __name__ == "__main__":
    _run_all()
