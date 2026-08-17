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


def test_source_below_tier_is_flagged_distinctly():
    """A source LOWER quality than the chosen tier is left alone (we never inflate) — but
    flagged 'below your quality tier', distinct from a file genuinely AT tier."""
    cfg = _cfg()                                            # H.265 output, EXCELLENT tier
    tgt = target_kbps(Tier.EXCELLENT, _PX, 24, OutCodec.H265)   # kbps at the tier
    below = _mi(path=Path("low.mp4"), vcodec="h264", fps=24.0, bit_rate=int(tgt * 1000 * 0.4))
    near  = _mi(path=Path("near.mp4"), vcodec="h264", fps=24.0, bit_rate=int(tgt * 1000 * 1.05))
    assert pipeline.decide(cfg, below)[1] is Outcome.SKIP_UNDER_TIER
    assert pipeline.decide(cfg, near)[1]  is Outcome.SKIP_AT_TIER


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


# ── "Avoid sidecar .srt files — MKV only when a subtitle can't embed in MP4" ──
def test_avoid_sidecar_subs_label():
    # A subtitle MP4 can't embed (PGS image track). imageSubs off so THIS toggle is
    # what's under test, not keep-image-subs.
    unembeddable = _mi(vcodec="hevc", audio=[AudioTrack(1, "aac", 2)],
                       subtitles=[SubtitleTrack(3, "hdmv_pgs_subtitle", False)])
    # A normal text subtitle embeds fine (mov_text) — it must NOT force MKV.
    embeddable = _mi(vcodec="hevc", audio=[AudioTrack(1, "aac", 2)],
                     subtitles=[SubtitleTrack(3, "subrip", True)])
    no_subs = _mi(vcodec="hevc", audio=[AudioTrack(1, "aac", 2)])
    # This toggle is an exception to a forced MP4, so it's under MP4 that it bites: ON
    # keeps MKV to embed the un-embeddable sub, OFF writes a sidecar .srt (stays MP4).
    assert encode.resolve_container(_cfg(container="mp4", forceMkvSubs=True, imageSubs=False), unembeddable) is Container.MKV
    assert encode.resolve_container(_cfg(container="mp4", forceMkvSubs=True), embeddable) is Container.MP4
    assert encode.resolve_container(_cfg(container="mp4", forceMkvSubs=False, imageSubs=False), unembeddable) is Container.MP4
    assert encode.resolve_container(_cfg(container="mp4", forceMkvSubs=True), no_subs) is Container.MP4
    # The only statically un-embeddable subs are image-based, and AUTO keeps THOSE as
    # MKV via the image-subs rule (which fires first). AUTO no longer forces MKV for the
    # forceMkvSubs toggle itself — a genuinely un-embeddable *text* sub would be written
    # to a lossless .srt — but every real text codec embeds, so that path is the runtime
    # sidecar fallback, not this static decision. Here the image rule governs, so: MKV.
    assert encode.resolve_container(_cfg(container="auto", forceMkvSubs=True, imageSubs=False), unembeddable) is Container.MKV


# ── "Keep image subtitles — prefer MKV over dropping PGS/DVD tracks" ──────────
def test_keep_image_subs_label():
    pgs = _mi(vcodec="hevc", subtitles=[SubtitleTrack(2, "hdmv_pgs_subtitle", False)])
    # Under a forced MP4 the toggle decides: ON keeps MKV for PGS, OFF drops to MP4.
    assert encode.resolve_container(_cfg(container="mp4", imageSubs=True), pgs) is Container.MKV
    assert encode.resolve_container(_cfg(container="mp4", imageSubs=False), pgs) is Container.MP4
    # Auto keeps PGS as MKV whatever the toggle.
    assert encode.resolve_container(_cfg(container="auto", imageSubs=False), pgs) is Container.MKV


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
    # Forced MP4 with its keep-MKV exceptions off is a hard MP4, even for PGS...
    assert encode.resolve_container(_cfg(container="mp4", imageSubs=False), pgs) is Container.MP4
    # ...but with the (default) image-subs exception on, it keeps MKV for PGS.
    assert encode.resolve_container(_cfg(container="mp4"), pgs) is Container.MKV
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



# ── the savings estimate ─────────────────────────────────────────────────────
def test_prediction_models_audio_separately():
    """The whole-file-ratio model mispredicts anything with substantial audio.

    A 2-hour film: 10 Mbps video + 3 Mbps lossless audio in a 11.7 GB file,
    shrunk to a 2.5 Mbps video target. The audio is passed through, so it
    survives at full size — the naive `size x target/source` model forgets that
    and promises a saving that cannot arrive.
    """
    dur = 7200.0
    video_bps, audio_bps = 10_000_000, 3_000_000
    size = int((video_bps + audio_bps) / 8 * dur)             # ~11.7 GB
    info = MediaInfo(path=Path("film.mkv"), ok=True, vcodec="h264", width=1920,
                     height=1080, fps=24.0, bit_rate=video_bps, duration=dur,
                     audio=[AudioTrack(1, "truehd", 6)])
    cfg = _cfg()
    got = pipeline.predict_output_bytes(cfg, info, size, Mode.SHRINK, 2500)

    # truth: 2.5 Mbps of video for 2 hours, plus the audio carried across intact
    expect = int(2_500_000 / 8 * dur + audio_bps / 8 * dur)
    assert abs(got - expect) / expect < 0.02, (got, expect)

    naive = int(size * (2500 / (video_bps / 1000)))           # the old model
    assert naive < expect * 0.8, "fixture no longer demonstrates the old error"


def test_prediction_reprices_audio_when_it_is_re_encoded():
    dur = 7200.0
    info = MediaInfo(path=Path("film.mkv"), ok=True, vcodec="h264", width=1920,
                     height=1080, fps=24.0, bit_rate=10_000_000, duration=dur,
                     audio=[AudioTrack(1, "truehd", 6)])
    size = int(13_000_000 / 8 * dur)
    cfg = _cfg(audio="aac", abMulti=448)
    got = pipeline.predict_output_bytes(cfg, info, size, Mode.SHRINK, 2500)
    expect = int(2_500_000 / 8 * dur + 448_000 / 8 * dur)      # video + AAC 448k
    assert abs(got - expect) / expect < 0.02, (got, expect)


def test_prediction_leaves_untouched_files_alone():
    info = MediaInfo(path=Path("x.mkv"), ok=True, vcodec="hevc", width=1920,
                     height=1080, fps=24.0, bit_rate=5_000_000, duration=100.0)
    for mode in (None, Mode.REMUX):
        assert pipeline.predict_output_bytes(_cfg(), info, 999, mode, 0) == 999


def test_prediction_never_exceeds_the_source():
    # A shrink that models bigger than the source would advertise a negative
    # saving; the size gate would throw such an encode away anyway.
    info = MediaInfo(path=Path("x.mkv"), ok=True, vcodec="h264", width=1920,
                     height=1080, fps=24.0, bit_rate=1_000_000, duration=100.0)
    size = int(1_000_000 / 8 * 100)
    assert pipeline.predict_output_bytes(_cfg(), info, size, Mode.SHRINK, 50_000) <= size


# ── recognising our own work ─────────────────────────────────────────────────
def _stamped(**tags):
    info = MediaInfo(path=Path("x.mp4"), ok=True, vcodec="h264", width=1920,
                     height=1080, fps=24.0, bit_rate=20_000_000, duration=100.0)
    info.vtc = tags
    return info


def test_second_generation_is_refused_by_default():
    """Re-encoding our own lossy output spends a generation nothing gives back."""
    ours = _stamped(VTC_MODE="shrink", VTC_CODEC="h264", VTC_QUALITY="109",
                    VTC_DATE="2026-08-03")
    assert pipeline.decide(_cfg(), ours)[1] is Outcome.SKIP_SECOND_GEN
    # ...but it is a decision the user can make on purpose
    cfg = _cfg()
    cfg.allow_second_generation = True
    assert pipeline.decide(cfg, ours)[0] is Mode.SHRINK


def test_a_remux_is_not_a_generation():
    """A remux is a stream copy: it costs nothing, so it must not block a shrink."""
    remuxed = _stamped(VTC_MODE="remux", VTC_CODEC="h264")
    assert pipeline.decide(_cfg(), remuxed)[0] is Mode.SHRINK


def test_untouched_files_are_unaffected():
    plain = MediaInfo(path=Path("x.mp4"), ok=True, vcodec="h264", width=1920,
                      height=1080, fps=24.0, bit_rate=20_000_000, duration=100.0)
    assert plain.vtc_lossy_generation is False
    assert pipeline.decide(_cfg(), plain)[0] is Mode.SHRINK


def test_the_comment_alone_is_enough_to_recognise_us():
    """MP4 drops custom keys without -movflags use_metadata_tags, so a third-party
    remux can strip them while leaving the comment. Either marker must do."""
    info = MediaInfo(path=Path("x.mp4"), ok=True, vcodec="h264", width=1920,
                     height=1080, fps=24.0, bit_rate=20_000_000, duration=100.0)
    info.comment = "Very Thoughtful Compression 1.1 — shrink to H.265 at quality 145 on 2026-08-11"
    assert info.vtc_lossy_generation is True
    assert pipeline.decide(_cfg(), info)[1] is Outcome.SKIP_SECOND_GEN
    # somebody else's comment is not our signature
    info.comment = "Ripped by SOMEGROUP"
    assert info.vtc_lossy_generation is False


def test_we_do_not_overwrite_an_existing_comment():
    from vtc import encode
    src = MediaInfo(path=Path("x.mp4"), ok=True, vcodec="h264", duration=100.0)
    args = " ".join(encode.vtc_metadata(_cfg(), Mode.SHRINK, 4000, src))
    assert "comment=Very Thoughtful Compression" in args      # none there: write ours
    src.comment = "Ripped by SOMEGROUP"
    args = " ".join(encode.vtc_metadata(_cfg(), Mode.SHRINK, 4000, src))
    assert "comment=" not in args                              # theirs: leave it alone
    assert "VTC_MODE=shrink" in args                           # structured tags regardless


# ── measuring the video bitrate, whatever the container says ─────────────────
def test_video_bitrate_is_not_the_container_total():
    """Only MP4/MOV/AVI report per-stream bitrate; MKV, TS and WebM say N/A.

    Charging the video for the audio over-stated a Blu-ray MKV by 34% (12.1 Mbps
    against a true 9.0) — plenty to push a lean file over the re-encode gate.
    """
    info = MediaInfo(path=Path("x.mkv"), ok=True, vcodec="h264", width=1920,
                     height=1080, fps=24.0, duration=100.0)
    info.container_bit_rate = 12_100_000
    info.audio_bps = 3_100_000
    assert info.effective_bps == 9_000_000        # audio taken off, not charged
    info.bit_rate = 9_017_498                     # exact figure wins outright
    assert info.effective_bps == 9_017_498


def test_stale_statistics_tags_are_rejected():
    """Statistics tags survive a re-encode — ffmpeg copies them onto the NEW
    stream, so our HEVC output claimed 2.9 GB of video inside a 1.75 GB file."""
    from vtc.ffprobe import _sane
    assert _sane(9_017_498, 5_374_111) == 0          # cannot out-weigh its container
    assert _sane(3_836_000, 5_374_111) == 3_836_000  # plausible, kept
    assert _sane(0, 5_374_111) == 0


def test_our_outputs_do_not_inherit_the_sources_statistics():
    from vtc import encode
    src = MediaInfo(path=Path("x.mkv"), ok=True, vcodec="h264", duration=100.0)
    args = encode.vtc_metadata(_cfg(), Mode.SHRINK, 4000, src)
    joined = " ".join(args)
    for stale in ("BPS=", "NUMBER_OF_BYTES=", "_STATISTICS_TAGS="):
        assert f"-metadata:s:v:0 {stale}" in joined.replace("' '", " "), stale


def test_audio_is_estimated_only_when_nothing_is_reported():
    """The fallback of last resort — a plain ffmpeg MKV or a WebM, where neither
    a per-stream bitrate nor a statistics tag exists. Only ever applies to
    containers that pass audio through untouched; MP4, which is the one that
    re-encodes audio to fit, reports its bitrates and never reaches this."""
    info = MediaInfo(path=Path("x.mkv"), ok=True, vcodec="h264", width=1920,
                     height=1080, fps=24.0, duration=100.0,
                     audio=[AudioTrack(1, "truehd", 6)])
    info.container_bit_rate = 12_000_000
    # nothing reported: subtract an estimate rather than charge the video for it
    assert info.effective_bps == 12_000_000 - 3_000_000
    # a real figure always wins over the estimate
    info.audio_bps = 3_110_857
    assert info.effective_bps == 12_000_000 - 3_110_857

def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} label tests passed.")


if __name__ == "__main__":
    _run_all()
