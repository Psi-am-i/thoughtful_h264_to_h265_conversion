#!/usr/bin/env python3
"""Frozen-app entry point for Very Thoughtful Compression (the GUI).

PyInstaller points at this launcher. It calls the real GUI with no arguments so
`vtc.webapp.main` uses the HTML and ffmpeg/ffprobe bundled inside the app rather
than anything on the host. Kept tiny on purpose — all logic lives in the package.
"""
import sys

from vtc.webapp import main

if __name__ == "__main__":
    sys.exit(main([]))
