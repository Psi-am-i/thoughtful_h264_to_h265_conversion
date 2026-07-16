#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# very_thoughtful_compression.sh — Batch re-encode fat H.264 video files to a sane size
#                        (H.265/HEVC or H.264/AVC, at a chosen quality tier)
#
# USAGE:
#   ./very_thoughtful_compression.sh [SRC]
#
#   SRC   Directory to scan (prompted for if not given; default: current dir)
#
# The script prompts interactively for: output codec (H.265 or H.264), quality
# tier (STANDARD/HIGH/EXCELLENT/STELLAR/INSANE — each a resolution-anchored
# quality ceiling), hardware vs software encoder (if a working hardware
# encoder is detected), output location, what to do with originals, parallel
# jobs, and minimum saving. Set FORCE_VT=1/0 to force or disable hardware
# (VideoToolbox) encoding and skip that prompt entirely.
#
# Text subtitles are embedded into the MP4 (mov_text), or extracted to sidecar
# .srt files if embedding fails. Image subtitles (PGS/DVD) can't live in MP4;
# when any subtitle track would be lost the original is archived — even in
# delete mode — and the run report explains why.
#
# The scan also covers .mov/.avi/.webm/.m4v/.ts/.wmv/.flv, and flags files
# whose codec/container combination is a poor fit for MP4/typical players
# (AV1/VP9 in WebM/MKV, legacy MPEG-2/VC-1/Xvid/WMV) regardless of size —
# offering to transcode them using the same codec/tier settings, once you
# confirm.
#
# GRACEFUL STOP:
#   touch /tmp/hevc_stop     — finish current file(s), skip the rest
#   rm /tmp/hevc_stop        — clear the stop flag to resume/re-run
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

SRC="${1:-}"
if [[ -z "$SRC" ]]; then
  printf '\nDirectory to scan for videos (recursively)?\n  (default: current directory — %s)\n' "$PWD" >&2
  read -r -p "  Path: " SRC </dev/tty 2>/dev/tty || SRC=""
  SRC="${SRC:-.}"
fi
SRC="${SRC/#\~/$HOME}"        # expand a typed leading ~
SRC="${SRC%/}"
# People often paste a shell-escaped path (backslashes before spaces) into the
# prompt, but read -r keeps those backslashes literal. If the path as typed
# isn't a directory yet unescaping it yields a real one, use that.
if [[ ! -d "$SRC" ]]; then
  _unescaped="$(printf '%s' "$SRC" | sed 's/\\\(.\)/\1/g')"
  _unescaped="${_unescaped%/}"
  [[ -d "$_unescaped" ]] && SRC="$_unescaped"
fi
if [[ ! -d "$SRC" ]]; then
  printf 'ERROR: "%s" is not a directory\n' "$SRC" >&2
  printf '       (tip: type the path with plain spaces — no backslashes or quotes)\n' >&2
  exit 1
fi
TMPROOT="/tmp/hevcwork"

FFMPEG="${FFMPEG:-ffmpeg}"
FFPROBE="${FFPROBE:-ffprobe}"

# Resolved this early (rather than in "Setup" below) so the encoder-choice
# prompt can call it before any per-file work begins. FORCE_VT=1/0 overrides
# and skips the probe entirely.
check_videotoolbox() {
  if [[ "${FORCE_VT:-}" == "1" ]]; then return 0; fi
  if [[ "${FORCE_VT:-}" == "0" ]]; then return 1; fi
  local enc
  enc="$( [[ "${OUT_CODEC:-h265}" == "h264" ]] && echo h264_videotoolbox || echo hevc_videotoolbox )"
  "$FFMPEG" -hide_banner -encoders 2>/dev/null | grep -q "$enc"
}

# ── Bitrate strategy ──────────────────────────────────────────────────────────
# The target bitrate comes from a quality TIER chosen per-run (see prompt below).
# Each tier is a fixed DENSITY — bits per pixel per frame (bpp) — anchored to an
# H.264 bitrate at 1080p/30fps and calibrated against streaming-service quality
# (EXCELLENT ≈ Netflix's top 1080p rung, plus headroom for a generic encoder):
#
#   target_kbps = tier_bpp * pixels * fps * codec_factor / 1000
#
# Because bpp is normalised by resolution AND frame rate, one tier scales to any
# source automatically — a 4K file gets ~4x a 1080p file, a 60fps file ~2x a
# 30fps file — with no per-resolution rules. codec_factor is 1.0 for H.264; for
# H.265 it reflects HEVC reaching the same quality at less bitrate, the advantage
# growing with resolution (validated against coding-efficiency studies):
#     <= 1080p -> 0.60      <= 4K -> 0.50      above -> 0.45
#
# The target is ABSOLUTE (a function of the tier and the source's pixels/fps), NOT
# a fraction of the source's current bitrate — so re-runs CONVERGE: once a file is
# at or under its tier target it is left alone, instead of being trimmed a little
# further every pass. A source is re-encoded only when it is more than
# TIER_OVER_TOLERANCE over its target.
BITRATE_FLOOR=1500          # never target below this (avoids garbage output)
TIER_OVER_TOLERANCE=1.10    # re-encode only a source that is >10% over its target
HEVC_EFF_HD="${HEVC_EFFICIENCY_HD:-0.60}"   # H.265 bitrate vs H.264 same quality, <= 1080p
HEVC_EFF_4K="${HEVC_EFFICIENCY_4K:-0.50}"   # ... <= 4K
HEVC_EFF_8K="${HEVC_EFFICIENCY_8K:-0.45}"   # ... above 4K

# Max-fidelity transcode ceiling — used only for the compatibility transcode of
# legacy/MP4-incompatible codecs (MPEG-2/VC-1/Xvid/WMV): quality-targeted CRF
# capped at the SOURCE's own bitrate so quality is preserved, not squeezed to a
# tier. Falls back to these caps only when the source bitrate can't be probed.
XCODE_CAP_HD="${XCODE_CAP_HD:-8000}"    # kbps, sources < 3840px wide
XCODE_CAP_4K="${XCODE_CAP_4K:-20000}"   # kbps, 4K sources

# ── Interactive prompts ───────────────────────────────────────────────────────
# Ordered to flow: QUALITY (what & how good) → COMPATIBILITY (what else to
# convert) → EXECUTION (how it runs) → DESTINATION (where files go).

# ── 1. Quality ────────────────────────────────────────────────────────────────

printf '\nOutput codec?\n  1) H.265 / HEVC  — ~50%% smaller at the same quality; plays on anything reasonably modern  [default]\n  2) H.264 / AVC   — almost universally playable, but ~2x larger — and increasingly inefficient\n                     above 1080p (for 4K+ sources H.265 is strongly recommended)\n' >&2
read -r -p "  Choice [1/2]: " _cchoice </dev/tty 2>/dev/tty || _cchoice=1
case "${_cchoice:-1}" in
  2) OUT_CODEC=h264 ;;
  *) OUT_CODEC=h265 ;;
esac
export OUT_CODEC

printf '\nQuality tier? Each tier is a fixed quality DENSITY (bits per pixel), anchored to an\nH.264 bitrate at 1080p/30fps and calibrated against streaming services. The target\nscales automatically with a file'"'"'s resolution and frame rate; H.265 output uses\n~40-55%% less bitrate for the same quality.\n  1) FINE       — ~4 Mbps 1080p H.264   — space-first / light streaming\n  2) GOOD       — ~5 Mbps 1080p H.264   — solid streaming quality\n  3) EXCELLENT  — ~6.8 Mbps 1080p H.264 — matches top streaming, with encoder headroom  [default]\n  4) INSANE     — ~9 Mbps 1080p H.264   — near-transparent for streaming-sourced files\n' >&2
read -r -p "  Choice [1-4]: " _tchoice </dev/tty 2>/dev/tty || _tchoice=3
case "${_tchoice:-3}" in
  1) TIER_NAME=FINE;      TIER_REF_MBPS=4.0 ;;
  2) TIER_NAME=GOOD;      TIER_REF_MBPS=5.0 ;;
  4) TIER_NAME=INSANE;    TIER_REF_MBPS=9.0 ;;
  *) TIER_NAME=EXCELLENT; TIER_REF_MBPS=6.8 ;;
esac
export TIER_NAME TIER_REF_MBPS

# Sensible minimum saving depends on the codec: a healthy H.265 re-encode saves
# 30-45%, so a small predicted saving means the source was already efficient and
# re-encoding would only cost a generation of quality. H.264->H.264 can only trim
# fat, so smaller savings are still worthwhile.
if [[ "$OUT_CODEC" == "h265" ]]; then
  printf '\nMinimum size saving required to keep a re-encoded file?\n  (H.265 typically saves 30-45%%; below ~25%% the source was already efficient)\n  1) 25%%  [default]\n  2) 15%%\n  3) 35%%\n  4) None — always keep if encode succeeds\n' >&2
  read -r -p "  Choice [1/2/3/4]: " _mschoice </dev/tty 2>/dev/tty || _mschoice=1
  case "${_mschoice:-1}" in
    2) MIN_SAVING_RATIO="0.85" ;;
    3) MIN_SAVING_RATIO="0.65" ;;
    4) MIN_SAVING_RATIO="1.00" ;;
    *) MIN_SAVING_RATIO="0.75" ;;
  esac
else
  printf '\nMinimum size saving required to keep a re-encoded file?\n  1) 15%%  [default]\n  2) 10%%\n  3) 5%%\n  4) None — always keep if encode succeeds\n' >&2
  read -r -p "  Choice [1/2/3/4]: " _mschoice </dev/tty 2>/dev/tty || _mschoice=1
  case "${_mschoice:-1}" in
    2) MIN_SAVING_RATIO="0.90" ;;
    3) MIN_SAVING_RATIO="0.95" ;;
    4) MIN_SAVING_RATIO="1.00" ;;
    *) MIN_SAVING_RATIO="0.85" ;;
  esac
fi
export MIN_SAVING_RATIO

# ── 2. Compatibility ──────────────────────────────────────────────────────────
# Instant preferences — applied per-file during the run, no folder pre-scan.

printf '\nIf possible, convert files into MP4 for maximum compatibility with NO loss of\nquality? (a file already using an MP4-friendly codec — H.264, H.265, AV1, VP9 —\nbut sitting in another container like MKV or WebM is remuxed into MP4: a fast,\nlossless copy, no re-encode.)\n  1) Yes  [default]\n  2) No\n' >&2
read -r -p "  Choice [1/2]: " _rmxchoice </dev/tty 2>/dev/tty || _rmxchoice=1
case "${_rmxchoice:-1}" in
  2) REMUX_TO_MP4=0 ;;
  *) REMUX_TO_MP4=1 ;;
esac
export REMUX_TO_MP4

printf '\nIf a file uses a codec incompatible with MP4 (MPEG-2, VC-1, Xvid/DivX, WMV …),\ntranscode it with maximum fidelity and convert it to MP4? (Modern high-compression\ncodecs — H.265, AV1, VP9 — are never transcoded, only remuxed as above.)\n  1) Yes  [default]\n  2) No\n' >&2
read -r -p "  Choice [1/2]: " _xcodechoice </dev/tty 2>/dev/tty || _xcodechoice=1
case "${_xcodechoice:-1}" in
  2) COMPAT_TRANSCODE=0 ;;
  *) COMPAT_TRANSCODE=1 ;;
esac
export COMPAT_TRANSCODE

# ── 3. Execution ──────────────────────────────────────────────────────────────

# Hardware (VideoToolbox) vs software (libx264/libx265) choice. FORCE_VT=1/0
# short-circuits this entirely (for scripted/non-interactive use); otherwise,
# if no working hardware encoder is even available, there's nothing to choose
# between and software is used silently, same as before.
if [[ "${FORCE_VT:-}" == "1" ]]; then
  USE_VT=1
elif [[ "${FORCE_VT:-}" == "0" ]]; then
  USE_VT=0
elif check_videotoolbox; then
  printf '\nWhich encoder should do the work?\n  1) Hardware (VideoToolbox) — fast, low CPU/power use; targets an average\n     bitrate only (no true per-bit quality control). Good for quick batch\n     jobs.  [default]\n  2) Software (libx264/libx265) — much slower and CPU-heavy, but uses\n     capped-CRF (true constant-quality encoding), generally better quality\n     per bit at the same ceiling — recommended for archival-quality work.\n' >&2
  read -r -p "  Choice [1/2]: " _echoice </dev/tty 2>/dev/tty || _echoice=1
  case "${_echoice:-1}" in
    2) USE_VT=0 ;;
    *) USE_VT=1 ;;
  esac
else
  USE_VT=0
fi
export USE_VT

printf '\nParallel encoding jobs? (H.265 is CPU-heavy)\n  1) 1 job  [default]\n  2) 2 jobs\n  3) 4 jobs\n' >&2
read -r -p "  Choice [1/2/3]: " _jchoice </dev/tty 2>/dev/tty || _jchoice=1
case "${_jchoice:-1}" in
  2) JOBS=2 ;;
  3) JOBS=4 ;;
  *) JOBS=1 ;;
esac
export JOBS

# ── 4. Destination ────────────────────────────────────────────────────────────

printf '\nWhere should the new %s file be written?\n' "$( [[ "$OUT_CODEC" == "h264" ]] && echo H.264 || echo H.265 )" >&2
printf '  1) Replace source in place  [default]\n  2) Write to a separate folder\n' >&2
read -r -p "  Choice [1/2]: " _ochoice </dev/tty 2>/dev/tty || _ochoice=1
case "${_ochoice:-1}" in
  2)
    OUTPUT_MODE=separate
    printf '\nDestination folder for new files?\n  (default: %s/new versions)\n' "$SRC" >&2
    read -r -p "  Path: " _opath </dev/tty 2>/dev/tty || _opath=""
    OUTPUT_DIR="${_opath:-$SRC/new versions}"
    printf '\nFolder structure in destination?\n  1) Mirror source folder structure  [default]\n  2) Flat — all files in destination root\n' >&2
    read -r -p "  Choice [1/2]: " _fchoice </dev/tty 2>/dev/tty || _fchoice=1
    case "${_fchoice:-1}" in
      2) OUTPUT_FLAT=1 ;;
      *) OUTPUT_FLAT=0 ;;
    esac
    ;;
  *)
    OUTPUT_MODE=inplace
    OUTPUT_DIR="$SRC"
    OUTPUT_FLAT=0
    ;;
esac
export OUTPUT_MODE OUTPUT_DIR OUTPUT_FLAT

printf '\nWhat should happen to the original file?\n  1) Archive (move to a folder)  [default]\n  2) Delete\n  3) Leave it where it is\n' >&2
read -r -p "  Choice [1/2/3]: " _schoice </dev/tty 2>/dev/tty || _schoice=1
case "${_schoice:-1}" in
  2) SOURCE_ACTION=delete ;;
  3) SOURCE_ACTION=keep ;;
  *) SOURCE_ACTION=archive ;;
esac

if [[ "$SOURCE_ACTION" == "archive" ]]; then
  printf '\nWhere should originals be archived?\n  (default: %s/originals)\n' "$SRC" >&2
  read -r -p "  Path: " _apath </dev/tty 2>/dev/tty || _apath=""
  ARCHIVE_DIR="${_apath:-$SRC/originals}"
else
  ARCHIVE_DIR=""
fi
export SOURCE_ACTION ARCHIVE_DIR

# ── Setup ─────────────────────────────────────────────────────────────────────

# Extensions scanned recursively — covers common containers likely to hold
# poorly-MP4-compatible codecs (webm/avi/wmv/flv/ts) alongside the originals.
VIDEO_EXTS=(mkv mp4 mov avi webm m4v ts wmv flv)
FIND_INAME_ARGS=()
VIDEO_EXTS_LABEL=""
for _ext in "${VIDEO_EXTS[@]}"; do
  [[ "${#FIND_INAME_ARGS[@]}" -gt 0 ]] && FIND_INAME_ARGS+=(-o)
  FIND_INAME_ARGS+=(-iname "*.${_ext}")
  VIDEO_EXTS_LABEL="${VIDEO_EXTS_LABEL:+$VIDEO_EXTS_LABEL, }.${_ext}"
done

mkdir -p "$TMPROOT"

# Sweep orphaned scratch files left by previously killed/crashed runs (a hard
# SIGKILL or power loss bypasses the per-file cleanup trap below). Safe because
# this run hasn't created any temps yet — but skip if another instance of this
# script is already running, so we don't delete its in-progress work.
_others="$(pgrep -f 'very_thoughtful_compression.sh' 2>/dev/null | grep -vw "$$" | wc -l | tr -d ' ')" || true
if [[ "${_others:-0}" -eq 0 ]]; then
  _swept="$(find "$TMPROOT" -maxdepth 1 -type f -name '*.mp4' 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "${_swept:-0}" -gt 0 ]]; then
    find "$TMPROOT" -maxdepth 1 -type f -name '*.mp4' -delete 2>/dev/null || true
    printf '\nSwept %s orphaned scratch file(s) from %s\n' "$_swept" "$TMPROOT" >&2
  fi
fi

PROBLEM_LOG="$(mktemp /tmp/hevc_problems.XXXXXX)"
# Each kept output appends "src_bytes<TAB>out_bytes" here (append is atomic for
# short writes, so parallel xargs workers can share it); summed for the report.
SAVINGS_LOG="$(mktemp /tmp/hevc_savings.XXXXXX)"
# Every file records its outcome here ("tag<TAB>path") so the end-of-run summary
# can account for ALL files, not just the ones that were replaced or had problems.
OUTCOME_LOG="$(mktemp /tmp/hevc_outcomes.XXXXXX)"
STOP_FILE="/tmp/hevc_stop"
export PROBLEM_LOG SAVINGS_LOG OUTCOME_LOG STOP_FILE
trap 'rm -f "$PROBLEM_LOG" "$SAVINGS_LOG" "$OUTCOME_LOG"' EXIT

# Resume ledger: persistent (unlike the temp logs above) so a re-run after a stop
# skips files already finished under the SAME settings. Lives at the scan root by
# default; override with LEDGER_FILE=..., or disable with LEDGER=0.
LEDGER_SIG="${TIER_NAME}|${OUT_CODEC}|rmx${REMUX_TO_MP4:-0}|xc${COMPAT_TRANSCODE:-0}|${OUTPUT_MODE:-inplace}"
if [[ "${LEDGER:-1}" == "0" ]]; then
  LEDGER_FILE=""
else
  LEDGER_FILE="${LEDGER_FILE:-$SRC/.vtc_processed.log}"
  if ! ( : >> "$LEDGER_FILE" ) 2>/dev/null; then
    printf '\nWARN: cannot write resume ledger at %s — resume disabled for this run\n' "$LEDGER_FILE" >&2
    LEDGER_FILE=""
  fi
fi
export LEDGER_FILE LEDGER_SIG

TTY=/dev/tty
log(){ printf '%s\n' "$*" > "$TTY"; }
ts(){ date +%H:%M:%S; }
problem(){ printf '%s\t%s\n' "$2" "$1" >> "$PROBLEM_LOG"; }
record_saving(){ printf '%s\t%s\n' "$1" "$2" >> "$SAVINGS_LOG"; }
record_outcome(){ printf '%s\t%s\n' "$1" "$2" >> "$OUTCOME_LOG"; }

# ── Resume ledger ─────────────────────────────────────────────────────────────
# A completed file is recorded as "<settings-sig> TAB abspath TAB size TAB mtime".
# The settings signature means a re-run with the SAME tier/codec/options skips
# done files, while changing any of them re-evaluates everything. Appends are
# atomic for short lines, so parallel workers can share the one file.
ledger_key(){
  local f="$1" d b sz mt
  d="$(cd "$(dirname "$f")" 2>/dev/null && pwd -P)" || d="$(dirname "$f")"
  b="$(basename "$f")"
  sz="$(file_size "$f" 2>/dev/null || echo 0)"
  mt="$(stat -f%m "$f" 2>/dev/null || stat -c%Y "$f" 2>/dev/null || echo 0)"
  printf '%s\t%s/%s\t%s\t%s' "${LEDGER_SIG:-}" "$d" "$b" "${sz:-0}" "${mt:-0}"
}
ledger_has(){ [[ -n "${LEDGER_FILE:-}" && -f "${LEDGER_FILE:-}" ]] && grep -qxF -- "$1" "$LEDGER_FILE"; }
ledger_add(){ [[ -n "${LEDGER_FILE:-}" ]] && printf '%s\n' "$1" >> "$LEDGER_FILE" 2>/dev/null || true; }
fsize(){ du -sh "$1" 2>/dev/null | awk '{print $1}'; }
sname(){ local b; b="$(basename "$1")"; printf '%s' "${b%.*}"; }

check_stop_requested() {
  if [[ -f "$STOP_FILE" ]]; then
    log "$(ts) STOP  : stop file found ($STOP_FILE) — skipping remaining files"
    return 0
  fi
  return 1
}

# Progress-aware ffmpeg wrapper.
#   ff_run_progress LABEL DURATION_SECS START_TS [ffmpeg args...]
ff_run_progress() {
  local label="$1" dur_secs="$2" start_ts="$3"
  shift 3

  local _e _prog _rc
  _e=$(mktemp /tmp/fferr.XXXXXX)
  _prog=$(mktemp /tmp/ffprog.XXXXXX)

  local interval offset monitor_pid
  if [[ "${JOBS:-1}" -le 1 ]]; then
    interval=10; offset=0
  else
    interval=20
    offset=$(( $(printf '%s' "$label" | cksum | awk '{print $1}') % interval ))
  fi

  ( sleep "$offset"
    while true; do
      sleep "$interval"
      [[ -f "$_prog" ]] || break
      local out_time_us speed bitrate elapsed
      out_time_us=$(grep '^out_time_us=' "$_prog" 2>/dev/null | tail -1 | cut -d= -f2)
      speed=$(grep       '^speed='       "$_prog" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' ')
      bitrate=$(grep     '^bitrate='     "$_prog" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' ')
      elapsed=$(( $(date +%s) - start_ts ))
      if [[ -n "$out_time_us" && "$out_time_us" =~ ^[0-9]+$ && \
            -n "$dur_secs"    && "$dur_secs"    =~ ^[0-9]+$  && \
            "$dur_secs" -gt 0 ]]; then
        local done_secs=$(( out_time_us / 1000000 ))
        local pct
        pct=$(python3 -c "print(f'{min($done_secs/$dur_secs*100,100):.1f}')")
        local eta_str=""
        if [[ "$elapsed" -gt 2 && $(python3 -c "print(1 if $done_secs>0 else 0)") == "1" ]]; then
          local remain
          remain=$(python3 -c "
done=$done_secs; total=$dur_secs; el=$elapsed
if done>0:
    eta=int(el/done*(total-done))
    m,s=divmod(eta,60); h,m=divmod(m,60)
    print(f'{h}h{m:02d}m' if h else f'{m}m{s:02d}s')
else:
    print('?')
")
          eta_str="  ETA=${remain}"
        fi
        log "$(ts)  ...  : ${pct}%  speed=${speed:-?}  bitrate=${bitrate:-?}  elapsed=${elapsed}s${eta_str}  — ${label}"
      else
        log "$(ts)  ...  : elapsed=${elapsed}s  speed=${speed:-?}  bitrate=${bitrate:-?}  — ${label}"
      fi
    done ) &
  monitor_pid=$!

  "$FFMPEG" -nostdin -hide_banner -loglevel error \
    -progress "$_prog" -stats_period 2 \
    "$@" 2>"$_e"
  _rc=$?

  kill "$monitor_pid" 2>/dev/null
  wait "$monitor_pid" 2>/dev/null || true

  if [[ -s "$_e" && $_rc -ne 0 ]]; then
    while IFS= read -r _l; do
      _l=$(printf '%s' "$_l" | sed $'s/\033\\[[0-9;]*m//g')
      [[ -z "$_l" ]] && continue
      printf '%s\n' "$(ts)   ! ${_l}" > "$TTY"
    done < "$_e"
  fi
  rm -f "$_e" "$_prog"
  return "$_rc"
}

ff_run() {
  local _e _rc
  _e=$(mktemp /tmp/fferr.XXXXXX)
  "$FFMPEG" -nostdin -hide_banner -loglevel error "$@" 2>"$_e"
  _rc=$?
  if [[ -s "$_e" && $_rc -ne 0 ]]; then
    while IFS= read -r _l; do
      _l=$(printf '%s' "$_l" | sed $'s/\033\\[[0-9;]*m//g')
      [[ -z "$_l" ]] && continue
      printf '%s\n' "$(ts)   ! ${_l}" > "$TTY"
    done < "$_e"
  fi
  rm -f "$_e"
  return "$_rc"
}

probe_video() {
  local file="$1" field="$2"
  # cut -d, -f1: ffprobe's csv writer appends a trailing empty field when a
  # stream carries (even empty) side_data — e.g. "mpeg2video," instead of
  # "mpeg2video" — which would otherwise break exact case-match comparisons.
  "$FFPROBE" -v error -select_streams v:0 \
    -show_entries "stream=$field" -of csv=p=0 "$file" | head -1 | cut -d, -f1
}

probe_container_bitrate() {
  "$FFPROBE" -v error \
    -show_entries format=bit_rate \
    -of csv=p=0 "$1" | head -1
}

probe_format() {
  "$FFPROBE" -v error -show_entries format=format_name -of csv=p=0 "$1" | head -1
}

file_size() {
  stat -f%z "$1" 2>/dev/null || stat -c%s "$1"
}

calc_bitrate() {
  local src_bps="$1" width="$2" height="$3" fps="$4"
  local result
  result=$(python3 -c "
src_bps  = $src_bps
width    = $width
height   = $height
fps      = $fps if $fps > 0 else 30.0
src_kbps = src_bps / 1000.0
pixels   = width * height
# Tier is a fixed H.264 bits-per-pixel-per-frame density (from the 1080p30 anchor).
tier_bpp = $TIER_REF_MBPS * 1e6 / (1920 * 1080 * 30)
if '$OUT_CODEC' == 'h265':
    cf = $HEVC_EFF_HD if pixels <= 1920*1080 else ($HEVC_EFF_4K if pixels <= 3840*2160 else $HEVC_EFF_8K)
else:
    cf = 1.0
# Absolute target for this file's resolution & frame rate — not a ratio of source.
target = tier_bpp * pixels * fps * cf / 1000.0
target = max($BITRATE_FLOOR, target)
if src_kbps > 0: target = min(target, src_kbps)          # never inflate a source
srcbpp = src_bps / (pixels * fps) if (pixels > 0 and fps > 0) else 0
print(int(target))
print(f'srcbpp={srcbpp:.4f} cf={cf:.2f} src={src_kbps:.0f}k -> target={int(target)}k')
")
  local kbps bpp_info
  kbps="$(echo "$result" | sed -n '1p')"
  bpp_info="$(echo "$result" | sed -n '2p')"
  log "$(ts) BPP   : $bpp_info"
  echo "${kbps}k"
}

prescan_worth_encoding() {
  local file="$1" quiet="${2:-}"
  local n; n="$(sname "$file")"

  local src_bps width height fps_raw fps
  width="$(probe_video "$file" width)"
  height="$(probe_video "$file" height)"
  src_bps="$(probe_video "$file" bit_rate)"
  [[ -z "$src_bps" || "$src_bps" == "N/A" ]] && src_bps="$(probe_container_bitrate "$file")"
  [[ -z "$src_bps" || ! "$src_bps" =~ ^[0-9]+$ ]] && return 0

  fps_raw="$(probe_video "$file" r_frame_rate)"
  fps="$(python3 -c "
from fractions import Fraction
try: print(float(Fraction('${fps_raw:-30/1}')))
except: print(30.0)
")"

  local result decision info
  result="$(python3 -c "
src_bps = $src_bps; width = $width; height = $height; fps = $fps if $fps > 0 else 30.0
src_kbps = src_bps / 1000.0
pixels   = width * height
tier_bpp = $TIER_REF_MBPS * 1e6 / (1920 * 1080 * 30)
if '$OUT_CODEC' == 'h265':
    cf = $HEVC_EFF_HD if pixels <= 1920*1080 else ($HEVC_EFF_4K if pixels <= 3840*2160 else $HEVC_EFF_8K)
else:
    cf = 1.0
target = max($BITRATE_FLOOR, tier_bpp * pixels * fps * cf / 1000.0)
over   = (src_kbps / target) if target > 0 else 0
# Leave a file alone once it is at (or within tolerance of) its tier target;
# only re-encode a source that is meaningfully OVER target. This is what makes
# re-runs converge instead of shaving the file down again every pass.
if src_kbps <= target * $TIER_OVER_TOLERANCE:
    decision = 'skip-at-tier'
else:
    decision = 'encode'
print(decision)
print(f'src={src_kbps:.0f}k vs target={target:.0f}k ({over:.2f}x target)')
")"

  decision="$(echo "$result" | sed -n '1p')"
  info="$(echo "$result" | sed -n '2p')"

  if [[ "$decision" == "skip-at-tier" ]]; then
    [[ "$quiet" != "quiet" ]] && log "$(ts) SKIP  : already at/under ${TIER_NAME} target ($info) — $n"
    return 1
  fi
  return 0
}

# Sort the primary video codec into one of four handling categories:
#   h264   — the tool's core target: shrink if fat, else remux/skip
#   modern — efficient modern codec (HEVC/AV1/VP9): NEVER transcoded (that would
#            only cost a generation of quality); remuxed into MP4 if it's in
#            another container, otherwise left alone
#   legacy — MP4-incompatible / legacy codec (MPEG-2/VC-1/Xvid/WMV/…):
#            transcoded to the chosen codec at maximum fidelity (opt-in)
#   other  — mezzanine/unknown (ProRes, DNxHD, FFV1, raw, …): left untouched
classify_codec() {
  case "$1" in
    h264|avc)    echo h264 ;;
    hevc|av1|vp9) echo modern ;;
    mpeg2video|mpeg4|msmpeg4v1|msmpeg4v2|msmpeg4v3|msmpeg4|vc1|wmv1|wmv2|wmv3|flv1|rv30|rv40) echo legacy ;;
    *)           echo other ;;
  esac
}

# Wrapper: consult the resume ledger before doing any work, and record the file
# once it reaches a terminal (non-error) decision so a re-run skips it. The key
# is computed BEFORE processing (while the source still exists on disk).
process_one() {
  local src="$1" file="$2"
  local _lkey=""
  if [[ -n "${LEDGER_FILE:-}" ]]; then
    _lkey="$(ledger_key "$file")"
    ledger_has "$_lkey" && { log "$(ts) RESUME: already done this run's settings — $(sname "$file")"; record_outcome resume "$file"; return 0; }
  fi
  local _rc=0
  _process_one_impl "$src" "$file" || _rc=$?
  [[ "$_rc" -eq 0 && -n "$_lkey" ]] && ledger_add "$_lkey"
  return "$_rc"
}

_process_one_impl() {
  local src="$1" file="$2"

  local rel out
  rel="${file#"$src"/}"

  # Determine output path
  if [[ "${OUTPUT_MODE:-inplace}" == "separate" ]]; then
    if [[ "${OUTPUT_FLAT:-0}" == "1" ]]; then
      out="${OUTPUT_DIR}/$(basename "${rel%.*}").mp4"
    else
      out="${OUTPUT_DIR}/${rel%.*}.mp4"
    fi
  else
    out="${src}/${rel%.*}.mp4"
  fi

  local n; n="$(sname "$file")"

  mkdir -p "$(dirname "$out")"

  if check_stop_requested; then return 0; fi

  if [[ -f "$out" && "$out" != "$file" ]]; then
    log "$(ts) SKIP  : already converted — $n"
    record_outcome skip-existing "$file"
    return 0
  fi

  # ── Decide what to do with this file (MODE) ────────────────────────────────
  #   shrink    — fat H.264, re-encode down to the chosen tier (the core job)
  #   transcode — legacy/MP4-incompatible codec, re-encode into MP4 at max fidelity
  #   remux     — MP4-friendly codec in a non-MP4 container, copied losslessly
  #   (return)  — nothing worth doing
  local codec category ext already_mp4 MODE
  codec="$(probe_video "$file" codec_name)"
  category="$(classify_codec "$codec")"
  ext="$(printf '%s' "${file##*.}" | tr '[:upper:]' '[:lower:]')"
  already_mp4=0
  case "$ext" in mp4|m4v|mov) already_mp4=1 ;; esac

  case "$category" in
    h264)
      if [[ "${REMUX_TO_MP4:-0}" == "1" && "$already_mp4" -eq 0 ]]; then
        # Non-MP4 container: shrink if it's fat, otherwise remux losslessly into
        # MP4 rather than leave it in the less-compatible container.
        if prescan_worth_encoding "$file" quiet; then MODE=shrink; else MODE=remux; fi
      else
        # Already MP4 (or remux declined): only re-encode if it's worth it.
        if prescan_worth_encoding "$file"; then MODE=shrink; else record_outcome skip-at-tier "$file"; return 0; fi
      fi
      ;;
    modern)
      # HEVC/AV1/VP9 are never transcoded — only rehomed into MP4 losslessly.
      if [[ "${REMUX_TO_MP4:-0}" == "1" && "$already_mp4" -eq 0 ]]; then
        MODE=remux
      else
        log "$(ts) SKIP  : already ${codec} in MP4 — nothing to do — $n"
        record_outcome skip-modern "$file"
        return 0
      fi
      ;;
    legacy)
      if [[ "${COMPAT_TRANSCODE:-0}" == "1" ]]; then
        MODE=transcode
      else
        log "$(ts) SKIP  : ${codec} (MP4-incompatible; transcode declined) — $n"
        record_outcome skip-incompatible "$file"
        return 0
      fi
      ;;
    *)
      log "$(ts) SKIP  : codec=${codec} (left untouched) — $n"
      record_outcome skip-codec "$file"
      return 0
      ;;
  esac

  # Probe source
  local src_bps width height fps fps_raw dur_secs
  width="$(probe_video "$file" width)"
  height="$(probe_video "$file" height)"
  src_bps="$(probe_video "$file" bit_rate)"
  [[ -z "$src_bps" || "$src_bps" == "N/A" ]] && src_bps="$(probe_container_bitrate "$file")"

  fps_raw="$(probe_video "$file" r_frame_rate)"
  fps="$(python3 -c "
from fractions import Fraction
try:    print(float(Fraction('${fps_raw:-30/1}')))
except: print(30.0)
")"

  local dur_raw
  dur_raw="$("$FFPROBE" -v error -show_entries format=duration \
    -of csv=p=0 "$file" 2>/dev/null | head -1)"
  if [[ "$dur_raw" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    dur_secs="$(python3 -c "print(int(float('$dur_raw')))")"
  else
    dur_secs=0
  fi

  # ── Subtitles ──────────────────────────────────────────────────────────────
  # Text-based tracks (SRT/ASS/...) can be embedded in MP4 as mov_text, or
  # extracted to sidecar .srt files. Image-based tracks (PGS/DVD/DVB) can do
  # neither without OCR — if any would be lost, the original must survive.
  local subs_probe text_sub_idx="" text_sub_count=0 image_sub_count=0 image_sub_codecs=""
  subs_probe="$("$FFPROBE" -v error -select_streams s \
    -show_entries stream=index,codec_name -of csv=p=0 "$file" 2>/dev/null || true)"
  local _sidx _scodec
  while IFS=, read -r _sidx _scodec; do
    [[ -z "$_sidx" ]] && continue
    case "$_scodec" in
      subrip|srt|ass|ssa|mov_text|webvtt|text)
        text_sub_idx="${text_sub_idx:+$text_sub_idx }$_sidx"
        text_sub_count=$(( text_sub_count + 1 )) ;;
      *)
        image_sub_count=$(( image_sub_count + 1 ))
        image_sub_codecs="${image_sub_codecs:+$image_sub_codecs, }${_scodec:-unknown}" ;;
    esac
  done <<< "$subs_probe"
  if [[ "$text_sub_count" -gt 0 || "$image_sub_count" -gt 0 ]]; then
    log "$(ts) SUBS  : found ${text_sub_count} text / ${image_sub_count} image subtitle track(s) — $n"
  fi

  # Target bitrate depends on MODE. shrink: the tuned tier target. transcode:
  # capped at the SOURCE's own bitrate (max fidelity — never inflate a legacy
  # source). remux: not needed (video is stream-copied).
  local bitrate=""
  if [[ "$MODE" == "shrink" ]]; then
    if [[ -n "$src_bps" && "$src_bps" =~ ^[0-9]+$ ]]; then
      bitrate="$(calc_bitrate "$src_bps" "$width" "$height" "$fps")"
    else
      bitrate="$(python3 -c "
w=$width; h=$height; fps=$fps if $fps > 0 else 30.0
pixels=w*h
tier_bpp=$TIER_REF_MBPS*1e6/(1920*1080*30)
if '$OUT_CODEC' == 'h265':
    cf = $HEVC_EFF_HD if pixels <= 1920*1080 else ($HEVC_EFF_4K if pixels <= 3840*2160 else $HEVC_EFF_8K)
else:
    cf = 1.0
target=max($BITRATE_FLOOR, tier_bpp*pixels*fps*cf/1000.0)
print(f'{int(target)}k')
")"
      log "$(ts) WARN  : could not probe bitrate, using tier target ($bitrate) — $n"
      problem "$file" "WARN: could not probe source bitrate; used tier target (${bitrate}) — verify output quality"
    fi
  elif [[ "$MODE" == "transcode" ]]; then
    if [[ -n "$src_bps" && "$src_bps" =~ ^[0-9]+$ ]]; then
      bitrate="$(( src_bps / 1000 ))k"
    else
      bitrate="$( [[ "${width:-0}" -ge 3840 ]] && echo "${XCODE_CAP_4K}k" || echo "${XCODE_CAP_HD}k" )"
      log "$(ts) WARN  : could not probe bitrate, using max-fidelity cap ($bitrate) — $n"
      problem "$file" "WARN: could not probe source bitrate; used max-fidelity cap (${bitrate})"
    fi
  fi

  local pix_fmt profile
  pix_fmt="$(probe_video "$file" pix_fmt)"
  if [[ "$OUT_CODEC" == "h264" ]]; then
    profile="high"      # 8-bit High for maximum player compatibility
  else
    case "$pix_fmt" in
      *10le|*10be|*10*) profile="main10" ;;
      *)                profile="main"   ;;
    esac
  fi

  local start base tmp src_size
  start="$(date +%s)"
  base="$(basename "${out%.mp4}")"
  tmp="$TMPROOT/${base}.$$.$RANDOM.mp4"
  src_size="$(file_size "$file")"

  # Remove this worker's scratch file if it is interrupted mid-encode (Ctrl-C,
  # graceful stop, TERM) so partial temps never accumulate. A successful encode
  # moves $tmp to $out first, making this a harmless no-op.
  trap 'rm -f "$tmp"' EXIT INT TERM

  log "$(ts) START : [$(fsize "$file")]  $n"
  log "$(ts) INFO  : ${width}x${height}  fps=$fps  pix=$pix_fmt — $n"

  # Build the video-encoder args.
  #   remux     — stream-copy the video (lossless, instant).
  #   shrink    — capped-CRF at the tuned tier ceiling.
  #   transcode — higher-fidelity CRF + slow preset, capped at the source bitrate.
  # Hardware (VideoToolbox) has no true CRF, so it targets -b:v instead.
  local use_vt="${USE_VT:-0}"

  local -a vargs
  if [[ "$MODE" == "remux" ]]; then
    vargs=(-c:v copy)
    [[ "$codec" == "hevc" ]] && vargs+=(-tag:v hvc1)   # help Apple players recognise it
    log "$(ts) REMUX : ${codec} copied into MP4, lossless — $n"
  else
    local br_k bufsize crf264 crf265 preset MODE_TAG
    br_k="${bitrate%k}"
    bufsize=$(( br_k * 2 ))
    if [[ "$MODE" == "transcode" ]]; then
      crf264=18; crf265=20; preset=slow;   MODE_TAG="XCODE "
    else
      crf264=20; crf265=21; preset=medium; MODE_TAG="ENCODE"
    fi
    if [[ "$OUT_CODEC" == "h264" ]]; then
      if [[ "$use_vt" -eq 1 ]]; then
        vargs=(-c:v h264_videotoolbox -b:v "$bitrate" -profile:v high -pix_fmt yuv420p)
        log "$(ts) ${MODE_TAG}: VideoToolbox H.264  target=$bitrate — $n"
      else
        vargs=(-c:v libx264 -crf "$crf264" -preset "$preset" -maxrate "$bitrate" -bufsize "${bufsize}k" \
               -profile:v high -pix_fmt yuv420p)
        log "$(ts) ${MODE_TAG}: libx264 crf=$crf264 $preset  cap=$bitrate — $n"
      fi
    else
      if [[ "$use_vt" -eq 1 ]]; then
        vargs=(-c:v hevc_videotoolbox -b:v "$bitrate" -profile:v "$profile" -tag:v hvc1 -bf 0 -fps_mode cfr)
        log "$(ts) ${MODE_TAG}: VideoToolbox H.265  target=$bitrate  profile=$profile — $n"
      else
        vargs=(-c:v libx265 -crf "$crf265" -preset "$preset" -maxrate "$bitrate" -bufsize "${bufsize}k" \
               -profile:v "$profile" -tag:v hvc1)
        log "$(ts) ${MODE_TAG}: libx265 crf=$crf265 $preset  cap=$bitrate  profile=$profile — $n"
      fi
    fi
  fi

  # Attempt matrix: embed text subs first (as mov_text), retry without subs only
  # if that fails; within each, try audio stream-copy first, then AAC fallback.
  # A normal file still takes exactly one attempt.
  local encode_ok=0 subs_embedded=0 sub_mode audio_mode
  local sub_attempts="none"
  [[ "$text_sub_count" -gt 0 ]] && sub_attempts="embed none"

  for sub_mode in $sub_attempts; do
    local sub_maps=() sub_flags=()
    if [[ "$sub_mode" == "embed" ]]; then
      for _sidx in $text_sub_idx; do sub_maps+=(-map "0:${_sidx}"); done
      sub_flags=(-c:s mov_text)
    fi
    for audio_mode in copy aac; do
      local audio_flags=()
      if [[ "$audio_mode" == "copy" ]]; then
        audio_flags=(-c:a copy)
      else
        log "$(ts) RETRY : audio copy failed, falling back to AAC — $n"
        audio_flags=(-c:a aac -b:a 384k)
      fi

      if ff_run_progress "$n" "$dur_secs" "$start" \
          -y -i "$file" \
          -map 0:v:0 -map 0:a? ${sub_maps[@]+"${sub_maps[@]}"} \
          "${vargs[@]}" \
          "${audio_flags[@]}" ${sub_flags[@]+"${sub_flags[@]}"} \
          -movflags +faststart \
          "$tmp"; then
        encode_ok=1
        [[ "$sub_mode" == "embed" ]] && subs_embedded=1
        break 2
      fi
      rm -f "$tmp"
    done
    if [[ "$sub_mode" == "embed" ]]; then
      log "$(ts) RETRY : embedding subtitles failed, retrying without — $n"
    fi
  done

  if [[ "$encode_ok" -eq 0 ]]; then
    log "$(ts) ERROR : encode failed — $n"
    problem "$file" "ERROR: encode failed (both audio-copy and AAC fallback) — file skipped"
    record_outcome error "$file"
    return 1
  fi

  local out_size threshold
  out_size="$(file_size "$tmp")"
  threshold=$(python3 -c "print(int($src_size * $MIN_SAVING_RATIO))")

  local end elapsed
  end="$(date +%s)"
  elapsed=$(( end - start ))

  # The minimum-saving gate only applies to a shrink re-encode. A transcode
  # (compatibility + max fidelity) and a remux (lossless container change) are
  # kept regardless of size — their purpose is compatibility, not shrinkage.
  if [[ "$MODE" == "shrink" ]]; then
    log "$(ts) SIZE  : $(( src_size / 1024 / 1024 ))MB → $(( out_size / 1024 / 1024 ))MB  (need < $(( threshold / 1024 / 1024 ))MB) — $n"
    if [[ "$out_size" -ge "$threshold" ]]; then
      rm -f "$tmp"
      log "$(ts) SKIP  : saving too small, keeping original — $n  [${elapsed}s]"
      record_outcome skip-min-saving "$file"
      return 0
    fi
  else
    log "$(ts) SIZE  : $(( src_size / 1024 / 1024 ))MB → $(( out_size / 1024 / 1024 ))MB — $n"
  fi

  # Sidecar fallback: embedding failed but the encode succeeded — extract each
  # text track from the SOURCE (must run before it can be overwritten below) to
  # .srt files next to the output; Plex/VLC/Infuse pick these up automatically.
  local sidecars_made=0 subs_sidecar_fail=0
  if [[ "$text_sub_count" -gt 0 && "$subs_embedded" -eq 0 ]]; then
    local k=0 _lang sidecar
    for _sidx in $text_sub_idx; do
      k=$(( k + 1 ))
      _lang="$("$FFPROBE" -v error -select_streams "$_sidx" \
        -show_entries stream_tags=language -of csv=p=0 "$file" 2>/dev/null | head -1)"
      _lang="${_lang:-und}"
      if [[ "$text_sub_count" -eq 1 ]]; then
        sidecar="${out%.mp4}.${_lang}.srt"
      else
        sidecar="${out%.mp4}.${k}.${_lang}.srt"
      fi
      if ff_run -y -i "$file" -map "0:${_sidx}" -c:s srt "$sidecar"; then
        sidecars_made=$(( sidecars_made + 1 ))
      else
        rm -f "$sidecar"
        subs_sidecar_fail=$(( subs_sidecar_fail + 1 ))
      fi
    done
    if [[ "$sidecars_made" -gt 0 ]]; then
      log "$(ts) SUBS  : could not embed — extracted ${sidecars_made} sidecar .srt file(s) — $n"
      problem "$file" "NOTE: ${sidecars_made} text subtitle track(s) written as sidecar .srt files (could not be embedded in the MP4)"
    fi
  fi

  # Anything about to be irrecoverably lost? Image subs never make it into MP4;
  # text subs that failed both embed and sidecar extraction are lost too.
  local subs_dropped_reason=""
  if [[ "$image_sub_count" -gt 0 ]]; then
    subs_dropped_reason="${image_sub_count} image-based subtitle track(s) (${image_sub_codecs}) cannot be carried into MP4"
  fi
  if [[ "$subs_sidecar_fail" -gt 0 ]]; then
    subs_dropped_reason="${subs_dropped_reason:+$subs_dropped_reason; }${subs_sidecar_fail} text subtitle track(s) could not be embedded or extracted"
  fi

  # Rescue: when the output overwrites the source path directly, the original
  # (and its subtitle tracks) would vanish at the mv below — move it to the
  # archive first, whatever the chosen source action.
  local rescued_dir=""
  if [[ -n "$subs_dropped_reason" && "$file" == "$out" ]]; then
    local fb_root fb_rel fb_dest
    fb_root="${ARCHIVE_DIR:-$src/archived}"
    fb_rel="$(dirname "$rel")"; [[ "$fb_rel" == "." ]] && fb_rel=""
    fb_dest="${fb_root}${fb_rel:+/$fb_rel}"
    mkdir -p "$fb_dest"
    mv -f "$file" "$fb_dest/$(basename "$file")"
    rescued_dir="$fb_dest"
  fi

  mv -f "$tmp" "$out"

  if [[ -s "$out" ]]; then
    record_saving "$src_size" "$out_size"
    record_outcome "$MODE" "$file"
    case "${SOURCE_ACTION:-archive}" in
      delete)
        if [[ -n "$subs_dropped_reason" ]]; then
          if [[ -z "$rescued_dir" ]]; then
            local fb_root fb_rel
            fb_root="${ARCHIVE_DIR:-$src/archived}"
            fb_rel="$(dirname "$rel")"; [[ "$fb_rel" == "." ]] && fb_rel=""
            rescued_dir="${fb_root}${fb_rel:+/$fb_rel}"
            mkdir -p "$rescued_dir"
            mv -f "$file" "$rescued_dir/$(basename "$file")"
          fi
          log "$(ts) DONE  : ${elapsed}s  [$(fsize "$out")]  $n  (original ARCHIVED, not deleted — subtitles would be lost)"
          problem "$file" "NOTE: original archived to ${rescued_dir} instead of deleted — ${subs_dropped_reason}"
        else
          [[ "$file" != "$out" ]] && rm -f "$file"
          log "$(ts) DONE  : ${elapsed}s  [$(fsize "$out")]  $n  (original deleted)"
        fi
        ;;
      archive)
        local arc_rel arc_dest
        arc_rel="$(dirname "$rel")"; [[ "$arc_rel" == "." ]] && arc_rel=""
        arc_dest="${ARCHIVE_DIR}${arc_rel:+/$arc_rel}"
        mkdir -p "$arc_dest"
        [[ "$file" != "$out" && -f "$file" ]] && mv -f "$file" "$arc_dest/$(basename "$file")"
        log "$(ts) DONE  : ${elapsed}s  [$(fsize "$out")]  $n  (original → $(basename "$ARCHIVE_DIR"))"
        if [[ -n "$subs_dropped_reason" ]]; then
          problem "$file" "NOTE: output MP4 is missing subtitle track(s) — ${subs_dropped_reason}; the archived original still has them"
        fi
        ;;
      keep)
        if [[ -n "$rescued_dir" ]]; then
          log "$(ts) DONE  : ${elapsed}s  [$(fsize "$out")]  $n  (original moved to archive — subtitles would be lost)"
          problem "$file" "NOTE: original moved to ${rescued_dir} (it was being overwritten in place) — ${subs_dropped_reason}"
        else
          log "$(ts) DONE  : ${elapsed}s  [$(fsize "$out")]  $n"
          if [[ -n "$subs_dropped_reason" ]]; then
            problem "$file" "NOTE: output MP4 is missing subtitle track(s) — ${subs_dropped_reason}; the original (kept in place) still has them"
          fi
        fi
        ;;
    esac
  else
    log "$(ts) WARN  : output empty after encode — $n"
    problem "$file" "WARN: output missing or empty after encode — source not deleted"
    rm -f "$out" 2>/dev/null || true
    record_outcome error "$file"
    return 1
  fi
}

export -f process_one _process_one_impl classify_codec prescan_worth_encoding probe_video probe_container_bitrate \
           probe_format file_size calc_bitrate log ts ff_run ff_run_progress problem record_saving record_outcome \
           fsize sname check_stop_requested ledger_key ledger_has ledger_add
export FFMPEG FFPROBE TMPROOT SOURCE_ACTION ARCHIVE_DIR \
       OUTPUT_MODE OUTPUT_DIR OUTPUT_FLAT PROBLEM_LOG SAVINGS_LOG OUTCOME_LOG STOP_FILE TTY
export BITRATE_FLOOR TIER_OVER_TOLERANCE MIN_SAVING_RATIO
export HEVC_EFF_HD HEVC_EFF_4K HEVC_EFF_8K XCODE_CAP_HD XCODE_CAP_4K
export OUT_CODEC TIER_NAME TIER_REF_MBPS
export LEDGER_FILE LEDGER_SIG
export REMUX_TO_MP4 COMPAT_TRANSCODE

# (plain case block — a case inside $() breaks macOS bash 3.2's parser)
case "$SOURCE_ACTION" in
  delete) _orig_desc="delete" ;;
  keep)   _orig_desc="keep" ;;
  *)      _orig_desc="archive → $ARCHIVE_DIR" ;;
esac

log ""
log "SRC:         $SRC"
log "SCAN:        ${VIDEO_EXTS_LABEL}"
log "REMUX→MP4:   $( [[ "${REMUX_TO_MP4:-0}" -eq 1 ]] && echo "yes — H.264/H.265/AV1/VP9 in other containers copied losslessly into MP4" || echo "no" )"
log "TRANSCODE:   $( [[ "${COMPAT_TRANSCODE:-0}" -eq 1 ]] && echo "yes — MP4-incompatible codecs (MPEG-2/VC-1/Xvid/WMV) re-encoded at max fidelity" || echo "no — incompatible codecs left untouched" )"
log "OUTPUT:      $( [[ "$OUTPUT_MODE" == "separate" ]] && echo "$OUTPUT_DIR ($( [[ $OUTPUT_FLAT == 1 ]] && echo flat || echo mirrored ))" || echo "in place" )"
log "ORIGINALS:   $_orig_desc"
log "JOBS:        $JOBS"
log "CODEC:       $( [[ "$OUT_CODEC" == "h264" ]] && echo "H.264 / AVC (universal playback, ~2x larger)" || echo "H.265 / HEVC (~50% smaller at the same quality)" )"
log "ENCODER:     $( [[ "${USE_VT:-0}" -eq 1 ]] && echo "Hardware (VideoToolbox)" || echo "Software (libx264/libx265, capped-CRF)" )"
log "TIER:        ${TIER_NAME} — ${TIER_REF_MBPS} Mbps H.264 @1080p30$( [[ "$OUT_CODEC" == "h265" ]] && python3 -c "print(f' (~{$TIER_REF_MBPS*$HEVC_EFF_HD:.1f} Mbps H.265 @1080p)', end='')" ), scaling with resolution & frame rate"
log "BITRATE:     absolute target = tier bpp × pixels × fps$( [[ "$OUT_CODEC" == "h265" ]] && echo " × ${HEVC_EFF_HD}/${HEVC_EFF_4K}/${HEVC_EFF_8K} (HD/4K/8K)" ) | floor=${BITRATE_FLOOR}k"
log "RE-ENCODE:   only sources >$(python3 -c "print(int(($TIER_OVER_TOLERANCE-1)*100))")% over their tier target (files already at target are left alone)"
log "MIN_SAVING:  a shrink is kept only if its output is ≥$(python3 -c "print(int((1-$MIN_SAVING_RATIO)*100))")% smaller than the source"
log "RESUME:      $( [[ -n "${LEDGER_FILE:-}" ]] && echo "ledger $LEDGER_FILE" || echo "disabled" )"
log "SUBTITLES:   text subs embedded (mov_text) or extracted to .srt; if image subs (PGS/DVD)"
log "             would be lost, the original is archived — even in delete mode"
log "STOP:        touch $STOP_FILE   (finish current file, skip the rest)"
log "             rm $STOP_FILE      (clear stop flag to re-run)"
log ""

find "$SRC" \
  \( -name '.Trashes' -o -name '.Spotlight-V100' -o -name '.fseventsd' -o -name '.TemporaryItems' \
     -o -name 'originals' -o -name 'new versions' -o -name 'archived' -o -name 'Library' \) -prune \
  -o \( -type f -not -name '._*' \( "${FIND_INAME_ARGS[@]}" \) -print0 \) \
  | xargs -0 -n 1 -P "$JOBS" bash -c 'process_one "$@"' _ "$SRC" || true
# `|| true`: a failed encode makes process_one (and so xargs) exit non-zero,
# which under set -e would kill the script here — before the run report prints.

# ── Run summary ───────────────────────────────────────────────────────────────
# Account for EVERY scanned file by outcome (each appended one row to OUTCOME_LOG),
# so a run over already-optimal files reports what it did instead of looking idle.
if [[ -s "$OUTCOME_LOG" ]]; then
  log ""
  python3 - "$OUTCOME_LOG" <<'PY' > "$TTY"
import sys
from collections import Counter
c = Counter()
with open(sys.argv[1]) as fh:
    for line in fh:
        line = line.rstrip('\n')
        if line:
            c[line.split('\t', 1)[0]] += 1
total = sum(c.values())
rows = [
    ("re-encoded (shrunk)",                "shrink"),
    ("transcoded (compatibility)",         "transcode"),
    ("remuxed into MP4 (lossless)",        "remux"),
    ("left as-is: already at tier",        "skip-at-tier"),
    ("left as-is: already H.265/AV1/VP9",  "skip-modern"),
    ("left as-is: output already existed", "skip-existing"),
    ("left as-is: saving too small",       "skip-min-saving"),
    ("left as-is: incompatible codec",     "skip-incompatible"),
    ("left as-is: unsupported codec",      "skip-codec"),
    ("skipped: already done (resume)",     "resume"),
    ("ERRORS (source untouched)",          "error"),
]
changed = c.get('shrink', 0) + c.get('transcode', 0) + c.get('remux', 0)
errors  = c.get('error', 0)
left    = total - changed - errors
print("══════════════════════════════════════════════════════════")
print(f" RUN SUMMARY — {total} file(s) scanned")
print(f"   changed {changed}    left as-is {left}    errors {errors}")
print("  ────────────────────────────────────────────────────────")
for label, key in rows:
    if c.get(key, 0):
        print(f"   {label:<36s} {c[key]:>4d}")
print("══════════════════════════════════════════════════════════")
PY
fi

# ── Total space savings ───────────────────────────────────────────────────────
# Sum src/out bytes over every file we actually replaced (each appended a row to
# SAVINGS_LOG). Remuxes contribute ~0; shrinks/transcodes carry the real savings.
if [[ -s "$SAVINGS_LOG" ]]; then
  log ""
  python3 - "$SAVINGS_LOG" <<'PY' > "$TTY"
import sys
src=out=n=0
with open(sys.argv[1]) as fh:
    for line in fh:
        line=line.strip()
        if not line: continue
        a,b=line.split('\t')
        src+=int(a); out+=int(b); n+=1
def h(x):
    for unit in ('B','KB','MB','GB','TB'):
        if x < 1024 or unit=='TB':
            return f'{x:.1f} {unit}'
        x/=1024
saved=src-out
pct=(saved/src*100) if src else 0
print("══════════════════════════════════════════════════════════")
print(f" SPACE SAVED — {n} file(s) replaced")
print(f"   original:  {h(src)}")
print(f"   new:       {h(out)}")
print(f"   saved:     {h(saved)}  ({pct:.1f}%)")
print("══════════════════════════════════════════════════════════")
PY
fi

if [[ -s "$PROBLEM_LOG" ]]; then
  count="$(wc -l < "$PROBLEM_LOG" | tr -d ' ')"
  log ""
  log "══════════════════════════════════════════════════════════"
  log " RUN REPORT — ${count} file(s) with notes, warnings or errors"
  log "   NOTE  = informational (e.g. why an original was archived)"
  log "   WARN  = worth a manual check"
  log "   ERROR = file could not be converted; source untouched"
  log "══════════════════════════════════════════════════════════"
  while IFS=$'\t' read -r reason file; do
    log "  [${reason}]"
    log "    ${file}"
    log ""
  done < "$PROBLEM_LOG"
  log "══════════════════════════════════════════════════════════"
else
  log ""
  log "All files processed without errors."
fi
log "Done."
