"""The live JSONL sinks (forensic + notifier) must stay size-bounded.

Forensic emits a full engine_snapshot + mt5_snapshot per tracked signal on every
auto/manage cycle; uncapped it filled the disk on a long-running executor
(2026-06). The sinks now rotate to a single `.1` backup at a byte cap, so on-disk
use is bounded at ~2x the cap regardless of uptime. Deterministic, no MT5.
"""
from __future__ import annotations

from pathlib import Path

from trading.engine import ForensicLog, Notifier, append_jsonl_line


def test_append_rotates_at_cap_and_keeps_one_backup(tmp_path: Path):
    p = tmp_path / "log.jsonl"
    line = "x" * 100 + "\n"
    for _ in range(50):  # 50 * 101 bytes ~= 5 KB, cap 1 KB
        append_jsonl_line(p, line, max_bytes=1024)

    backup = p.with_name("log.jsonl.1")
    assert p.exists() and backup.exists()
    # Active file never exceeds the cap by more than one line; total <= ~2x cap.
    assert p.stat().st_size <= 1024 + len(line.encode())
    assert p.stat().st_size + backup.stat().st_size <= 2 * 1024 + 2 * len(line.encode())


def test_append_unbounded_when_cap_disabled(tmp_path: Path):
    p = tmp_path / "log.jsonl"
    for _ in range(20):
        append_jsonl_line(p, "y" * 100 + "\n", max_bytes=0)      # 0 = unbounded
        append_jsonl_line(p, "z" * 100 + "\n", max_bytes=None)   # None = unbounded
    assert not p.with_name("log.jsonl.1").exists()
    assert p.stat().st_size == 40 * 101


def test_forensic_log_is_size_bounded(tmp_path: Path):
    p = tmp_path / "forensic.jsonl"
    log = ForensicLog(path=p, max_bytes=4096)
    for i in range(2000):
        log.decision(signal_key=f"SIG-{i}", action="FOLLOW",
                     rationale="x" * 50, iteration=i)

    assert p.stat().st_size <= 4096 + 1024            # active file capped
    backup = p.with_name("forensic.jsonl.1")
    assert backup.exists()
    total = p.stat().st_size + backup.stat().st_size
    assert total <= 2 * 4096 + 1024                   # bounded, not 2000 lines' worth


def test_notifier_is_size_bounded(tmp_path: Path):
    p = tmp_path / "notifications.jsonl"
    n = Notifier(path=p, max_bytes=2048)
    for i in range(1000):
        n._emit("test", signal_key=f"S-{i}", text="t" * 40)

    assert p.stat().st_size <= 2048 + 512
    assert p.with_name("notifications.jsonl.1").exists()


def test_disabled_sinks_write_nothing(tmp_path: Path):
    # path=None stays a no-op (disabling contract unchanged).
    ForensicLog(path=None).decision(signal_key="X", action="SKIP")
    Notifier(path=None)._emit("test", signal_key="X", text="hi")
    assert list(tmp_path.iterdir()) == []
