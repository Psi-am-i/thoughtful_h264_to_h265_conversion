"""Command-line front-end — the parity replacement for the bash script.

Three ways to drive it:
  * flags:        vtc /media/tv --codec h265 --tier excellent
  * interactive:  vtc            (or `vtc -i`) — guided prompts with defaults
  * preview:      vtc /media/tv --dry-run — decide every file, encode nothing

Builds a RunConfig, prints a run header, streams a line per file, and prints the
end-of-run report. The GUI (Stage 2) drives the same pipeline.run.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__, pipeline, report
from .config import AudioPolicy, Container, Encoder, OutputMode, RunConfig, SourceAction
from .model import OutCodec, Tier, hevc_factor
from .pipeline import PlanRow
from .report import human_bytes
from .result import FileResult, Outcome

_OUTCOME_LINE = {
    Outcome.SHRINK: "DONE   shrink",
    Outcome.TRANSCODE: "DONE   transcode",
    Outcome.REMUX: "DONE   remux",
    Outcome.SKIP_AT_TIER: "SKIP   already at tier",
    Outcome.SKIP_UNDER_TIER: "SKIP   below your quality tier",
    Outcome.SKIP_MODERN: "SKIP   already H.265/AV1/VP9",
    Outcome.SKIP_EXISTING: "SKIP   output already exists",
    Outcome.SKIP_MIN_SAVING: "SKIP   saving too small, kept original",
    Outcome.SKIP_INCOMPATIBLE: "SKIP   incompatible codec (transcode off)",
    Outcome.SKIP_CODEC: "SKIP   unsupported codec",
    Outcome.SKIP_SECOND_GEN: "SKIP   already encoded by this tool",
    Outcome.RESUME: "RESUME already done",
    Outcome.ERROR: "ERROR  encode failed",
}


# ── Argument parsing ──────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vtc",
        description="Very Thoughtful Compression — codec-aware, quality-density video re-encoder.",
        epilog="Run with no directory (or -i) for guided prompts. Use --dry-run to preview.",
    )
    p.add_argument("src", type=Path, nargs="?", help="directory to scan (omit for interactive mode)")
    p.add_argument("--version", action="version", version=f"vtc {__version__}")
    p.add_argument("-i", "--interactive", action="store_true", help="ask for settings with prompts")
    p.add_argument("--dry-run", action="store_true", help="show what would happen; encode nothing")
    q = p.add_argument_group("quality")
    q.add_argument("--codec", choices=["h265", "h264"], default="h265", help="output codec (default: h265)")
    q.add_argument("--tier", choices=["ok", "good", "excellent", "stellar", "insane"], default="excellent",
                   help="quality tier (default: excellent)")
    q.add_argument("--min-saving", type=float, default=0.25, metavar="FRACTION",
                   help="minimum size saving to keep a shrink, e.g. 0.25 = 25%% (default: 0.25)")
    q.add_argument("--bpp", type=float, default=None, metavar="BPP",
                   help="override the chosen tier's quality density (bits per pixel per frame), "
                        "e.g. 0.12; default is the tier's own anchored value")
    g_ign = p.add_argument_group("ignore rules (files the scan pretends it never saw)")
    g_ign.add_argument("--ignore-under", type=float, default=None, metavar="MB",
                       help="ignore files smaller than this many MB")
    g_ign.add_argument("--ignore-over", type=float, default=None, metavar="MB",
                       help="ignore files larger than this many MB")
    g_ign.add_argument("--ignore-ext", action="append", default=[], metavar="EXT",
                       help="ignore this extension (e.g. .avi); repeatable")
    g_ign.add_argument("--ignore-name", action="append", default=[], metavar="TEXT",
                       help="ignore files whose name contains TEXT; repeatable")
    c = p.add_argument_group("compatibility")
    c.add_argument("--no-remux", action="store_true", help="do not rehome MP4-friendly codecs into MP4")
    c.add_argument("--no-transcode", action="store_true", help="leave MP4-incompatible legacy codecs untouched")
    c.add_argument("--container", choices=["auto", "mp4", "mkv"], default="auto",
                   help="output container (default: auto — MP4, MKV when it must)")
    c.add_argument("--audio", choices=["passthrough", "aac", "ac3", "flac"], default="passthrough",
                   help="audio policy (default: passthrough; flac forces MKV)")
    c.add_argument("--drop-image-subs", action="store_true",
                   help="drop image subs (PGS/DVD) instead of preferring MKV to keep them")
    e = p.add_argument_group("execution")
    e.add_argument("--encoder", choices=["auto", "hardware", "software"], default="auto",
                   help="encoder backend (default: auto)")
    e.add_argument("--jobs", type=int, default=1, help="parallel encode jobs (default: 1)")
    e.add_argument("--software-file", action="append", default=[], metavar="PATH",
                   help="encode just this file in software, even on a hardware run; "
                        "repeatable (the GUI offers this as a tick-list)")
    d = p.add_argument_group("destination")
    d.add_argument("--output", metavar="DIR", default=None,
                   help="write outputs to DIR (mirroring the tree); default is in-place")
    d.add_argument("--flat", action="store_true", help="with --output, flatten instead of mirroring")
    d.add_argument("--originals", choices=["archive", "delete", "keep"], default="archive",
                   help="what to do with replaced originals (default: archive)")
    d.add_argument("--archive-dir", type=Path, default=None, help="archive location (default: <src>/originals)")
    g = p.add_argument_group("misc")
    g.add_argument("--no-ledger", action="store_true", help="disable the resume ledger")
    g.add_argument("--ledger-file", type=Path, default=None, help="ledger path (default: <src>/.vtc_processed.log)")
    g.add_argument("--clear-history", action="store_true",
                   help="empty the resume ledger (processing history) before running")
    g.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg binary")
    g.add_argument("--ffprobe", default="ffprobe", help="ffprobe binary")
    return p


def config_from_args(a: argparse.Namespace) -> RunConfig:
    tier = Tier.from_name(a.tier)
    return RunConfig(
        src=a.src,
        out_codec=OutCodec(a.codec),
        tier=tier,
        tier_bpp={tier.name: a.bpp} if a.bpp else {},
        ignore_under_bytes=int((a.ignore_under or 0) * 1e6),
        ignore_over_bytes=int((a.ignore_over or 0) * 1e6),
        ignore_exts=tuple(e.strip().lstrip(".").lower() for e in a.ignore_ext if e.strip()),
        ignore_name_contains=tuple(n for n in a.ignore_name if n.strip()),
        min_saving_ratio=1.0 - a.min_saving,
        remux_to_mp4=not a.no_remux,
        compat_transcode=not a.no_transcode,
        container=Container(a.container),
        audio_policy=AudioPolicy(a.audio),
        keep_image_subs=not a.drop_image_subs,
        encoder=Encoder(a.encoder),
        jobs=a.jobs,
        software_files=frozenset(str(Path(p).expanduser().resolve()) for p in a.software_file),
        output_mode=OutputMode.SEPARATE if a.output else OutputMode.INPLACE,
        output_dir=Path(a.output) if a.output else None,
        output_flat=a.flat,
        source_action=SourceAction(a.originals),
        archive_dir=a.archive_dir,
        ledger_enabled=not a.no_ledger,
        ledger_file=a.ledger_file,
        ffmpeg=a.ffmpeg,
        ffprobe=a.ffprobe,
    )


# ── Interactive prompts (retires the bash prompt UX) ──────────────────────────
def _menu(title: str, options: list[str], default: int) -> int:
    print(f"\n{title}")
    for i, opt in enumerate(options, 1):
        mark = "  [default]" if i == default else ""
        print(f"  {i}) {opt}{mark}")
    try:
        raw = input(f"  Choice [1-{len(options)}]: ").strip()
    except EOFError:
        raw = ""
    if not raw:
        return default
    try:
        n = int(raw)
        return n if 1 <= n <= len(options) else default
    except ValueError:
        return default


def _ask(prompt: str, default: str) -> str:
    try:
        raw = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        raw = ""
    return raw or default


def _yesno(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    try:
        raw = input(f"{prompt} [{d}]: ").strip().lower()
    except EOFError:
        raw = ""
    if not raw:
        return default
    return raw.startswith("y")


def interactive_config(src: Path | None) -> RunConfig:
    if src is None:
        src = Path(_ask("\nDirectory to scan (recursively)", str(Path.cwd()))).expanduser()

    codec = [OutCodec.H265, OutCodec.H264][_menu(
        "Output codec?",
        ["H.265 / HEVC — ~40-55% smaller, modern players",
         "H.264 / AVC  — universal playback, larger"], default=1) - 1]

    tier = [Tier.OK, Tier.GOOD, Tier.EXCELLENT, Tier.STELLAR, Tier.INSANE][_menu(
        "Quality tier? (Mbps are H.264 @1080p30; scales with resolution/fps)",
        ["OK        — ~4 Mbps   (space-first)",
         "GOOD      — ~5 Mbps   (solid streaming)",
         "EXCELLENT — ~6.8 Mbps (matches top streaming, with headroom)",
         "STELLAR   — ~8 Mbps   (above streaming, approaching Blu-ray)",
         "INSANE    — ~9 Mbps   (near-transparent for streaming sources)"], default=3) - 1]

    min_saving = {1: 0.25, 2: 0.15, 3: 0.35, 4: 0.0}[_menu(
        "Minimum size saving to keep a re-encode?",
        ["25%", "15%", "35%", "none — keep any successful encode"], default=1)]

    remux = _yesno("\nRemux MP4-friendly codecs (in MKV/WebM) into MP4 losslessly?", True)
    transcode = _yesno("Transcode MP4-incompatible legacy codecs (MPEG-2/VC-1/WMV)?", True)

    enc = [Encoder.AUTO, Encoder.HARDWARE, Encoder.SOFTWARE][_menu(
        "Encoder?",
        ["auto     — hardware if available",
         "hardware — VideoToolbox (fast, bitrate-targeted)",
         "software — libx264/libx265 (slower, capped-CRF, best quality)"], default=1) - 1]

    jobs = {1: 1, 2: 2, 3: 4}[_menu("Parallel encode jobs?", ["1", "2", "4"], default=1)]

    out_mode = _menu("Where should outputs be written?",
                     ["Replace in place", "A separate folder"], default=1)
    output_dir = None
    if out_mode == 2:
        output_dir = Path(_ask("  Output folder", str(src / "converted"))).expanduser()

    originals = [SourceAction.ARCHIVE, SourceAction.DELETE, SourceAction.KEEP][_menu(
        "What should happen to replaced originals?",
        ["Archive (move to a folder)", "Delete", "Leave in place"], default=1) - 1]

    return RunConfig(
        src=src, out_codec=codec, tier=tier, min_saving_ratio=1.0 - min_saving,
        remux_to_mp4=remux, compat_transcode=transcode, encoder=enc, jobs=jobs,
        output_mode=OutputMode.SEPARATE if output_dir else OutputMode.INPLACE,
        output_dir=output_dir, source_action=originals,
    )


# ── Output ────────────────────────────────────────────────────────────────────
def _ignore_line(cfg: RunConfig) -> list[str]:
    """One line naming the ignore rules in force, or nothing when there are none."""
    bits = []
    if cfg.ignore_under_bytes:
        bits.append(f"under {cfg.ignore_under_bytes / 1e6:g} MB")
    if cfg.ignore_over_bytes:
        bits.append(f"over {cfg.ignore_over_bytes / 1e6:g} MB")
    if cfg.ignore_exts:
        bits.append("ext " + ", ".join("." + e for e in cfg.ignore_exts))
    if cfg.ignore_name_contains:
        bits.append("name contains " + ", ".join(repr(n) for n in cfg.ignore_name_contains))
    return [f"IGNORE:    {'  ·  '.join(bits)}"] if bits else []


def _print_header(cfg: RunConfig, dry: bool = False) -> None:
    tier = cfg.tier
    # A retuned tier is no longer described by its anchor, so derive the 1080p30
    # reference Mbps back out of whatever density is actually in force.
    bpp = cfg.bpp_for()
    ref_mbps = bpp * 1920 * 1080 * 30 / 1e6
    tuned = "" if abs(bpp - tier.bpp) < 1e-9 else f" [retuned to {bpp:.4f} bpp]"
    h265 = ""
    if cfg.out_codec is OutCodec.H265:
        h265 = f"  (~{ref_mbps * hevc_factor(1920 * 1080, cfg.hevc_factors()):.1f} Mbps H.265 @1080p)"
    out = str(cfg.output_dir) if cfg.output_mode is OutputMode.SEPARATE else "in place"
    banner = "DRY RUN — nothing will be encoded" if dry else None
    lines = [
        "",
        *( [banner, ""] if banner else [] ),
        f"SRC:       {cfg.src}",
        f"CODEC:     {cfg.out_codec.value.upper()}",
        f"TIER:      {tier.label} — {ref_mbps:.1f} Mbps H.264 @1080p30{h265}, "
        f"scales with resolution & fps{tuned}",
        *_ignore_line(cfg),
        *([f"SOFTWARE:  {len(cfg.software_files)} file(s) picked out for the software encoder"]
          if cfg.software_files else []),
        f"ENCODER:   {cfg.encoder.value}",
        f"REMUX:     {'yes' if cfg.remux_to_mp4 else 'no'}    TRANSCODE: {'yes' if cfg.compat_transcode else 'no'}",
        f"OUTPUT:    {out}    ORIGINALS: {cfg.source_action.value}    JOBS: {cfg.jobs}",
        f"RE-ENCODE: only sources >{int((cfg.tier_over_tolerance - 1) * 100)}% over tier target",
        *( [] if dry else [f"RESUME:    {cfg.resolved_ledger_file() or 'disabled'}"] ),
        "",
    ]
    print("\n".join(lines))


def _on_result(r: FileResult) -> None:
    label = _OUTCOME_LINE.get(r.outcome, r.outcome.value)
    extra = ""
    if r.outcome.changed and r.src_bytes:
        pct = 100 * (1 - r.out_bytes / r.src_bytes)
        extra = f"  ({pct:.0f}% smaller)"
    print(f"  {label:42s} {r.path.name}{extra}", flush=True)
    for note in r.notes:
        print(f"      [{note.level}] {note.message}", flush=True)


def _plan_action(row: PlanRow) -> str:
    if row.mode is not None:
        return {"shrink": "shrink", "transcode": "transcode", "remux": "remux"}[row.mode.value]
    return _OUTCOME_LINE.get(row.outcome, row.outcome.value if row.outcome else "?").split()[0].lower()


def _print_dry_run(cfg: RunConfig) -> int:
    rows = pipeline.plan(cfg)
    if not rows:
        print("  (no video files found)")
        return 0
    print(f"  {'file':<44s} {'res':>9s} {'codec':>6s} {'src':>8s} {'→':^3s} {'action':<10s} {'target':>8s} {'~save':>6s}")
    print("  " + "─" * 100)
    est_saved = 0.0
    n_change = 0
    work_src = 0
    for r in sorted(rows, key=lambda x: x.path.name):
        name = r.path.name if len(r.path.name) <= 44 else r.path.name[:41] + "..."
        res = f"{r.info.width}x{r.info.height}" if r.info.width else "?"
        srcmbps = f"{r.src_kbps/1000:.1f}M" if r.src_kbps else "?"
        # Pass the config: without it this falls back to the bitrate ratio, which
        # ignores audio and so promises savings that cannot arrive.
        sav = r.projected_saving(cfg)
        tgt = f"{r.target_kbps/1000:.1f}M" if r.target_kbps else "—"
        savtxt = f"{sav*100:.0f}%" if sav else ("0%" if sav == 0.0 else "—")
        try:
            size = r.path.stat().st_size
        except OSError:
            size = 0
        if sav:
            est_saved += size * sav
            n_change += 1
            work_src += size
        print(f"  {name:<44s} {res:>9s} {r.info.vcodec or '?':>6s} {srcmbps:>8s} "
              f"{'→':^3s} {_plan_action(r):<10s} {tgt:>8s} {savtxt:>6s}")
    print("  " + "─" * 100)
    # Lead with what happens to the files being TOUCHED. Averaging the saving
    # across a whole library — most of which is deliberately left alone — makes
    # worthwhile work read as pointless.
    print(f"  {len(rows)} file(s): {n_change} would be re-encoded, {len(rows)-n_change} left as-is")
    if n_change:
        pct = (est_saved / work_src * 100) if work_src else 0
        print(f"  Those {n_change}: {human_bytes(work_src)} -> ~{human_bytes(work_src - est_saved)}"
              f"  |  est. recovery ~{human_bytes(est_saved)} ({pct:.0f}% of what is touched)")
    return 0


# ── Entry point ───────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.interactive or args.src is None:
        cfg = interactive_config(args.src)
    else:
        cfg = config_from_args(args)

    errs = cfg.validate()
    if errs:
        for e in errs:
            print(f"error: {e}", file=sys.stderr)
        return 2

    if getattr(args, "clear_history", False):
        import dataclasses
        # Force the ledger on just to reach the file: the history exists on disk
        # whether or not this run is using it, and --clear-history must empty it.
        n = pipeline.Ledger(dataclasses.replace(cfg, ledger_enabled=True)).clear()
        print(f"cleared processing history: {n} entr{'y' if n == 1 else 'ies'}")

    _print_header(cfg, dry=args.dry_run)
    if args.dry_run:
        return _print_dry_run(cfg)

    start = time.monotonic()
    try:
        results = pipeline.run(cfg, on_result=_on_result)
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        return 130

    print()
    print(report.render(results))
    print(f"\nDone in {time.monotonic() - start:.0f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
