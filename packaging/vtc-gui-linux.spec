# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — Linux build of "Very Thoughtful Compression" (the GUI).

Produces a single-folder Linux app (pywebview via the GTK/WebKit2 backend) that
bundles the Python runtime, the vtc engine, the interface HTML, and static ffmpeg
+ ffprobe (paths from FFMPEG_BINARY_PATH / FFPROBE_BINARY_PATH).

UNTESTED on Linux from this repo's mac-first workflow. Requirements on the target:
python3-gi, gir1.2-webkit2-4.1 (or 4.0), libgtk-3. HEVC preview playback under
WebKitGTK is unreliable — the app already defaults previews to H.264 off macOS.
"""

import os

repo_root = os.path.dirname(SPECPATH)
ffmpeg = os.environ.get('FFMPEG_BINARY_PATH')
ffprobe = os.environ.get('FFPROBE_BINARY_PATH')
for label, path in (('FFMPEG_BINARY_PATH', ffmpeg), ('FFPROBE_BINARY_PATH', ffprobe)):
    if not path or not os.path.exists(path):
        raise SystemExit(f"{label} must point to a static Linux binary.")

html = os.path.join(repo_root, 'vtc', 'vtc_app_v3.html')
if not os.path.exists(html):
    raise SystemExit("vtc/vtc_app_v3.html missing from the repo.")

a = Analysis(
    [os.path.join(repo_root, 'packaging', 'vtc_app.py')],
    pathex=[repo_root],
    binaries=[(ffmpeg, '.'), (ffprobe, '.')],
    datas=[(html, '.')],
    hiddenimports=['webview', 'webview.platforms.gtk',
                   'gi', 'gi.repository.Gtk', 'gi.repository.WebKit2',
                   'vtc', 'vtc.webapp', 'vtc.pipeline', 'vtc.encode',
                   'vtc.ffprobe', 'vtc.model', 'vtc.config', 'vtc.ledger',
                   'vtc.report', 'vtc.result'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'PIL', 'numpy', 'pytest'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='VeryThoughtfulCompression',
    debug=False, strip=False, upx=False,
    console=False,
    argv_emulation=False,
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name='VeryThoughtfulCompression',
)
