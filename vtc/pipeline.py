"""The orchestrator: scan -> decide -> encode -> place -> record.

`run()` walks the scan tree, and for each file decides a Mode (shrink/transcode/
remux) or a skip Outcome, does the work, places the output and original per the
chosen source action, updates the resume ledger, and returns a FileResult per
file. Mirrors process_one / _process_one_impl in the bash script.
"""

from __future__ import annotations

import os
import shutil
import tempfile
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
from .result import EncodeResult, FileDetail, FileResult, Mode, Note, Outcome, ProgressCB

# The encode temp is LOCAL scratch, deliberately NOT on the (possibly network)
# output volume: ffmpeg writes the output incrementally, and streaming those many
# small writes over a network share is punishing — so we encode to a local disk and
# move the finished file to the destination once, in one pass. Using the OS temp dir
# (not a hardcoded "/tmp") makes that work on Windows too. The stop-flag lives there
# as well and is per-process, so two app instances can't stop each other.
TMPROOT = Path(tempfile.gettempdir()) / "vtcwork"
STOP_FILE = Path(tempfile.gettempdir()) / f"vtc_stop.{os.getpid()}"

# A transcode may end up a whisker larger than the source at a matched bitrate
# (container/codec overhead); allow up to this much before treating it as inflation
# and keeping the original. 0.5% is negligible — the gate exists to stop the ~30%
# floor-inflation blow-ups, not to nitpick a rounding-error of overhead.
_TRANSCODE_GROW_TOLERANCE = 1.005

# Directories never descended into during a scan. (No blanket "Library" — it's a
# real media/user folder on Windows/Linux; the Apple dot-dirs are harmless elsewhere.)
_PRUNE_DIRS = {
    ".Trashes", ".Spotlight-V100", ".fseventsd", ".TemporaryItems",
    "originals", "new versions", "archived",
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

    # "Leave non-MP4 files alone": don't touch anything outside an MP4 container.
    if config.leave_non_mp4 and not already_mp4:
        return (None, Outcome.SKIP_NON_MP4, 0)

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
            # Rescue legacy to a modern codec at ~15% below the source bitrate. H.264
            # is far more efficient than XviD/MPEG-2, so this preserves quality while
            # actually SHRINKING the file (never inflating — the floor is only a
            # fallback when the source bitrate is unknown).
            t = int(src_kbps * 0.85) if src_kbps > 0 else config.bitrate_floor_kbps
            return (Mode.TRANSCODE, None, t)
        return (None, Outcome.SKIP_INCOMPATIBLE, 0)

    return (None, Outcome.SKIP_CODEC, 0)


# ── Placement (archive / delete / keep + subtitle rescue) ─────────────────────
def _archive_dest(config: RunConfig, src_file: Path) -> Path:
    rel_parent = _rel(config, src_file).parent
    base = config.resolved_archive_dir()
    return base / rel_parent if str(rel_parent) != "." else base


def _place(config: RunConfig, src_file: Path, out: Path, tmp: Path,
           res: EncodeResult) -> list[Note]:
    """Move tmp->out, relocate sidecars, and handle the original. Returns notes.

    Critical ordering: when the output lands on the SAME path as the source (an
    in-place re-encode where the extension doesn't change, e.g. h264.mp4 ->
    h265.mp4), writing the output destroys the original. So anything that must
    keep the original (archive, or delete-but-subs-were-dropped) MUST move it out
    of the way BEFORE the output is written — never after.
    """
    notes: list[Note] = []
    dropped = res.dropped_subs_reason
    overwrites_source = out == src_file
    action = config.source_action
    archived_dir: Path | None = None

    def _archive_original() -> None:
        nonlocal archived_dir
        dest = _archive_dest(config, src_file)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_file), str(dest / src_file.name))
        archived_dir = dest

    # The original is preserved (archived) rather than discarded when: the action
    # is ARCHIVE; or subtitles were dropped (we never silently lose them, even on
    # DELETE/KEEP). If that original is about to be overwritten in place, move it
    # to the archive FIRST — this is the fix for the archive-not-happening bug.
    keep_original = (action == SourceAction.ARCHIVE) or bool(dropped)
    if overwrites_source and keep_original and src_file.exists():
        _archive_original()

    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), str(out))
    # Relocate any sidecar .srt files the encoder wrote next to the (local) temp.
    # shutil.move, not Path.rename: the destination is often a different volume than
    # the local scratch, and os.rename across filesystems throws EXDEV and hangs the run.
    for sc in tmp.parent.glob(tmp.stem + "*.srt"):
        shutil.move(str(sc), str(out.with_name(out.stem + sc.name[len(tmp.stem):])))
    if res.sidecars_made:
        notes.append(Note("NOTE", f"{res.sidecars_made} subtitle track(s) written as sidecar .srt "
                                  f"(could not be embedded in the MP4)"))

    # Handle the original for the non-overwrite case (out != src_file) + notes.
    if action == SourceAction.DELETE:
        if dropped:
            if archived_dir is None and src_file.exists():
                _archive_original()      # subs dropped -> archive instead of delete
            notes.append(Note("NOTE", f"original archived to {archived_dir} instead of deleted — {dropped}"))
        elif not overwrites_source and src_file.exists():
            src_file.unlink()
    elif action == SourceAction.ARCHIVE:
        if archived_dir is None and not overwrites_source and src_file.exists():
            _archive_original()
        if dropped:
            notes.append(Note("NOTE", f"output MP4 is missing subtitle track(s) — {dropped}; "
                                      f"the archived original still has them"))
    else:  # KEEP (only ever used with a separate output path, so never overwrites)
        if archived_dir is not None:
            notes.append(Note("NOTE", f"original moved to {archived_dir} (it was being overwritten "
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
def process_file(config: RunConfig, ledger: Ledger, hw_encoder: str | None,
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
    # Encode to LOCAL scratch, never onto the (possibly network) output volume —
    # ffmpeg writes the output incrementally and streaming those writes over a share
    # is punishing. The finished file is moved to the destination once, below.
    TMPROOT.mkdir(parents=True, exist_ok=True)
    tmp = TMPROOT / f".{out.stem}.{os.getpid()}.{id(src_file) & 0xffff}{ext}"

    res = encode.run_encode(config, info, mode, src_file, tmp, target, hw_encoder, container, progress)
    if not res.ok:
        tmp.unlink(missing_ok=True)
        return FileResult(src_file, Outcome.ERROR,
                          notes=[Note("ERROR", f"encode failed: {res.error}")])

    # Size-safety gate. A shrink must clear the savings bar. A transcode (legacy
    # rescue) must at least not INFLATE — we never replace an original with a bigger
    # file, even for compatibility, so a library can't silently grow. A 0.5% buffer
    # allows for the small container/codec overhead a modern encoder adds at a
    # matched bitrate, so a genuine same-size rescue isn't rejected. Both keep the
    # original untouched and drop the temp.
    too_big = (mode is Mode.SHRINK and res.out_bytes >= src_bytes * config.min_saving_ratio) \
        or (mode is Mode.TRANSCODE and res.out_bytes > src_bytes * _TRANSCODE_GROW_TOLERANCE)
    if too_big:
        tmp.unlink(missing_ok=True)
        r = FileResult(src_file, Outcome.SKIP_MIN_SAVING)
        if ledger.enabled:
            ledger.add(lkey)
        return r

    notes = _place(config, src_file, out, tmp, res)
    outcome = {Mode.SHRINK: Outcome.SHRINK, Mode.TRANSCODE: Outcome.TRANSCODE,
               Mode.REMUX: Outcome.REMUX}[mode]
    detail = _build_detail(config, info, mode, target, container, ext, src_file, res)
    r = FileResult(src_file, outcome, src_bytes=src_bytes, out_bytes=res.out_bytes,
                   notes=notes, detail=detail)
    if ledger.enabled:
        ledger.add(lkey)
    return r


# ffprobe codec name for the chosen output codec, so the record reads codec→codec.
_OUT_VCODEC = {OutCodec.H264: "h264", OutCodec.H265: "hevc"}


def _build_detail(config: RunConfig, info: MediaInfo, mode: Mode, target: int,
                  container: Container, ext: str, src_file: Path,
                  res: EncodeResult) -> FileDetail:
    """Assemble the one structured 'what happened' record from everything the
    per-file path already knows. This is the single place capture happens."""
    dur = info.duration or 0.0
    out_kbps = (res.out_bytes * 8 / 1000.0 / dur) if dur > 0 and res.out_bytes else 0.0
    # bpp from the VIDEO bitrate we aimed for (target for a re-encode, source for a
    # lossless remux), not the size-derived total (which includes audio/subs).
    vid_kbps = float(target) if mode in (Mode.SHRINK, Mode.TRANSCODE) else info.effective_bps / 1000.0
    bpp = (vid_kbps * 1000.0) / (info.pixels * info.fps) if info.pixels and info.fps else 0.0

    nsub = len(info.subtitles)
    if nsub == 0:
        subs_summary = ""
    elif container == Container.MKV:
        subs_summary = f"kept all {nsub} subtitle track(s)"
    else:
        parts: list[str] = []
        if res.subs_embedded and info.text_subs:
            parts.append(f"{len(info.text_subs)} text sub(s) embedded")
        if res.sidecars_made:
            parts.append(f"{res.sidecars_made} sidecar .srt")
        if info.image_subs:
            parts.append(f"dropped {len(info.image_subs)} image sub(s)")
        subs_summary = "; ".join(parts)

    return FileDetail(
        mode=mode.value,
        src_vcodec=info.vcodec or "",
        out_vcodec="" if mode is Mode.REMUX else _OUT_VCODEC.get(config.out_codec, ""),
        src_ext=src_file.suffix.lower(),
        out_ext=ext,
        container_reason=encode.container_reason(config, info),
        width=info.width, height=info.height, fps=info.fps,
        src_kbps=info.effective_bps / 1000.0, vid_kbps=vid_kbps, out_kbps=out_kbps, bpp=bpp,
        audio_action=res.audio_action,
        subs_summary=subs_summary,
    )


# ── Run ───────────────────────────────────────────────────────────────────────
def run(config: RunConfig, progress: ProgressCB | None = None,
        on_result: ResultCB | None = None,
        files: list[Path] | None = None) -> list[FileResult]:
    """Process the scan tree (or an explicit `files` list — used to retry just the
    files that failed). Returns one FileResult per file processed."""
    ledger = Ledger(config)
    hw_encoder = encode.select_hw_encoder(config)
    files = list(iter_video_files(config)) if files is None else list(files)
    results: list[FileResult] = []

    # The stop check must live INSIDE the work, not just around submission: with
    # jobs=1 every file is submitted to the pool up front (submitting is instant),
    # so a guard around submit() has nothing left to stop. Checking STOP_FILE at
    # the start of each unit means the in-flight file(s) finish and every remaining
    # queued file returns None immediately -> a true "stop after current file".
    def work(f: Path) -> FileResult | None:
        if STOP_FILE.exists():
            return None
        return process_file(config, ledger, hw_encoder, f, progress)

    with ThreadPoolExecutor(max_workers=max(1, config.jobs)) as pool:
        futures = [pool.submit(work, f) for f in files]
        for fut in futures:
            r = fut.result()
            if r is None:          # skipped because a stop was requested
                continue
            results.append(r)
            if on_result:
                on_result(r)
    return results
