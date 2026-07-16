"""Command-line front-end — the parity replacement for the bash script.

Builds a RunConfig from arguments, prints a run header, streams a line per file,
and prints the end-of-run report. The GUI (Stage 2) drives the same pipeline.run.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import pipeline, report
from .config import Encoder, OutputMode, RunConfig, SourceAction
from .model import OutCodec, Tier, hevc_factor
from .result import FileResult, Outcome

_OUTCOME_LINE = {
    Outcome.SHRINK: "DONE   shrink",
    Outcome.TRANSCODE: "DONE   transcode",
    Outcome.REMUX: "REMUX  ",
    Outcome.SKIP_AT_TIER: "SKIP   already at tier",
    Outcome.SKIP_MODERN: "SKIP   already H.265/AV1/VP9",
    Outcome.SKIP_EXISTING: "SKIP   output already exists",
    Outcome.SKIP_MIN_SAVING: "SKIP   saving too small, kept original",
    Outcome.SKIP_INCOMPATIBLE: "SKIP   incompatible codec (transcode off)",
    Outcome.SKIP_CODEC: "SKIP   unsupported codec",
    Outcome.RESUME: "RESUME already done",
    Outcome.ERROR: "ERROR  encode failed",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vtc",
        description="Very Thoughtful Compression — codec-aware, quality-density video re-encoder.",
    )
    p.add_argument("src", type=Path, help="directory to scan for videos (recursively)")
    q = p.add_argument_group("quality")
    q.add_argument("--codec", choices=["h265", "h264"], default="h265", help="output codec (default: h265)")
    q.add_argument("--tier", choices=["fine", "good", "excellent", "insane"], default="excellent",
                   help="quality tier (default: excellent)")
    q.add_argument("--min-saving", type=float, default=0.25, metavar="FRACTION",
                   help="minimum size saving to keep a shrink, e.g. 0.25 = 25%% (default: 0.25)")
    c = p.add_argument_group("compatibility")
    c.add_argument("--no-remux", action="store_true", help="do not rehome MP4-friendly codecs into MP4")
    c.add_argument("--no-transcode", action="store_true", help="leave MP4-incompatible legacy codecs untouched")
    e = p.add_argument_group("execution")
    e.add_argument("--encoder", choices=["auto", "hardware", "software"], default="auto",
                   help="encoder backend (default: auto)")
    e.add_argument("--jobs", type=int, default=1, help="parallel encode jobs (default: 1)")
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
    g.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg binary")
    g.add_argument("--ffprobe", default="ffprobe", help="ffprobe binary")
    return p


def config_from_args(a: argparse.Namespace) -> RunConfig:
    return RunConfig(
        src=a.src,
        out_codec=OutCodec(a.codec),
        tier=Tier.from_name(a.tier),
        min_saving_ratio=1.0 - a.min_saving,
        remux_to_mp4=not a.no_remux,
        compat_transcode=not a.no_transcode,
        encoder=Encoder(a.encoder),
        jobs=a.jobs,
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


def _print_header(cfg: RunConfig) -> None:
    tier = cfg.tier
    h265 = ""
    if cfg.out_codec is OutCodec.H265:
        h265 = f"  (~{tier.ref_mbps * hevc_factor(1920 * 1080):.1f} Mbps H.265 @1080p)"
    out = str(cfg.output_dir) if cfg.output_mode is OutputMode.SEPARATE else "in place"
    lines = [
        "",
        f"SRC:       {cfg.src}",
        f"CODEC:     {cfg.out_codec.value.upper()}",
        f"TIER:      {tier.label} — {tier.ref_mbps} Mbps H.264 @1080p30{h265}, scales with resolution & fps",
        f"ENCODER:   {cfg.encoder.value}",
        f"REMUX:     {'yes' if cfg.remux_to_mp4 else 'no'}    TRANSCODE: {'yes' if cfg.compat_transcode else 'no'}",
        f"OUTPUT:    {out}    ORIGINALS: {cfg.source_action.value}    JOBS: {cfg.jobs}",
        f"RE-ENCODE: only sources >{int((cfg.tier_over_tolerance - 1) * 100)}% over tier target",
        f"RESUME:    {cfg.resolved_ledger_file() or 'disabled'}",
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_from_args(args)

    errs = cfg.validate()
    if errs:
        for e in errs:
            print(f"error: {e}", file=sys.stderr)
        return 2

    _print_header(cfg)
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
