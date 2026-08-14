"""The bitrate / quality model — the heart of the tool.

A tier is a fixed quality *density* (bits per pixel per frame), anchored to an
H.264 bitrate at 1080p/30fps. Targets scale with a file's actual pixels and frame
rate, so one tier covers SD -> 8K at any fps. The target is ABSOLUTE (a function
of the tier and the source's pixels/fps), never a fraction of the source's
current bitrate — which is what makes re-runs converge instead of shaving a file
smaller every pass.

See docs/quality-model.md for the full rationale. This module is pure (no I/O),
so it is trivially testable and shared by the CLI and the GUI.
"""

from __future__ import annotations

from enum import Enum

# ── Reference anchor: every tier's bpp is derived from H.264 @ 1080p / 30 fps ──
_REF_PIXELS = 1920 * 1080
_REF_FPS = 30
_REF_PIXEL_FPS = _REF_PIXELS * _REF_FPS

_PIXELS_1080P = 1920 * 1080
_PIXELS_4K = 3840 * 2160

# ── Tunables (mirror the bash constants; overridable by the caller) ───────────
BITRATE_FLOOR_KBPS = 1500          # never target below this (avoids garbage output)
TIER_OVER_TOLERANCE = 1.10         # re-encode only a source that is >10% over target

# H.265 bitrate vs H.264 at equal quality, by OUTPUT resolution. The HEVC
# advantage grows with frame size (validated against coding-efficiency studies:
# theoretical ~50%, practical ~25-40% at HD; 0.60 at HD hands H.265 more bitrate
# than the theoretical 50% to protect quality on a generic encoder).
HEVC_FACTOR_HD = 0.60              # <= 1080p  (40% saving)
HEVC_FACTOR_4K = 0.50              # <= 4K     (50% saving)
HEVC_FACTOR_8K = 0.45              # above 4K  (55% saving)


class OutCodec(str, Enum):
    H264 = "h264"
    H265 = "h265"


class CodecCategory(str, Enum):
    H264 = "h264"        # core target: shrink if fat, else remux/leave
    MODERN = "modern"    # HEVC/AV1/VP9 — never transcoded, only remuxed into MP4
    LEGACY = "legacy"    # MP4-incompatible (MPEG-2/VC-1/Xvid/WMV) — transcode opt-in
    OTHER = "other"      # mezzanine/unknown (ProRes/DNxHD/FFV1/raw) — left untouched


class Tier(Enum):
    """Quality tiers, anchored to an H.264 bitrate (Mbps) at 1080p / 30fps."""

    # Raised so INSANE is essentially transparent (visually indistinguishable from
    # source on demanding footage) and the rest cascade down from it.
    OK = ("OK", 6.0)
    GOOD = ("GOOD", 8.0)
    EXCELLENT = ("EXCELLENT", 10.5)
    STELLAR = ("STELLAR", 13.0)
    INSANE = ("INSANE", 15.5)

    def __init__(self, label: str, ref_mbps: float) -> None:
        self.label = label
        self.ref_mbps = ref_mbps

    @property
    def bpp(self) -> float:
        """H.264 bits-per-pixel-per-frame implied by the 1080p30 anchor."""
        return self.ref_mbps * 1e6 / _REF_PIXEL_FPS

    @classmethod
    def from_name(cls, name: str) -> "Tier":
        try:
            return cls[name.strip().upper()]
        except KeyError:
            names = ", ".join(t.name for t in cls)
            raise ValueError(f"unknown tier {name!r} (choose from: {names})") from None


# Codec-name -> category. Mirrors classify_codec() in the bash script exactly.
_H264_CODECS = {"h264", "avc"}
_MODERN_CODECS = {"hevc", "av1", "vp9"}
_LEGACY_CODECS = {
    "mpeg2video", "mpeg4", "msmpeg4v1", "msmpeg4v2", "msmpeg4v3", "msmpeg4",
    "vc1", "wmv1", "wmv2", "wmv3", "flv1", "rv30", "rv40",
}


def classify_codec(codec_name: str | None) -> CodecCategory:
    """Sort a probed video codec name into a handling category."""
    c = (codec_name or "").strip().lower()
    if c in _H264_CODECS:
        return CodecCategory.H264
    if c in _MODERN_CODECS:
        return CodecCategory.MODERN
    if c in _LEGACY_CODECS:
        return CodecCategory.LEGACY
    return CodecCategory.OTHER


def hevc_factor(pixels: int, factors: tuple[float, float, float] | None = None) -> float:
    """H.265 efficiency factor for an output frame of `pixels` (w*h).

    `factors` overrides the (HD, 4K, 8K+) defaults — how the Advanced settings'
    three HEVC-factor boxes reach the arithmetic.
    """
    hd, uhd4k, uhd8k = factors or (HEVC_FACTOR_HD, HEVC_FACTOR_4K, HEVC_FACTOR_8K)
    if pixels <= _PIXELS_1080P:
        return hd
    if pixels <= _PIXELS_4K:
        return uhd4k
    return uhd8k


def codec_factor(out_codec: OutCodec, pixels: int,
                 hevc: tuple[float, float, float] | None = None) -> float:
    """Multiplier applied to the H.264 target for the chosen output codec."""
    return hevc_factor(pixels, hevc) if out_codec == OutCodec.H265 else 1.0


def source_bpp(src_bps: float, pixels: int, fps: float) -> float:
    """Bits per pixel per frame of a source (the density metric)."""
    if pixels <= 0 or fps <= 0:
        return 0.0
    return src_bps / (pixels * fps)


def target_kbps(
    tier: Tier,
    pixels: int,
    fps: float,
    out_codec: OutCodec,
    src_kbps: float | None = None,
    floor_kbps: int = BITRATE_FLOOR_KBPS,
    bpp: float | None = None,
    hevc: tuple[float, float, float] | None = None,
) -> int:
    """Absolute target bitrate (kbps) for a file at this resolution/fps/codec.

    target = tier_bpp * pixels * fps * codec_factor, clamped to the floor and
    never above the source (we don't inflate). Returns an integer kbps.

    `bpp` overrides the tier's own density — that is how a user-edited tier (see
    RunConfig.tier_bpp) reaches the arithmetic without mutating the shared enum.
    """
    fps = fps if fps > 0 else float(_REF_FPS)
    density = tier.bpp if bpp is None else bpp
    raw = density * pixels * fps * codec_factor(out_codec, pixels, hevc) / 1000.0
    target = max(float(floor_kbps), raw)
    if src_kbps is not None and src_kbps > 0:
        target = min(target, src_kbps)
    return int(target)


def over_target(src_kbps: float, target_kbps_: int, tolerance: float = TIER_OVER_TOLERANCE) -> bool:
    """True if the source is far enough over target to be worth re-encoding.

    This is the convergence gate: a file at or within `tolerance` of its tier
    target is left alone, so re-runs don't keep shaving it down.
    """
    if target_kbps_ <= 0:
        return False
    return src_kbps > target_kbps_ * tolerance
