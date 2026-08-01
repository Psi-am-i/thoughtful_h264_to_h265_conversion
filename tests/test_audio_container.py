"""Audio-policy + MKV-container tests.

Unit checks need no ffmpeg; the integration check generates real clips (with an
audio track) and skips if ffmpeg is absent.
Run:  python3 tests/test_audio_container.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc import encode, pipeline  # noqa: E402
from vtc.config import AudioPolicy, Container, Encoder, RunConfig, SourceAction  # noqa: E402
from vtc.ffprobe import AudioTrack, MediaInfo, SubtitleTrack, probe  # noqa: E402
from vtc.result import Outcome  # noqa: E402

_HAVE_FF = shutil.which("ffmpeg") and shutil.which("ffprobe")


def _mi(subs=(), audio=(), vcodec="hevc"):
    return MediaInfo(path=Path("x.mkv"), ok=True, vcodec=vcodec, width=1920, height=1080, fps=24,
                     bit_rate=8_000_000, subtitles=list(subs), audio=list(audio))


def test_resolve_container():
    img = [SubtitleTrack(2, "hdmv_pgs_subtitle", False)]
    c = RunConfig(src=Path("/t"))
    assert encode.resolve_container(c, _mi(subs=img)) is Container.MKV          # keep PGS
    assert encode.resolve_container(c, _mi()) is Container.MP4                   # nothing forces MKV
    assert encode.resolve_container(RunConfig(src=Path("/t"), audio_policy=AudioPolicy.FLAC),
                                    _mi()) is Container.MKV                      # lossless
    assert encode.resolve_container(RunConfig(src=Path("/t"), container=Container.MP4),
                                    _mi(subs=img)) is Container.MP4              # forced MP4
    assert encode.resolve_container(RunConfig(src=Path("/t"), keep_image_subs=False),
                                    _mi(subs=img)) is Container.MP4              # opted out of keeping
    print("  ok  resolve_container")


def test_audio_attempts():
    dts = [AudioTrack(1, "dts", 6)]      # 5.1, not MP4-friendly
    aac = [AudioTrack(1, "aac", 2)]
    c = RunConfig(src=Path("/t"))        # passthrough
    assert encode._audio_attempts(c, _mi(audio=dts), Container.MP4) == \
        [["-c:a", "copy"], ["-c:a", "aac", "-b:a", "448k"]]                      # multichannel bitrate
    assert encode._audio_attempts(c, _mi(audio=dts), Container.MKV) == [["-c:a", "copy"]]
    assert encode._audio_attempts(c, _mi(audio=aac), Container.MP4) == [["-c:a", "copy"]]  # all friendly
    ac3 = RunConfig(src=Path("/t"), audio_policy=AudioPolicy.AC3)
    assert encode._audio_attempts(ac3, _mi(audio=dts), Container.MP4) == [["-c:a", "ac3", "-b:a", "448k"]]
    flac = RunConfig(src=Path("/t"), audio_policy=AudioPolicy.FLAC)
    assert encode._audio_attempts(flac, _mi(audio=aac), Container.MKV) == [["-c:a", "flac"]]
    print("  ok  audio_attempts")


def _clip(path: Path, secs=2):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-t", str(secs), "-c:v", "libx264", "-b:v", "12000k", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-shortest", str(path)],
        check=True, stdin=subprocess.DEVNULL,
    )


def test_flac_forces_mkv_end_to_end():
    if not _HAVE_FF:
        print("  skip flac/mkv integration (no ffmpeg)")
        return
    pipeline.STOP_FILE.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _clip(d / "fat.mkv")
        cfg = RunConfig(src=d, encoder=Encoder.SOFTWARE,
                        audio_policy=AudioPolicy.FLAC, source_action=SourceAction.ARCHIVE)
        results = pipeline.run(cfg)
        assert results[0].outcome is Outcome.SHRINK, results[0].outcome
        out = d / "fat.mkv"                     # FLAC → MKV, in place, same .mkv name
        assert out.exists()
        info = probe(out)
        assert info.vcodec == "hevc", info.vcodec
        assert any(a.codec == "flac" for a in info.audio), info.audio
        print(f"  ok  FLAC policy → MKV output: {info.vcodec} video + "
              f"{[a.codec for a in info.audio]} audio")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} audio/container test(s) done.")


if __name__ == "__main__":
    _run_all()
