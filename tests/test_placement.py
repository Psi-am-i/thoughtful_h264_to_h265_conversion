"""Placement tests — the original must never be lost.

Regression for the archive-not-happening bug: an in-place re-encode whose output
lands on the SAME path as the source (h264.mp4 -> h265.mp4, same name) used to
overwrite the original BEFORE the archive step, and the archive step only ran when
the paths differed — so "Replace · archive" silently destroyed the originals.

Run:  python tests/test_placement.py   |   pytest tests/test_placement.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vtc import encode, pipeline  # noqa: E402
from vtc.config import OutputMode, RunConfig, SourceAction  # noqa: E402
from vtc.ffprobe import MediaInfo  # noqa: E402
from vtc.ledger import Ledger  # noqa: E402
from vtc.pipeline import _place, process_file  # noqa: E402
from vtc.result import EncodeResult, Outcome  # noqa: E402

_ORIG = b"ORIGINAL" * 1000
_NEW = b"NEWENCODE"


def _place_case(action: SourceAction, same_path: bool, dropped: str = ""):
    d = Path(tempfile.mkdtemp())
    src = d / "Show - S01E01 [h264].mp4"
    src.write_bytes(_ORIG)
    out = src if same_path else (d / "Show - S01E01 [h264].mkv")
    tmp = d / ".tmp.mp4"
    tmp.write_bytes(_NEW)
    cfg = RunConfig(src=d, source_action=action, output_mode=OutputMode.INPLACE)
    res = EncodeResult(ok=True, out_path=out, out_bytes=len(_NEW), dropped_subs_reason=dropped)
    _place(cfg, src, out, tmp, res)
    archived = d / "originals" / src.name
    return {
        "out_ok": out.exists() and out.read_bytes() == _NEW,
        "archived_ok": archived.exists() and archived.read_bytes() == _ORIG,
    }


def test_archive_same_path_preserves_original():
    r = _place_case(SourceAction.ARCHIVE, same_path=True)
    assert r["out_ok"], "output not written"
    assert r["archived_ok"], "original was LOST (not archived) on same-path replace"
    print("  ok  ARCHIVE same-path -> original archived, not overwritten")


def test_archive_diff_path_preserves_original():
    r = _place_case(SourceAction.ARCHIVE, same_path=False)
    assert r["out_ok"] and r["archived_ok"]
    print("  ok  ARCHIVE different-path -> original archived")


def test_delete_same_path_overwrites():
    r = _place_case(SourceAction.DELETE, same_path=True)
    assert r["out_ok"] and not r["archived_ok"]
    print("  ok  DELETE same-path -> original intentionally overwritten")


def test_delete_with_dropped_subs_archives():
    r = _place_case(SourceAction.DELETE, same_path=True, dropped="PGS subtitles")
    assert r["archived_ok"], "dropped subs must be archived even on DELETE"
    print("  ok  DELETE + dropped subs -> original archived, never silently lost")


def test_place_failure_is_reported_not_raised(monkeypatch):
    """A stalled/failed placement (e.g. NFS ETIMEDOUT on the output share) must fail
    THIS file as a retryable ERROR, not escape process_file. An uncaught error here
    killed the whole worker mid-queue: "stop after current file" never fired and the
    UI froze on the last frame with no completion event. Regression for that hang.
    """
    d = Path(tempfile.mkdtemp())
    src = d / "Show - S01E01 [h264].mkv"
    src.write_bytes(_ORIG)
    cfg = RunConfig(src=d, source_action=SourceAction.DELETE, output_mode=OutputMode.INPLACE)

    info = MediaInfo(path=src, ok=True, vcodec="h264", width=1920, height=1080,
                     fps=24.0, bit_rate=8_000_000, duration=60.0)
    # probe() answers both the source measurement and the post-encode validity check;
    # a valid, full-length result lets the run reach _place (the path under test).
    monkeypatch.setattr(pipeline, "probe", lambda *a, **k: info)
    # REMUX so no size gate is involved; output is .mp4 (differs from the .mkv src, so
    # it isn't short-circuited as a same-container no-op).
    monkeypatch.setattr(pipeline, "decide", lambda *a, **k: (pipeline.Mode.REMUX, None, 0))
    monkeypatch.setattr(encode, "resolve_container", lambda *a, **k: pipeline.Container.MP4)
    monkeypatch.setattr(encode, "run_encode",
                        lambda *a, **k: EncodeResult(ok=True, out_path=src, out_bytes=len(_NEW)))

    def _boom(*a, **k):
        raise OSError(60, "Operation timed out")
    monkeypatch.setattr(pipeline, "_place", _boom)

    r = process_file(cfg, Ledger(cfg), None, src)
    assert r is not None and r.outcome is Outcome.ERROR, r
    assert src.exists() and src.read_bytes() == _ORIG, "original must survive a failed place"
    print("  ok  failed placement -> ERROR row, run survives, original intact")


def test_invalid_output_never_deletes_original(monkeypatch):
    """The safety gate: if the freshly-encoded file fails ffprobe (corrupt or a
    truncated write — e.g. the share dropped mid-encode), we must NOT overwrite or
    delete the original. It fails as a retryable ERROR and the original is untouched.
    """
    d = Path(tempfile.mkdtemp())
    src = d / "Show - S01E01 [h264].mkv"
    src.write_bytes(_ORIG)                                   # 8000 bytes, so the shrink clears the gate
    cfg = RunConfig(src=d, source_action=SourceAction.DELETE, output_mode=OutputMode.INPLACE)

    good = MediaInfo(path=src, ok=True, vcodec="h264", width=1920, height=1080,
                     fps=24.0, bit_rate=8_000_000, duration=60.0)
    corrupt = MediaInfo(path=src, ok=False, error="moov atom not found")
    # Source measurement comes from the estimate cache (probed=), so the monkeypatched
    # probe() only ever answers the post-encode validity check — where we return the
    # "corrupt file" verdict a truncated write would produce.
    monkeypatch.setattr(pipeline, "probe", lambda *a, **k: corrupt)
    monkeypatch.setattr(pipeline, "decide", lambda *a, **k: (pipeline.Mode.SHRINK, None, 4000))
    monkeypatch.setattr(encode, "resolve_container", lambda *a, **k: pipeline.Container.MP4)

    def _fake_encode(config, info, mode, src_file, tmp, *a, **k):
        Path(tmp).write_bytes(b"not a real video")          # what a truncated encode leaves behind
        return EncodeResult(ok=True, out_path=tmp, out_bytes=99)   # tiny -> clears the shrink gate
    monkeypatch.setattr(encode, "run_encode", _fake_encode)

    r = process_file(cfg, Ledger(cfg), None, src, probed={src: good})
    assert r is not None and r.outcome is Outcome.ERROR, r
    assert src.exists() and src.read_bytes() == _ORIG, "original must survive an invalid encode"
    assert not any(p.suffix == ".mp4" for p in d.iterdir()), "no broken output should be left in place"
    print("  ok  invalid/truncated output -> ERROR, original preserved, no broken file placed")


def _run_all():
    import inspect
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    # Tests taking fixture args (e.g. monkeypatch) only run under pytest; skip here.
    fns = [fn for fn in fns if not inspect.signature(fn).parameters]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} placement test(s) done.")


if __name__ == "__main__":
    _run_all()
