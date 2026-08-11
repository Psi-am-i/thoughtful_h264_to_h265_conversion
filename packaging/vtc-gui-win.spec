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

from PyInstaller.utils.hooks import collect_all

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

# pywebview on Windows does NOT drive EdgeChromium directly — it runs a .NET
# WinForms host on pythonnet, with EdgeChromium only the renderer inside it. That
# host, and clr_loader's runtime DLLs + .runtimeconfig.json (shipped as DATA, so
# invisible to the import graph), must be collected explicitly or the app launches
# and hangs with nothing logged. pythonnet is present at build time (pywebview
# declares it for win32), so the build never fails — the pieces just never ship.
# (WINDOWS-GOTCHAS.md #1)
_pn_datas, _pn_bins, _pn_hidden = collect_all('pythonnet')
_cl_datas, _cl_bins, _cl_hidden = collect_all('clr_loader')

a = Analysis(
    [os.path.join(repo_root, 'packaging', 'vtc_app.py')],
    pathex=[repo_root],
    binaries=[(ffmpeg, '.'), (ffprobe, '.'), *_pn_bins, *_cl_bins],
    datas=[(html, '.'), *_pn_datas, *_cl_datas],   # -> bundle root, next to the exe
    hiddenimports=['webview',
                   'webview.platforms.winforms',       # the actual Windows host
                   'webview.platforms.edgechromium',   # the renderer it drives
                   'clr', 'clr_loader',
                   *_pn_hidden, *_cl_hidden,
                   'vtc', 'vtc.webapp', 'vtc.pipeline', 'vtc.encode',
                   'vtc.ffprobe', 'vtc.model', 'vtc.config', 'vtc.ledger',
                   'vtc.report', 'vtc.result', 'vtc.winproc'],
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
