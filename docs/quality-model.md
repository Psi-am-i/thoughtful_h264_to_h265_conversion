# Quality model — how tiers, targets, and re-encode decisions work

This documents the bitrate/quality logic now implemented in the `vtc` Python
engine (`vtc/model.py`, `vtc/pipeline.py`, `vtc/encode.py`) — the reference spec
that the retired `very_thoughtful_compression.sh` originally established.

## The core idea: a tier is a quality *density*, not a bitrate

A bitrate on its own is meaningless without knowing what it pays for — 8 Mbps is
fat at 720p, fine at 1080p, and starved at 4K. So a tier is defined as a **density**:
**bits per pixel per frame (bpp)** = `bitrate ÷ (pixels × fps)`. Because bpp is
normalised by resolution *and* frame rate, one tier scales to any source with no
per-resolution rules: a 4K file gets ~4× a 1080p file, a 60fps file ~2× a 30fps file.

### Why bpp is not the same number in every codec

The obvious objection: a decoded frame is a decoded frame. H.264 and H.265 both
hand back 1920×1080 pixels, the same raw bytes. Nothing is smaller after
decoding — so how can the same quality cost different bits?

Because bpp does not measure what the frame *holds*. It measures **how many bits
we had to spend describing it** well enough to rebuild. Both codecs produce a
full frame; neither produces the *same* frame. Each is an approximation of the
original, and the bits decide how close.

> Two people describe the same painting down a phone line. Both listeners end up
> with a canvas the same size. The better describer gets a closer likeness in
> fewer words. **Canvas size is the resolution — fixed. Word count is the
> bitrate. Likeness is the quality. The skill of the describer is the codec.**

So 0.077 bpp of H.265 and 0.129 bpp of H.264 look the same to you: different
bits, same likeness. H.265 spends them better — smarter prediction, variable
block sizes, better entropy coding.

This is why a tier cannot simply *be* a bpp. A tier is a **fidelity**; bpp is
what that fidelity costs in a particular codec.

### The quality number, and how it resolves

Each tier is a **quality number** — the H.264 bpp × 1000. It is codec-independent
and does not move when you change anything else; it names the likeness you want.

| Tier | Quality | H.264 bpp | 1080p30 H.264 |
|------|---------|-----------|---------------|
| OK | 64 | 0.0643 | 4.0 Mbps |
| GOOD | 80 | 0.0804 | 5.0 Mbps |
| EXCELLENT (default) | 109 | 0.1093 | 6.8 Mbps |
| STELLAR | 129 | 0.1286 | 8.0 Mbps |
| INSANE | 145 | 0.1447 | 9.0 Mbps |

From there, two steps and nothing else:

```
    codec bpp = quality ÷ 1000 × codec factor
    bitrate   = codec bpp × pixels × fps
```

The **codec factor** is what that codec's skill is worth. H.264 is the reference,
so its factor is 1.0. H.265 needs fewer bits for the same likeness, and its
advantage grows with frame size — more neighbouring pixels to predict from:

| Output codec | ≤1080p | ≤4K | above 4K |
|---|---|---|---|
| H.264 | 1.00 | 1.00 | 1.00 |
| H.265 | 0.60 | 0.50 | 0.45 |

So STELLAR (quality 129) resolves to:

| | bpp | 1080p24 | 4K24 |
|---|---|---|---|
| H.264 | 0.1286 | 6.4 Mbps | 25.6 Mbps |
| H.265 at ≤1080p | 0.0772 | 3.8 Mbps | — |
| H.265 at 4K | 0.0643 | — | 12.8 Mbps |

One quality number, one factor per codec and frame size, and the bitrate falls
out. Nothing else is tuned per resolution.

`H.264 bpp = ref_mbps × 1e6 ÷ (1920 × 1080 × 30)`, and `quality = bpp × 1000`.

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

The signature is `TIER|CODEC|rmx?|xc?|outputmode`, with `|bppN` appended when the
tier has been retuned (only then, so history written before per-tier bpp existed
still matches). On a re-run:

- **Same settings** → recorded files are skipped without re-probing (fast resume,
  e.g. after `touch /tmp/hevc_stop` stops a run mid-way).
- **Changed settings** (different tier/codec/options) → signature differs, so
  everything is re-evaluated.

Correctness does not depend on the ledger — the absolute-target gate already prevents
re-cutting. The ledger is a resume/speed optimisation. Disable with `LEDGER=0`;
relocate with `LEDGER_FILE=...`.

## Knobs

Model constants live in `vtc/model.py` and per-run settings in `vtc/config.py`
(`RunConfig`); the CLI exposes them as flags.

| Constant / setting | Default | Meaning | CLI |
|---|---|---|---|
| `tier_bpp` | the tier's own anchor | per-tier density override (Advanced settings → Quality tiers) | `--bpp` |
| `TIER_OVER_TOLERANCE` | 1.10 | re-encode only if source is >10% over target | — |
| `BITRATE_FLOOR_KBPS` | 1500 | never target below this (kbps) | — |
| `HEVC_FACTOR_HD/4K/8K` | 0.60 / 0.50 / 0.45 | H.265 bitrate vs H.264 at same quality | — |
| ignore rules | none | size / extension / filename rules that remove files from the scan | `--ignore-under/-over/-ext/-name` |
| ledger enabled / file | on / `<scan>/.vtc_processed.log` | resume ledger toggle / path | `--no-ledger` / `--ledger-file` |
| encoder backend | auto | hardware (VideoToolbox) vs software | `--encoder {auto,hardware,software}` |
