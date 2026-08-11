"""pywebview desktop shell — loads the HTML design and drives the real engine.

    pip install pywebview
    python -m vtc.webapp [/path/to/vtc_app_v3.html]

The design HTML stays a self-contained mockup (opening it in a browser runs on
mock data). When it runs *inside* this shell, a small JS bridge is injected that
feature-detects `window.pywebview` and swaps the three mock seams for real engine
calls:

    gate folder-pick   -> Api.pick_folder()  (native dialog + real scan)
    drawEstimate()      -> Api.estimate(answers)  (pipeline.plan math on probed files)
    the run             -> Api.run(answers)   (pipeline.run streamed back per file)

The engine (pipeline/model/config) is untouched and UI-agnostic.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile

from . import encode, netmove, pipeline
from .config import AudioPolicy, Container, Encoder, OutputMode, RunConfig, SourceAction
from .ffprobe import probe
from .model import OutCodec, Tier, target_kbps
from .result import Mode, Outcome
from .winproc import NO_WINDOW, TEXT_UTF8, reconfigure_std_streams


# ── debug log ────────────────────────────────────────────────────────────────
# A packaged app has no console, so everything of interest goes to a rotating-ish
# file the user can hand back when something misbehaves. Path is logged on startup.
log = logging.getLogger("vtc.app")


def _log_path() -> Path:
    if sys.platform == "darwin":
        d = Path.home() / "Library" / "Logs"
    elif os.name == "nt":
        d = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VeryThoughtfulCompression"
    else:
        d = Path.home() / ".local" / "state" / "vtc"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        d = Path(tempfile.gettempdir())
    return d / "VeryThoughtfulCompression.log"


def _session_path() -> Path:
    """Where the in-progress run is remembered so a crash / force-quit / reboot mid-run
    can be resumed. Sits next to the log (a persistent dir, NOT temp — it must survive
    a reboot)."""
    return _log_path().with_name("vtc_session.json")


def _setup_logging() -> Path:
    path = _log_path()
    if not log.handlers:
        log.setLevel(logging.DEBUG)
        try:
            fh = logging.FileHandler(path, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
            log.addHandler(fh)
        except OSError:
            log.addHandler(logging.NullHandler())
    return path


def _install_crash_handlers(log_path: Path) -> None:
    """Capture the ways the app can die that the normal logger misses: uncaught
    exceptions on the main thread and on worker threads, and native faults
    (segfaults / fatal signals) via faulthandler. Without these, a crash just ends
    the log with no reason — which is exactly what we saw."""
    def _main_hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb); return
        log.critical("UNCAUGHT (main thread)", exc_info=(exc_type, exc, tb))
    sys.excepthook = _main_hook

    def _thread_hook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        log.critical("UNCAUGHT (thread %s)", getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    try:
        threading.excepthook = _thread_hook
    except Exception:  # noqa: BLE001
        pass

    # native faults -> a Python traceback dumped to a sidecar file (kept open)
    try:
        import faulthandler
        fault_path = log_path.with_name("VeryThoughtfulCompression-crash.log")
        global _FAULT_FILE
        _FAULT_FILE = open(fault_path, "a", encoding="utf-8")   # noqa: SIM115 — must stay open
        _FAULT_FILE.write(f"\n=== faulthandler armed ===\n")
        _FAULT_FILE.flush()
        faulthandler.enable(file=_FAULT_FILE, all_threads=True)
        log.info("crash handlers installed (faults -> %s)", fault_path)
    except Exception as e:  # noqa: BLE001
        log.warning("faulthandler unavailable: %s", e)


_FAULT_FILE = None


def _log_environment() -> None:
    """Record what the app resolved to and whether hardware encoding is real."""
    log.info("=== Very Thoughtful Compression starting ===")
    log.info("python %s on %s (%s)", platform.python_version(),
             platform.platform(), platform.machine())
    log.info("frozen=%s  resource_base=%s", bool(getattr(sys, "_MEIPASS", None)), _resource_base())
    for label, tool in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)):
        ver = "?"
        try:
            ver = subprocess.run([tool, "-version"], capture_output=True, text=True,
                                 timeout=15, **TEXT_UTF8, **NO_WINDOW).stdout.splitlines()[0]
        except Exception as e:  # noqa: BLE001
            ver = f"<could not run: {e}>"
        log.info("%s -> %s  [%s]", label, tool, ver)
    log.info("hardware encoders: %s", encode.hardware_report(FFMPEG))


# ── bundle-aware resource + tool resolution ──────────────────────────────────
# The packaged app (PyInstaller) ships the HTML and a static ffmpeg/ffprobe
# beside the executable; a source / `pip install` run finds the HTML next to
# this module and ffmpeg/ffprobe on PATH. One resolver covers both.
def _resource_base() -> Path:
    base = getattr(sys, "_MEIPASS", None)          # set only inside a frozen app
    return Path(base) if base else Path(__file__).resolve().parent


def _resolve_tool(name: str, env_var: str) -> str:
    """bundled binary -> $ENV override -> PATH -> bare name (dev fallback)."""
    exe = name + (".exe" if os.name == "nt" else "")
    bundled = _resource_base() / exe
    if bundled.exists():
        return str(bundled)
    override = os.environ.get(env_var)
    if override and Path(override).exists():
        return override
    return shutil.which(name) or name


def _bundled_html() -> Path:
    return _resource_base() / "vtc_app_v3.html"


FFMPEG = _resolve_tool("ffmpeg", "FFMPEG_BINARY")
FFPROBE = _resolve_tool("ffprobe", "FFPROBE_BINARY")


def _ffmpeg_version() -> str:
    """Short ffmpeg version for the toolbar readout, e.g. '8.1.2'. Best-effort."""
    try:
        out = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=5,
                             **TEXT_UTF8, **NO_WINDOW).stdout
        m = re.search(r"ffmpeg version (\S+)", out)
        if m:
            return m.group(1).split("-")[0]
    except Exception:  # noqa: BLE001
        pass
    return "?"


# ── preview server: serve generated sample encodes to the webview <video>s ────
# A tiny loopback HTTP server with Range (206) support — WKWebView will not play
# a <video> without it. Rooted at a temp dir the previews are written into.
import functools
import http.server
import socketserver

_preview_dir: Path | None = None
_preview_port: int | None = None


class _RangeHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):  # noqa: N802
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        size = os.path.getsize(path)
        ctype = self.guess_type(path)
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                s, e = rng[6:].split("-", 1)
                start = int(s) if s else 0
                end = int(e) if e else size - 1
            except ValueError:
                start, end = 0, size - 1
            start = max(0, start)
            end = min(end, size - 1)
            length = max(0, end - start + 1)
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(path, "rb") as f:
                try:
                    shutil.copyfileobj(f, self.wfile)
                except (BrokenPipeError, ConnectionResetError):
                    pass


def _ensure_preview_server() -> tuple[Path, int]:
    global _preview_dir, _preview_port
    if _preview_dir is not None and _preview_port is not None:
        return _preview_dir, _preview_port
    _preview_dir = Path(tempfile.mkdtemp(prefix="vtc_prev_"))
    handler = functools.partial(_RangeHandler, directory=str(_preview_dir))
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    _preview_port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("preview server on 127.0.0.1:%s -> %s", _preview_port, _preview_dir)
    return _preview_dir, _preview_port


# The variations shown side by side: the untouched SOURCE, then each quality tier.
_PREVIEW_PANELS = [
    ("source", "Source", None),
    ("ok", "OK", Tier.OK),
    ("good", "Good", Tier.GOOD),
    ("excellent", "Excellent", Tier.EXCELLENT),
    ("stellar", "Stellar", Tier.STELLAR),
    ("insane", "Insane", Tier.INSANE),
]


# ── mockup answer-index -> engine value (mirrors the M model in the HTML) ─────
_CODECS = [OutCodec.H264, OutCodec.H265, None, None]    # 0=H264, 1=H265, 2=AV1, 3=VVC (2/3 unsupported)
# Default codec for the PREVIEW samples (independent of the chosen OUTPUT codec):
# the mac webview plays H.265, but Windows WebView2 has no HEVC decoder, so preview
# in H.264 there or the tier panels are black. The output codec is unaffected.
_PREVIEW_CODEC = OutCodec.H265 if sys.platform == "darwin" else OutCodec.H264
_TIERS = [Tier.OK, Tier.GOOD, Tier.EXCELLENT, Tier.STELLAR, Tier.INSANE]
# The progress list finds the CURRENT file inside the queue it was given, so a
# truncated queue means everything past the cut-off loses its "processing" row and
# its upcoming files. Ship the whole queue, but in chunks — one evaluate_js call
# carrying 20k filenames is a megabyte-plus string.
_QUEUE_CHUNK = 2000
_QUEUE_MAX = 50_000                     # backstop for an absurd library
_PX_1080P = 1920 * 1080                 # the frame the encoder speeds are quoted at
_SAVING = [0.15, 0.25, 0.40]
_ENCODER = [Encoder.HARDWARE, Encoder.SOFTWARE]
# non-MP4 policy: ADV.format -> the four container flags. Convert = to MP4 (remux +
# transcode legacy); Remux = lossless to MP4 only; Shrink-keep = apply the quality
# shrink but keep the source container; Leave = don't touch non-MP4 containers.
#             format          remux, transcode, keep_container, leave_non_mp4
_FORMAT = {
    "convert":     (True,  True,  False, False),
    "remux":       (True,  False, False, False),
    "shrink_keep": (False, False, True,  False),
    "leave":       (False, False, False, True),
}


def build_config(src: Path, a: dict) -> RunConfig:
    """Map the mockup's `answers` (question id -> chosen index) to a RunConfig."""
    codec = _CODECS[a["codec"]]
    if codec is None:
        raise ValueError("AV1 output is not supported by the engine yet")
    adv = a.get("adv") or {}
    remux, transcode, keep_container, leave = _FORMAT.get(adv.get("format"), _FORMAT["convert"])
    dest = a["dest"]  # 0 archive, 1 delete, 2 new folder
    if dest == 2:
        # New folder: use the folder the user picked, else <src>/converted.
        chosen = a.get("outputDir")
        out_dir = Path(chosen) if chosen else src / "converted"
        output_mode, source_action, output_dir = OutputMode.SEPARATE, SourceAction.KEEP, out_dir
    else:
        output_mode, output_dir = OutputMode.INPLACE, None
        source_action = SourceAction.ARCHIVE if dest == 0 else SourceAction.DELETE
    cfg = RunConfig(
        src=src, out_codec=codec, tier=_TIERS[a["quality"]],
        min_saving_ratio=1.0 - _SAVING[a["saving"]],
        remux_to_mp4=remux, compat_transcode=transcode,
        keep_source_container=keep_container, leave_non_mp4=leave,
        encoder=_ENCODER[a["encoder"]],
        output_mode=output_mode, output_dir=output_dir, source_action=source_action,
        ffmpeg=FFMPEG, ffprobe=FFPROBE,
    )
    # Files the user ticked for the slow encoder. Sent as resolved paths, so the
    # engine can match them without re-deriving anything.
    picked = a.get("softwareFiles")
    if isinstance(picked, list):
        cfg.software_files = frozenset(str(p) for p in picked if p)
    # Subtitle policy from Advanced settings: language and kind are independent
    # filters (see RunConfig.sub_langs / sub_kinds).
    langs = adv.get("subLangs")
    if isinstance(langs, list):
        cfg.sub_langs = tuple(str(x) for x in langs)
    kinds = adv.get("subKinds")
    if isinstance(kinds, list):
        valid = {"normal", "forced", "hoh"}
        picked = tuple(str(k) for k in kinds if str(k) in valid)
        # All three ticked is the same as no filter; store it as the empty
        # "keep every kind" rather than an explicit list, so the engine's
        # meaning stays obvious in logs and in the ledger.
        cfg.sub_kinds = () if len(picked) == len(valid) else picked
    _apply_advanced(cfg, adv)
    return cfg


_AUDIO_POLICY = {"passthrough": AudioPolicy.PASSTHROUGH, "aac": AudioPolicy.AAC,
                 "ac3": AudioPolicy.AC3, "flac": AudioPolicy.FLAC}
_CONTAINER = {"auto": Container.AUTO, "mp4": Container.MP4, "mkv": Container.MKV}


def _clear_stop_flags() -> None:
    """Start a run unstopped. BOTH flags must go — a leftover abort flag would make
    the next run drop every file it touched as 'cancelled' without saying why."""
    pipeline.STOP_FILE.unlink(missing_ok=True)
    pipeline.ABORT_FILE.unlink(missing_ok=True)
    encode.clear_abort()


def _apply_advanced(cfg: RunConfig, adv: dict) -> None:
    """Overlay the Advanced Settings modal's tunables onto a base RunConfig.

    Each value is optional and clamped to a sane range — a malformed or missing
    key leaves the engine default untouched. `tol` is entered as a percent over
    target (10 -> 1.10 ratio); everything else maps one-to-one.
    """
    def _num(key, cast, lo, hi):
        v = adv.get(key)
        if v is None or v == "":
            return None
        try:
            return max(lo, min(hi, cast(v)))
        except (TypeError, ValueError):
            return None

    if (v := _num("floor", int, 200, 20000)) is not None:
        cfg.bitrate_floor_kbps = v
    if (v := _num("tol", float, 0.0, 100.0)) is not None:
        cfg.tier_over_tolerance = 1.0 + v / 100.0
    if (v := _num("hevcHd", float, 0.2, 1.0)) is not None:
        cfg.hevc_factor_hd = v
    if (v := _num("hevc4k", float, 0.2, 1.0)) is not None:
        cfg.hevc_factor_4k = v
    if (v := _num("hevc8k", float, 0.2, 1.0)) is not None:
        cfg.hevc_factor_8k = v
    if (v := _num("abStereo", int, 64, 640)) is not None:
        cfg.audio_bitrate_stereo = v
    if (v := _num("abMulti", int, 128, 1024)) is not None:
        cfg.audio_bitrate_multichannel = v
    if (v := _num("mkvTracks", int, 0, 64)) is not None:
        cfg.mkv_if_tracks_over = v
    if (v := _num("jobs", int, 1, 16)) is not None:
        cfg.jobs = v
    if isinstance(adv.get("audio"), str):
        cfg.audio_policy = _AUDIO_POLICY.get(adv["audio"], cfg.audio_policy)
    if isinstance(adv.get("container"), str):
        cfg.container = _CONTAINER.get(adv["container"], cfg.container)
    if "imageSubs" in adv:
        cfg.keep_image_subs = bool(adv["imageSubs"])
    if "ledger" in adv:
        cfg.ledger_enabled = bool(adv["ledger"])

    # Per-tier quality density. Only tiers the user actually retuned are carried
    # across, so an untouched tier keeps its anchored default rather than being
    # pinned to whatever the UI last rounded it to.
    bpp = adv.get("bpp")
    if isinstance(bpp, dict):
        picked: dict[str, float] = {}
        for name, raw in bpp.items():
            try:
                tier = Tier.from_name(str(name))
                val = float(raw)
            except (ValueError, TypeError):
                continue
            val = max(0.005, min(1.0, val))
            if abs(val - tier.bpp) > 1e-9:
                picked[tier.name] = val
        cfg.tier_bpp = picked

    # Ignore rules. Sizes arrive from the UI in MB; the engine works in bytes.
    if (v := _num("ignUnderMb", float, 0.0, 1_000_000.0)) is not None:
        cfg.ignore_under_bytes = int(v * 1e6)
    if (v := _num("ignOverMb", float, 0.0, 1_000_000.0)) is not None:
        cfg.ignore_over_bytes = int(v * 1e6)
    if isinstance(adv.get("ignExts"), list):
        cfg.ignore_exts = tuple(
            str(x).strip().lstrip(".").lower() for x in adv["ignExts"] if str(x).strip())
    if isinstance(adv.get("ignNames"), list):
        cfg.ignore_name_contains = tuple(
            str(x).strip() for x in adv["ignNames"] if str(x).strip())


# ── the JS bridge, injected after the page loads (only takes effect in-shell) ──
_BRIDGE_JS = r"""
(function(){
  if(!window.pywebview || !window.pywebview.api){ return; }   // standalone file: keep mock
  const api = window.pywebview.api;

  // Check the machine's real hardware ability ON LOAD and make the ENCODER
  // question tell the truth — disable Hardware if nothing works here, else name
  // the actual encoder (videotoolbox / nvenc / qsv / amf).
  api.hw_capabilities().then(cap=>{
    window.__vtcHW = cap;
    // Toolbar readout: real ffmpeg version + real hardware encoder for this machine.
    const enc = cap && (cap.h265 || cap.h264 || '');
    const famMap = {videotoolbox:'VideoToolbox', nvenc:'NVENC', qsv:'QuickSync', amf:'AMF'};
    let fam = ''; for(const k in famMap){ if(enc && enc.indexOf(k)>=0){ fam = famMap[k]; break; } }
    const hwCodecs = [cap&&cap.h264&&'H.264', cap&&cap.h265&&'H.265'].filter(Boolean).join(' ');
    const rv = document.getElementById('rig-v');
    if(rv) rv.innerHTML = (cap && cap.available)
      ? `Soft <b>ffmpeg ${cap.ffmpeg_version||'?'}</b> · Hard <b>${fam||'hardware'}</b> ${hwCodecs}`
      : `Soft <b>ffmpeg ${(cap&&cap.ffmpeg_version)||'?'}</b> · no hardware encoder`;
    // Where HEVC won't play in the webview (Windows), default previews to H.264.
    if(cap && cap.preview_codec === 'h264' && typeof pvCodec!=='undefined'){
      try { pvCodec='h264'; document.querySelectorAll('#pv-codec button').forEach(b=>b.classList.toggle('on', b.dataset.c==='h264')); } catch(e){}
    }
    const q = (typeof M!=='undefined') && M.find(x=>x.id==='encoder');
    if(!q) return;
    if(!cap || !cap.available){
      q.opts[0].disabled = true;
      q.opts[0].tag = 'not available on this machine';
      q.sub = 'No working hardware encoder was detected on this machine, so software is the only option here.';
    } else {
      const name = (cap.h265 || cap.h264 || 'hardware').replace(/_/g,' ');
      q.opts[0].tag = name + ' · default';
      q.sub = 'A working hardware encoder (' + name + ') was detected on this machine, so you get the choice.';
    }
    if(typeof step!=='undefined' && M[step] && M[step].id==='encoder' && typeof render==='function') render();
  }).catch(()=>{});

  // Real mode has no "recent folders": Start goes straight to the native picker.
  try { FOLDERS.length = 0; } catch(e){}
  document.getElementById('picks').innerHTML =
    '<button class="pick" data-f="-1"><b>Browse…</b><span>choose a media folder</span></button>';
  document.getElementById('start').onclick = ()=> pickFolder(-1);

  // Folder pick -> native dialog returns the PATH instantly; the count runs in the
  // background so the deck flips to a "Scanning…" state right away instead of the
  // gate button sitting frozen. __vtcScanDone fills in the real totals.
  window.pickFolder = async ()=>{
    const s = await api.pick_folder();
    if(!s) return;                                  // cancelled
    SRC = { k:s.k, files:0, tb:0, nonmp4:0, scanning:true };
    document.getElementById('src-v').textContent = SRC.k;
    document.getElementById('readout').classList.add('slid');
    document.getElementById('unit').classList.remove('off');
    document.getElementById('unit').classList.add('on');
    render(); setTimeout(paintCorpse, 80);
  };
  window.__vtcScanDone = (info)=>{                   // library counted
    if(!SRC || SRC.k !== info.k) return;             // a newer pick superseded it
    SRC.files = info.files; SRC.tb = info.tb; SRC.nonmp4 = info.nonmp4; SRC.scanning = false;
    SRC.ignored = info.ignored || 0;                 // removed by the user's ignore rules
    drawEstimate();
    if(window.maybeAskCompat) maybeAskCompat();       // ask the non-MP4 policy, now that we know
  };
  document.querySelectorAll('#picks .pick').forEach(b=> b.onclick = ()=> pickFolder(+b.dataset.f));

  // The brow "SOURCE" control changes the folder. In real mode that's one native
  // dialog — go straight to it instead of first revealing a one-item "Browse…" list.
  var srcBtn = document.getElementById('src');
  if(srcBtn) srcBtn.onclick = ()=> pickFolder(-1);

  // Estimate -> real per-file plan math (measured once files are probed).
  const baseEstimate = window.drawEstimate;
  window.drawEstimate = ()=>{
    if(!SRC) return;
    if(SRC.scanning){                                // count not in yet
      document.getElementById('now-v').innerHTML = '<span style="font-size:.5em;letter-spacing:.1em">SCANNING…</span>';
      document.getElementById('now-n').textContent = 'counting your library…';
      return;
    }
    document.getElementById('now-v').innerHTML = tbHTML(SRC.tb);
    // Say when the user's own rules have taken files off the table — otherwise a
    // count that doesn't match the folder looks like the scan missed something.
    document.getElementById('now-n').textContent = `${SRC.files.toLocaleString()} files`
      + (SRC.ignored ? ` · ${SRC.ignored.toLocaleString()} ignored by your rules` : '');
    if(answers.codec === undefined){ return baseEstimate(); }   // not enough set yet
    api.estimate(Object.assign({adv: window.ADV||{}, outputDir: window.__outputDir||''}, answers)).then(e=>{
      if(!e || e.error){ return baseEstimate(); }
      document.getElementById('est').innerHTML = tbHTML(e.out_tb);
      document.getElementById('est-d').textContent = `−${e.saved_pct}% · ${tbStr(SRC.tb-e.out_tb)} back`;
      document.getElementById('est-n').textContent =
        `${e.reencoded.toLocaleString()} re-encoded · ${e.skipped.toLocaleString()} already at tier, left alone. ${e.measured?'Measured.':'Modelled while probing…'}`;
      gateStart();
    });
    gateStart();
  };
  window.__vtcProbeProgress = (done)=> {                 // refine estimate as files are probed
    window.__vtcProbed = done;                          // ...and let the confirm screen show its working
    if(SRC) drawEstimate();
    if(window.confirmProbeTick) confirmProbeTick();
  };
  window.__vtcProbesReady = ()=> { if(SRC) drawEstimate(); };   // final: fully measured

  // Run -> real pipeline.run streamed back per file, with LIVE progress, then the report.
  const acc = [];
  window.__vtcRunStart = (total, est, files)=> pgStart(total, est, files);   // engine: run begins
  window.__vtcQueueMore = (files)=> pgQueueMore(files);                       // engine: rest of the queue
  window.__vtcETA = (sec)=> pgSetETA(sec);                                    // engine: work-based ETA
  window.__vtcEncodeProgress = (name, frac, stats)=> pgFile(name, frac, stats);  // current file
  window.__vtcStop = ()=> { try { api.stop_run(); } catch(e){} };        // Stop button
  window.__vtcAbort = ()=> { try { api.abort_run(); } catch(e){} };      // Stop NOW button
  window.__vtcRegenPreviews = (codec, start)=> { try { api.regenerate_previews(codec||'h265', start); } catch(e){} };
  window.__vtcRetryFailed = ()=> { try { api.retry_failed_software(); } catch(e){} };
  // Two-phase save: ask for the path FIRST (nothing but a filename crosses the
  // bridge, so the native dialog opens immediately), then build the log text and
  // ship it. Passing a builder means a huge report isn't assembled at all if the
  // user cancels. A plain string still works.
  window.__vtcSaveLog = (build)=> {
    try {
      Promise.resolve(api.pick_save_path('vtc-run-report.txt')).then(r=>{
        if(!r || !r.path) return;                                  // cancelled
        api.write_text_file(r.path, typeof build==='function' ? build() : String(build));
      });
    } catch(e){}
  };
  window.__vtcOnResult = (r)=>{                                          // r: {name,t,d,sev,work}
    acc.push(r);                                     // every file lands in the report
    // ...but only the files being WORKED ON move the progress bar. The rest are
    // instant skips; counting them made the bar race to 90% and then crawl.
    if(r.work !== false) pgDone1({ f:r.name, t:r.t, sev:r.sev, star:r.star, detail:r.detail });
  };
  window.__vtcOnDone = (summary)=>{
    pgFinish();
    RUN = {
      rows: acc.map(r=>({ f:r.name, t:r.t, d:r.d, sev:r.sev,
                          detail:r.detail||'', star:!!r.star, sbytes:r.sbytes||0, obytes:r.obytes||0 })),
      done: summary.done, skip: summary.skip, fail: summary.fail,
      failedRetryable: summary.failed_retryable||0,
      tb: summary.tb, mins: summary.mins, stopped: !!summary.stopped,
    };
    drawReport(); openSheet('#report-sheet');
  };
  window.runNow = ()=>{
    shutSheet('#confirm-sheet');
    acc.length = 0;
    pgStart(0, 0);               // show the working screen IMMEDIATELY — scanning a big
                                 // library can take a moment, and a blank pause looks broken
    api.run(Object.assign({adv: window.ADV||{}, outputDir: window.__outputDir||'',
                           softwareFiles: window.__softwareFiles||[]}, answers));
  };
  // "New folder" destination: let the user pick where outputs go. Returns the dir
  // (or '' if cancelled — build_config then falls back to <src>/converted).
  window.__pickOutputDir = async ()=>{
    try { const d = await api.pick_output_folder(); if(d && d.dir){ window.__outputDir = d.dir; return d.dir; } }
    catch(e){}
    return window.__outputDir || '';
  };

  // ── Network-volume banner ───────────────────────────────────────────────────
  // The output library is often a network share. When placing a finished file the
  // engine tells us if that share goes missing or stalls (__vtcVolumeStuck) and when
  // it returns (__vtcVolumeBack). Surface it loudly: a stalled move is the one thing
  // that can make a healthy run look frozen. The run continues on its own the moment
  // the share is back — the user just has to reconnect it.
  function volBanner(){
    let el = document.getElementById('vtc-volbar');
    if(!el){
      el = document.createElement('div');
      el.id = 'vtc-volbar';
      el.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:99999;'
        + 'font:600 14px/1.4 system-ui,-apple-system,sans-serif;padding:11px 18px;'
        + 'text-align:center;color:#1a1200;box-shadow:0 2px 10px rgba(0,0,0,.35);'
        + 'transition:transform .2s ease;transform:translateY(-100%)';
      document.body.appendChild(el);
    }
    return el;
  }
  window.__vtcVolumeStuck = (path)=>{
    const el = volBanner();
    const name = String(path||'the output volume').replace(/\/+$/,'').split('/').pop() || path;
    el.style.background = 'linear-gradient(#ffd257,#f4b41a)';
    el.innerHTML = '⚠️ The output volume <b>'+name+'</b> appears missing or stuck — '
      + 'reconnect it and the run will continue on its own. '
      + '<span style="opacity:.7">(or use Stop now to give up)</span>';
    el.style.transform = 'translateY(0)';
    clearTimeout(el._hideT);
  };
  window.__vtcVolumeBack = (path)=>{
    const el = volBanner();
    el.style.background = 'linear-gradient(#b8f0c0,#7fd897)';
    el.innerHTML = '✓ Output volume reconnected — continuing…';
    el.style.transform = 'translateY(0)';
    clearTimeout(el._hideT);
    el._hideT = setTimeout(()=>{ el.style.transform = 'translateY(-100%)'; }, 3200);
  };

  // ── Resume an interrupted run ────────────────────────────────────────────────
  // If a previous run was cut off mid-flight (crash / force-quit / reboot — e.g. the
  // only way out of a fully-hung network move), offer to continue it. The ledger makes
  // resume cheap: it re-runs the same settings and every already-done file is skipped
  // instantly, so only the interrupted file (and the rest of the queue) is processed.
  function askResume(src){
    const wrap = document.createElement('div');
    wrap.style.cssText = 'position:fixed;inset:0;z-index:99998;display:flex;'
      + 'align-items:center;justify-content:center;background:rgba(0,0,0,.55)';
    const name = String(src).replace(/\/+$/,'').split('/').pop() || src;
    wrap.innerHTML =
      '<div style="max-width:460px;background:#1c1c22;color:#eee;border:1px solid #333;'
      + 'border-radius:14px;padding:26px 26px 20px;font:14px/1.5 system-ui,sans-serif;'
      + 'box-shadow:0 18px 60px rgba(0,0,0,.6)">'
      + '<div style="font-size:17px;font-weight:700;margin-bottom:8px">Continue previous run?</div>'
      + '<div style="opacity:.85">A run over <b>'+name+'</b> didn’t finish last time. '
      + 'Resume it? Files already done are skipped — it picks up where it stopped.</div>'
      + '<div style="opacity:.55;font-size:12px;margin:6px 0 18px">'+String(src)+'</div>'
      + '<div style="display:flex;gap:10px;justify-content:flex-end">'
      + '<button id="vtc-res-no" style="padding:9px 16px;border-radius:9px;border:1px solid #444;'
      + 'background:#26262d;color:#ddd;font-weight:600;cursor:pointer">Not now</button>'
      + '<button id="vtc-res-yes" style="padding:9px 16px;border-radius:9px;border:0;'
      + 'background:#f4b41a;color:#1a1200;font-weight:700;cursor:pointer">Resume</button>'
      + '</div></div>';
    document.body.appendChild(wrap);
    wrap.querySelector('#vtc-res-no').onclick = ()=>{
      try { api.discard_session(); } catch(e){}
      wrap.remove();
    };
    wrap.querySelector('#vtc-res-yes').onclick = ()=>{
      wrap.remove();
      try { pgStart(0, 0); } catch(e){}          // flip to the working screen at once
      try { api.resume_session(); } catch(e){}
    };
  }
  try {
    Promise.resolve(api.pending_session()).then(r=>{ if(r && r.src) askResume(r.src); }).catch(()=>{});
  } catch(e){}
})();
"""


class Api:
    """Exposed to JS as `window.pywebview.api.*`. All methods return JSON-able data."""

    def __init__(self) -> None:
        self.window = None
        self._src: Path | None = None
        self._probes: list[tuple] = []     # [(MediaInfo, size_bytes)]
        self._probed_for: Path | None = None
        self._total_files = 0
        self._total_tb = 0.0
        self._ignored = 0                  # files the user's ignore rules removed
        # Last Advanced-settings payload the UI pushed. Needed OUTSIDE a run,
        # because the ignore rules change what a scan even counts and the tier
        # bpp changes what the preview panels encode.
        self._adv: dict = {}
        self._scan_gen = 0
        self._probe_gen = 0
        self._preview_gen = 0              # bumped whenever previews are (re)requested;
        self._preview_start = 0.0         # a running worker aborts if its gen is stale
        self._preview_seg = 5.0           # sample length (seconds)
        self._preview_codec = _PREVIEW_CODEC   # codec the panels are currently in
        self._last_config: RunConfig | None = None   # last run's config (for retry)
        self._last_failed: list[Path] = []           # files that ERRORed last run

    # -- Advanced settings pushed from the UI ------------------------------------
    def _scan_config(self, src: Path) -> RunConfig:
        """A bare config for WALKING a folder, carrying the current ignore rules."""
        cfg = RunConfig(src=src, ffmpeg=FFMPEG, ffprobe=FFPROBE)
        _apply_advanced(cfg, self._adv)
        return cfg

    @staticmethod
    def _ignore_key(adv: dict) -> str:
        """The part of Advanced settings that changes what a scan sees."""
        return json.dumps({k: adv.get(k) for k in
                           ("ignUnderMb", "ignOverMb", "ignExts", "ignNames")}, sort_keys=True)

    def set_adv(self, adv: dict):
        """Take the Advanced-settings object from the UI, and react to the two
        parts of it that invalidate work already done: the ignore rules (which
        change what the library even contains) and the tier densities (which
        change what the preview panels are showing)."""
        old, self._adv = self._adv, dict(adv or {})
        if self._src is None:
            return {"ok": True}
        rescanned = previews = False
        if self._ignore_key(old) != self._ignore_key(self._adv):
            log.info("ignore rules changed -> rescanning %s", self._src)
            self._rescan(self._src)
            rescanned = True
        if json.dumps(old.get("bpp") or {}, sort_keys=True) != \
                json.dumps(self._adv.get("bpp") or {}, sort_keys=True):
            log.info("tier bpp changed -> regenerating previews")
            self._preview_gen += 1
            threading.Thread(target=self._preview_worker,
                             args=(self._src, self._preview_codec, self._preview_gen),
                             daemon=True).start()
            previews = True
        return {"ok": True, "rescanned": rescanned, "previews": previews}

    def _rescan(self, src: Path) -> None:
        """Recount and re-probe `src` under the current rules. Both workers carry a
        generation so an older one can't publish over a newer one's results."""
        self._probes, self._probed_for = [], None
        self._scan_gen += 1
        self._probe_gen += 1
        threading.Thread(target=self._scan_folder, args=(src, self._scan_gen), daemon=True).start()
        threading.Thread(target=self._warm_probes, args=(src, self._probe_gen), daemon=True).start()

    def history_info(self):
        """Entry count + path of the processing history for the current folder.

        Built with the ledger force-enabled: the history file exists on disk
        whether or not the toggle is on, and "Clear history" must be able to see
        and empty it either way.
        """
        if self._src is None:
            return {"entries": 0, "path": ""}
        led = pipeline.Ledger(RunConfig(src=self._src, ledger_enabled=True))
        return {"entries": led.count(), "path": str(led.path or "")}

    def clear_history(self):
        """Empty the processing history for the current folder, so every file is
        considered again on the next run."""
        if self._src is None:
            return {"error": "no folder"}
        led = pipeline.Ledger(RunConfig(src=self._src, ledger_enabled=True))
        n = led.clear()
        log.info("processing history cleared: %d entr%s from %s",
                 n, "y" if n == 1 else "ies", led.path)
        return {"cleared": n, "entries": 0, "path": str(led.path or "")}

    # -- folder pick + fast scan -------------------------------------------------
    def _file_dialog(self, *args, **kwargs):
        """Show a native file dialog safely across platforms.

        js_api methods run on pywebview's MTA worker thread (so a slow bridge call
        can't freeze the UI), but the Windows picker is the Vista COM
        ``IFileDialog`` and showing a COM dialog off the STA GUI thread hangs
        outright. Marshal onto the owning form's thread — the same mechanism
        pywebview uses internally. macOS/Linux have no such requirement, and if the
        winforms host isn't present we just call through. (WINDOWS-GOTCHAS.md #2)"""
        if sys.platform != "win32":
            return self.window.create_file_dialog(*args, **kwargs)
        try:
            from System import Action                       # pythonnet
            from webview.platforms.winforms import BrowserView
        except Exception:  # noqa: BLE001 — not the winforms backend; call directly
            return self.window.create_file_dialog(*args, **kwargs)
        form = BrowserView.instances.get(self.window.uid)
        if form is None:                                    # fall back rather than hang
            return self.window.create_file_dialog(*args, **kwargs)
        box: dict = {}
        def _on_gui_thread():
            box["result"] = self.window.create_file_dialog(*args, **kwargs)
        form.Invoke(Action(_on_gui_thread))                 # blocks until the STA call returns
        return box.get("result")

    def pick_output_folder(self):
        """Native folder picker for the 'New folder' destination. Returns {dir} or
        None (cancelled) — does not scan; just the path the outputs should go to."""
        import webview
        picked = self._file_dialog(webview.FOLDER_DIALOG)
        if not picked:
            return None
        return {"dir": str(Path(picked[0]))}

    def pick_folder(self):
        import webview
        log.info('pick_folder: opening native folder dialog')
        picked = self._file_dialog(webview.FOLDER_DIALOG)
        if not picked:
            return None
        src = Path(picked[0])
        self._src = src
        self._probes, self._probed_for = [], None
        self._total_files, self._total_tb, self._ignored = 0, 0.0, 0
        self._preview_start = 0.0         # new folder -> sample from the middle again
        # Count files in the BACKGROUND. On a large library the walk takes seconds;
        # doing it inline froze the "Choose media folder" button with no feedback, so
        # people clicked again and reopened the dialog. Return the path immediately so
        # the UI flips to a "Scanning…" state; __vtcScanDone fills in the counts.
        self._rescan(src)
        # Build the real preview encodes in the background while they configure.
        self._preview_gen += 1
        threading.Thread(target=self._preview_worker,
                         args=(src, self._preview_codec, self._preview_gen), daemon=True).start()
        return {"k": str(src), "scanning": True}

    def _scan_folder(self, src: Path, gen: int) -> None:
        """Walk the library counting files/size/non-MP4, then hand the totals to the
        UI. Runs off the pick_folder call so the interface never freezes on a scan.

        Files removed by the user's ignore rules are counted separately and left
        out of everything else — they are not part of this library as far as the
        rest of the app is concerned."""
        files = total = nonmp4 = ignored = 0
        cfg = self._scan_config(src)
        for f, reason in pipeline.iter_scan_entries(cfg):
            if reason is not None:
                ignored += 1
                continue
            files += 1
            if f.suffix.lower() != ".mp4":
                nonmp4 += 1
            try:
                total += f.stat().st_size
            except OSError:
                pass
        if self._src != src or gen != self._scan_gen:
            return                        # a newer folder pick superseded this scan
        self._total_files, self._total_tb, self._ignored = files, total / 1e12, ignored
        self._emit("__vtcScanDone",
                   {"k": str(src), "files": files, "tb": total / 1e12, "nonmp4": nonmp4,
                    "ignored": ignored})

    # -- previews: extract a 5s sample, encode it at each tier, stream URLs --------
    def _emit(self, fn: str, payload) -> None:
        if self.window:
            self.window.evaluate_js(f"window.{fn} && window.{fn}({json.dumps(payload)})")

    def _preview_worker(self, src: Path, codec: OutCodec = OutCodec.H265, gen: int = 0):
        codec_label = "H.264" if codec == OutCodec.H264 else "H.265"
        stale = lambda: self._src != src or (gen and gen != self._preview_gen)
        try:
            pdir, port = _ensure_preview_server()
            for f in pdir.glob("*.mp4"):        # only clear old previews, not the served HTML
                try:
                    f.unlink()
                except OSError:
                    pass
            cfg = RunConfig(src=src, ffmpeg=FFMPEG, ffprobe=FFPROBE)
            first = next(iter(pipeline.iter_video_files(cfg)), None)
            if first is None:
                self._emit("__vtcPreviewError", "no video files found here"); return
            info = probe(first, FFPROBE)
            if not info.ok or not info.vcodec or info.width <= 0:
                self._emit("__vtcPreviewError", "could not read the first file"); return
            dur = info.duration or 0.0
            seglen = min(self._preview_seg, dur) if dur > 0 else self._preview_seg
            # start position: the chosen fraction through the file, else the middle
            if self._preview_start > 0 and dur > 0:
                start = max(0.0, min(dur - seglen, self._preview_start * dur))
            else:
                start = max(0.0, (dur - seglen) / 2.0)
            is_hevc = (info.vcodec or "").lower() == "hevc"
            src_codec_label = {"h264": "H.264", "hevc": "H.265", "av1": "AV1", "vp9": "VP9"}.get(
                (info.vcodec or "").lower(), (info.vcodec or "?").upper())
            log.info("previews: %s (%dx%d, %.1fs) sample @ %.1fs codec=%s",
                     first.name, info.width, info.height, dur, start, codec.value)

            # SOURCE panel = the source frames, cut and made PLAYABLE. If the source
            # codec is one the webview can show, copy it losslessly; otherwise (MPEG-4/
            # XviD/VP9/AV1 on mac, or anything but H.264 on Windows) the panel would be
            # a black "CAN'T PLAY" tile, so transcode the sample to H.264 for viewing.
            # The panel label still names the real source codec.
            sample = pdir / "source.mp4"
            playable = (info.vcodec or "").lower() in (
                ("h264", "hevc") if sys.platform == "darwin" else ("h264",))
            if playable:
                svargs = ["-c:v", "copy", *(["-tag:v", "hvc1"] if is_hevc else [])]
            else:
                svargs = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                          "-pix_fmt", "yuv420p"]
            # NB: a stream copy can only start on a keyframe, so ffmpeg writes an
            # EDIT LIST to trim back to the requested moment — the sample's frame 0
            # is therefore a player-interpreted thing, while the tier panels
            # (re-encoded from this sample) start plainly at 0. Measured: dropping
            # the edit list (-use_editlist 0) is worse, not better — it re-exposes
            # the pre-roll AND leaves an 0.08s start offset. The panels are aligned
            # on the front end instead, by seeking every video to one position.
            r = subprocess.run(
                [FFMPEG, "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(first),
                 "-t", f"{seglen:.3f}", "-map", "0:v:0", *svargs, "-an",
                 "-movflags", "+faststart", str(sample)],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                **TEXT_UTF8, **NO_WINDOW)
            if r.returncode != 0 or not sample.exists() or sample.stat().st_size == 0:
                self._emit("__vtcPreviewError", "could not extract a sample clip"); return
            if stale():
                return                              # a newer request superseded us

            sinfo = probe(sample, FFPROBE)
            self._emit("__vtcPreviewStart",
                       {"w": sinfo.width or info.width, "h": sinfo.height or info.height,
                        "n": len(_PREVIEW_PANELS), "codec": codec_label})
            hw = encode.select_hw_encoder(RunConfig(src=src, out_codec=codec, ffmpeg=FFMPEG))

            for idx, (key, label, tier) in enumerate(_PREVIEW_PANELS):
                if stale():                         # source changed OR a newer request came in
                    return
                panel_codec = src_codec_label if tier is None else codec_label
                if tier is None:
                    out = sample
                    # The SOURCE's own density, from its real bitrate — the number that
                    # explains the shrink. A source already at/below a tier's BPP is
                    # already efficient and will barely move; showing it stops "only 10%
                    # smaller?" from looking like a bug when the source was simply lean.
                    bpp = (info.effective_bps / (info.pixels * info.fps)
                           if info.pixels and info.fps else 0.0)
                else:
                    out = pdir / f"{key}.mp4"
                    # The panels must show the tiers AS TUNED: a retuned bpp that
                    # only took effect at run time would make the comparison a lie.
                    cfg2 = RunConfig(src=src, out_codec=codec, tier=tier, ffmpeg=FFMPEG)
                    _apply_advanced(cfg2, self._adv)
                    tgt = target_kbps(tier, sinfo.pixels, sinfo.fps, codec,
                                      floor_kbps=cfg2.bitrate_floor_kbps,
                                      bpp=cfg2.bpp_for(tier), hevc=cfg2.hevc_factors())
                    # The tier's TARGET density for this file — compare against source
                    # BPP. Derived from the TUNED target above, so a retuned tier's
                    # panel reports the density it was actually encoded at.
                    bpp = (tgt * 1000.0 / (sinfo.pixels * sinfo.fps)
                           if sinfo.pixels and sinfo.fps else 0.0)
                    vargs = encode.build_video_args(cfg2, sinfo, Mode.SHRINK, tgt, hw)
                    rr = subprocess.run(
                        [FFMPEG, "-y", "-v", "error", "-i", str(sample), *vargs, "-an",
                         "-movflags", "+faststart", str(out)],
                        stdin=subprocess.DEVNULL, capture_output=True, text=True,
                        **TEXT_UTF8, **NO_WINDOW)
                    if rr.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                        log.error("preview %s failed: %s", key,
                                  (rr.stderr or "").strip().splitlines()[-1:])
                        self._emit("__vtcPreviewPanel", {"i": idx, "key": key, "label": label,
                                                         "codec": panel_codec, "error": "encode failed"})
                        continue
                size = out.stat().st_size
                self._emit("__vtcPreviewPanel",
                           {"i": idx, "key": key, "label": label, "codec": panel_codec, "bytes": size,
                            "bpp": round(bpp, 3),
                            "url": f"http://127.0.0.1:{port}/{out.name}?v={size}"})
            self._emit("__vtcPreviewDone", "")
            log.info("previews: done (codec=%s)", codec.value)
        except Exception as e:  # noqa: BLE001 — previews must never crash the app
            log.exception("preview worker failed")
            self._emit("__vtcPreviewError", str(e))

    def _warm_probes(self, src: Path, gen: int = 0):
        """Probe every file, publishing partial results periodically so the
        estimate refines live (file count rising, prediction updating)."""
        probes = []
        done = 0
        stale = lambda: self._src != src or (gen and gen != self._probe_gen)
        cfg = self._scan_config(src)
        # Concurrent: ffprobe is latency-bound, and on a network volume a serial
        # walk of a big library takes tens of minutes — all of it spent waiting.
        for info in pipeline.probe_many(cfg, pipeline.iter_video_files(cfg), stale=stale):
            done += 1
            if info.ok and info.vcodec:
                try:
                    probes.append((info, info.path.stat().st_size))
                except OSError:
                    pass
            if done % 25 == 0:
                self._probes = list(probes)   # publish a measured-so-far sample
                if self.window:
                    self.window.evaluate_js(f"window.__vtcProbeProgress && window.__vtcProbeProgress({done})")
        if not stale():
            self._probes, self._probed_for = probes, src
            log.info("probed %d file(s) under %s", len(probes), src)
            if self.window:
                self.window.evaluate_js("window.__vtcProbesReady && window.__vtcProbesReady()")

    # -- projected estimate (real plan arithmetic on probed files) --------------
    def estimate(self, answers: dict):
        if self._src is None:
            return {"error": "no folder"}
        try:
            config = build_config(self._src, answers)
        except ValueError as e:
            return {"error": str(e)}
        if not self._probes:
            return {"error": "probing", "measured": False}    # nothing probed yet -> JS keeps modelled
        from .result import Mode
        src_bytes = out_bytes = 0
        reencoded = skipped = 0
        # The cohort that actually gets worked on. Reporting the saving against the
        # WHOLE library averages a handful of files halving themselves against
        # hundreds that never move, which makes worthwhile work look pointless:
        # "26 files, 90 GB, saves 3 GB" when the truth was "6 files, 12.6 GB ->
        # 6.4 GB". So these are totalled separately and it is these the UI leads on.
        work_src = work_out = 0
        for info, size in self._probes:
            mode, _outcome, target = pipeline.decide(config, info)
            predicted = pipeline.predict_output_bytes(config, info, size, mode, target)
            if mode in (Mode.SHRINK, Mode.TRANSCODE):
                reencoded += 1
                work_src += size
                work_out += predicted
            else:                                             # remux (~same size) or skip
                skipped += 1
            out_bytes += predicted
            src_bytes += size
        ratio = (out_bytes / src_bytes) if src_bytes else 1.0   # sample's out/in ratio
        sample = len(self._probes)
        scale = (self._total_files / sample) if sample else 1.0
        return {
            "out_tb": self._total_tb * ratio,                   # extrapolate the ratio to the library
            "saved_pct": round((1 - ratio) * 100),
            "reencoded": round(reencoded * scale),
            "skipped": round(skipped * scale),
            # The honest headline: the files that will be touched, and what
            # happens to THEM. Bytes, not TB, because a cohort is often small.
            "work_files": reencoded,
            "work_bytes": work_src,
            "work_out_bytes": work_out,
            "work_saved_bytes": max(0, work_src - work_out),
            "work_saved_pct": round((1 - work_out / work_src) * 100) if work_src else 0,
            "measured": self._probed_for == self._src,          # True once the full scan finishes
        }

    # -- the software picker: which files are even worth choosing ---------------
    def reencode_candidates(self, answers: dict):
        """The files this run would actually RE-ENCODE, biggest saving first.

        Only shrink/transcode files are offered: ticking something that is going
        to be skipped as already-efficient would do nothing, and on a real library
        the skips outnumber the work by an order of magnitude. Requires the probe
        pass, so it reports `measured` and lets the UI say when it is still
        counting rather than showing a half-empty list as if it were the answer.
        """
        if self._src is None:
            return {"error": "no folder"}
        try:
            config = build_config(self._src, answers)
        except ValueError as e:
            return {"error": str(e)}
        rows = []
        for info, size in self._probes:
            mode, _outcome, target = pipeline.decide(config, info)
            if mode not in (Mode.SHRINK, Mode.TRANSCODE):
                continue
            src_kbps = info.effective_bps / 1000.0
            saving = max(0.0, 1.0 - target / src_kbps) if src_kbps > 0 else 0.0
            rows.append({
                "path": str(info.path.resolve()),
                "name": info.path.name,
                "bytes": size,
                "res": f"{info.width}x{info.height}" if info.width else "",
                "px": info.pixels,
                "dur": info.duration or 0.0,
                "saving": round(saving * 100),
            })
        rows.sort(key=lambda r: r["bytes"], reverse=True)
        return {"files": rows, "measured": self._probed_for == self._src,
                "total": len(rows)}

    # -- the run: stream each file's result back, then a summary ----------------
    def hw_capabilities(self):
        """What hardware encoding is actually available — probed on demand at load
        so the UI's encoder choice reflects this machine, not a guess."""
        rep = dict(encode.hardware_report(FFMPEG))
        rep["preview_codec"] = _PREVIEW_CODEC.value   # 'h265' on mac, 'h264' where HEVC won't play
        rep["ffmpeg_version"] = _ffmpeg_version()      # for the toolbar readout
        log.info("hw_capabilities: %s", rep)
        return rep

    def regenerate_previews(self, codec: str = "h265", start=None):
        """Re-run the sample encodes for the current source, at the chosen codec and
        (optionally) from a chosen start fraction 0..1 through the file. A fresh
        generation supersedes any worker still running, so rapid clicks can't race."""
        oc = OutCodec.H264 if str(codec).lower() == "h264" else OutCodec.H265
        self._preview_codec = oc
        if start is not None:
            try:
                self._preview_start = max(0.0, min(1.0, float(start)))
            except (TypeError, ValueError):
                pass
        if self._src is None:
            return {"ok": False, "error": "no folder"}
        self._preview_gen += 1
        log.info("regenerate_previews: codec=%s start=%s gen=%s",
                 oc.value, self._preview_start, self._preview_gen)
        threading.Thread(target=self._preview_worker,
                         args=(self._src, oc, self._preview_gen), daemon=True).start()
        return {"ok": True}

    def stop_run(self):
        """Ask the run to stop after the file(s) already in flight (graceful)."""
        try:
            pipeline.STOP_FILE.touch()
            log.info("stop requested (after files in flight)")
        except OSError as e:
            log.error("stop_run: %s", e)
        return {"stopping": True}

    def abort_run(self):
        """Stop NOW: start nothing further and kill the encodes already running.

        Safe by construction — an encode writes to a temp file and is only moved
        into place once it succeeds, so killing one discards the temp and leaves
        the original exactly as it was. The killed file is dropped from the report
        rather than counted as a failure, since it did not fail: it was cancelled.
        """
        try:
            pipeline.ABORT_FILE.touch()
        except OSError as e:
            log.error("abort_run: %s", e)
            return {"error": str(e)}
        killed = encode.abort_running()
        log.info("abort requested — killed %d running encode(s)", killed)
        return {"stopping": True, "killed": killed}

    def pick_save_path(self, name: str = "report.txt"):
        """Ask for a save location and return it. Nothing but the filename crosses
        the bridge, so the dialog appears the instant the button is clicked — the
        one-shot version shipped the whole (possibly multi-megabyte) log across the
        JS bridge FIRST, which is why "Save log…" sat there spinning."""
        import webview
        try:
            picked = self._file_dialog(
                webview.SAVE_DIALOG, save_filename=name or "report.txt")
        except Exception as e:  # noqa: BLE001
            log.error("save dialog failed: %s", e); return {"error": str(e)}
        if not picked:
            return {"cancelled": True}
        path = picked[0] if isinstance(picked, (list, tuple)) else picked
        return {"path": str(path)}

    def write_text_file(self, path: str, content: str):
        """Write `content` to an already-chosen path, UTF-8. (The blob+download this
        replaced made WKWebView navigate the whole app window to the text — mojibake,
        no way back.)"""
        if not path:
            return {"error": "no path"}
        try:
            Path(path).write_text(content, encoding="utf-8")
            log.info("saved report -> %s", path)
        except OSError as e:
            log.error("could not write %s: %s", path, e); return {"error": str(e)}
        return {"saved": str(path)}

    def save_text_file(self, name: str, content: str):
        """One-shot pick-then-write. Kept for callers that already hold the text."""
        picked = self.pick_save_path(name)
        if "path" not in picked:
            return picked
        return self.write_text_file(picked["path"], content)

    def run(self, answers: dict):
        if self._src is None:
            return {"error": "no folder"}
        try:
            config = build_config(self._src, answers)
        except ValueError as e:
            log.error("run: bad config: %s", e)
            return {"error": str(e)}
        _clear_stop_flags()                          # a new run starts unstopped
        self._last_config = config
        self._save_session(answers)                  # so a crash mid-run can be resumed
        threading.Thread(target=self._run_worker, args=(config,), daemon=True).start()
        return {"started": True}

    # ── crash-resumable session ───────────────────────────────────────────────
    # The whole run is reconstructable from the src folder + the answer dict: the
    # ledger (on by default, <src>/.vtc_processed.log) already skips files finished
    # under the same settings, so "resume" is just "run the same thing again" and the
    # done files fall away instantly. We persist that intent at run start and clear it
    # on a clean finish; if the app dies mid-run the file survives and next launch
    # offers to continue.
    def _save_session(self, answers: dict) -> None:
        try:
            _session_path().write_text(
                json.dumps({"src": str(self._src), "answers": answers}), encoding="utf-8")
        except OSError as e:
            log.warning("could not save session: %s", e)

    def _clear_session(self) -> None:
        try:
            _session_path().unlink(missing_ok=True)
        except OSError:
            pass

    def pending_session(self):
        """Called on launch. If a previous run was cut off mid-flight, return its src
        so the UI can offer to continue; otherwise {}."""
        try:
            data = json.loads(_session_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        src = data.get("src")
        if not src or not Path(src).is_dir():        # folder gone -> nothing to resume
            self._clear_session()
            return {}
        return {"src": src}

    def resume_session(self):
        """Continue the interrupted run: re-run the saved answers; the ledger skips
        everything already done and redoes only the file that was in flight."""
        try:
            data = json.loads(_session_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"error": "no session"}
        src, answers = data.get("src"), data.get("answers")
        if not src or answers is None:
            return {"error": "no session"}
        self._src = Path(src)
        log.info("resuming previous session: %s", src)
        return self.run(answers)

    def discard_session(self):
        self._clear_session()
        return {"discarded": True}

    def retry_failed_software(self):
        """Re-run just the files that ERRORed in the last run, in software — a quirky
        hardware encoder (e.g. h264_videotoolbox choking on a file) should not slow
        the whole run, so we offer software only for the failures, at the end."""
        cfg = getattr(self, "_last_config", None)
        failed = list(getattr(self, "_last_failed", []) or [])
        if cfg is None or not failed:
            return {"error": "nothing to retry"}
        import dataclasses
        soft = dataclasses.replace(cfg, encoder=Encoder.SOFTWARE)
        _clear_stop_flags()
        log.info("retry_failed_software: %d file(s)", len(failed))
        threading.Thread(target=self._run_worker, args=(soft,), kwargs={"files": failed},
                         daemon=True).start()
        return {"started": True, "count": len(failed)}

    def _run_worker(self, config: RunConfig, files=None):
        hw = encode.select_hw_encoder(config)
        # jobs is logged because it changes what "stop after current file" means: with
        # more than one worker, several files are in flight and all of them finish.
        log.info("run start: src=%s codec=%s tier=%s encoder=%s -> hw=%s jobs=%d remux=%s "
                 "xcode=%s dest=%s%s",
                 config.src, config.out_codec.value, config.tier.name, config.encoder.value,
                 hw or "software", config.jobs, config.remux_to_mp4, config.compat_transcode,
                 config.source_action.value, f" (retry {len(files)} files)" if files else "")

        import time as _time
        files = list(files) if files is not None else list(pipeline.iter_video_files(config))
        total = len(files)

        # ETA by WORK, not by file count. A run is mostly instant skips plus a few
        # slow encodes, so seconds-per-file is meaningless early (two skips → a
        # fantasy 14-min estimate for 1,882 files). Instead predict each file's wall
        # cost: an encode ≈ its video duration ÷ encoder speed; a remux is a quick
        # stream copy; a skip is ~free. The live ETA below then SELF-CORRECTS this
        # by how our prediction has tracked the real clock so far.
        # Constants measured from real runs on this machine (M1, VideoToolbox):
        # an encode averaged ~230s a file at ~11x realtime, a remux ~10s, and every
        # flavour of skip landed between 0.02s and 0.25s. enc_speed stays a little
        # pessimistic because corr below scales it to whatever the content really is.
        # Hardware speed measured on this machine (M1 + h264_videotoolbox, 1080p):
        # ffmpeg reported 4.4-6.3x realtime and encodes took a 303s median, so the
        # old 8.0 under-priced every encode by about a third. 6.0 starts slightly
        # pessimistic, which is the right way for a clock to be wrong, and corr
        # below pulls it to whatever this run's content and codec really do.
        # Two speeds, because a run can now be MIXED: files picked out for software
        # cost roughly 7x what the same file costs in hardware, so a single global
        # constant would make the clock nonsense the moment one film is ticked.
        #
        # Both are ×realtime AT 1080p and scale with frame size, because an encoder
        # is really a pixels-per-second machine: 4K costs ~4x what 1080p costs. A
        # flat ×realtime figure under-priced 4K by that factor.
        # Measured on this machine (M-series, 1080p, preset medium): hardware 6.0x
        # — the old constant was already right — and software 0.82x, against an
        # assumed 0.18x that was 4.5x too pessimistic. That mattered beyond the
        # clock: the picker quotes this figure BEFORE you commit, with no chance to
        # self-correct, so it was quoting 545 hours for a job nearer 120.
        HW_SPEED, SW_SPEED = 6.0, 0.82            # ×realtime at 1080p
        enc_speed = HW_SPEED if hw else SW_SPEED
        REMUX_S, SKIP_S = 10.0, 0.10
        probe_by_path = {info.path: info for info, _ in self._probes}

        # A RESUMED file costs nothing — the pipeline sees it in the ledger and
        # returns immediately (measured: 0.02s). decide() knows nothing about the
        # ledger, so without this every already-done file was budgeted as a full
        # encode. On a resumed run that is thousands of phantom encodes: the model
        # "completed" hundreds of predicted hours in seconds, which collapsed the
        # calibration below and with it the whole estimate.
        run_ledger = pipeline.Ledger(config)

        def _resumed(f: Path) -> bool:
            if not run_ledger.enabled:
                return False
            try:
                return run_ledger.has(run_ledger.key(f))
            except OSError:
                return False

        def _work_of(info) -> float:
            """Predicted wall-seconds for one PROBED file (a skip is ~free)."""
            mode = pipeline.decide(config, info)[0]
            if mode in (Mode.SHRINK, Mode.TRANSCODE):
                # This file's OWN speed: a ticked file is software even on a
                # hardware run, and it dominates the clock when it is.
                spd = SW_SPEED if config.forces_software(info.path) else enc_speed
                spd *= _PX_1080P / info.pixels if info.pixels else 1.0   # 4K costs ~4x
                return (info.duration / spd) if info.duration else (30 * 60 / spd)
            return REMUX_S if mode is Mode.REMUX else SKIP_S

        # Average over the PROBED MIX — skips included. An un-probed file is assumed
        # to be a TYPICAL file for this library, NOT automatically a full encode:
        # assuming every un-probed file was an encode is what produced the 380-hour
        # fantasy on a library that's mostly already-lean skips.
        probed_works = [_work_of(info) for info, _ in self._probes if info.ok]
        avg_work = (sum(probed_works) / len(probed_works)) if probed_works else (5 * 60 / enc_speed)

        def _work(f):
            if _resumed(f):
                return SKIP_S
            info = probe_by_path.get(f)
            return _work_of(info) if (info and info.ok) else avg_work

        # Which files are real WORK (an encode), as opposed to an instant skip.
        # Un-probed files count as work: we cannot know yet, and a file that
        # appears in the list mid-run is worse than one that leaves it.
        def _is_work(f: Path) -> bool:
            if _resumed(f):
                return False
            info = probe_by_path.get(f)
            if info is None or not info.ok:
                return True
            return pipeline.decide(config, info)[0] in (Mode.SHRINK, Mode.TRANSCODE)

        work_files = [f for f in files if _is_work(f)]
        work_names = {f.name for f in work_files}
        log.info("queue: %d file(s) to process of %d scanned", len(work_files), len(files))
        work_by_path = {f: _work(f) for f in files}
        total_work = sum(work_by_path.values())
        est_seconds = int(max(1, total_work))
        if self.window:
            # The ordered file names let the progress list pin the current file and
            # show what's coming next. It used to ship only the first 2000 — so on a
            # big library every file past #2000 fell out of the queue entirely: no
            # "processing" row, no upcoming files, just a list of what was already
            # done. Send all of them, chunked so no single JS call is enormous.
            # Only the files that will actually be WORKED ON. A library is mostly
            # files that are already efficient and get left alone in milliseconds;
            # counting those in the progress bar buries the real work ("3 of 1,155"
            # crawling for an hour) and tells the user nothing they can act on. The
            # skipped files still appear in the end-of-run report, with the reason
            # for each. Anything we could not probe stays IN the queue — unknown is
            # not the same as "will be skipped", and it is better to over-count the
            # work than to have a file appear from nowhere mid-run.
            names = [f.name for f in work_files[:_QUEUE_MAX]]
            self.window.evaluate_js(
                f"window.__vtcRunStart && window.__vtcRunStart({len(work_files)}, "
                f"{est_seconds}, {json.dumps(names[:_QUEUE_CHUNK])})")
            for i in range(_QUEUE_CHUNK, len(names), _QUEUE_CHUNK):
                self.window.evaluate_js(
                    f"window.__vtcQueueMore && window.__vtcQueueMore("
                    f"{json.dumps(names[i:i + _QUEUE_CHUNK])})")
            if len(work_files) > _QUEUE_MAX:
                log.warning("queue list truncated at %d of %d files (list only, the "
                            "run still covers everything)", _QUEUE_MAX, len(work_files))

        run_t0 = _time.monotonic()
        # Two POOLS, calibrated separately. Measured on real runs: an encode averages
        # ~230s while every kind of skip is 0.02-0.25s, so the two classes are five
        # thousand times apart and their prediction errors are unrelated — one shared
        # correction factor just lets the thousands of skips drag the handful of
        # encodes around. Heavy = encodes and remuxes (predicted work, scaled by how
        # the prediction has actually tracked); light = skips and resumes (a measured
        # flat cost per file, since predicting them individually is pointless).
        HEAVY_S = 5.0                        # predicted-seconds above which a file is "heavy"
        eta_state = {
            "file_t0": run_t0, "boundary": run_t0,
            "heavy_left": sum(w for w in work_by_path.values() if w >= HEAVY_S),
            "light_left": sum(1 for w in work_by_path.values() if w < HEAVY_S),
            "heavy_pred": 0.0, "heavy_real": 0.0,       # finished heavy: predicted vs actual
            "light_real": 0.0, "light_done": 0,
        }
        # Progress throttling is PER FILE. With jobs>1 every worker calls prog()
        # for its own file against one shared "last frac", so whichever reported
        # most recently suppressed the others and their bars sat frozen. eta stays
        # shared — there is only one clock.
        last = {"eta": 0.0}
        seen: dict[str, dict] = {}                   # label -> {"frac", "t"}

        # ETA cadence. Within a single file the estimate barely moves, so re-emitting
        # it every few seconds only made the clock twitch and flashed "re-estimating…"
        # at the user for no new information. Instead: re-estimate at every file
        # boundary (emit()), then every ETA_REFRESH seconds for the first
        # ETA_SETTLE seconds of a file — which covers short files and titles, where
        # the boundaries are what actually move the number — then leave the clock
        # alone to count down until the file ends.
        ETA_REFRESH, ETA_SETTLE = 30.0, 300.0

        def _emit_eta():
            """Remaining PREDICTED WORK × how much a predicted second really costs.

            Never seconds-per-file × files-left. That average is mix-blind, and a
            library is not a uniform mix: it is ordered, so a run can spend hours
            on instant skips (a lean show, alphabetically early) and then meet a
            block of real encodes. At 3,308 of 3,680 the flat average was 0.7s a
            file and promised 4 minutes for 372 files that were mostly encodes.
            The work model already predicts each file individually (encode ≈
            duration ÷ encoder speed, remux ≈ a stream copy, skip ≈ free), so what
            is LEFT is known — it only needs calibrating against the real clock.
            """
            if not self.window:
                return
            s = eta_state
            # How a predicted encode-second has really cost so far. Clamped so one
            # freak file (a 4K feature among episodes) cannot produce a fantasy in
            # either direction; held at 1.0 until enough encoding is behind us.
            corr = (s["heavy_real"] / s["heavy_pred"]) if s["heavy_pred"] > 30 else 1.0
            corr = max(0.2, min(5.0, corr))
            # Skips get a MEASURED flat cost, not a predicted one.
            rate = (s["light_real"] / s["light_done"]) if s["light_done"] > 20 else SKIP_S
            eta = s["heavy_left"] * corr + s["light_left"] * rate
            self.window.evaluate_js(f"window.__vtcETA && window.__vtcETA({max(0.0, eta):.0f})")

        def prog(label, frac, stats=None):
            if not self.window:
                return
            f = -1.0 if frac is None else float(frac)
            now = _time.monotonic()
            if (now - eta_state["file_t0"] < ETA_SETTLE
                    and now - last["eta"] >= ETA_REFRESH):
                last["eta"] = now
                _emit_eta()
            # emit on a ~1% move OR at least every ~1.5s (so stats keep ticking),
            # judged against THIS file's own last frame
            st = seen.setdefault(label, {"frac": -1.0, "t": 0.0})
            if frac is not None and abs(f - st["frac"]) < 0.01 and (now - st["t"]) < 1.5:
                return
            st["frac"] = f
            st["t"] = now
            self.window.evaluate_js(
                f"window.__vtcEncodeProgress && window.__vtcEncodeProgress("
                f"{json.dumps(label)}, {'null' if frac is None else f}, "
                f"{json.dumps(stats or {})})")

        def emit(r):
            seen.pop(r.path.name, None)             # this file is no longer in flight
            # Book this file's REAL wall time against the pool it was predicted in.
            # (With jobs>1 the per-file split is rough, but the pool totals still
            # add up to the wall clock, which is all the ratios need.)
            now = _time.monotonic()
            dt = max(0.0, now - eta_state["boundary"])
            eta_state["boundary"] = now
            w = work_by_path.get(r.path, avg_work)
            if w >= HEAVY_S:
                eta_state["heavy_left"] = max(0.0, eta_state["heavy_left"] - w)
                eta_state["heavy_pred"] += w
                eta_state["heavy_real"] += dt
            else:
                eta_state["light_left"] = max(0, eta_state["light_left"] - 1)
                eta_state["light_real"] += dt
                eta_state["light_done"] += 1
            if r.outcome is Outcome.ERROR:
                log.error("  FAIL %s: %s", r.path.name,
                          r.notes[0].message if r.notes else "encode failed")
            else:
                log.debug("  %s %s", r.outcome.value, r.path.name)
            if self.window:
                row = _row(r)
                # Was this one of the files the progress bar is counting? Skips
                # still reach the report — they just do not move the bar.
                row["work"] = r.path.name in work_names
                self.window.evaluate_js(
                    f"window.__vtcOnResult && window.__vtcOnResult({json.dumps(row)})")
            _emit_eta()                             # file boundary: the honest moment
            eta_state["file_t0"] = last["eta"] = _time.monotonic()

        def notify(event, path):
            """Placement told us the output volume went missing/stuck (or came back).
            Raise a banner in the UI so the user can reconnect the share and let the
            run continue on its own — a stalled network move is the one thing that can
            otherwise look like a frozen app (see the S02E01 incident)."""
            if not self.window:
                return
            self._volume_stuck = (event == netmove.STUCK)
            fn = "__vtcVolumeStuck" if event == netmove.STUCK else "__vtcVolumeBack"
            log.warning("output volume %s: %s", event, path)
            self.window.evaluate_js(
                f"window.{fn} && window.{fn}({json.dumps(path)})")

        results = pipeline.run(config, progress=prog, on_result=emit, files=files,
                               notify=notify)
        summary = _summary(results)
        summary["mins"] = int((_time.monotonic() - run_t0) / 60)   # real elapsed (was hardcoded 0)
        summary["stopped"] = pipeline.stop_requested()              # user hit either Stop
        # remember which files errored so the report can offer a software retry
        self._last_failed = [r.path for r in results if r.outcome is Outcome.ERROR]
        summary["failed_retryable"] = len(self._last_failed)
        log.info("run done: %s", summary)
        # Reached the end under our own power (finished, stopped, or aborted — all
        # user-controlled). Nothing is left dangling, so forget the resume session; it
        # only survives to next launch when the app dies WITHOUT getting here.
        self._clear_session()
        if self.window:
            self.window.evaluate_js(
                f"window.__vtcOnDone && window.__vtcOnDone({json.dumps(summary)})")


# ── FileResult -> the mockup's report row / summary shapes ────────────────────
_OK = {Outcome.SHRINK, Outcome.TRANSCODE, Outcome.REMUX}
# Plain-English "left alone" labels — the raw outcome names ("existing", "modern",
# "at tier") were cryptic in the report.
_SKIP_LABEL = {
    Outcome.SKIP_AT_TIER: "already efficient",
    Outcome.SKIP_MODERN: "already modern",
    Outcome.SKIP_EXISTING: "already converted",
    Outcome.SKIP_MIN_SAVING: "saving too small",
    Outcome.SKIP_INCOMPATIBLE: "codec kept",
    Outcome.SKIP_NON_MP4: "left as-is",
    Outcome.SKIP_CODEC: "unsupported codec",
    Outcome.RESUME: "already done",
}
_SKIP = {Outcome.SKIP_AT_TIER, Outcome.SKIP_MODERN, Outcome.SKIP_EXISTING,
         Outcome.SKIP_MIN_SAVING, Outcome.SKIP_INCOMPATIBLE, Outcome.SKIP_CODEC, Outcome.RESUME}


def _human_gb(n: int) -> float:
    return n / 1e9


def _human2(n: int) -> str:
    """Size with two decimals and an auto unit — '1.90 GB', '463.74 MB' — instead of
    the old one-decimal GB that flattened every SD file to a useless '0.4 GB'."""
    n = max(0, int(n))
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f} GB"
    if n >= 1_000_000:
        return f"{n / 1e6:.2f} MB"
    if n >= 1_000:
        return f"{n / 1e3:.2f} KB"
    return f"{n} B"


def _row(r) -> dict:
    if r.outcome in _OK:
        t, sev = "ok", ""
        d = (f"{_human2(r.src_bytes)} → {_human2(r.out_bytes)}"
             if r.src_bytes else r.outcome.value)
    elif r.outcome is Outcome.ERROR:
        t, sev, d = "fail", "err", (r.notes[0].message if r.notes else "encode failed")
    else:
        t, sev = "skip", ""
        d = _SKIP_LABEL.get(r.outcome, r.outcome.value.replace("skip-", "").replace("-", " "))
    if r.notes and t != "fail":
        sev = "warn" if any(n.level == "WARN" for n in r.notes) else "note"
        t = "fail" if sev == "warn" else t   # WARN surfaces under "needs a look"
    # the structured "what happened" record — caption drives the log/detail, star
    # marks a row worth reading (kept a non-MP4 container, subtitle caveat, …)
    detail = r.detail.caption() if r.detail else ""
    star = bool(r.detail and r.detail.has_note)
    return {"name": r.path.name, "t": t, "d": d, "sev": sev,
            "detail": detail, "star": star,
            "sbytes": r.src_bytes, "obytes": r.out_bytes}


def _summary(results: list) -> dict:
    done = sum(1 for r in results if r.outcome in _OK)
    fail = sum(1 for r in results if r.outcome is Outcome.ERROR)
    skip = sum(1 for r in results if r.outcome in _SKIP)
    saved = sum(r.saved_bytes for r in results)
    return {"done": done, "skip": skip, "fail": fail, "tb": saved / 1e12, "mins": 0}


def main(argv: list[str] | None = None) -> int:
    reconfigure_std_streams()   # UTF-8 stdout/stderr before anything prints a path
    log_path = _setup_logging()
    _install_crash_handlers(log_path)
    try:
        import webview
    except ModuleNotFoundError:
        print("pywebview is required:  pip install pywebview", file=sys.stderr)
        return 1
    argv = sys.argv[1:] if argv is None else argv
    # explicit path wins; otherwise the copy bundled beside this module / in the app
    html = Path(argv[0]) if argv else _bundled_html()
    if not html.is_file():
        legacy = Path.home() / "Downloads" / "vtc_app_v3.html"   # dev fallback
        html = legacy if legacy.is_file() else html
    if not html.is_file():
        print(f"HTML not found: {html}", file=sys.stderr)
        return 2
    _log_environment()          # record tools + hardware ability on load
    # Serve the app over the same local HTTP server that serves the previews, so the
    # page origin is http:// — the generated preview <video>s then load without the
    # file:// mixed-content restrictions WKWebView/EdgeChromium impose.
    pdir, port = _ensure_preview_server()
    try:
        shutil.copyfile(html, pdir / "vtc_app_v3.html")
        target = f"http://127.0.0.1:{port}/vtc_app_v3.html"
    except OSError:
        target = str(html)      # fall back to file:// if the copy fails
    log.info("loading %s (html=%s)", target, html)
    print(f"Very Thoughtful Compression — debug log: {log_path}")
    api = Api()
    window = webview.create_window("Very Thoughtful Compression", target, js_api=api,
                                   width=1360, height=1020, min_size=(940, 720))
    api.window = window
    window.events.loaded += lambda: (log.info("window loaded"), window.evaluate_js(_BRIDGE_JS))
    try:
        window.events.closing += lambda: log.info("window closing (user)")
        window.events.closed += lambda: log.info("window closed")
    except Exception:  # noqa: BLE001 — event names vary across pywebview versions
        pass
    log.info("webview.start()")
    try:
        webview.start()
    except Exception:
        log.critical("webview.start() crashed", exc_info=True)
        raise
    log.info("=== app exited normally ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
