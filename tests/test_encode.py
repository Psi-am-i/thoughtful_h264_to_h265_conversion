"""encode tests — unit-test the arg builder, then a real software HEVC encode.

The arg-builder tests run without ffmpeg. The integration test generates a fat
1080p30 h264 clip, probes it, and runs a software (libx265) SHRINK, asserting the
output is a valid HEVC MP4.

Run:  pytest tests/test_encode.py   |   python tests/test_encode.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc.config import Encoder, RunConfig  # noqa: E402
from vtc.encode import build_video_args, run_encode, use_hardware, select_hw_encoder  # noqa: E402
from vtc.ffprobe import MediaInfo, probe  # noqa: E402
from vtc.model import OutCodec  # noqa: E402
from vtc.result import Mode  # noqa: E402

_HAVE_FF = shutil.which("ffmpeg") and shutil.which("ffprobe")


def _cfg(**kw) -> RunConfig:
    return RunConfig(src=Path("."), **kw)


# ── Unit tests (no ffmpeg) ────────────────────────────────────────────────────

def test_build_video_args_shrink_h265_software():
    cfg = _cfg(out_codec=OutCodec.H265, encoder=Encoder.SOFTWARE)
    info = MediaInfo(path=Path("x.mp4"), ok=True, vcodec="h264", pix_fmt="yuv420p")
    args = build_video_args(cfg, info, Mode.SHRINK, 4080, hw_encoder=None)
    assert "libx265" in args
    assert "-crf" in args and args[args.index("-crf") + 1] == "21"
    # capped-CRF ceiling sits AT the tier target with a tight (~1s) bufsize, so the
    # software average holds near it and re-runs converge (over_target absorbs the slop)
    assert "-maxrate" in args and args[args.index("-maxrate") + 1] == "4080k"
    assert "-bufsize" in args and args[args.index("-bufsize") + 1] == "4080k"
    assert "-preset" in args and args[args.index("-preset") + 1] == "medium"
    assert "-tag:v" in args and args[args.index("-tag:v") + 1] == "hvc1"
    # 8-bit source -> main profile
    assert args[args.index("-profile:v") + 1] == "main"
    print("  ok  build_video_args SHRINK h265 software")


def test_build_video_args_shrink_h265_10bit():
    cfg = _cfg(out_codec=OutCodec.H265, encoder=Encoder.SOFTWARE)
    info = MediaInfo(path=Path("x.mp4"), ok=True, vcodec="hevc", pix_fmt="yuv420p10le")
    args = build_video_args(cfg, info, Mode.SHRINK, 4080, hw_encoder=None)
    assert args[args.index("-profile:v") + 1] == "main10"
    print("  ok  build_video_args SHRINK h265 10-bit -> main10")


def test_build_video_args_transcode_fidelity():
    cfg = _cfg(out_codec=OutCodec.H265, encoder=Encoder.SOFTWARE)
    info = MediaInfo(path=Path("x.mp4"), ok=True, vcodec="mpeg2video", pix_fmt="yuv420p")
    args = build_video_args(cfg, info, Mode.TRANSCODE, 5000, hw_encoder=None)
    assert args[args.index("-crf") + 1] == "20"
    assert args[args.index("-preset") + 1] == "slow"
    print("  ok  build_video_args TRANSCODE h265 -> crf 20 / slow")


def test_build_video_args_h264_software():
    cfg = _cfg(out_codec=OutCodec.H264, encoder=Encoder.SOFTWARE)
    info = MediaInfo(path=Path("x.mp4"), ok=True, vcodec="h264", pix_fmt="yuv420p")
    args = build_video_args(cfg, info, Mode.SHRINK, 6800, hw_encoder=None)
    assert "libx264" in args
    assert args[args.index("-crf") + 1] == "20"
    # software ceiling capped at the tier target with tight bufsize so re-runs converge
    assert args[args.index("-maxrate") + 1] == "6800k"
    assert args[args.index("-bufsize") + 1] == "6800k"
    assert args[args.index("-profile:v") + 1] == "high"
    assert args[args.index("-pix_fmt") + 1] == "yuv420p"
    print("  ok  build_video_args SHRINK h264 software")


def test_build_video_args_hardware_no_crf():
    cfg = _cfg(out_codec=OutCodec.H265, encoder=Encoder.HARDWARE)
    info = MediaInfo(path=Path("x.mp4"), ok=True, vcodec="h264", pix_fmt="yuv420p")
    args = build_video_args(cfg, info, Mode.SHRINK, 4080, hw_encoder="hevc_videotoolbox")
    assert "hevc_videotoolbox" in args
    assert "-crf" not in args
    assert args[args.index("-b:v") + 1] == "4080k"
    assert "-tag:v" in args and args[args.index("-tag:v") + 1] == "hvc1"
    print("  ok  build_video_args hardware h265 -> b:v, no crf")


def test_build_video_args_remux():
    cfg = _cfg(out_codec=OutCodec.H265, encoder=Encoder.SOFTWARE)
    # source hevc -> copy + hvc1 tag
    info_hevc = MediaInfo(path=Path("x.mkv"), ok=True, vcodec="hevc")
    args = build_video_args(cfg, info_hevc, Mode.REMUX, 0, hw_encoder=None)
    assert args[:2] == ["-c:v", "copy"]
    assert "-tag:v" in args and args[args.index("-tag:v") + 1] == "hvc1"
    # source h264 -> copy, no hvc1 tag
    info_h264 = MediaInfo(path=Path("x.mkv"), ok=True, vcodec="h264")
    args2 = build_video_args(cfg, info_h264, Mode.REMUX, 0, hw_encoder=None)
    assert args2 == ["-c:v", "copy"]
    print("  ok  build_video_args REMUX copy (+hvc1 for hevc)")


def test_hw_encoder_selection():
    # SOFTWARE is always None / False, regardless of machine.
    assert select_hw_encoder(_cfg(encoder=Encoder.SOFTWARE)) is None
    assert use_hardware(_cfg(encoder=Encoder.SOFTWARE)) is False
    # HARDWARE/AUTO are functionally probed, so the result is machine-dependent
    # (None on a CI box with no GPU/VideoToolbox). Assert only the invariants:
    # use_hardware agrees with select_hw_encoder, and any pick is a real candidate.
    from vtc.encode import _HW_CANDIDATES
    for enc_mode in (Encoder.HARDWARE, Encoder.AUTO):
        cfg = _cfg(encoder=enc_mode, out_codec=OutCodec.H265)
        pick = select_hw_encoder(cfg)
        assert use_hardware(cfg) is (pick is not None)
        assert pick is None or pick in _HW_CANDIDATES[OutCodec.H265]
    print("  ok  hw encoder selection (functionally probed)")


# ── Integration test (needs ffmpeg) ───────────────────────────────────────────

def _make_fat_h264(path: Path):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=1920x1080:rate=30", "-t", "2",
         "-c:v", "libx264", "-b:v", "12000k", "-pix_fmt", "yuv420p", str(path)],
        check=True, stdin=subprocess.DEVNULL,
    )


def test_software_shrink_produces_hevc():
    if not _HAVE_FF:
        print("  skip test_software_shrink_produces_hevc (ffmpeg/ffprobe not found)")
        return
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        fat = d / "fat.mp4"
        out = d / "out.mp4"
        _make_fat_h264(fat)

        info = probe(fat)
        assert info.ok and info.vcodec == "h264"

        cfg = _cfg(out_codec=OutCodec.H265, encoder=Encoder.SOFTWARE)
        res = run_encode(cfg, info, Mode.SHRINK, fat, out, target_kbps=4080, hw_encoder=None)

        assert res.ok, f"encode failed: {res.error}"
        assert out.exists()
        assert res.out_bytes > 0
        assert res.out_path == out

        outi = probe(out)
        assert outi.ok and outi.vcodec == "hevc", f"expected hevc, got {outi.vcodec}"
        print(f"  ok  software SHRINK -> {outi.vcodec} "
              f"({res.out_bytes // 1024}KB, was {fat.stat().st_size // 1024}KB)")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} encode test(s) done.")


if __name__ == "__main__":
    _run_all()
