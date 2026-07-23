#!/usr/bin/env bash
#
# Build the self-contained macOS app "Very Thoughtful Compression".
#
# Output: packaging/dist-gui/VeryThoughtfulCompression.zip  (send this)
#
# A windowed macOS .app (pywebview / WKWebView) with folder pickers and the real
# encode engine. Python, pywebview, the vtc engine, the interface HTML, and a
# static ffmpeg + ffprobe are all bundled — recipients install nothing.
#
# ffmpeg/ffprobe source (redistributable GPL static builds; --enable-nonfree
# refused). MUST be arm64: an x86_64 ffmpeg under Rosetta LISTS hevc_videotoolbox
# but fails every hardware encode (-22), and a software-only app is not useful —
# so this build is Apple-Silicon-only and gates on VideoToolbox actually working.
#   1. $FFMPEG_STATIC / $FFPROBE_STATIC — arm64 binaries you already have
#   2. downloaded from martin-riedl.de (self-contained static GPL arm64 builds
#      with a working hevc_videotoolbox / h264_videotoolbox)
#
set -euo pipefail

PKG_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$PKG_DIR/.." && pwd)"
# shellcheck source=lib_licenses.sh
source "$PKG_DIR/lib_licenses.sh"
BUILD_DIR="$PKG_DIR/build-gui"
DIST_DIR="$PKG_DIR/dist-gui"
VENV_DIR="$BUILD_DIR/venv"
APP_NAME="VeryThoughtfulCompression"
APP_DIR="$DIST_DIR/Very Thoughtful Compression.app"

echo "==> Cleaning previous build"
rm -rf "$BUILD_DIR" "$DIST_DIR"
mkdir -p "$BUILD_DIR" "$DIST_DIR"

fetch_tool() {  # fetch_tool <name> <override-var-value> <evermeet-url>
    local name="$1" override="$2" url="$3" dest="$BUILD_DIR/$1"
    if [[ -n "$override" ]]; then
        echo "    Using provided $name: $override"; cp "$override" "$dest"
    else
        echo "    Downloading evermeet static $name"
        curl -L --fail -o "$BUILD_DIR/$name.zip" "$url"
        unzip -o -j "$BUILD_DIR/$name.zip" "$name" -d "$BUILD_DIR" >/dev/null
    fi
    chmod +x "$dest"
}

MR="https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release"
echo "==> Obtaining static arm64 ffmpeg + ffprobe (redistributable GPL builds)"
fetch_tool ffmpeg  "${FFMPEG_STATIC:-}"  "$MR/ffmpeg.zip"
fetch_tool ffprobe "${FFPROBE_STATIC:-}" "$MR/ffprobe.zip"
FFMPEG_BIN="$BUILD_DIR/ffmpeg"
FFPROBE_BIN="$BUILD_DIR/ffprobe"

# Architecture gate: must be arm64 (Rosetta x86_64 breaks VideoToolbox).
if ! file "$FFMPEG_BIN" | grep -q arm64; then
    echo "ERROR: bundled ffmpeg is not arm64 — VideoToolbox will not work under Rosetta."
    echo "Provide an arm64 static build via \$FFMPEG_STATIC / \$FFPROBE_STATIC."; exit 1
fi

# License gate: --enable-nonfree builds are NOT redistributable.
if "$FFMPEG_BIN" -version 2>/dev/null | grep -q -- "--enable-nonfree"; then
    echo "ERROR: this ffmpeg is built --enable-nonfree and cannot be redistributed."; exit 1
fi
FFVER="$("$FFMPEG_BIN" -version 2>/dev/null | head -1)"
echo "    $FFVER"

# Encoder gate: needs libx264 + libx265 (software) AND the VideoToolbox encoders.
for enc in libx264 libx265 h264_videotoolbox hevc_videotoolbox; do
    if ! "$FFMPEG_BIN" -hide_banner -encoders 2>/dev/null | grep -q " $enc "; then
        echo "ERROR: bundled ffmpeg is missing the '$enc' encoder."; exit 1
    fi
done
"$FFPROBE_BIN" -version >/dev/null || { echo "ERROR: ffprobe not runnable"; exit 1; }

# Function gate: VideoToolbox must ACTUALLY encode here, not merely be listed.
# This is the whole point — ship only a build whose hardware path works.
echo "==> Verifying hevc_videotoolbox actually works on this machine"
if ! "$FFMPEG_BIN" -hide_banner -v error -f lavfi -i testsrc2=size=128x128:rate=1 \
        -frames:v 1 -c:v hevc_videotoolbox -f null - 2>/dev/null; then
    echo "ERROR: hevc_videotoolbox failed a test encode. Build on Apple Silicon with"
    echo "       an arm64 ffmpeg — a software-only app is not shippable."; exit 1
fi
echo "    hevc_videotoolbox: OK"

echo "==> Selecting a build Python (3.11+)"
BUILD_PYTHON=""
for cand in "${PYTHON:-}" python3.13 python3.12 python3.11 python3; do
    [[ -n "$cand" ]] || continue
    command -v "$cand" >/dev/null || continue
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,11) else 1)' 2>/dev/null; then
        BUILD_PYTHON="$(command -v "$cand")"; break
    fi
done
[[ -n "$BUILD_PYTHON" ]] || { echo "ERROR: need Python >= 3.11 (set PYTHON=/path/to/python)"; exit 1; }
echo "    Using $BUILD_PYTHON ($("$BUILD_PYTHON" --version))"

echo "==> Creating build virtualenv"
"$BUILD_PYTHON" -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip
# pywebview pulls the pyobjc WKWebView backend on macOS
pip install --quiet pyinstaller pywebview

echo "==> Running PyInstaller"
export FFMPEG_BINARY_PATH="$FFMPEG_BIN"
export FFPROBE_BINARY_PATH="$FFPROBE_BIN"
pyinstaller \
    --clean --noconfirm \
    --distpath "$DIST_DIR" \
    --workpath "$BUILD_DIR/pyi-work" \
    "$PKG_DIR/vtc-gui.spec"

[[ -d "$APP_DIR" ]] || { echo "ERROR: PyInstaller did not produce $APP_DIR"; exit 1; }

echo "==> Writing recipient README + licenses, zipping"
STAGE="$BUILD_DIR/stage/$APP_NAME"
rm -rf "$BUILD_DIR/stage"; mkdir -p "$STAGE"
cp -R "$APP_DIR" "$STAGE/"
cp "$PKG_DIR/APP_README.txt" "$STAGE/README.txt"
write_ffmpeg_licenses "$STAGE/licenses" "$FFVER"

( cd "$BUILD_DIR/stage" && ditto -c -k --sequesterRsrc --keepParent "$APP_NAME" "$DIST_DIR/$APP_NAME.zip" )

deactivate || true
echo ""
echo "Done."
echo "  App: $APP_DIR"
echo "  Zip: $DIST_DIR/$APP_NAME.zip  (send this)"
echo ""
echo "NOTE: unsigned by default — first launch on another Mac is right-click -> Open."
echo "      To sign + notarize for distribution, see packaging/BUILD.md."
