#!/usr/bin/env bash
# Shared helper: write FFmpeg license + attribution into a distributable.
# Sourced by build_app.sh and build_gui_app.sh.

# write_ffmpeg_licenses <dest_dir> <ffmpeg_version_line>
write_ffmpeg_licenses() {
    local dest="$1"
    local ffver="$2"
    mkdir -p "$dest"

    cat > "$dest/FFmpeg-NOTICE.txt" <<NOTICE
This application bundles FFmpeg.

FFmpeg version : ${ffver}
License        : GNU General Public License, version 3 (--enable-gpl --enable-version3)

FFmpeg is free software licensed under the GNU GPL v3 (see GPL-3.0.txt in this
folder). It is a separate program invoked by this app as a subprocess; this
app's own code is not a derivative work of FFmpeg.

The bundled ffmpeg binary is an unmodified static build. Corresponding source
code for FFmpeg is available from:
  https://ffmpeg.org/releases/
  https://github.com/FFmpeg/FFmpeg

FFmpeg is a trademark of Fabrice Bellard, originator of the FFmpeg project.
This software uses code of FFmpeg licensed under the GPLv3 and its source can
be downloaded from the links above.
NOTICE

    # Full GPL v3 text must accompany the binary. Fetch it; fall back to a
    # pointer if offline.
    if curl -s -L --fail --connect-timeout 12 \
        -o "$dest/GPL-3.0.txt" "https://www.gnu.org/licenses/gpl-3.0.txt"; then
        :
    else
        cat > "$dest/GPL-3.0.txt" <<'FALLBACK'
The full text of the GNU General Public License v3 could not be downloaded at
build time. Obtain it from: https://www.gnu.org/licenses/gpl-3.0.txt
FALLBACK
    fi
    echo "    wrote FFmpeg license files to $(basename "$(dirname "$dest")")/$(basename "$dest")/"
}
