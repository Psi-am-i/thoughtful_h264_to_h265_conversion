"""Model tests — reproduce the numbers validated against the bash implementation.

Runnable two ways:  pytest tests/   |   python tests/test_model.py
"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc.model import (  # noqa: E402
    CodecCategory,
    OutCodec,
    Tier,
    classify_codec,
    over_target,
    source_bpp,
    target_kbps,
)

_1080P = (1920, 1080)
_4K = (3840, 2160)


def _approx(a: float, b: float, tol: float = 1e-3) -> bool:
    return math.isclose(a, b, rel_tol=0, abs_tol=tol)


def test_tier_bpp_anchors():
    assert _approx(Tier.OK.bpp, 0.0965)
    assert _approx(Tier.GOOD.bpp, 0.1286)
    assert _approx(Tier.EXCELLENT.bpp, 0.1688)
    assert _approx(Tier.STELLAR.bpp, 0.2090)
    assert _approx(Tier.INSANE.bpp, 0.2492)


def test_classify_codec_matches_bash():
    assert classify_codec("h264") is CodecCategory.H264
    assert classify_codec("AVC") is CodecCategory.H264
    assert classify_codec("hevc") is CodecCategory.MODERN
    assert classify_codec("av1") is CodecCategory.MODERN
    assert classify_codec("vp9") is CodecCategory.MODERN
    assert classify_codec("mpeg2video") is CodecCategory.LEGACY
    assert classify_codec("mpeg4") is CodecCategory.LEGACY  # Xvid/DivX
    assert classify_codec("wmv3") is CodecCategory.LEGACY
    assert classify_codec("prores") is CodecCategory.OTHER
    assert classify_codec(None) is CodecCategory.OTHER


def test_targets_1080p30():
    px = _1080P[0] * _1080P[1]
    assert target_kbps(Tier.EXCELLENT, px, 30, OutCodec.H264) == 10500
    assert target_kbps(Tier.GOOD, px, 30, OutCodec.H264) == 8000
    assert target_kbps(Tier.INSANE, px, 30, OutCodec.H264) == 15499
    # H.265 @1080p uses the 0.60 HD factor -> 6300k
    assert target_kbps(Tier.EXCELLENT, px, 30, OutCodec.H265) == 6300


def test_targets_scale_with_resolution_and_codec():
    px4k = _4K[0] * _4K[1]
    # 4K EXCELLENT H.265 via the 0.50 factor
    assert target_kbps(Tier.EXCELLENT, px4k, 30, OutCodec.H265) == 21000
    # Same tier, H.264, 4K, 24fps (bpp * pixels * fps)
    assert target_kbps(Tier.EXCELLENT, px4k, 24, OutCodec.H264) == 33600


def test_convergence_gate():
    px = _1080P[0] * _1080P[1]
    tgt = target_kbps(Tier.EXCELLENT, px, 30, OutCodec.H264)  # 10500
    # The 3.5->2.2->1.2 staircase must NOT happen: only a source well over target encodes.
    assert over_target(14000, tgt) is True       # fat original -> encode once
    assert over_target(10500, tgt) is False      # at target -> leave alone
    assert over_target(9000, tgt) is False        # yesterday's mid-state -> leave alone
    assert over_target(3200, tgt) is False        # already lean -> leave alone
    # Just over the 10% tolerance boundary:
    assert over_target(int(10500 * 1.10) + 5, tgt) is True
    assert over_target(int(10500 * 1.10) - 5, tgt) is False


def test_never_inflate_source():
    px = _1080P[0] * _1080P[1]
    # A lean source: encode target is clamped to the source, never above it.
    assert target_kbps(Tier.EXCELLENT, px, 30, OutCodec.H264, src_kbps=5000) == 5000


def test_floor():
    # A tiny frame would compute below the floor; it is clamped up.
    assert target_kbps(Tier.OK, 160 * 120, 24, OutCodec.H265) == 1500


def test_source_bpp():
    px = _1080P[0] * _1080P[1]
    # 12.5 Mbps 1080p30 -> ~0.20 bpp (the fat test clip)
    assert _approx(source_bpp(12_500_000, px, 30), 0.2011, tol=1e-3)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} model tests passed.")


if __name__ == "__main__":
    _run_all()
