# very_thoughtful_compression

Selectively repackages and/or re-encodes your videos so that they are a sane size at the quality you want and are as comaptible as you need them to be.

## Why "thoughtful"

The tool never applies one dumb rule (like "half the bitrate") to every file. It adjusts it's settings for every file:

1. **Reads the source video's information density** — not just bitrate, it looks at bitrate, resolution and frame rate → giving bits per pixel per frame.
2. **Compares against an absolute tier target**, based on your quality requirement, it  works out what *this* resolution/fps should cost at your chosen tier and only re-encodes a file that is **more than 10 % over** the density required for the quality you want at the resolution and frame rate you want (`SKIP: already at tier` otherwise). A file already at or under its target is left exactly as it is.
3. **Converges instead of grinding files down.** Because the target is absolute, running the tool twice is safe: once a file has been brought to its tier, a second run sees it's at target and skips it. It never shaves the same file smaller and smaller across runs — which is what normal batch processing generally does. 
4. **Never re-encodes an already-efficient codec** — H.265/AV1/VP9 are only ever remuxed, never transcoded (that just costs a generation of quality). Optimizing these codecs might be added in future .
5.  **Never wastes time** It pre-scans each file, models the expected output size, and skips files that definitely won't meet your space-saving threshold before spending hours encoding them.
5. **Verifies after encoding** — a shrink replaces the source only if the new file exists, is non-empty, and is meaningfully smaller than your minimum-saving threshold; otherwise the original stays and the file is reported.
6. **Remembers what it did.** A resume ledger (`.vtc_processed.log` at the scan root) lets a re-run skip files already handled under the same settings, so a big job you stopped picks up where it left off.
7. **Will never destroy subtitle tracks silently** — it preserves and embeds all subtitles if it can, or it uses sidecar `.srt` files when they cannot be embedded. It can optionally, leave containers like MKV alone.
9. Fully configurable


If you would like to know more details, read on...


## What actually matters: codecs vs containers

Everyone wants the same four things — **quality, small size, fast encoding, wide compatibility** — but you can't max all four at once. Understanding the two independent choices behind every video file makes the trade-offs obvious.

- **The codec** (H.264, H.265, AV1, VP9, Xvid…) does the actual compression. It decides the **file size at a given quality** and **how long encoding takes**. Nearly all the size difference between two files comes from here.
- **The container** (MP4, MKV, WebM, AVI) is just the wrapper holding the video bitstream plus its audio and subtitle tracks. It adds only rounding-error overhead to the size — what it really decides is **compatibility**, **streaming behaviour**, and **which subtitle/audio track types can ride along**.

> **Myth: "MKV is smaller than MP4."** It isn't. The same H.264 video is the same size in either container. MKV files are used because they're can contain high-bitrate video with multiple audio and subtitle tracks in multiple formats. They make distribution easier but they are not as compatible as MP4, especially when they have exotic qualities. Most users only want some of what these fat MKV's contain, so we enable you to choose and move everything to a more compatible container without any quality loss. 


### An unscientific comparison of Containers and Codecs

There are always trade-offs, so you need to decide which is the most important: file size, playback compatibility or the speed at which files can be made. 

So using a scale of 1–10 (higher is better). **Space** = how small at equal quality · **Compat** = how well does it play out-of-the-box across today's phones, TVs and browsers · **Stream** = progressive + adaptive (HLS/DASH) friendliness · **Speed** = encode speed. an Asterisk * means *if your machine has a hardware encoder for that codec*.

To make files of the same perceptual quality this is roughly how containers and codecs perform.

| Era | Container | Codec | Space | Compat | Stream | Speed |
|-----|-----------|-------|:-:|:-:|:-:|:-:|
| **Modern standard** | MP4 | H.264 | 5.5 | 10 | 9.5 | 8 ★ |
| | MKV | H.264 | 5.5 | 7 | 4 | 8 ★ |
| **More Modern** | MP4 | H.265 | 7.5 | 8 | 8 | 5 ★ |
| | MKV | H.265 | 7.5 | 6.5 | 4 | 5 ★ |
| | WebM | VP9 | 7 | 5.5 | 7 | 3 ★ |
| **Next-gen** | MP4 | AV1 | 9 | 5.5 | 7 | 2 ★ |
| | MKV | AV1 | 9 | 5 | 4.5 | 2 ★ |
| **Legacy** | AVI | Xvid / DivX | 3 | 6 | 2 | 8 |
| | MPG | MPEG-2 | 2 | 6.5 | 3 | 9 |

*(Scores are rough calibration for guidance, not precise benchmarks. Space efficiency is a property of the **codec** — it's identical across containers on the same row.)*

Reading it: **H.264** is the "just works everywhere" baseline, but the least space-efficient modern codec. **H.265** roughly halves the size and still plays on most 2015-and-newer hardware. **AV1** is smaller again, but hardware *decoding* is only on very recent devices. **Legacy** codecs (Xvid, MPEG-2) are both bigger *and* older — almost always worth replacing.

### What do you actually want?

Pick what matters for *your* library — the tool can't guess it:

1. **Compatibility** — must it play on anything you own (→ MP4 + H.264), or is a modern-device-only library fine (→ H.265 / AV1)? Planning to **archive or re-edit** the footage? Consider working in **lossless** first (below) and only making a lossy copy for final delivery.
2. **Space saving** — as small as possible · a modest trim · keep the quality and only shave obvious fat · size doesn't matter.
3. **Encode speed** — as fast as possible (→ hardware encoder) · roughly real-time is fine · time is no object (→ software, best quality-per-bit).

### Platform sweet-spots

- **macOS** — MP4 with H.264 or H.265. Almost every Mac has a *hardware* encoder for one or both, so they're fast *and* high quality: choose H.264 for maximum compatibility, or H.265 when size matters more. Hardware AV1 encoding doesn't exist on Apple silicon yet.
- **Windows / Linux** — depends on your GPU. Recent NVIDIA / Intel / AMD chips add hardware **H.265** and often **AV1** (sometimes VP9), shifting the sweet-spot toward those for much smaller files at similar speed. Without a supported GPU, hardware H.264 or software H.265 is the practical choice.

## Quality tiers

A Quality Tier is **information density**, not a fixed bitrate. Density means **bits per pixel per frame** (bpp) — `bitrate ÷ (pixels × frame-rate)` — which is what actually decides how a file *looks*, because a bitrate only means something once you know the resolution and frame rate it's paying for. 8 Mbps is lavish at 720p, fine at 1080p, and starved at 4K; bits/pixel/frame folds all three into one number.

What Quality to Expect
To make it obvious what quality to expect, we compare to Netflix with full HD videos at 30fps, then extrapolate the 'bpp' that implies. Because bpp is normalised for resolution *and* frame rate, that one anchor scales to any file automatically — a 4K clip gets ~4× the 1080p bitrate, a 60 fps clip ~2× the 30 fps bitrate — with no per-resolution rules.

| Tier | 1080p30 H.264 | bpp | …as H.265 @1080p | What it's for |
|------|--------------|-----|------------------|---------------|
| **OK** | 4.0 Mbps | 0.064 | ~2.4 Mbps | Space-first. Fine for phones, tablets and softer/older content; visible softening on detailed 1080p. |
| **GOOD** | 5.0 Mbps | 0.080 | ~3.0 Mbps | Solid streaming quality you see on the web.|
| **EXCELLENT** *(default)* | 6.8 Mbps | 0.109 | ~4.1 Mbps | Matches the top streaming rung you would get wit hNetlix or Amazon - with some headroom for a home encoder. The safe default. |
| **STELLAR** | 8.0 Mbps | 0.129 | ~4.8 Mbps | Above streaming, heading toward Blu-ray-lite — for films or grainy/high-motion material you want kept crisp and good for projectors. |
| **INSANE** | 9.0 Mbps | 0.145 | ~5.4 Mbps | Near-transparent for anything sourced from streaming / WEB-DL. Past this, you may as well keep the original. |

*Mbps shown are H.264 at 1080p30. The tool re-derives the real target for every file from its own resolution and frame rate.*

### What the codec choice changes

The **tier** fixes the quality; the **output codec** fixes how many bits that quality costs.

- **H.265 / HEVC** *(default)* — reaches the same quality as H.264 at roughly **40–55% less bitrate**, the advantage growing with resolution. Plays on essentially all 2015-and-newer hardware. Pick this unless you have a specific reason not to.
- **H.264 / AVC** — the universal baseline that direct-plays on virtually anything, but ~2× larger at the same quality and increasingly wasteful above 1080p. If you are streaming to many people or all sorts of devices, this offers maximum compatibility (all modern equipmen, old TVs, projectors, ancient phones).

EXCELLENT QUALITY: in H.264 it will cost about 6.8 Mbps at 1080p, in H.265 it is only 4.1 Mbps — same picture, half the size.

### How the target is computed
```
target_kbps = tier_bpp × pixels × frame_rate × codec_factor ÷ 1000
```

- **codec_factor** is `1.0` for H.264. For H.265 it reflects HEVC's growing efficiency: **×0.60 ≤1080p, ×0.50 ≤4K, ×0.45 above** (a 40 / 50 / 55 % saving).
- Clamped to a **floor of 1500 kbps**, and **never set above the source** — a file is only ever shrunk, never inflated.
- The target is **absolute** — a function of the tier and the file's own pixels/fps, *not* a fraction of the file's current bitrate. This is what makes repeated runs safe (see [Why "thoughtful"](#why-thoughtful)).

**Sanity anchors:** EXCELLENT @1080p → 6.8 Mbps H.264 / ~4.1 Mbps H.265 (Netflix's 1080p HEVC band); EXCELLENT @4K H.265 → ~13.6 Mbps (≈ Netflix 4K).

**Honesty notes.** "Quality" assumes typical film/TV at 24–30 fps; very grainy or 50/60 fps material may want a tier up. Software (libx264/libx265) uses quality-targeted capped-CRF — slightly better per bit than the hardware VideoToolbox encoder's plain bitrate targeting at the same target, though hardware like VideoToolbox is far faster. Streaming services hit their numbers with per-shot encoders you don't have, so these anchors sit a little above theirs on purpose. The full derivation lives in [`docs/quality-model.md`](docs/quality-model.md).


## What gets encoded

The scan covers `.mkv`, `.mp4`, `.mov`, `.avi`, `.webm`, `.m4v`, `.ts`, `.wmv`, `.flv`, recursively. Each file is sorted by its **video codec** into one of four actions:

| Source codec | Action |
|---|---|
| **H.264** (`h264`/`avc`) | **Shrink** if it's fat and worth it (see below). If it's already efficient but sits in a non-MP4 container, it's **remuxed** to MP4 instead - so you get better compatibilty and no loss of quality. |
| **H.265 / AV1 / VP9** (modern, efficient) | **Never transcoded** — re-encoding these only costs a generation of quality. If in a non-MP4 container they're **remuxed** losslessly into MP4; if already MP4, left alone. |
| **Legacy / MP4-incompatible** (MPEG-2, VC-1, Xvid/DivX, WMV, MS-MPEG4, …) | **Transcoded** to your chosen codec at **maximum fidelity** (quality-targeted CRF capped at the *source's own* bitrate, so quality is preserved rather than squeezed to a tier) and written as MP4. |
| **Mezzanine / other** (ProRes, DNxHD, FFV1, raw, …) | Left untouched. |

The last two behaviours are opt-in, asked once at startup:

- *"If possible, convert files into MP4 for maximum compatibility with NO loss of quality?"* — the lossless **remux** (a fast `-c copy`, no re-encode; also fixes MP4 faststart). Default yes.
- *"If a file uses a codec incompatible with MP4, transcode it with maximum fidelity and convert it to MP4?"* — the legacy **transcode**. Default yes.

The **minimum-saving** gate is a second safety net: even when a file is over target, the re-encode is only *kept* if the output turns out to be smaller by at least your threshold. It's codec-aware — a healthy H.265 re-encode saves 30–45 %, so its default is **25 %**; H.264→H.264 only trims fat, so its default is **15 %**. The gate applies only to a shrink — a remux (lossless) and a compatibility transcode (fidelity-first) are kept regardless of size.

At the end of the run a **space-saved** summary reports the total original size, new size, and bytes/percent saved across every file replaced.

## Streams: video, audio, subtitles

**Video profiles.** H.265 output uses the `main` profile — or `main10` when the source is 10-bit — tagged `hvc1` so Apple players recognise it. H.264 output forces 8-bit `yuv420p` + High profile for maximum player compatibility.

**Audio.** All audio tracks are stream-copied untouched (original codec, channels and quality preserved). Only if the audio codec can't live in an MP4 container does the script fall back to re-encoding to AAC at 384 kbps.

**Subtitles.**

- **Text tracks** (SRT, ASS/SSA, WebVTT — typical in MKVs) are embedded into the MP4 as `mov_text`, all of them.
- If embedding fails, each text track is **extracted to a sidecar `.srt`** next to the output (`Name.eng.srt`, or `Name.1.eng.srt`, `Name.2.ger.srt`… when there are several) — Plex, VLC and Infuse pick these up automatically.
- **Image-based tracks** (PGS from Blu-ray, DVD/DVB bitmaps) can't exist in MP4 and can't become `.srt` without OCR. They are necessarily dropped from the output — **but the original file is then archived instead of deleted, even if you chose "delete originals"** (to `originals/` if you configured an archive folder, else `archived/` in the source root), so nothing is irrecoverably lost.
- Every one of these events is explained in the run report at the end.

## Two ways to run it

The engine is one Python package (`vtc`); everything drives it. There are exactly
two front-ends:

1. **The desktop app** (GUI) — a windowed Mac/Windows app with folder pickers and
   a guided flow. Python, the engine, the interface and a static ffmpeg/ffprobe are
   all bundled, so recipients install nothing.
2. **The CLI** (`vtc`) — the same engine for scripts and servers.

> The original `very_thoughtful_compression.sh` has been retired — the Python CLI
> supersedes it. Its history remains in git.

## Requirements

- The **app** bundles everything — no requirements for end users (see below).
- To run from source you need [`ffmpeg`](https://ffmpeg.org/download.html) and
  `ffprobe` with `libx265` / `libx264` support, and `python3` ≥ 3.11 (the engine and
  CLI are standard-library only; the GUI additionally needs `pywebview`).
- macOS uses VideoToolbox hardware encoding when present; otherwise (and on
  Linux/Windows) it uses `libx265` / `libx264`.

## The desktop app

Grab `VeryThoughtfulCompression-macos.zip` / `-windows.zip` from the project's
Releases, or build them yourself — see [`packaging/BUILD.md`](packaging/BUILD.md)
(macOS builds locally with `packaging/build_gui_app.sh`; both are also produced by
CI on a version tag, with per-platform code-signing/notarization instructions).
Recipient install + first-launch notes live in
[`packaging/APP_README.txt`](packaging/APP_README.txt).

In the window: choose a media folder, answer the questions on the left (codec,
tier, minimum saving, compatibility, encoder, originals), and when every section
is set the finished configuration comes to the centre — click any setting there to
change it, then Start. Nothing is written until you do.

## The CLI

```bash
pip install .           # or: pipx install .   (exposes `vtc` and `vtc-gui`)
vtc [SRC] [options]     # or, from source: python -m vtc.cli [SRC]
```

`SRC` is the directory to scan recursively; omit it (or pass `-i`) for the
interactive prompts. Everything the app asks is a flag:

**Quality** — `--codec {h265,h264}` · `--tier {ok,good,excellent,stellar,insane}` ·
`--min-saving 0.25` (fraction a shrink must save to be kept) · `--bpp 0.12`
(retune the chosen tier's density — see [the quality model](docs/quality-model.md)).
**Ignore rules** — `--ignore-under MB` · `--ignore-over MB` · `--ignore-ext .avi` ·
`--ignore-name sample` (both repeatable). An ignored file is left out of the scan
entirely: it is never probed, counted, estimated or reported.
**Compatibility** — `--no-remux` · `--no-transcode` · `--container {auto,mp4,mkv}` ·
`--audio {passthrough,aac,ac3,flac}` · `--drop-image-subs`.
**Execution** — `--encoder {auto,hardware,software}` · `--jobs N` ·
`--software-file PATH` (repeatable: encode just these files in software even on a
hardware run — the GUI offers this as a tick-list after the scan).
**Destination** — `--output DIR` (mirror the tree) · `--flat` ·
`--originals {archive,delete,keep}` · `--archive-dir DIR`.
**Other** — `--dry-run` (decide + report, encode nothing) · `--no-ledger` /
`--ledger-file` · `--clear-history` (empty the resume ledger first) ·
`--ffmpeg` / `--ffprobe` (override the binaries) · `--version`.

Run `vtc --help` for the full list and defaults.

### Graceful stop

```bash
touch /tmp/hevc_stop   # finish current file(s), skip the rest
rm /tmp/hevc_stop      # clear the stop flag to resume/re-run
```

## Bitrate model (the maths)

```
tier_bpp     = ref_mbps × 1e6 ÷ (1920 × 1080 × 30)          # per-tier constant
codec_factor = 1.0 (H.264) | 0.60 ≤1080p / 0.50 ≤4K / 0.45 >4K (H.265)
target_kbps  = tier_bpp × pixels × frame_rate × codec_factor ÷ 1000
target_kbps  = min(source_kbps, max(BITRATE_FLOOR, target_kbps))   # floored; never inflate
```

A source is re-encoded **only when** `source_kbps > target_kbps × TIER_OVER_TOLERANCE` (default `1.10`); otherwise it's left at tier. The target depends only on the tier and the file's own resolution/frame rate — never on the file's current bitrate — so repeated runs converge instead of shrinking a file again and again.

If the source bitrate cannot be probed, the tier target for that resolution is used as a safe fallback and the file is flagged in the run report. The full rationale, calibration and validation live in [`docs/quality-model.md`](docs/quality-model.md).

## Encoding strategy

Attempts are ordered so a normal file takes exactly one pass: embed text subs + stream-copy audio → then AAC audio fallback → then (only if subtitles were the problem) the same two without embedded subs, followed by sidecar `.srt` extraction.

On macOS, VideoToolbox hardware encoding is used when available. Otherwise — or with `--encoder software` — `libx265` / `libx264` runs at `crf=21` / `crf=20` `preset=medium`, quality-targeted but capped **at** the tier target with a tight `-maxrate` / `-bufsize` (≈1 s). Capping at the target rather than above it is what lets a software re-encode land at tier and be recognised as done on the next run, so repeated runs converge.

Output always includes `-movflags +faststart` so files are immediately streamable.

## Run report

At the end of each run the script prints a consolidated report of anything that needs your attention:

```
══════════════════════════════════════════════════════════
 RUN REPORT — 2 file(s) with notes, warnings or errors
   NOTE  = informational (e.g. why an original was archived)
   WARN  = worth a manual check
   ERROR = file could not be converted; source untouched
══════════════════════════════════════════════════════════
  [NOTE: original archived to /Volumes/NAS/Movies/archived instead of deleted — 2 image-based subtitle track(s) (hdmv_pgs_subtitle) cannot be carried into MP4]
    /Volumes/NAS/Movies/BluRayRip.mkv

  [ERROR: encode failed (both audio-copy and AAC fallback) — file skipped]
    /Volumes/NAS/Movies/Problematic.mp4

══════════════════════════════════════════════════════════
```

| Label | Meaning |
|-------|---------|
| `NOTE` (subtitles) | Subtitle tracks couldn't all be carried over; explains where the original went (archived, kept) or that sidecar `.srt` files were written. |
| `WARN` (bitrate) | Source bitrate could not be probed; the tier target was used. Worth a manual check. |
| `WARN` (output) | Output file missing or empty after the encode — source was not deleted. |
| `ERROR` | All encode strategies failed; the source file was left untouched. |

## License

[MIT](LICENSE) — see the license file for the FFmpeg compatibility note.
