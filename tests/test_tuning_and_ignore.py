"""Advanced-settings tests: retunable tier densities and the ignore rules.

Covers the two things a user can now change that alter what the run DOES rather
than how it looks — the bpp behind each tier, and the rules that take files out
of the library entirely — plus the history controls beside them.

Runnable two ways:  pytest tests/   |   python tests/test_tuning_and_ignore.py
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc import pipeline  # noqa: E402
from vtc.config import RunConfig  # noqa: E402
from vtc.ledger import Ledger  # noqa: E402
from vtc.model import OutCodec, Tier, target_kbps  # noqa: E402
from vtc.webapp import _apply_advanced  # noqa: E402

_PX_1080P = 1920 * 1080


def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return math.isclose(a, b, rel_tol=0, abs_tol=tol)


def _touch(path: Path, size: int = 1024) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


# ── tier densities ────────────────────────────────────────────────────────────
def test_bpp_override_moves_the_target():
    # Double the density -> double the target, exactly. This is the whole model.
    assert target_kbps(Tier.EXCELLENT, _PX_1080P, 30, OutCodec.H264) == 6800
    assert target_kbps(Tier.EXCELLENT, _PX_1080P, 30, OutCodec.H264,
                       bpp=Tier.EXCELLENT.bpp * 2) == 13600


def test_bpp_for_falls_back_to_the_tier():
    cfg = RunConfig(src=Path("."), tier=Tier.GOOD)
    assert _approx(cfg.bpp_for(), Tier.GOOD.bpp)
    # An override for a DIFFERENT tier must not touch this one.
    cfg.tier_bpp = {"INSANE": 0.5}
    assert _approx(cfg.bpp_for(), Tier.GOOD.bpp)
    assert _approx(cfg.bpp_for(Tier.INSANE), 0.5)
    # Junk (zero, negative, unparseable) falls back rather than zeroing a target.
    for bad in (0, -1, "", "abc", None):
        cfg.tier_bpp = {"GOOD": bad}
        assert _approx(cfg.bpp_for(), Tier.GOOD.bpp), bad


def test_retuned_tier_changes_the_ledger_signature():
    base = RunConfig(src=Path("."), tier=Tier.EXCELLENT)
    same = RunConfig(src=Path("."), tier=Tier.EXCELLENT,
                     tier_bpp={"EXCELLENT": Tier.EXCELLENT.bpp})
    tuned = RunConfig(src=Path("."), tier=Tier.EXCELLENT, tier_bpp={"EXCELLENT": 0.13})
    # A no-op override keeps the old signature, so history written before this
    # feature existed still counts; a real retune invalidates it.
    assert same.settings_signature() == base.settings_signature()
    assert tuned.settings_signature() != base.settings_signature()


def test_hevc_factors_reach_the_target():
    # These three were settable but unread before; a changed factor must move the
    # H.265 target and leave H.264 alone.
    assert target_kbps(Tier.EXCELLENT, _PX_1080P, 30, OutCodec.H265) == 4080
    assert target_kbps(Tier.EXCELLENT, _PX_1080P, 30, OutCodec.H265,
                       hevc=(0.30, 0.50, 0.45)) == 2040
    assert target_kbps(Tier.EXCELLENT, _PX_1080P, 30, OutCodec.H264,
                       hevc=(0.30, 0.50, 0.45)) == 6800


# ── ignore rules ──────────────────────────────────────────────────────────────
def test_ignore_reason_per_rule():
    cfg = RunConfig(src=Path("."))
    assert cfg.ignore_reason("show.mkv", 1_000) is None      # nothing set: keep everything

    cfg.ignore_under_bytes = 10_000_000
    assert cfg.ignore_reason("small.mkv", 5_000_000) is not None
    assert cfg.ignore_reason("big.mkv", 20_000_000) is None
    # A size that could not be read must not be guessed at either way.
    assert cfg.ignore_reason("unknown.mkv", None) is None

    cfg = RunConfig(src=Path("."), ignore_over_bytes=10_000_000)
    assert cfg.ignore_reason("huge.mkv", 20_000_000) is not None
    assert cfg.ignore_reason("fine.mkv", 5_000_000) is None

    cfg = RunConfig(src=Path("."), ignore_exts=("avi",))
    assert cfg.ignore_reason("old.avi", 1) is not None
    assert cfg.ignore_reason("OLD.AVI", 1) is not None        # case-insensitive
    assert cfg.ignore_reason("new.mkv", 1) is None
    assert cfg.ignore_reason("avi.mkv", 1) is None            # extension, not substring

    cfg = RunConfig(src=Path("."), ignore_name_contains=("sample",))
    assert cfg.ignore_reason("movie-Sample.mkv", 1) is not None
    assert cfg.ignore_reason("movie.mkv", 1) is None


def test_iter_video_files_applies_the_rules():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d)
        _touch(src / "keep.mkv", 20_000_000)
        _touch(src / "tiny.mkv", 1_000)
        _touch(src / "trailer-sample.mkv", 20_000_000)
        _touch(src / "old.avi", 20_000_000)

        plain = RunConfig(src=src)
        assert len(list(pipeline.iter_video_files(plain))) == 4

        cfg = RunConfig(src=src, ignore_under_bytes=1_000_000,
                        ignore_exts=("avi",), ignore_name_contains=("sample",))
        kept = [p.name for p in pipeline.iter_video_files(cfg)]
        assert kept == ["keep.mkv"]
        # The paired walk still SEES the ignored files, with a reason each — that
        # is what lets the UI say how many its own rules removed.
        entries = list(pipeline.iter_scan_entries(cfg))
        assert len(entries) == 4
        assert sum(1 for _, reason in entries if reason) == 3


def test_a_directory_name_cannot_trigger_a_filename_rule():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d)
        _touch(src / "samples" / "movie.mkv", 20_000_000)
        cfg = RunConfig(src=src, ignore_name_contains=("sample",))
        assert [p.name for p in pipeline.iter_video_files(cfg)] == ["movie.mkv"]


# ── processing history ────────────────────────────────────────────────────────
def test_history_count_and_clear():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d)
        f = _touch(src / "clip.mkv")
        led = Ledger(RunConfig(src=src))
        assert led.count() == 0
        led.add(led.key(f))
        assert led.count() == 1
        assert led.has(led.key(f)) is True
        assert led.clear() == 1
        assert led.count() == 0
        assert led.has(led.key(f)) is False      # cleared history re-considers the file


# ── the UI payload -> config mapping ──────────────────────────────────────────
def test_apply_advanced_maps_bpp_and_ignore_rules():
    cfg = RunConfig(src=Path("."), tier=Tier.EXCELLENT)
    _apply_advanced(cfg, {
        "bpp": {"EXCELLENT": 0.13, "OK": Tier.OK.bpp, "NONSENSE": 9, "GOOD": "x"},
        "ignUnderMb": 50, "ignOverMb": 20000,
        "ignExts": [".AVI", "wmv", "  "], "ignNames": ["sample", " "],
    })
    # Only genuinely retuned tiers are carried; untouched/invalid ones are dropped.
    assert cfg.tier_bpp == {"EXCELLENT": 0.13}
    assert _approx(cfg.bpp_for(), 0.13)
    assert cfg.ignore_under_bytes == 50_000_000
    assert cfg.ignore_over_bytes == 20_000_000_000
    assert cfg.ignore_exts == ("avi", "wmv")      # dot stripped, lowercased, blanks gone
    assert cfg.ignore_name_contains == ("sample",)


def test_apply_advanced_ignores_junk_and_leaves_defaults():
    cfg = RunConfig(src=Path("."))
    _apply_advanced(cfg, {"bpp": "not a dict", "ignUnderMb": "", "ignExts": "avi"})
    assert cfg.tier_bpp == {}
    assert cfg.ignore_under_bytes == 0
    assert cfg.ignore_exts == ()


# ── the two front-ends must agree ─────────────────────────────────────────────
def _html() -> str:
    return (Path(__file__).resolve().parent.parent / "vtc" / "vtc_app_v3.html").read_text(
        encoding="utf-8")


def test_html_tier_defaults_match_the_engine():
    """The modal ships the default densities as literals — they are what "Reset
    tiers" restores — so they must be the engine's own anchors, not a stale copy."""
    import re
    block = re.search(r"const BPP_DEFAULTS = \{([^}]*)\}", _html())
    assert block, "BPP_DEFAULTS not found in the app HTML"
    shown = {k: float(v) for k, v in re.findall(r"(\w+):([\d.]+)", block.group(1))}
    assert set(shown) == {t.name for t in Tier}
    for name, value in shown.items():
        # 4 dp is what the UI displays; the anchor itself carries more.
        assert _approx(value, Tier[name].bpp, tol=5e-5), (name, value, Tier[name].bpp)


def test_html_advanced_keys_are_all_understood_by_the_engine():
    """Every key the modal sends must land somewhere in RunConfig — a renamed key
    on one side would otherwise fail silently, which is how a setting becomes a
    control that does nothing (exactly what happened to the HEVC factors)."""
    import re
    block = re.search(r"const ADV_DEFAULTS = \{(.*?)\n  ignUnderMb", _html(), re.S)
    assert block
    keys = set(re.findall(r"(\w+)\s*:", block.group(1)))
    keys |= {"ignUnderMb", "ignOverMb", "ignExts", "ignNames"}
    # format/subLangs/subKinds are consumed by build_config, not _apply_advanced.
    handled_elsewhere = {"format", "subLangs", "subKinds"}
    cfg = RunConfig(src=Path("."))
    before = {f: getattr(cfg, f) for f in
              ("bitrate_floor_kbps", "tier_over_tolerance", "hevc_factor_hd", "hevc_factor_4k",
               "hevc_factor_8k", "audio_bitrate_stereo", "audio_bitrate_multichannel",
               "mkv_if_tracks_over", "jobs", "audio_policy", "container", "keep_image_subs",
               "ledger_enabled", "tier_bpp", "ignore_under_bytes", "ignore_over_bytes",
               "ignore_exts", "ignore_name_contains")}
    # Feed every key a value that differs from the default and check something moved.
    _apply_advanced(cfg, {
        "floor": 2000, "tol": 25, "hevcHd": 0.5, "hevc4k": 0.4, "hevc8k": 0.35,
        "abStereo": 192, "abMulti": 384, "mkvTracks": 8, "jobs": 3,
        "audio": "aac", "container": "mkv", "imageSubs": False, "ledger": False,
        "bpp": {"OK": 0.09}, "ignUnderMb": 5, "ignOverMb": 50,
        "ignExts": ["avi"], "ignNames": ["sample"],
    })
    after = {f: getattr(cfg, f) for f in before}
    unchanged = [f for f in before if before[f] == after[f]]
    assert not unchanged, f"settings the engine ignored: {unchanged}"
    assert keys - handled_elsewhere, "sanity: the modal sends keys"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tuning/ignore tests passed.")


if __name__ == "__main__":
    _run_all()


def test_encoder_choice_is_in_the_ledger_signature():
    """A file finished in hardware is not finished in software.

    Found the hard way: after a hardware run, re-running the same folder with
    --encoder software skipped every file as 'already done' — exactly defeating
    the reason someone switches to software. AUTO stays unsuffixed so ledgers
    written before this change still match.
    """
    from vtc.config import Encoder
    auto = RunConfig(src=Path("."))
    hard = RunConfig(src=Path("."), encoder=Encoder.HARDWARE)
    soft = RunConfig(src=Path("."), encoder=Encoder.SOFTWARE)
    assert auto.settings_signature() == RunConfig(src=Path(".")).settings_signature()
    assert "enc" not in auto.settings_signature()
    assert hard.settings_signature() != soft.settings_signature()
    assert hard.settings_signature() != auto.settings_signature()
