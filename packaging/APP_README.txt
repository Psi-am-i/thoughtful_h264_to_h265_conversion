# Very Thoughtful Compression 1.2 — by Picnic Labs

WINDOWS: EXPERIMENTAL
---------------------
The macOS (Apple Silicon) build is the supported one. The Windows build is
produced by CI but is NOT yet verified end to end — treat it as buggy and
testers are very welcome.

What is most likely to be wrong on Windows: the quality previews (WebView2 has
no HEVC decoder, so they fall back to H.264, and the full-screen comparison has
never been seen there), and hardware encoding via NVENC / QuickSync / AMF, which
is implemented and probed at runtime but untested on real hardware — if the
probe fails it falls back to software, so expect slow rather than wrong.

The safety model is the same on every platform: a source is only replaced after
the new file verifies, and originals are archived by default.

Log file:  %LOCALAPPDATA%\VeryThoughtfulCompression\VeryThoughtfulCompression.log


A codec-aware video re-encoder for large libraries. It measures every file first
(bits per pixel per frame) and only re-encodes the ones that are actually over
your chosen quality tier — so already-lean files are left untouched, and a re-run
converges instead of grinding the same file smaller every pass. H.265/AV1/VP9
sources are never transcoded (only remuxed); MP4-incompatible legacy codecs
(MPEG-2, VC-1, Xvid, WMV) are rescued at full fidelity.

Python, the engine, the interface, and a static ffmpeg + ffprobe are all bundled.
You install nothing. The macOS build is Apple Silicon and encodes on the GPU via
VideoToolbox; the Windows build uses whatever hardware encoder your machine has
(NVIDIA NVENC / Intel QSV / AMD AMF), falling back to software only if none works.

Your originals are never at risk. Every encode is written to a temporary file and
only replaces the original once it has finished AND passed the size check, so a
crash, a power cut, or pressing "Stop now" always leaves the original exactly as
it was.


** INSTALLATION **

macOS (`VeryThoughtfulCompression-macos.zip`, Apple Silicon):

  1. Unzip and drag "Very Thoughtful Compression.app" to your Applications folder.
  2. If the app is unsigned, macOS blocks the first launch. Pick either fix:
     - Right-click -> Open -> Open. If it still refuses, go to System Settings ->
       Privacy & Security, scroll down and click "Open Anyway", then open again
       (once only); or
     - Self-sign it — open Terminal and paste this line once:
       codesign --force --deep -s - "/Applications/Very Thoughtful Compression.app" && xattr -rd com.apple.quarantine "/Applications/Very Thoughtful Compression.app"
  3. Launch by double-clicking, or:
       open "/Applications/Very Thoughtful Compression.app"

  (A properly signed + notarized build launches with no warning at all.)

Windows (`VeryThoughtfulCompression-windows.zip`):

  1. Unzip the whole folder somewhere (keep the files together).
  2. Double-click "VeryThoughtfulCompression.exe". If SmartScreen objects, click
     "More info" -> "Run anyway" (needed once only, on an unsigned build).
  3. First run needs the Microsoft Edge WebView2 runtime. It ships with Windows 11
     and current Windows 10; if the window is blank, install the evergreen runtime
     from https://developer.microsoft.com/microsoft-edge/webview2/ (once).


** USING IT **

  1. Choose a media folder. It scans and probes your library in the background,
     and builds real preview encodes from your own footage while you configure.
  2. Answer the questions on the left — codec, quality tier, minimum saving,
     compatibility, encoder, and what happens to the originals.
  3. When every section is set, the finished configuration comes to the centre.
     Click any setting there to change it; press Start to run.
  4. A live progress view shows the file being worked on, what is queued behind
     it, and an estimate of the time left.
  5. A report shows exactly what was re-encoded, left alone, or needs a look, and
     can save the whole thing to a text file.

Nothing is written until you press Start.

Stopping a run — two buttons, both safe:
  - "Stop after current file" finishes what is being encoded and keeps it, then
    stops. With Parallel jobs above 1 there is more than one file in flight and
    the button says so; all of them finish.
  - "Stop now" cancels the encodes in progress immediately. Click it twice (it
    arms, then fires). Anything already finished is kept; a cancelled file is
    left exactly as it was.

Resume: a run records what it finished, so re-running the same folder with the
same settings picks up where it left off instead of re-examining everything.
Change any setting and the whole library is re-evaluated.


** WHAT'S NEW IN 1.2 **

  - A tier is now a QUALITY NUMBER (Excellent 109, Stellar 129) rather than a
    bits-per-pixel figure. The same quality costs different bits in different
    codecs, so one bpp was only ever true for H.264 — the number is now
    codec-independent and the app shows what it costs in the codec you picked.
  - The tool signs its own output and refuses, by default, to re-encode
    something it made earlier: a second lossy generation is unrecoverable. It
    is one click if you actually want it (H.264 -> H.265, say).
  - A scan no longer walks into its own output folder, which is how a second
    run used to re-encode the first run's results.
  - Source bitrate is measured properly for MKV, TS and WebM. Those containers
    do not report a per-stream video bitrate, and using the container total
    charged the video for the audio — over-stating a Blu-ray MKV by a third and
    re-encoding files that did not need it.
  - Pick individual files for the slower, better software encoder.
  - The interface is rebuilt: one progress strip along the top, one large
    preview deck, and a full-screen comparison at true 1:1 with no letterboxing.

** WHAT'S NEW IN 1.0 **

  - Quality tiers anchored to bits-per-pixel-per-frame, so "GOOD" means the same
    thing to a 4K film and a 720p episode.
  - Convergence: a file at or near its target is left alone, so re-running the
    same library does not shave it smaller every time.
  - A file is only re-encoded when the result can actually clear your minimum
    saving. Files that would be encoded and then thrown away are now left alone,
    which makes the estimate honest and stops wasting time on them.
  - Non-MP4 libraries get a real choice: convert, remux only, shrink but keep the
    original container, or leave alone entirely (see Advanced settings).
  - Independent subtitle filters — pick languages AND kinds, so "English forced
    subs only" is expressible. Image subtitles (PGS/DVD) either keep the file in
    MKV or are reported as dropped, never silently lost.
  - Side-by-side preview of your own footage at every tier, with a wipe/split
    comparison, so you can see the quality before committing to a run.
  - A time estimate built on predicted WORK rather than seconds-per-file, which
    matters because a library is mostly instant skips plus a few slow encodes.
    It knows which files a resumed run will skip, and calibrates itself against
    the real clock as it goes.
  - Progress view shows the current file, the queue behind it, and live encoder
    statistics; the whole queue is visible however large the library.
  - "Stop now" as well as "stop after current file".
  - Files that a quirky hardware encoder chokes on can be retried in software
    from the report, rather than sinking the whole run.
  - Bundled fonts and a bundled ffmpeg — the app makes no network requests.


** ADVANCED SETTINGS **

The gear icon (top right) opens settings you do not normally need. Every one of
them has a sane default; "Reset" restores the lot. They apply to the next run.

NON-MP4 FILES — what to do with MKV, AVI, WMV and friends. MP4 is the most widely
compatible container, but converting is not always what you want.
  - Convert all        Re-encode or remux everything into MP4. The default.
  - Remux only         Repackage into MP4 without touching the video. Lossless
                       and fast, but only possible when the codec is MP4-legal.
  - Shrink, keep format  Re-encode over-target files but leave them in their own
                       container (an MKV stays an MKV). Use this when you want
                       the space back but rely on MKV features.
  - Leave alone        Do not touch anything that is not already an MP4.

SUBTITLES — two INDEPENDENT filters, applied together.
  - Languages to keep  Comma-separated ISO codes (eng, fre, spa). Empty keeps
                       every language.
  - Kinds to keep      Tick any combination of Normal, Forced and SDH/HoH. Empty
                       (or all three) keeps every kind. A track that is both
                       forced and SDH is kept if either box is ticked.
  Text subtitles embed into MP4. Image subtitles (PGS, DVD) cannot, so they
  either force MKV or are dropped — see "Keep image subtitles" below.

VIDEO TUNING — the quality model itself. Leave these alone unless you have a
reason and a test file.
  - Bitrate floor      Never target below this (kbps). Stops a very small or very
                       short file being given a nonsense target.
  - Re-encode tolerance  Only re-encode a source more than this percent over its
                       target. This is the convergence guard: raise it to be more
                       conservative, lower it to re-encode more aggressively.
  - HEVC factor HD / 4K / 8K+  How much cheaper H.265 is than H.264 at each
                       resolution. Target = the H.264 target x this factor. Lower
                       values mean smaller files and more risk; the defaults
                       (0.60 / 0.50 / 0.45) are deliberately cautious.

AUDIO & CONTAINER
  - Container          Auto picks MP4 and falls back to MKV only when it must
                       (image subtitles, FLAC, too many tracks). Force one if you
                       would rather decide yourself.
  - Audio              Passthrough copies the existing tracks untouched — the
                       default, and lossless. AAC/AC-3 re-encode; FLAC forces MKV.
  - Audio bitrate      Stereo and 5.1+ targets, used only when audio is actually
                       re-encoded (never on passthrough).
  - Keep image subtitles  On: a file with PGS/DVD subtitles becomes MKV so the
                       subtitles survive. Off: it becomes MP4 and they are
                       dropped — reported in the run report, never silent.
  - Force MKV over N tracks  0 is off. Use MKV whenever a file has more than N
                       audio+subtitle tracks, since MP4 handles many tracks badly.

EXECUTION
  - Parallel jobs      How many files are encoded at once. More is not always
                       faster: hardware encoders have a limited number of engines
                       (an Apple M1 has one; M1 Max has two) and software encoders
                       already use every core, so 1-2 is usually right. Note this
                       also changes what "stop after current file" means — every
                       file in flight finishes.
  - Resume ledger      On: files already processed at these exact settings are
                       skipped on a re-run, which makes an interrupted run cheap
                       to resume. Change any setting and the ledger no longer
                       matches, so everything is re-evaluated. Turn it off to
                       force a full pass.


** TROUBLESHOOTING **

Not saving as much as expected? The codec is the lever, not the tier. Re-encoding
H.264 into H.264 at the same tier targets roughly the source bitrate, so there is
little to gain — choose H.265 for a genuinely smaller library.

A few files failed? A hardware encoder occasionally refuses a perfectly valid
file. The report offers to retry just those in software.

Something worth reporting: the app writes a log you can send back.
  macOS    ~/Library/Logs/VeryThoughtfulCompression.log
  Windows  %LOCALAPPDATA%\VeryThoughtfulCompression\VeryThoughtfulCompression.log


This app bundles FFmpeg (GPLv3) — see the "licenses" folder. FFmpeg is a separate
program invoked as a subprocess; this app's own code is not a derivative work of
it. Corresponding source: https://ffmpeg.org/releases/
