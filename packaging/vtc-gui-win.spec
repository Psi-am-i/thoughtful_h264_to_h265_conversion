# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — Windows build of "Very Thoughtful Compression" (the GUI).

Windows counterpart of vtc-gui.spec: a windowed onedir build bundling the Python
runtime + pywebview (EdgeChromium / WebView2 backend), the vtc engine, the
interface HTML, and static ffmpeg.exe + ffprobe.exe (both resolved by
vtc.webapp at runtime).

FFMPEG_BINARY_PATH / FFPROBE_BINARY_PATH must point at static GPL builds (the
release CI downloads them from gyan.dev). Built by .github/workflows/release.yml
on a windows-latest runner — PyInstaller cannot cross-compile.

WebView2: the recipient needs the Microsoft Edge WebView2 runtime. It ships with
Windows 11 and current Windows 10; on an older box it installs once (see
packaging/BUILD.md and the recipient README).
"""

import os

repo_root = os.path.dirname(SPECPATH)
ffmpeg = os.environ.get('FFMPEG_BINARY_PATH')
ffprobe = os.environ.get('FFPROBE_BINARY_PATH')
for label, path in (('FFMPEG_BINARY_PATH', ffmpeg), ('FFPROBE_BINARY_PATH', ffprobe)):
    if not path or not os.path.exists(path):
        raise SystemExit(f"{label} must point to a static .exe. "
                         f"See .github/workflows/release.yml for how CI obtains one.")

html = os.path.join(repo_root, 'vtc', 'vtc_app_v3.html')
if not os.path.exists(html):
    raise SystemExit("vtc/vtc_app_v3.html missing from the repo.")

a = Analysis(
    [os.path.join(repo_root, 'packaging', 'vtc_app.py')],
    pathex=[repo_root],
    binaries=[(ffmpeg, '.'), (ffprobe, '.')],
    datas=[(html, '.')],                       # -> bundle root, next to the exe
    hiddenimports=['webview', 'webview.platforms.edgechromium',
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
    console=False,                             # windowed GUI, no console window
    argv_emulation=False,
    icon=os.path.join(repo_root, 'packaging', 'app_icon.ico'),
    version=os.path.join(repo_root, 'packaging', 'version_win.txt'),
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name='VeryThoughtfulCompression',
)
