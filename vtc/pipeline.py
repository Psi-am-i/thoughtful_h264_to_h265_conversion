"""The orchestrator: scan -> decide -> encode -> place -> record.

`run()` walks the scan tree, and for each file decides a Mode (shrink/transcode/
remux) or a skip Outcome, does the work, places the output and original per the
chosen source action, updates the resume ledger, and returns a FileResult per
file. Mirrors process_one / _process_one_impl in the bash script.
"""

from __future__ import annotations

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from dataclasses import dataclass

from . import encode
from .config import Container, OutputMode, RunConfig, SourceAction
from .ffprobe import MediaInfo, probe
from .ledger import Ledger
from .model import OutCodec, classify_codec, over_target, target_kbps
from .model import CodecCategory
from .result import EncodeResult, FileResult, Mode, Note, Outcome, ProgressCB

TMPROOT = Path("/tmp/vtcwork")
STOP_FILE = Path("/tmp/hevc_stop")

# Directories never descended into during a scan.
_PRUNE_DIRS = {
    ".Trashes", ".Spotlight-V100", ".fseventsd", ".TemporaryItems",
    "originals", "new versions", "archived", "Library",
}

# Per-file event callback: (result). Used by the CLI/GUI for live logging.
ResultCB = Callable[[FileResult], None]


# ── Scanning ──────────────────────────────────────────────────────────────────
def iter_video_files(config: RunConfig):
    """Yield video files under config.src, pruning archive/system dirs."""
    exts = {"." + e.lower() for e in config.video_exts}
    for root, dirs, files in os.walk(config.src):
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
        for name in files:
            if name.startswith("._"):
                continue
            if Path(name).suffix.lower() in exts:
                yield Path(root) / name


def _rel(config: RunConfig, path: Path) -> Path:
    try:
        return path.relative_to(config.src)
    except ValueError:
        return Path(path.name)


def output_path(config: RunConfig, src_file: Path, ext: str = ".mp4") -> Path:
    """Where the produced file goes for this source (`ext` from the container)."""
    rel = _rel(config, src_file)
    if config.output_mode == OutputMode.SEPARATE:
        assert config.output_dir is not None
        if config.output_flat:
            return config.output_dir / (rel.stem + ext)
        return config.output_dir / rel.with_suffix(ext)
    return src_file.with_suffix(ext)


# ── Decision ──────────────────────────────────────────────────────────────────
def decide(config: RunConfig, info: MediaInfo) -> tuple[Mode | None, Outcome | None, int]:
    """Return (mode, skip_outcome, encode_target_kbps). Exactly one of mode/outcome is set."""
    category = classify_codec(info.vcodec)
    already_mp4 = info.path.suffix.lower() in (".mp4", ".m4v", ".mov")
    src_kbps = info.effective_bps / 1000.0

    def tgt(clamp: bool) -> int:
        return target_kbps(
            config.tier, info.pixels, info.fps, config.out_codec,
            src_kbps=src_kbps if clamp else None,
            floor_kbps=config.bitrate_floor_kbps,
        )

    if category is CodecCategory.H264:
        decision_target = tgt(clamp=False)
        worth = over_target(src_kbps, decision_target, config.tier_over_tolerance)
        if config.remux_to_mp4 and not already_mp4:
            return (Mode.SHRINK, None, tgt(clamp=True)) if worth else (Mode.REMUX, None, 0)
        if worth:
            return (Mode.SHRINK, None, tgt(clamp=True))
        return (None, Outcome.SKIP_AT_TIER, 0)

    if category is CodecCategory.MODERN:
        if config.remux_to_mp4 and not already_mp4:
            return (Mode.REMUX, None, 0)
        return (None, Outcome.SKIP_MODERN, 0)

    if category is CodecCategory.LEGACY:
        if config.compat_transcode:
            # Max-fidelity: target the source's own bitrate (never inflate).
            return (Mode.TRANSCODE, None, max(config.bitrate_floor_kbps, int(src_kbps)))
        return (None, Outcome.SKIP_INCOMPATIBLE, 0)

    return (None, Outcome.SKIP_CODEC, 0)


# ── Placement (archive / delete / keep + subtitle rescue) ─────────────────────
def _archive_dest(config: RunConfig, src_file: Path) -> Path:
    rel_parent = _rel(config, src_file).parent
    base = config.resolved_archive_dir()
    return base / rel_parent if str(rel_parent) != "." else base


def _place(config: RunConfig, src_file: Path, out: Path, tmp: Path,
           res: EncodeResult) -> list[Note]:
    """Move tmp->out, relocate sidecars, and handle the original. Returns notes."""
    notes: list[Note] = []
    dropped = res.dropped_subs_reason
    overwrites_source = out == src_file

    # Rescue the original before it is overwritten in place, if subs would be lost.
    rescued_dir: Path | None = None
    if dropped and overwrites_source:
        rescued_dir = _archive_dest(config, src_file)
        rescued_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_file), str(rescued_dir / src_file.name))

    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), str(out))
    # Relocate any sidecar .srt files the encoder wrote next to the temp.
    for sc in tmp.parent.glob(tmp.stem + "*.srt"):
        sc.rename(out.with_name(out.stem + sc.name[len(tmp.stem):]))
    if res.sidecars_made:
        notes.append(Note("NOTE", f"{res.sidecars_made} subtitle track(s) written as sidecar .srt "
                                  f"(could not be embedded in the MP4)"))

    action = config.source_action
    if action == SourceAction.DELETE:
        if dropped:
            if rescued_dir is None:
                rescued_dir = _archive_dest(config, src_file)
                rescued_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src_file), str(rescued_dir / src_file.name))
            notes.append(Note("NOTE", f"original archived to {rescued_dir} instead of deleted — {dropped}"))
        elif not overwrites_source and src_file.exists():
            src_file.unlink()
    elif action == SourceAction.ARCHIVE:
        if not overwrites_source and src_file.exists():
            dest = _archive_dest(config, src_file)
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_file), str(dest / src_file.name))
        if dropped:
            notes.append(Note("NOTE", f"output MP4 is missing subtitle track(s) — {dropped}; "
                                      f"the archived original still has them"))
    else:  # KEEP
        if rescued_dir is not None:
            notes.append(Note("NOTE", f"original moved to {rescued_dir} (it was being overwritten "
                                      f"in place) — {dropped}"))
        elif dropped:
            notes.append(Note("NOTE", f"output MP4 is missing subtitle track(s) — {dropped}; "
                                      f"the original (kept in place) still has them"))
    return notes


# ── Dry-run planning (decide without encoding) ────────────────────────────────
@dataclass
class PlanRow:
    path: Path
    info: MediaInfo
    mode: Mode | None          # set if the file would be processed
    outcome: Outcome | None    # set if the file would be skipped
    target_kbps: int

    @property
    def src_kbps(self) -> float:
        return self.info.effective_bps / 1000.0

    def projected_saving(self) -> float | None:
        """Estimated size saving fraction for a shrink/transcode; None otherwise."""
        if self.mode in (Mode.SHRINK, Mode.TRANSCODE) and self.src_kbps > 0:
            return max(0.0, 1.0 - self.target_kbps / self.src_kbps)
        if self.mode is Mode.REMUX:
            return 0.0
        return None


def plan(config: RunConfig) -> list[PlanRow]:
    """Probe + decide for every file WITHOUT encoding — powers `--dry-run`."""
    rows: list[PlanRow] = []
    for f in iter_video_files(config):
        info = probe(f, config.ffprobe)
        if not info.ok or not info.vcodec:
            rows.append(PlanRow(f, info, None, Outcome.SKIP_CODEC, 0))
            continue
        mode, outcome, target = decide(config, info)
        rows.append(PlanRow(f, info, mode, outcome, target))
    return rows


# ── Per-file processing ───────────────────────────────────────────────────────
def process_file(config: RunConfig, ledger: Ledger, hardware: bool,
                 src_file: Path, progress: ProgressCB | None = None) -> FileResult:
    lkey = ledger.key(src_file) if ledger.enabled else ""
    if ledger.enabled and ledger.has(lkey):
        return FileResult(src_file, Outcome.RESUME)

    info = probe(src_file, config.ffprobe)
    if not info.ok or not info.vcodec:
        return FileResult(src_file, Outcome.SKIP_CODEC,
                          notes=[Note("WARN", f"could not probe: {info.error}")])

    mode, skip_outcome, target = decide(config, info)
    if skip_outcome is not None:
        r = FileResult(src_file, skip_outcome)
        if ledger.enabled:
            ledger.add(lkey)
        return r

    assert mode is not None
    container = encode.resolve_container(config, info)
    ext = ".mkv" if container == Container.MKV else ".mp4"
    out = output_path(config, src_file, ext)

    # Remuxing a file into the container it already lives in is a no-op.
    if mode is Mode.REMUX and out == src_file:
        if ledger.enabled:
            ledger.add(lkey)
        return FileResult(src_file, Outcome.SKIP_MODERN)
    if out.exists() and out != src_file:
        r = FileResult(src_file, Outcome.SKIP_EXISTING)
        if ledger.enabled:
            ledger.add(lkey)
        return r

    src_bytes = src_file.stat().st_size
    TMPROOT.mkdir(parents=True, exist_ok=True)
    tmp = TMPROOT / f".{out.stem}.{os.getpid()}.{id(src_file) & 0xffff}{ext}"

    res = encode.run_encode(config, info, mode, src_file, tmp, target, hardware, container, progress)
    if not res.ok:
        tmp.unlink(missing_ok=True)
        return FileResult(src_file, Outcome.ERROR,
                          notes=[Note("ERROR", f"encode failed: {res.error}")])

    # Minimum-saving gate applies only to a shrink.
    if mode is Mode.SHRINK and res.out_bytes >= src_bytes * config.min_saving_ratio:
        tmp.unlink(missing_ok=True)
        r = FileResult(src_file, Outcome.SKIP_MIN_SAVING)
        if ledger.enabled:
            ledger.add(lkey)
        return r

    notes = _place(config, src_file, out, tmp, res)
    outcome = {Mode.SHRINK: Outcome.SHRINK, Mode.TRANSCODE: Outcome.TRANSCODE,
               Mode.REMUX: Outcome.REMUX}[mode]
    r = FileResult(src_file, outcome, src_bytes=src_bytes, out_bytes=res.out_bytes, notes=notes)
    if ledger.enabled:
        ledger.add(lkey)
    return r


# ── Run ───────────────────────────────────────────────────────────────────────
def run(config: RunConfig, progress: ProgressCB | None = None,
        on_result: ResultCB | None = None) -> list[FileResult]:
    """Process the whole scan tree. Returns one FileResult per file processed."""
    ledger = Ledger(config)
    hardware = encode.use_hardware(config)
    files = list(iter_video_files(config))
    results: list[FileResult] = []

    def work(f: Path) -> FileResult:
        return process_file(config, ledger, hardware, f, progress)

    stopped = False
    with ThreadPoolExecutor(max_workers=max(1, config.jobs)) as pool:
        futures = []
        for f in files:
            if STOP_FILE.exists():
                stopped = True
                break
            futures.append(pool.submit(work, f))
        for fut in futures:
            r = fut.result()
            results.append(r)
            if on_result:
                on_result(r)
    if stopped and on_result:
        pass  # caller can note the stop; remaining files simply weren't submitted
    return results
