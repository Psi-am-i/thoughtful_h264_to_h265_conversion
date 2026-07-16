# Quality model — how tiers, targets, and re-encode decisions work

This documents the bitrate/quality logic in `very_thoughtful_compression.sh` as of
the 2026-07 rework. It is also the reference spec for the planned Python port.

## The core idea: a tier is a quality *density*, not a bitrate

A bitrate on its own is meaningless without knowing what it pays for — 8 Mbps is
fat at 720p, fine at 1080p, and starved at 4K. So a tier is defined as a **density**:
**bits per pixel per frame (bpp)** = `bitrate ÷ (pixels × fps)`. Because bpp is
normalised by resolution *and* frame rate, one tier scales to any source with no
per-resolution rules: a 4K file gets ~4× a 1080p file, a 60fps file ~2× a 30fps file.

### The four tiers

Each tier is anchored to an **H.264 bitrate at 1080p / 30fps** and converted to a bpp:

| Tier | 1080p30 H.264 | bpp anchor |
|------|---------------|-----------|
| OK | 4.0 Mbps | 0.0643 |
| GOOD | 5.0 Mbps | 0.0804 |
| EXCELLENT (default) | 6.8 Mbps | 0.1093 |
| STELLAR | 8.0 Mbps | 0.1286 |
| INSANE | 9.0 Mbps | 0.1447 |

`bpp = ref_mbps × 1e6 ÷ (1920 × 1080 × 30)`.

EXCELLENT is calibrated so a generic ffmpeg encoder roughly matches Netflix's top
1080p rung (~5.8 Mbps, achieved with far more sophisticated per-shot encoding) —
6.8 Mbps gives the naïve encoder headroom to reach the same look. There is
deliberately **no lossy "archive" tier above INSANE**: true archival quality means
keeping the source lossless, not spending more lossy bitrate.

## The target: absolute, not relative

For a given file the target bitrate is:

```
target_kbps = tier_bpp × pixels × fps × codec_factor ÷ 1000
target_kbps = max(BITRATE_FLOOR, target_kbps)      # floor 1500 kbps
target_kbps = min(target_kbps, source_kbps)        # never inflate a source
```

`codec_factor` is 1.0 for H.264 output. For H.265 it reflects HEVC reaching the same
quality at less bitrate, with the advantage growing at higher resolution (validated
against coding-efficiency studies — theoretical ~50%, practical ~25–40% at HD):

| Output resolution | H.265 factor | ≈ saving vs H.264 |
|---|---|---|
| ≤ 1080p | 0.60 | 40% |
| ≤ 4K | 0.50 | 50% |
| > 4K | 0.45 | 55% |

**Critically, the target is a function of the tier and the source's pixels/fps — NOT
a fraction of the source's current bitrate.** This is what fixed the original bug:
the old logic computed `target = source_bitrate × ratio`, so every re-run re-anchored
to the (now smaller) file and shaved another ~40% off, cutting a file down across
successive runs (3.5 GB → 2.2 GB → 1.2 GB …) until it bottomed out near a bpp floor.

## The re-encode gate: converge, don't re-cut

A source is re-encoded **only if it is more than 10% over its tier target**
(`TIER_OVER_TOLERANCE = 1.10`):

```
if source_kbps <= target_kbps × 1.10:  skip  ("already at/under <TIER> target")
else:                                  encode to the target
```

Because a first-pass encode lands at or under the target, the next run sees the file
as at-tier and leaves it alone — the process **converges after one encode** instead
of nibbling forever. Already-efficient sources (below target) are simply left alone;
there is no separate "bpp skip floor" any more — the tier target *is* the floor.

H.265/AV1/VP9 sources are classified `modern` and never transcoded (that would only
add a generation of loss); they are only remuxed losslessly into MP4 if asked.

## Encoders and what actually controls quality

- **Software (libx264/libx265)** — capped-CRF: `-crf 20/21 -maxrate <target> -bufsize`.
  CRF is a constant-*quality* target; the tier bitrate is a ceiling on peaks. This is
  the quality path.
- **Hardware (VideoToolbox)** — `-b:v <target>` only (no true CRF). Here the tier
  bitrate *is* the quality knob, which is why the bpp calibration matters most on this
  path.

`MIN_SAVING_RATIO` is a separate, post-encode guard: a shrink is only kept if the
output is actually ≥ N% smaller than the source.

## Resume ledger

Every file that reaches a terminal (non-error) decision is recorded in
`.vtc_processed.log` at the scan root, as:

```
<settings-signature>\t<abspath>\t<size>\t<mtime>
```

The signature is `TIER|CODEC|rmx?|xc?|outputmode`. On a re-run:

- **Same settings** → recorded files are skipped without re-probing (fast resume,
  e.g. after `touch /tmp/hevc_stop` stops a run mid-way).
- **Changed settings** (different tier/codec/options) → signature differs, so
  everything is re-evaluated.

Correctness does not depend on the ledger — the absolute-target gate already prevents
re-cutting. The ledger is a resume/speed optimisation. Disable with `LEDGER=0`;
relocate with `LEDGER_FILE=...`.

## Knobs (env overrides)

| Var | Default | Meaning |
|---|---|---|
| `TIER_OVER_TOLERANCE` | 1.10 | re-encode only if source is >10% over target |
| `BITRATE_FLOOR` | 1500 | never target below this (kbps) |
| `HEVC_EFFICIENCY_HD/4K/8K` | 0.60 / 0.50 / 0.45 | H.265 bitrate vs H.264 at same quality |
| `LEDGER` / `LEDGER_FILE` | 1 / `<scan>/.vtc_processed.log` | resume ledger toggle / path |
| `FORCE_VT` | (auto) | 1 = force hardware, 0 = force software |
