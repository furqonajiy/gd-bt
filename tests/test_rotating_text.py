"""Tests for the time-windowed console text log (RotatingTextLog) + the stdout tee.

The live ``auto`` console is mirrored to a .txt so a crash leaves the recent
history on disk; the file is kept to the last N hours. Covered: append, the
time-based prune (drop old, keep recent, block-inheritance for un-stamped lines,
atomic swap), unbounded mode, best-effort on a bad path, and the tee semantics.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from trading.engine import RotatingTextLog


def _stamp(dt: datetime, msg: str) -> str:
    return f"[{dt:%Y-%m-%d %H:%M:%S}] {msg}\n"


def test_write_appends_lines(tmp_path):
    p = tmp_path / "console.txt"
    log = RotatingTextLog(p, retain_hours=24)
    now = datetime(2026, 7, 1, 15, 0, 0)
    log.write(_stamp(now, "EXECUTION: placed=0"))
    log.write(_stamp(now, "Signal V017-...#05: trailing-open waiting"))
    text = p.read_text()
    assert "EXECUTION: placed=0" in text
    assert "trailing-open waiting" in text
    assert text.count("\n") == 2


def test_prune_drops_lines_older_than_retain_hours(tmp_path):
    p = tmp_path / "console.txt"
    log = RotatingTextLog(p, retain_hours=2)
    now = datetime(2026, 7, 1, 15, 0, 0)
    # 5h ago (aged out), 1h ago (kept), now (kept)
    p.write_text(
        _stamp(now - timedelta(hours=5), "OLD line")
        + _stamp(now - timedelta(hours=1), "recent line")
        + _stamp(now, "newest line"))
    log.prune(_now=now)
    text = p.read_text()
    assert "OLD line" not in text
    assert "recent line" in text and "newest line" in text
    # no leftover temp file
    assert not (tmp_path / "console.txt.prune.tmp").exists()


def test_prune_keeps_unstamped_continuation_with_its_block(tmp_path):
    p = tmp_path / "console.txt"
    log = RotatingTextLog(p, retain_hours=2)
    now = datetime(2026, 7, 1, 15, 0, 0)
    # a recent stamped header followed by an un-stamped continuation line (the
    # "#1 arms when Ask<=..." style) -> the continuation inherits the header time.
    p.write_text(
        _stamp(now - timedelta(hours=5), "OLD block header")
        + "  old continuation (no stamp)\n"
        + _stamp(now, "Signal ...#05: trailing-open waiting")
        + "  #1 arms when Ask<=3964.5\n")
    log.prune(_now=now)
    text = p.read_text()
    assert "OLD block header" not in text
    assert "old continuation" not in text          # dropped with its old header
    assert "#1 arms when Ask<=3964.5" in text        # kept with its recent header


def test_retain_hours_zero_is_unbounded(tmp_path):
    p = tmp_path / "console.txt"
    log = RotatingTextLog(p, retain_hours=0)
    now = datetime(2026, 7, 1, 15, 0, 0)
    p.write_text(_stamp(now - timedelta(days=30), "ancient line"))
    log.prune(_now=now)
    assert "ancient line" in p.read_text()          # never pruned


def test_write_is_best_effort_on_bad_path(tmp_path):
    # a path whose parent is a FILE (not a dir) can't be created/opened; write
    # must swallow the error, never raise (observability must not break trading).
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    log = RotatingTextLog(blocker / "nested" / "console.txt", retain_hours=24)
    assert log.write("[2026-07-01 15:00:00] anything\n") == len("[2026-07-01 15:00:00] anything\n")


def test_prune_only_rewrites_when_something_ages_out(tmp_path):
    p = tmp_path / "console.txt"
    log = RotatingTextLog(p, retain_hours=24)
    now = datetime(2026, 7, 1, 15, 0, 0)
    p.write_text(_stamp(now, "fresh") + _stamp(now, "also fresh"))
    before = p.read_text()
    log.prune(_now=now)
    assert p.read_text() == before                  # untouched


def test_tee_stdout_writes_to_both_and_survives_sink_failure():
    from trading.engine.cli import _TeeStdout

    class _Wrapped:
        def __init__(self): self.buf = ""
        def write(self, d): self.buf += d; return len(d)
        def flush(self): pass

    class _GoodSink:
        def __init__(self): self.buf = ""
        def write(self, d): self.buf += d

    class _BadSink:
        def write(self, d): raise OSError("disk full")

    wrapped = _Wrapped()
    good = _GoodSink()
    tee = _TeeStdout(wrapped, good)
    tee.write("hello\n")
    assert wrapped.buf == "hello\n" and good.buf == "hello\n"

    # a sink that raises must NOT break the real stdout write.
    wrapped2 = _Wrapped()
    tee2 = _TeeStdout(wrapped2, _BadSink())
    tee2.write("still shown\n")
    assert wrapped2.buf == "still shown\n"
