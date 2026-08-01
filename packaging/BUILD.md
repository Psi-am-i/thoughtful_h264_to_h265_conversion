# Building & signing the distributable apps

Two self-contained, double-clickable apps — a macOS `.app` and a Windows
onedir `.exe` — each bundling Python, pywebview, the `vtc` engine, the interface
HTML, and a static **ffmpeg + ffprobe**. Recipients install nothing.

PyInstaller cannot cross-compile, so each OS bundle is built on that OS. Locally
you build the macOS app; `.github/workflows/release.yml` builds both on GitHub
runners and attaches the zips to a `v*` tag release.

---

## macOS — build locally

From the repo root:

```bash
packaging/build_gui_app.sh
```

Outputs:
- `packaging/dist-gui/Very Thoughtful Compression.app` — the app
- `packaging/dist-gui/VeryThoughtfulCompression.zip` — **send the zip** (macOS
  keeps the executable bits inside a zip; a raw `.app` copied around can lose them)

Everything under `packaging/build-gui/` is intermediate — rebuild rather than
reuse. Override the bundled binaries with `FFMPEG_STATIC=/path/to/ffmpeg
FFPROBE_STATIC=/path/to/ffprobe packaging/build_gui_app.sh`, or the build Python
with `PYTHON=/opt/homebrew/bin/python3.13`.

### ffmpeg/ffprobe source & licensing

Static builds are required — Homebrew's link against many dylibs and aren't
portable. The script downloads **arm64** builds from
[martin-riedl.de](https://ffmpeg.martin-riedl.de/) (**GPL**, redistributable)
unless you supply your own via `$FFMPEG_STATIC` / `$FFPROBE_STATIC`, and
**refuses** any `--enable-nonfree` build. Because the app calls ffmpeg as a
subprocess (not linked), the app's own code is not a derivative work. Each zip
carries a `licenses/` folder with the full GPL text + attribution NOTICE.

**Architecture: arm64 / Apple Silicon only — this is deliberate.** An x86_64
ffmpeg under Rosetta 2 *lists* `hevc_videotoolbox` but fails every hardware encode
(`-22`), and a software-only app is not useful (libx265 is far too slow for a TV
library). So the build **gates**: it verifies the bundled ffmpeg is arm64 and that
`hevc_videotoolbox` actually encodes a test frame on the build host — otherwise it
refuses to build. Build on an Apple Silicon Mac. (At runtime the app re-probes and
falls back to software only if hardware genuinely can't run — see the engine's
`select_hw_encoder`.)

### Gatekeeper (unsigned)

Unsigned, another Mac blocks the first launch. Recipient does one of these once
(also in the recipient README):

- Right-click `Very Thoughtful Compression.app` → **Open** → **Open** (then
  System Settings → Privacy & Security → **Open Anyway** if prompted), or
- Ad-hoc self-sign + clear quarantine:

  ```bash
  codesign --force --deep -s - "/Applications/Very Thoughtful Compression.app" \
    && xattr -rd com.apple.quarantine "/Applications/Very Thoughtful Compression.app"
  ```

### Signing + notarizing for real distribution (no warning)

Needs an **Apple Developer ID** ($99/yr). One-time: install your "Developer ID
Application" certificate into the login keychain, and store a notarization
credential:

```bash
xcrun notarytool store-credentials VTC-NOTARY \
  --apple-id "you@example.com" --team-id "TEAMID1234" --password "app-specific-pw"
```

Then, after `packaging/build_gui_app.sh` has produced the `.app`:

```bash
APP="packaging/dist-gui/Very Thoughtful Compression.app"

# 1. Deep-sign with a hardened runtime and a secure timestamp.
codesign --force --deep --timestamp --options runtime \
  --sign "Developer ID Application: Your Name (TEAMID1234)" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

# 2. Notarize a zip of the signed app and wait for the ticket.
ZIP="packaging/dist-gui/VeryThoughtfulCompression.zip"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
xcrun notarytool submit "$ZIP" --keychain-profile VTC-NOTARY --wait

# 3. Staple the ticket into the .app, then re-zip THAT for distribution.
xcrun stapler staple "$APP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
spctl --assess --type execute --verbose "$APP"   # should say "accepted / Notarized Developer ID"
```

To wire signing into the build itself, add `codesign_identity` /
`entitlements_file` to the `BUNDLE(...)` in `packaging/vtc-gui.spec` and notarize
the result. (Bundling an x86_64 ffmpeg is fine under a hardened runtime — it is a
separate signed executable, not a loaded library.)

---

## Windows — build via CI (or on a Windows box)

PyInstaller can't build Windows apps from macOS. Use the release workflow, or on
a Windows machine with Python 3.11+:

```powershell
pip install pyinstaller pywebview
# fetch a static GPL ffmpeg + ffprobe (gyan.dev "essentials" has both .exe):
#   ffmpeg.exe, ffprobe.exe  ->  set the two env vars to their full paths
$env:FFMPEG_BINARY_PATH  = "C:\path\to\ffmpeg.exe"
$env:FFPROBE_BINARY_PATH = "C:\path\to\ffprobe.exe"
pyinstaller --clean --noconfirm --distpath dist-win --workpath build-win packaging/vtc-gui-win.spec
# dist-win\VeryThoughtfulCompression\  ->  add licenses\ + README.txt, then zip the folder
```

**WebView2:** the app uses pywebview's EdgeChromium backend, which needs the
Microsoft Edge WebView2 runtime. It ships with Windows 11 and current Windows 10;
on older boxes the recipient installs the evergreen runtime once
(https://developer.microsoft.com/microsoft-edge/webview2/).

### SmartScreen (unsigned)

Unsigned, Windows SmartScreen warns on first run: **More info → Run anyway**
(once). To remove it, sign the `.exe` with a code-signing certificate.

### Signing for real distribution

Get an **OV** or, to skip the SmartScreen reputation wait, an **EV** code-signing
certificate (from a CA such as DigiCert/Sectigo; modern certs live on a hardware
token or cloud HSM). With the Windows SDK's `signtool`:

```powershell
# Sign the exe (and ideally the bundled ffmpeg.exe / ffprobe.exe too), RFC-3161 timestamp:
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
  /n "Your Company Name" `
  "dist-win\VeryThoughtfulCompression\VeryThoughtfulCompression.exe"
signtool verify /pa "dist-win\VeryThoughtfulCompression\VeryThoughtfulCompression.exe"
```

For a token/HSM cert use `/csp` + `/kc` (or the CA's KSP/`/sha1` thumbprint) per
your provider's docs. In CI, sign as a step after the PyInstaller build using a
cloud-signing action for your provider (Azure Trusted Signing, DigiCert
KeyLocker, etc.). An OV cert clears SmartScreen once reputation accrues; an EV
cert clears it immediately.

---

## What ships in each zip

```
VeryThoughtfulCompression/            (Windows folder — keep intact)
  VeryThoughtfulCompression.exe
  ffmpeg.exe  ffprobe.exe  vtc_app_v3.html  ...python runtime...
  README.txt        (recipient guide)
  licenses/         (FFmpeg GPLv3 + NOTICE)

Very Thoughtful Compression.app/      (macOS)
  Contents/MacOS/VeryThoughtfulCompression
  Contents/Resources/  ffmpeg  ffprobe  vtc_app_v3.html  ...
```

At runtime `vtc.webapp` resolves ffmpeg/ffprobe and the HTML from the bundle
(`sys._MEIPASS`) before falling back to `$FFMPEG_BINARY` / `$FFPROBE_BINARY` or
`PATH` — so the same code runs framed in the app or from a plain `pip install`.
