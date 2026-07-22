# Very Thoughtful Compression — by Picnic Labs

A codec-aware video re-encoder for large libraries. It measures every file first
(bits per pixel per frame) and only re-encodes the ones that are actually over
your chosen quality tier — so already-lean files are left untouched, and a re-run
converges instead of grinding the same file smaller every pass. H.265/AV1/VP9
sources are never transcoded (only remuxed); MP4-incompatible legacy codecs
(MPEG-2, VC-1, Xvid, WMV) are rescued at full fidelity.

Python, the engine, the interface, and a static ffmpeg + ffprobe are all bundled.
You install nothing.

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

  1. Choose a media folder. It scans and probes your library.
  2. Answer the questions on the left — codec, quality tier, minimum saving,
     compatibility, encoder, and what happens to the originals.
  3. When every section is set, the finished configuration comes to the centre.
     Click any setting there to change it; press Start to run.
  4. A report shows exactly what was re-encoded, left alone, or needs a look.

Your originals are handled the way you chose (archived, deleted after verify, or
kept). Nothing is written until you press Start.

This app bundles FFmpeg (GPLv3) — see the "licenses" folder. FFmpeg is a separate
program invoked as a subprocess; this app's own code is not a derivative work of
it. Corresponding source: https://ffmpeg.org/releases/
