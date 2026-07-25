# Very Thoughtful Compression — Copy Deck

> Every user-facing string in the app, grouped by screen. **Edit the text here and send it back** — I'll apply the changes.
> Keep the `code` anchors (e.g. `q:codec`, `codec.1`) so edits map to the exact spot. Lines starting `—` are notes to me, not app text.

---

## 1 · Splash & gate (first screen)
- Wordmark eyebrow: **Very Thoughtful**
- Wordmark: **Compression**
- Button: **Choose media folder**
- Recent list header: **Recent**  — _(real app: goes straight to the folder picker)_
- Tagline: **Everything you didn't know you needed**

## 2 · Header bar (always visible)
- Left / encoder readout: **Encoder** · e.g. `ffmpeg 7.1 · VideoToolbox ready`  — _(auto-filled at runtime)_
- Source control label: **SOURCE** · then the chosen folder path

## 3 · The questions (left panel — one per step)

### 3.1 CODEC  `q:codec`
- **Title:** What video codec should I use?
- **Subtitle:** The only choice that meaningfully changes file size. Every codec trades off size, quality and speed — older ones are worse at all three. For a given quality the codec doesn't change how it looks, only how big the file is and how long it takes to make. If your machine has a hardware encoder for a codec, it will be considerably faster than choosing a codec without hardware encoding. 
- **Options:**
  - `codec.0` **H.264 / AVC**
    - Tag: Compatibility
    - Description: The universal baseline — direct-plays on virtually anything since 2003 — but about twice the size for the same quality as H.265/AV1, and increasingly wasteful above 1080p. Choose it for maximum compatibility, or for great performance on old devices.
  - `codec.1` **H.265 / HEVC**
    - Tag: Efficiency · suggested
    - Description: A truly modern codec with wide — not universal — support. Same quality as H.264 in roughly 40–55% less space, and the advantage grows with resolution: up to ~60% smaller at 4K and above. Plays on most devices since 2015 — Apple, recent TVs, Plex, VLC, Infuse. The big exceptions are some SmartTV's and Firefox Browser. For most this is a good middle ground - unless you have a specific reason not to, like running a media server and you want video as widely as possible. 
  - `codec.2` **AV1** _(coming soon)_
    - Tag: Next-gen · coming later
    - Description: Likely the future — files 30–50% smaller than H.265 at equal quality, royalty-free and backed by the Alliance for Open Media (Apple, Google, Microsoft, Netflix). BUT software encoding is glacial and hardware decoders are only starting to roll out (Apple plays it from the M3 on), and a five-year-old TV probably won't play it at all. Genuinely good; genuinely early.
  - `codec.3` **H.266 / VVC** _(coming soon)_
    - Tag: Bleeding-edge · coming later
    - Description: In objective tests VVC is slightly more efficient than AV1 at high resolutions (4K/UHD), but AV1 has a big head start on support and tooling — and almost no consumer devices decode VVC yet.

### 3.2 QUALITY  `q:quality`
- **Title:** What quality are you looking for?
- **Subtitle:** Quality is part objective — how much information is in each frame (bits per pixel per frame) — and part subjective: what you think that looks like. Our baseline is the perceptual quality of a Netflix TV show. We call that quality Excellent and that is what you will get (assuming your source is that good or better). Resolution doesn't matter: we set the right bits per pixel per frame, which decides the bitrate for any resolution + quality automatically.
- **Options:**
  - `quality.0` **OK**
    - Tag: 0.064 bpp · 4.0 Mbps at 1080p30
    - Recap label (Start modal): OK · 0.064 bpp
    - Description: Uses little space. Fine for phones, tablets and older or low-quality material. On a big 1080p TV you will see it soften.
  - `quality.1` **GOOD**
    - Tag: 0.080 bpp · 5.0 Mbps at 1080p30
    - Recap label (Start modal): Good · 0.080 bpp
    - Description: Solid streaming quality — what most websites deliver, and a little under Netflix and Amazon. On a laptop or smaller TV it is usually more than enough.
  - `quality.2` **EXCELLENT**
    - Tag: 0.109 bpp · 6.8 Mbps at 1080p30 · default
    - Recap label (Start modal): Excellent · 0.109 bpp
    - Description: Matches Netflix's top streaming quality. They manage 5.8 Mbps with per-shot encoding you do not have, so we bump our number deliberately above theirs. For almost every library, this is the answer. For excellent sources you want to maintain — or if you use a projector — consider Stellar.
  - `quality.3` **STELLAR**
    - Tag: 0.129 bpp · 8.0 Mbps at 1080p30
    - Recap label (Start modal): Stellar · 0.129 bpp
    - Description: If your source is great, this beats streaming from the big players — heading into Blu-ray territory. For high-quality films, grainy or high-motion footage, or a projector, choose this.
  - `quality.4` **INSANE**
    - Tag: 0.145 bpp · 9.0 Mbps at 1080p30
    - Recap label (Start modal): Insane · 0.145 bpp
    - Description: Near-transparent from the original on most devices. Past this you may as well keep the original or use a lossless format — archival means lossless, not more bits.

### 3.3 SAVING  `q:saving`
- **Title:** How much smaller must a file get to be worth it?
- **Subtitle:** Your call — the tool has no view on it. We predict the file size of each file according to your quality and codec choice BEFORE encoding starts. A file that won't clear your savings expectation is skipped rather than discovered an hour later. We also check the final file to ensure original files are only replaced if it's worth it. If we are just doing a lossless conversion, those files will always be the same size. 
- **Options:**
  - `saving.0` **15%**
    - Tag: Trim the obvious fat
    - Recap label (Start modal): Must be 15% smaller — or else keep the original
    - Description: The sensible pick if you chose H.264, where a re-encode only shaves headroom off a file rather than changing how it compresses — 15% is a real win there. Expect a lot of files to qualify.
  - `saving.1` **25%**
    - Tag: The healthy-encode bar
    - Recap label (Start modal): Must be 25% smaller — or else keep the original
    - Description: The sensible pick if you chose H.265 or AV1: a healthy re-encode there saves 35–50%, so anything predicting under 25% was already pretty efficient — and re-encoding it spends a generation of quality for almost nothing.
  - `saving.2` **40%**
    - Tag: Only the truly bloated
    - Recap label (Start modal): Must be 40% smaller — or else keep the original
    - Description: Strict. Touches only the worst offenders — the 20 GB rips with generous headroom. Most of the library goes untouched, which may be exactly what you want.

### 3.4 COMPATIBILITY  `q:compat`
- **Title:** What about files that aren't MP4?
- **Subtitle:** Two separate behaviours, both opt-in. Neither is a quality decision — they are about whether the file plays.
- **Options:**
  - `compat.0` **Remux + transcode**
    - Tag: Both · default
    - Recap label (Start modal): Remux to MP4, and transcode legacy codecs
    - Description: MP4-friendly codecs sitting in other containers get losslessly rehomed into MP4 — a stream copy, no re-encode, seconds not hours, and it fixes faststart on the way through. Legacy codecs from old files that MP4 cannot hold (MPEG-2, VC-1, Xvid, WMV) get re-encoded at maximum fidelity, capped at the source's own bitrate rather than at your tier target — fidelity first, because the point is to rescue the file, not to shrink it.
  - `compat.1` **Remux only**
    - Tag: No re-encoding
    - Recap label (Start modal): Remux to MP4 · leave legacy codecs alone
    - Description: Rehome what can be rehomed for free. Leave legacy codecs exactly where they are — bigger and older, but untouched.
  - `compat.2` **Neither**
    - Tag: Leave containers alone
    - Recap label (Start modal): Leave every container as it is
    - Description: Non-MP4 files stay as they are. Nothing gets rewrapped. Only the shrink logic runs on MP4 files.

### 3.5 ENCODER  `q:encoder`
- **Title:** Hardware or software encoder?
- **Subtitle:** A working {something-encoder} encoder was detected on this machine, so you get the choice. Without one, this question doesn't appear.
- **Options:**
  - `encoder.0` **Hardware**
    - Tag: {hardware-encoder} · default
    - Description: Many times faster, using a fixed-function block on the chip. It targets an average bitrate rather than a quality level, so here the tier target IS the quality knob — which is exactly why the bpp calibration matters most on this path. Slightly blunter than software at the same target; the difference is small and the time saved is not.
  - `encoder.1` **Software**
    - Tag: {something-encoder} / {something-encoder}
    - Description: Capped CRF: crf=21 for H.265, crf=20 for H.264, preset medium. CRF is a constant-quality target and the tier bitrate becomes a ceiling on peaks via maxrate and bufsize — quality first, with the target as a limit rather than a goal. Better per bit. Considerably slower. Correct if time is genuinely no object.

### 3.6 DESTINATION  `q:dest`
- **Title:** What happens to the originals?
- **Subtitle:** The last decision, and the only irreversible one. Verification runs first regardless — a source is never replaced unless the new file exists, is non-empty, and is meaningfully smaller.
- **Options:**
  - `dest.0` **Replace · archive**
    - Tag: Keeps libraries tidy · default
    - Recap label (Start modal): Replace in place · originals archived
    - Description: The new file takes the original's exact name and location, so Plex and Jellyfin libraries don't notice a thing. Each original is moved to an archive folder (at the source root by default, or one you choose) — you decide if and when to trash originals.
  - `dest.1` **Replace · delete**
    - Tag: Most space, no undo
    - Recap label (Start modal): Replace in place · originals deleted
    - Description: The new file takes the original's name and location; the original is deleted once verification passes. Maximum space saved immediately, no undo. The run report still tells you exactly what happened to every file.
  - `dest.2` **New folder**
    - Tag: Non-destructive
    - Recap label (Start modal): New files to a folder · originals untouched
    - Description: New files are written to a folder you choose — flat, or mirroring the source tree. Nothing is removed or moved: your originals stay exactly where they are. Uses the most disk during the run, and lets you A/B at leisure.

- **Meter labels (below the options):** Space · Compat · Stream · Speed
- Commit button: **Confirm** (becomes **Update** when changing a set answer) · hint **Nothing armed** / **Armed: X**

## 4 · Right rail (config + estimate)
- Header: **Settled** · `N / 6`
- Before/after: **Now** (TB, file count) → **Projected** (TB, `−X% · Y TB back`)
- Under estimate: **Nothing is written until you press Start.** / **N re-encoded · M already at tier, left alone. Modelled/Measured.**
- Run button: **Start** (or **Start · N of 6 set** until complete)

## 5 · Start modal (the finished configuration)
- Eyebrow: **Your configuration · ready to run**
- Title: **Run these settings on `<folder>`?**
- Hint: **Click any setting above to change it. Nothing is written until you press Start.**
- Summary tiles: **Source** · **Projected** · **Originals**
- Warning (delete): **Originals are deleted** after each file verifies — the new file must exist, be non-empty, and be smaller. A file that fails verification keeps its original. There is no undo.
- Warning (archive): Originals move to an archive folder beside the output. Nothing is deleted; you can clear the archive yourself once you have watched a few.
- Warning (keep both): Both copies are kept. This uses more disk, not less, until you remove the originals yourself.
- Buttons: **Go back** · **Yes, start**

## 6 · Progress screen (during a run)
- Eyebrow: **Processing**
- Title: **Working…** _(animated)_
- Clock label: **estimated time remaining**
- Counts: `N / M files` · `X%`
- Current file line: filename · then `X% · NNN fps · N.N Mbps · N.N×`
- Button: **Stop after current file**

## 7 · Report screen (after a run)
- Eyebrow: **Run complete · Xh YYm elapsed**
- Title: **X.XX TB recovered** (+ **· N files need a look** if any)
- Stat tiles: **Thoughtfully processed** · **Left alone** · **Needs a look** · **Recovered**
- Tabs: **Success** · **Left alone** · **Needs a look**
- Row tags: **done** · **left alone** · **note** / **warn** / **error**
- Empty state: **Nothing here** (+ **— which is the good outcome** on the Needs-a-look tab)
- Buttons: **Save log…** · **Start over** · **New folder · same settings**

## 8 · Previews deck (bottom)
- Tab: **Previews**
- Note: **Your footage at each tier — drag any panel to pan the crop, scrub to compare a moment, full screen for more pixels.**
- Placeholder (no folder yet): **Choose a folder — your footage previews here at each quality tier.**
- Panel labels: **Source · OK · Good · Excellent · Stellar · Insane** (each shows `codec · size · +% vs smallest`)
- Toolbar: **sample @** `<position>` · codec toggle **H.265 / H.264** · **Refresh previews** · **Full screen**
- Fullscreen header: **Drag a panel to pan · scrub to compare a moment · every panel is the same 1:1 crop** / **Your footage at each tier**
- Transport: play · prev/next frame · scrub · `0.00s / 5.00s` · `1× ½× ¼×` · **Close**

## 9 · "How it decides" tab (left column, prose)
- Batch tools apply one setting to every file. A library isn't uniform — some files were encoded with generous headroom, others were squeezed dry years ago. Re-encoding the second kind buys almost nothing and costs a generation of quality.
- So each file is measured first: bits per pixel per frame, derived from its real bitrate, resolution and frame rate. Your tier is a bpp too — a density, not a bitrate — so the target for this file is simply tier bpp × its pixels × its frame rate × the codec factor. A 4K file gets about four times a 1080p one, a 60fps file twice a 30fps one, with no per-resolution rules anywhere.
- The target is absolute: a function of the tier and the file's own pixels and fps, never a fraction of what the file currently weighs. That distinction is the whole design. Compute a target as "70% of the current bitrate" and every re-run re-anchors to the file it just shrank — 3.5 GB, then 2.2, then 1.2 — grinding the same file down forever. An absolute target converges: one encode lands at the tier, and every run after that sees a file already at tier and leaves it alone.
- So a file is only re-encoded if it is more than 10% over its target. At or under, it is left exactly as it is — judged by density, not by how big the file looks. H.265, AV1 and VP9 sources are never transcoded at all; re-encoding them only spends a generation of quality. Targets are floored at 1500 kbps and never set above the source, so a file is only ever shrunk, never inflated.
- Before encoding, the expected output is modelled: if it won't clear your saving threshold, the encode never starts. Afterwards the source is replaced only if the new file exists, is non-empty, and actually beat that threshold. Otherwise the original stays and says so in the run report.
- Myth: MKV is smaller than MP4. It isn't. The same H.264 video is the same size in either container. MKV files are often big because they're used for high-bitrate rips — not because of the wrapper. The container decides compatibility. The codec decides size.
- Subtitles are never silently destroyed. Text tracks (SRT, ASS/SSA, WebVTT) are embedded into the MP4 as mov_text, all of them. If embedding fails they are extracted to sidecar .srt files beside the output, which Plex, VLC and Infuse pick up automatically. Image-based tracks — PGS from Blu-ray, DVD bitmaps — cannot exist in an MP4 and cannot become .srt without OCR, so they have to be dropped. When that happens the original is archived instead of deleted, even if you chose delete. Nothing is irrecoverable, and every case is named in the run report.
- A stopped run picks up where it left off. Every file that reaches a decision is recorded in a resume ledger at the scan root, tagged with a signature of the settings that produced it. Re-run with the same settings and those files are skipped without re-probing. Change any setting and the signature changes, so everything is re-evaluated. Correctness never depends on it — the absolute target already prevents re-cutting — so the ledger is only ever a speed optimisation.
- Honesty note. Quality assumes typical film and TV at 24–30 fps; very grainy or 50/60 fps material may want a tier up. Software (libx264/libx265) uses quality-targeted capped-CRF and is slightly better per bit than hardware VideoToolbox at the same target — hardware is simply much faster. Streaming services hit their numbers with per-shot encoders you don't have, so these anchors sit above theirs on purpose.

## 10 · Reference tables (right column of "How it decides")
- **Quality tiers** table: Tier / bpp / H.264 / H.265 / What it's for (OK, Good, Excellent, Stellar, Insane)
- **Codecs at the same quality** table: Codec / Space / Compat / Stream / Speed (H.264, H.265, VP9, AV1, Xvid, MPEG-2)
- _(These are data tables — tell me any number/label to change.)_

## 11 · About tab
- Wordmark + **by Picnic Labs**
- Blurb: A codec-aware re-encoder for large video libraries… _(full text in the app)_
- **Version 0.1.0 · Apple Silicon**
- Right column: **Licenses & credits** — FFmpeg (GPL v3), hardware encoding, your files / no telemetry.

