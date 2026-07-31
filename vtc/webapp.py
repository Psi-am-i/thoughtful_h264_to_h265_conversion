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

from . import encode, pipeline
from .config import AudioPolicy, Container, Encoder, OutputMode, RunConfig, SourceAction
from .ffprobe import probe
from .model import OutCodec, Tier, target_kbps
from .result import Mode, Outcome


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
                                 timeout=15).stdout.splitlines()[0]
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
                             stdin=subprocess.DEVNULL, timeout=5).stdout
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
    # Subtitle policy from Advanced settings.
    sub = adv.get("subs")
    if isinstance(sub, str) and sub in ("all", "forced", "hoh", "lang"):
        cfg.sub_mode = sub
    langs = adv.get("subLangs")
    if isinstance(langs, list):
        cfg.sub_langs = tuple(str(x) for x in langs)
    _apply_advanced(cfg, adv)
    return cfg


_AUDIO_POLICY = {"passthrough": AudioPolicy.PASSTHROUGH, "aac": AudioPolicy.AAC,
                 "ac3": AudioPolicy.AC3, "flac": AudioPolicy.FLAC}
_CONTAINER = {"auto": Container.AUTO, "mp4": Container.MP4, "mkv": Container.MKV}


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
    document.getElementById('now-n').textContent = `${SRC.files.toLocaleString()} files`;
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
  window.__vtcProbeProgress = ()=> { if(SRC) drawEstimate(); }; // refine estimate as files are probed
  window.__vtcProbesReady = ()=> { if(SRC) drawEstimate(); };   // final: fully measured

  // Run -> real pipeline.run streamed back per file, with LIVE progress, then the report.
  const acc = [];
  window.__vtcRunStart = (total, est, files)=> pgStart(total, est, files);   // engine: run begins
  window.__vtcETA = (sec)=> pgSetETA(sec);                                    // engine: work-based ETA
  window.__vtcEncodeProgress = (name, frac, stats)=> pgFile(name, frac, stats);  // current file
  window.__vtcStop = ()=> { try { api.stop_run(); } catch(e){} };        // Stop button
  window.__vtcRegenPreviews = (codec, start)=> { try { api.regenerate_previews(codec||'h265', start); } catch(e){} };
  window.__vtcRetryFailed = ()=> { try { api.retry_failed_software(); } catch(e){} };
  window.__vtcSaveLog = (text)=> { try { api.save_text_file('vtc-run-report.txt', text); } catch(e){} };
  window.__vtcOnResult = (r)=>{                                          // r: {name,t,d,sev}
    acc.push(r);
    pgDone1({ f:r.name, t:r.t, sev:r.sev });
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
    api.run(Object.assign({adv: window.ADV||{}, outputDir: window.__outputDir||''}, answers));
  };
  // "New folder" destination: let the user pick where outputs go. Returns the dir
  // (or '' if cancelled — build_config then falls back to <src>/converted).
  window.__pickOutputDir = async ()=>{
    try { const d = await api.pick_output_folder(); if(d && d.dir){ window.__outputDir = d.dir; return d.dir; } }
    catch(e){}
    return window.__outputDir || '';
  };
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
        self._preview_gen = 0              # bumped whenever previews are (re)requested;
        self._preview_start = 0.0         # a running worker aborts if its gen is stale
        self._preview_seg = 5.0           # sample length (seconds)
        self._last_config: RunConfig | None = None   # last run's config (for retry)
        self._last_failed: list[Path] = []           # files that ERRORed last run

    # -- folder pick + fast scan -------------------------------------------------
    def pick_output_folder(self):
        """Native folder picker for the 'New folder' destination. Returns {dir} or
        None (cancelled) — does not scan; just the path the outputs should go to."""
        import webview
        picked = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not picked:
            return None
        return {"dir": str(Path(picked[0]))}

    def pick_folder(self):
        import webview
        log.info('pick_folder: opening native folder dialog')
        picked = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not picked:
            return None
        src = Path(picked[0])
        self._src = src
        self._probes, self._probed_for = [], None
        self._total_files, self._total_tb = 0, 0.0
        self._preview_start = 0.0         # new folder -> sample from the middle again
        # Count files in the BACKGROUND. On a large library the walk takes seconds;
        # doing it inline froze the "Choose media folder" button with no feedback, so
        # people clicked again and reopened the dialog. Return the path immediately so
        # the UI flips to a "Scanning…" state; __vtcScanDone fills in the counts.
        self._scan_gen = getattr(self, "_scan_gen", 0) + 1
        threading.Thread(target=self._scan_folder, args=(src, self._scan_gen), daemon=True).start()
        threading.Thread(target=self._warm_probes, args=(src,), daemon=True).start()
        # Build the real preview encodes in the background while they configure.
        self._preview_gen += 1
        threading.Thread(target=self._preview_worker,
                         args=(src, _PREVIEW_CODEC, self._preview_gen), daemon=True).start()
        return {"k": str(src), "scanning": True}

    def _scan_folder(self, src: Path, gen: int) -> None:
        """Walk the library counting files/size/non-MP4, then hand the totals to the
        UI. Runs off the pick_folder call so the interface never freezes on a scan."""
        files = total = nonmp4 = 0
        for f in pipeline.iter_video_files(RunConfig(src=src)):
            files += 1
            if f.suffix.lower() != ".mp4":
                nonmp4 += 1
            try:
                total += f.stat().st_size
            except OSError:
                pass
        if self._src != src or gen != self._scan_gen:
            return                        # a newer folder pick superseded this scan
        self._total_files, self._total_tb = files, total / 1e12
        self._emit("__vtcScanDone",
                   {"k": str(src), "files": files, "tb": total / 1e12, "nonmp4": nonmp4})

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
            r = subprocess.run(
                [FFMPEG, "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(first),
                 "-t", f"{seglen:.3f}", "-map", "0:v:0", *svargs, "-an",
                 "-movflags", "+faststart", str(sample)],
                stdin=subprocess.DEVNULL, capture_output=True, text=True)
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
                else:
                    out = pdir / f"{key}.mp4"
                    tgt = target_kbps(tier, sinfo.pixels, sinfo.fps, codec)
                    cfg2 = RunConfig(src=src, out_codec=codec, tier=tier, ffmpeg=FFMPEG)
                    vargs = encode.build_video_args(cfg2, sinfo, Mode.SHRINK, tgt, hw)
                    rr = subprocess.run(
                        [FFMPEG, "-y", "-v", "error", "-i", str(sample), *vargs, "-an",
                         "-movflags", "+faststart", str(out)],
                        stdin=subprocess.DEVNULL, capture_output=True, text=True)
                    if rr.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                        log.error("preview %s failed: %s", key,
                                  (rr.stderr or "").strip().splitlines()[-1:])
                        self._emit("__vtcPreviewPanel", {"i": idx, "key": key, "label": label,
                                                         "codec": panel_codec, "error": "encode failed"})
                        continue
                size = out.stat().st_size
                self._emit("__vtcPreviewPanel",
                           {"i": idx, "key": key, "label": label, "codec": panel_codec, "bytes": size,
                            "url": f"http://127.0.0.1:{port}/{out.name}?v={size}"})
            self._emit("__vtcPreviewDone", "")
            log.info("previews: done (codec=%s)", codec.value)
        except Exception as e:  # noqa: BLE001 — previews must never crash the app
            log.exception("preview worker failed")
            self._emit("__vtcPreviewError", str(e))

    def _warm_probes(self, src: Path):
        """Probe every file, publishing partial results periodically so the
        estimate refines live (file count rising, prediction updating)."""
        probes = []
        done = 0
        for f in pipeline.iter_video_files(RunConfig(src=src)):
            if self._src != src:              # folder changed under us — abandon
                return
            info = probe(f, FFPROBE)
            done += 1
            if info.ok and info.vcodec:
                try:
                    probes.append((info, f.stat().st_size))
                except OSError:
                    pass
            if done % 25 == 0:
                self._probes = list(probes)   # publish a measured-so-far sample
                if self.window:
                    self.window.evaluate_js(f"window.__vtcProbeProgress && window.__vtcProbeProgress({done})")
        if self._src == src:
            self._probes, self._probed_for = probes, src
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
        for info, size in self._probes:
            mode, _outcome, target = pipeline.decide(config, info)
            src_kbps = info.effective_bps / 1000.0
            if mode in (Mode.SHRINK, Mode.TRANSCODE) and src_kbps > 0:
                out_bytes += int(size * min(1.0, target / src_kbps)); reencoded += 1
            else:                                             # remux (~same size) or skip
                out_bytes += size; skipped += 1
            src_bytes += size
        ratio = (out_bytes / src_bytes) if src_bytes else 1.0   # sample's out/in ratio
        sample = len(self._probes)
        scale = (self._total_files / sample) if sample else 1.0
        return {
            "out_tb": self._total_tb * ratio,                   # extrapolate the ratio to the library
            "saved_pct": round((1 - ratio) * 100),
            "reencoded": round(reencoded * scale),
            "skipped": round(skipped * scale),
            "measured": self._probed_for == self._src,          # True once the full scan finishes
        }

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
        """Ask the run to stop after the current file(s) (graceful, no corruption)."""
        try:
            pipeline.STOP_FILE.touch()
            log.info("stop requested")
        except OSError as e:
            log.error("stop_run: %s", e)
        return {"stopping": True}

    def save_text_file(self, name: str, content: str):
        """Write `content` to a path the user picks in a native Save dialog, UTF-8.
        Replaces the old blob+download, which made WKWebView navigate the whole app
        window to the text (mojibake, no way back)."""
        import webview
        try:
            picked = self.window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=name or "report.txt")
        except Exception as e:  # noqa: BLE001
            log.error("save dialog failed: %s", e); return {"error": str(e)}
        if not picked:
            return {"cancelled": True}
        path = picked[0] if isinstance(picked, (list, tuple)) else picked
        try:
            Path(path).write_text(content, encoding="utf-8")
            log.info("saved report -> %s", path)
        except OSError as e:
            log.error("could not write %s: %s", path, e); return {"error": str(e)}
        return {"saved": str(path)}

    def run(self, answers: dict):
        if self._src is None:
            return {"error": "no folder"}
        try:
            config = build_config(self._src, answers)
        except ValueError as e:
            log.error("run: bad config: %s", e)
            return {"error": str(e)}
        pipeline.STOP_FILE.unlink(missing_ok=True)   # clear any prior stop flag
        self._last_config = config
        threading.Thread(target=self._run_worker, args=(config,), daemon=True).start()
        return {"started": True}

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
        pipeline.STOP_FILE.unlink(missing_ok=True)
        log.info("retry_failed_software: %d file(s)", len(failed))
        threading.Thread(target=self._run_worker, args=(soft,), kwargs={"files": failed},
                         daemon=True).start()
        return {"started": True, "count": len(failed)}

    def _run_worker(self, config: RunConfig, files=None):
        hw = encode.select_hw_encoder(config)
        log.info("run start: src=%s codec=%s tier=%s encoder=%s -> hw=%s remux=%s xcode=%s dest=%s%s",
                 config.src, config.out_codec.value, config.tier.name, config.encoder.value,
                 hw or "software", config.remux_to_mp4, config.compat_transcode,
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
        enc_speed = 8.0 if hw else 0.18            # encode ×realtime (hardware vs software)
        REMUX_S, SKIP_S = 4.0, 0.05
        probe_by_path = {info.path: info for info, _ in self._probes}
        _enc_durs = [info.duration for info, _ in self._probes
                     if info.duration and pipeline.decide(config, info)[0] in (Mode.SHRINK, Mode.TRANSCODE)]
        avg_enc_work = (sum(_enc_durs) / len(_enc_durs) / enc_speed) if _enc_durs else (30 * 60 / enc_speed)

        def _work(f):
            info = probe_by_path.get(f)
            if info is None or not info.ok:
                return avg_enc_work                # not probed yet → assume an average encode
            mode = pipeline.decide(config, info)[0]
            if mode in (Mode.SHRINK, Mode.TRANSCODE):
                return (info.duration / enc_speed) if info.duration else avg_enc_work
            return REMUX_S if mode is Mode.REMUX else SKIP_S

        work_by_path = {f: _work(f) for f in files}
        total_work = sum(work_by_path.values())
        est_seconds = int(max(1, total_work))
        if self.window:
            # the ordered file names let the progress list show what's coming next;
            # cap what we ship so a huge library doesn't bloat the JS call (the UI
            # windows the list anyway and shows "+N more" beyond the cap).
            names = [f.name for f in files[:2000]]
            self.window.evaluate_js(
                f"window.__vtcRunStart && window.__vtcRunStart({total}, {est_seconds}, "
                f"{json.dumps(names)})")

        run_t0 = _time.monotonic()
        eta_state = {"done_work": 0.0}
        last = {"frac": -1.0, "t": 0.0, "eta": 0.0}   # throttle progress + ETA chatter

        def _emit_eta():
            if not self.window:
                return
            elapsed = _time.monotonic() - run_t0
            done = eta_state["done_work"]
            remaining = max(0.0, total_work - done)
            corr = (elapsed / done) if done > 30 else 1.0   # learn the real speed once past the noise
            self.window.evaluate_js(f"window.__vtcETA && window.__vtcETA({remaining * corr:.0f})")

        def prog(label, frac, stats=None):
            if not self.window:
                return
            f = -1.0 if frac is None else float(frac)
            now = _time.monotonic()
            if now - last["eta"] >= 3.0:            # refresh the work-based ETA every ~3s
                last["eta"] = now
                _emit_eta()
            # emit on a ~1% move OR at least every ~1.5s (so stats keep ticking)
            if frac is not None and abs(f - last["frac"]) < 0.01 and (now - last["t"]) < 1.5:
                return
            last["frac"] = f
            last["t"] = now
            self.window.evaluate_js(
                f"window.__vtcEncodeProgress && window.__vtcEncodeProgress("
                f"{json.dumps(label)}, {'null' if frac is None else f}, "
                f"{json.dumps(stats or {})})")

        def emit(r):
            last["frac"] = -1.0                     # next file starts fresh
            eta_state["done_work"] += work_by_path.get(r.path, avg_enc_work)
            if r.outcome is Outcome.ERROR:
                log.error("  FAIL %s: %s", r.path.name,
                          r.notes[0].message if r.notes else "encode failed")
            else:
                log.debug("  %s %s", r.outcome.value, r.path.name)
            if self.window:
                self.window.evaluate_js(
                    f"window.__vtcOnResult && window.__vtcOnResult({json.dumps(_row(r))})")
            _emit_eta()

        results = pipeline.run(config, progress=prog, on_result=emit, files=files)
        summary = _summary(results)
        summary["mins"] = int((_time.monotonic() - run_t0) / 60)   # real elapsed (was hardcoded 0)
        summary["stopped"] = pipeline.STOP_FILE.exists()            # user hit Stop mid-run
        # remember which files errored so the report can offer a software retry
        self._last_failed = [r.path for r in results if r.outcome is Outcome.ERROR]
        summary["failed_retryable"] = len(self._last_failed)
        log.info("run done: %s", summary)
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
