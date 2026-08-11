"""Every Advanced-settings control must do what its label says.

The UI half is driven separately in a DOM harness (tick the box, read the state
it hands over). This is the other half: take the payload the modal actually
produces and prove the ENGINE then behaves the way the label promised. A control
that stores a value nobody reads looks identical to a working one from the UI
side — which is exactly how the HEVC-factor boxes sat dead for months.

Runnable two ways:  pytest tests/   |   python tests/test_advanced_labels.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc import encode, pipeline  # noqa: E402
from vtc.config import AudioPolicy, Container, RunConfig  # noqa: E402
from vtc.ffprobe import AudioTrack, MediaInfo, SubtitleTrack  # noqa: E402
from vtc.model import OutCodec, Tier, over_target, target_kbps  # noqa: E402
from vtc.result import Mode, Outcome  # noqa: E402
from vtc.webapp import build_config  # noqa: E402

_PX = 1920 * 1080


def _answers(**adv):
    """The shape the GUI really sends: flow answers + the Advanced payload."""
    base = {"codec": 1, "quality": 2, "saving": 1, "encoder": 0, "dest": 0}  # H265/EXCELLENT
    base["adv"] = adv
    return base


def _cfg(**adv) -> RunConfig:
    return build_config(Path("/tmp"), _answers(**adv))


def _mi(**kw) -> MediaInfo:
    d = dict(path=Path("x.mkv"), ok=True, vcodec="h264", width=1920, height=1080,
             fps=24.0, bit_rate=20_000_000)
    d.update(kw)
    return MediaInfo(**d)


# ── "Bitrate floor — never target below this" ─────────────────────────────────
def test_bitrate_floor_label():
    cfg = _cfg(floor=3000)
    assert cfg.bitrate_floor_kbps == 3000
    # A frame small enough to compute under the floor must be clamped UP to it.
    assert target_kbps(Tier.OK, 160 * 120, 24, OutCodec.H265,
                       floor_kbps=cfg.bitrate_floor_kbps) == 3000


# ── "Re-encode tolerance — only re-encode a source more than this % over" ─────
def test_reencode_tolerance_label():
    cfg = _cfg(tol=25)
    assert abs(cfg.tier_over_tolerance - 1.25) < 1e-9
    tgt = 4000
    assert over_target(4000 * 1.24, tgt, cfg.tier_over_tolerance) is False   # inside 25%
    assert over_target(4000 * 1.26, tgt, cfg.tier_over_tolerance) is True    # past it
    # and it really reaches decide(): a source 20% over is left alone at tol=25
    info = _mi(path=Path("x.mp4"),
               bit_rate=int(target_kbps(Tier.EXCELLENT, _PX, 24, OutCodec.H265) * 1000 * 1.20))
    assert pipeline.decide(cfg, info)[1] is Outcome.SKIP_AT_TIER


# ── "HEVC factor · HD — target = H.264 target × this, at ≤1080p" ──────────────
def test_hevc_factor_label():
    cfg = _cfg(hevcHd=0.30)
    h264 = target_kbps(Tier.EXCELLENT, _PX, 30, OutCodec.H264)
    h265 = target_kbps(Tier.EXCELLENT, _PX, 30, OutCodec.H265, hevc=cfg.hevc_factors())
    assert h265 == int(h264 * 0.30), (h264, h265)


# ── "Quality tiers · bits per pixel per frame" ────────────────────────────────
def test_tier_bpp_label():
    cfg = _cfg(bpp={"EXCELLENT": 0.20})
    # The label promises the target is bpp x pixels x fps x codec factor.
    expect = int(0.20 * _PX * 30 * 0.60 / 1000)
    assert target_kbps(Tier.EXCELLENT, _PX, 30, OutCodec.H265,
                       bpp=cfg.bpp_for(), hevc=cfg.hevc_factors()) == expect


# ── "Force MKV over N tracks — forces MKV when audio+sub tracks exceed this" ──
def test_force_mkv_over_n_tracks_label():
    tracks = _mi(vcodec="hevc",
                 audio=[AudioTrack(1, "aac", 2)] * 2,
                 subtitles=[SubtitleTrack(3, "subrip", True)] * 2)   # 4 tracks total
    assert encode.resolve_container(_cfg(mkvTracks=0), tracks) is Container.MP4   # off
    assert encode.resolve_container(_cfg(mkvTracks=5), tracks) is Container.MP4   # under
    assert encode.resolve_container(_cfg(mkvTracks=3), tracks) is Container.MKV   # exceeded


# ── "Keep image subtitles — prefer MKV over dropping PGS/DVD tracks" ──────────
def test_keep_image_subs_label():
    pgs = _mi(vcodec="hevc", subtitles=[SubtitleTrack(2, "hdmv_pgs_subtitle", False)])
    assert encode.resolve_container(_cfg(imageSubs=True), pgs) is Container.MKV
    assert encode.resolve_container(_cfg(imageSubs=False), pgs) is Container.MP4


# ── "Audio — Passthrough copies tracks; FLAC forces MKV" ─────────────────────
def test_audio_policy_label():
    assert _cfg(audio="flac").audio_policy is AudioPolicy.FLAC
    assert encode.resolve_container(_cfg(audio="flac"), _mi(vcodec="hevc")) is Container.MKV
    assert _cfg(audio="passthrough").audio_policy is AudioPolicy.PASSTHROUGH


# ── "Audio bitrate · stereo / 5.1+" ──────────────────────────────────────────
def test_audio_bitrate_labels():
    cfg = _cfg(audio="aac", abStereo=192, abMulti=512)
    stereo = encode._audio_attempts(cfg, _mi(audio=[AudioTrack(1, "flac", 2)]), Container.MP4)
    multi = encode._audio_attempts(cfg, _mi(audio=[AudioTrack(1, "flac", 6)]), Container.MP4)
    assert "192k" in " ".join(stereo[0]), stereo
    assert "512k" in " ".join(multi[0]), multi


# ── "Container — Auto picks MP4, falling back to MKV only when it must" ──────
def test_container_label():
    pgs = _mi(vcodec="hevc", subtitles=[SubtitleTrack(2, "hdmv_pgs_subtitle", False)])
    assert encode.resolve_container(_cfg(container="mp4"), pgs) is Container.MP4  # forced
    assert encode.resolve_container(_cfg(container="mkv"), _mi(vcodec="hevc")) is Container.MKV
    assert encode.resolve_container(_cfg(container="auto"), _mi(vcodec="hevc")) is Container.MP4


# ── "What to do with files that aren't MP4" — all four options ───────────────
def test_non_mp4_policy_labels():
    mkv_h264 = _mi(path=Path("show.mkv"), vcodec="h264", bit_rate=30_000_000)
    mkv_hevc = _mi(path=Path("show.mkv"), vcodec="hevc", bit_rate=30_000_000)
    legacy = _mi(path=Path("old.avi"), vcodec="mpeg4", bit_rate=8_000_000)

    # Convert all: rehome what can be, transcode the legacy codecs.
    c = _cfg(format="convert")
    assert pipeline.decide(c, mkv_hevc)[0] is Mode.REMUX
    assert pipeline.decide(c, legacy)[0] is Mode.TRANSCODE
    # Remux only: lossless rehoming, legacy left alone.
    c = _cfg(format="remux")
    assert pipeline.decide(c, mkv_hevc)[0] is Mode.REMUX
    assert pipeline.decide(c, legacy)[1] is Outcome.SKIP_INCOMPATIBLE
    # Shrink, keep format: a fat H.264 MKV still shrinks, but is not rehomed.
    c = _cfg(format="shrink_keep")
    assert c.keep_source_container is True
    assert pipeline.decide(c, mkv_h264)[0] is Mode.SHRINK
    assert pipeline.decide(c, mkv_hevc)[1] is Outcome.SKIP_MODERN
    # Leave alone: nothing outside MP4 is touched at all.
    c = _cfg(format="leave")
    assert pipeline.decide(c, mkv_h264)[1] is Outcome.SKIP_NON_MP4
    assert pipeline.decide(c, legacy)[1] is Outcome.SKIP_NON_MP4


# ── "Subtitles — languages / kinds to keep" ──────────────────────────────────
def test_subtitle_filter_labels():
    cfg = _cfg(subLangs=["eng", "fre"], subKinds=["forced"])
    assert cfg.sub_langs == ("eng", "fre")
    assert cfg.sub_kinds == ("forced",)
    # All three kinds ticked means "keep everything", stored as the empty filter.
    assert _cfg(subKinds=["normal", "forced", "hoh"]).sub_kinds == ()


# ── "Parallel jobs — files encoded at once" ──────────────────────────────────
def test_parallel_jobs_label():
    assert _cfg(jobs=4).jobs == 4


# ── "Ignore files" — the rules remove files from the scan entirely ───────────
def test_ignore_rules_labels():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d)
        for name, size in (("keep.mkv", 50_000_000), ("tiny.mkv", 500_000),
                           ("huge.mkv", 900_000_000), ("old.avi", 50_000_000),
                           ("movie-sample.mkv", 50_000_000)):
            (src / name).write_bytes(b"\0" * size)
        cfg = build_config(src, _answers(ignUnderMb=1, ignOverMb=500,
                                         ignExts=[".avi"], ignNames=["sample"]))
        assert [p.name for p in pipeline.iter_video_files(cfg)] == ["keep.mkv"]


# ── "Resume ledger / processing history" ─────────────────────────────────────
def test_history_toggle_label():
    assert _cfg(ledger=False).resolved_ledger_file() is None
    assert _cfg(ledger=True).resolved_ledger_file() is not None


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} label tests passed.")


if __name__ == "__main__":
    _run_all()
