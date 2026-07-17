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
import threading
from pathlib import Path

from . import pipeline
from .config import Encoder, OutputMode, RunConfig, SourceAction
from .ffprobe import probe
from .model import OutCodec, Tier
from .result import Outcome

# ── mockup answer-index -> engine value (mirrors the M model in the HTML) ─────
_CODECS = [OutCodec.H265, OutCodec.H264, None]          # 2 = AV1 (not supported yet)
_TIERS = [Tier.OK, Tier.GOOD, Tier.EXCELLENT, Tier.STELLAR, Tier.INSANE]
_SAVING = [0.15, 0.25, 0.40]
_COMPAT = [(True, True), (True, False), (False, False)]  # (remux, transcode)
_ENCODER = [Encoder.HARDWARE, Encoder.SOFTWARE]


def build_config(src: Path, a: dict) -> RunConfig:
    """Map the mockup's `answers` (question id -> chosen index) to a RunConfig."""
    codec = _CODECS[a["codec"]]
    if codec is None:
        raise ValueError("AV1 output is not supported by the engine yet")
    remux, transcode = _COMPAT[a["compat"]]
    dest = a["dest"]  # 0 archive, 1 delete, 2 keep both
    if dest == 2:
        output_mode, source_action, output_dir = OutputMode.SEPARATE, SourceAction.KEEP, src / "converted"
    else:
        output_mode, output_dir = OutputMode.INPLACE, None
        source_action = SourceAction.ARCHIVE if dest == 0 else SourceAction.DELETE
    return RunConfig(
        src=src, out_codec=codec, tier=_TIERS[a["quality"]],
        min_saving_ratio=1.0 - _SAVING[a["saving"]],
        remux_to_mp4=remux, compat_transcode=transcode,
        encoder=_ENCODER[a["encoder"]],
        output_mode=output_mode, output_dir=output_dir, source_action=source_action,
    )


# ── the JS bridge, injected after the page loads (only takes effect in-shell) ──
_BRIDGE_JS = r"""
(function(){
  if(!window.pywebview || !window.pywebview.api){ return; }   // standalone file: keep mock
  const api = window.pywebview.api;

  // Real mode has no "recent folders": Start goes straight to the native picker.
  try { FOLDERS.length = 0; } catch(e){}
  document.getElementById('picks').innerHTML =
    '<button class="pick" data-f="-1"><b>Browse…</b><span>choose a media folder</span></button>';
  document.getElementById('start').onclick = ()=> pickFolder(-1);

  // Folder pick -> native dialog + real scan, then the mockup's own transition.
  window.pickFolder = async ()=>{
    const s = await api.pick_folder();
    if(!s) return;                                  // cancelled
    SRC = s;                                         // {k, files, tb}
    document.getElementById('src-v').textContent = SRC.k;
    document.getElementById('readout').classList.add('slid');
    document.getElementById('unit').classList.remove('off');
    document.getElementById('unit').classList.add('on');
    render(); setTimeout(paintCorpse, 80);
  };
  document.querySelectorAll('#picks .pick').forEach(b=> b.onclick = ()=> pickFolder(+b.dataset.f));

  // Estimate -> real per-file plan math (measured once files are probed).
  const baseEstimate = window.drawEstimate;
  window.drawEstimate = ()=>{
    if(!SRC) return;
    document.getElementById('now-v').innerHTML = `${SRC.tb.toFixed(2)}<span>TB</span>`;
    document.getElementById('now-n').textContent = `${SRC.files.toLocaleString()} files`;
    if(answers.codec === undefined){ return baseEstimate(); }   // not enough set yet
    api.estimate(answers).then(e=>{
      if(!e || e.error){ return baseEstimate(); }
      document.getElementById('est').innerHTML = `${e.out_tb.toFixed(2)}<span>TB</span>`;
      document.getElementById('est-d').textContent = `−${e.saved_pct}% · ${(SRC.tb-e.out_tb).toFixed(2)} TB back`;
      document.getElementById('est-n').textContent =
        `${e.reencoded.toLocaleString()} re-encoded · ${e.skipped.toLocaleString()} already at tier, left alone. ${e.measured?'Measured.':'Modelled while probing…'}`;
      gateStart();
    });
    gateStart();
  };
  window.__vtcProbeProgress = ()=> { if(SRC) drawEstimate(); }; // refine estimate as files are probed
  window.__vtcProbesReady = ()=> { if(SRC) drawEstimate(); };   // final: fully measured

  // Run -> real pipeline.run streamed back per file, then the mockup's report.
  const acc = [];
  window.__vtcOnResult = (r)=>{ acc.push(r); };                 // r: {name, outcome, saved_bytes, note}
  window.__vtcOnDone = (summary)=>{
    RUN = {
      rows: acc.map(r=>({ f:r.name, t:r.t, d:r.d, sev:r.sev })),
      done: summary.done, skip: summary.skip, fail: summary.fail,
      tb: summary.tb, mins: summary.mins,
    };
    drawReport(); openSheet('#report-sheet');
  };
  window.runNow = ()=>{
    shutSheet('#confirm-sheet');
    acc.length = 0;
    api.run(answers);            // fire-and-forget; results stream via __vtcOnResult/__vtcOnDone
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

    # -- folder pick + fast scan -------------------------------------------------
    def pick_folder(self):
        import webview
        picked = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not picked:
            return None
        src = Path(picked[0])
        self._src = src
        files = total = 0
        for f in pipeline.iter_video_files(RunConfig(src=src)):
            files += 1
            try:
                total += f.stat().st_size
            except OSError:
                pass
        self._probes, self._probed_for = [], None
        self._total_files, self._total_tb = files, total / 1e12
        threading.Thread(target=self._warm_probes, args=(src,), daemon=True).start()
        return {"k": str(src), "files": files, "tb": total / 1e12}

    def _warm_probes(self, src: Path):
        """Probe every file, publishing partial results periodically so the
        estimate refines live (file count rising, prediction updating)."""
        probes = []
        done = 0
        for f in pipeline.iter_video_files(RunConfig(src=src)):
            if self._src != src:              # folder changed under us — abandon
                return
            info = probe(f, "ffprobe")
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
    def run(self, answers: dict):
        if self._src is None:
            return {"error": "no folder"}
        try:
            config = build_config(self._src, answers)
        except ValueError as e:
            return {"error": str(e)}
        threading.Thread(target=self._run_worker, args=(config,), daemon=True).start()
        return {"started": True}

    def _run_worker(self, config: RunConfig):
        def emit(r):
            if self.window:
                self.window.evaluate_js(
                    f"window.__vtcOnResult && window.__vtcOnResult({json.dumps(_row(r))})")
        results = pipeline.run(config, on_result=emit)
        summary = _summary(results)
        if self.window:
            self.window.evaluate_js(
                f"window.__vtcOnDone && window.__vtcOnDone({json.dumps(summary)})")


# ── FileResult -> the mockup's report row / summary shapes ────────────────────
_OK = {Outcome.SHRINK, Outcome.TRANSCODE, Outcome.REMUX}
_SKIP = {Outcome.SKIP_AT_TIER, Outcome.SKIP_MODERN, Outcome.SKIP_EXISTING,
         Outcome.SKIP_MIN_SAVING, Outcome.SKIP_INCOMPATIBLE, Outcome.SKIP_CODEC, Outcome.RESUME}


def _human_gb(n: int) -> float:
    return n / 1e9


def _row(r) -> dict:
    if r.outcome in _OK:
        t, sev = "ok", ""
        d = (f"{_human_gb(r.src_bytes):.1f} → {_human_gb(r.out_bytes):.1f} GB"
             if r.src_bytes else r.outcome.value)
    elif r.outcome is Outcome.ERROR:
        t, sev, d = "fail", "err", (r.notes[0].message if r.notes else "encode failed")
    else:
        t, sev, d = "skip", "", r.outcome.value.replace("skip-", "").replace("-", " ")
    if r.notes and t != "fail":
        sev = "warn" if any(n.level == "WARN" for n in r.notes) else "note"
        t = "fail" if sev == "warn" else t   # WARN surfaces under "needs a look"
    return {"name": r.path.name, "t": t, "d": d, "sev": sev}


def _summary(results: list) -> dict:
    done = sum(1 for r in results if r.outcome in _OK)
    fail = sum(1 for r in results if r.outcome is Outcome.ERROR)
    skip = sum(1 for r in results if r.outcome in _SKIP)
    saved = sum(r.saved_bytes for r in results)
    return {"done": done, "skip": skip, "fail": fail, "tb": saved / 1e12, "mins": 0}


def main(argv: list[str] | None = None) -> int:
    import sys
    try:
        import webview
    except ModuleNotFoundError:
        print("pywebview is required:  pip install pywebview", file=sys.stderr)
        return 1
    argv = sys.argv[1:] if argv is None else argv
    html = Path(argv[0]) if argv else (Path.home() / "Downloads" / "vtc_app_v3.html")
    if not html.is_file():
        print(f"HTML not found: {html}", file=sys.stderr)
        return 2
    api = Api()
    window = webview.create_window("Very Thoughtful Compression", str(html), js_api=api,
                                   width=1280, height=980, min_size=(900, 700))
    api.window = window
    window.events.loaded += lambda: window.evaluate_js(_BRIDGE_JS)
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
