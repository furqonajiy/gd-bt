from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import xauusd_trading.cli as cli
from xauusd_trading import DEFAULT_CONFIG, ExecutionLog, parse_one_signal


class _FakeTick:
    bid = 4500.0
    ask = 4500.2


class _FakeMt5:
    def symbol_info_tick(self, symbol):
        return _FakeTick()


class _FakeConn:
    def __init__(self):
        self.mt5 = _FakeMt5()


class _FakeChart:
    def __init__(self, now: datetime):
        self.now = now

    def last_time(self):
        return self.now

    def latest(self):
        return None

    def bars_between(self, start, end):
        return []


class _FakeRegistry:
    entries: list[dict] = []

    def __init__(self, path: Path):
        self.path = path

    def load(self):
        return list(self.entries)

    def add(self, signal, equity, executed_at=None):
        self.entries.append({"signal_key": signal.signal_key})

    def prune(self, alive_magics: set[int]):
        return 0


class _FakeExecutor:
    place_log = None

    def __init__(self, *args, **kwargs):
        pass

    def reconcile_with_mt5(self, *args, **kwargs):
        return ExecutionLog()

    def find_orders(self, magic):
        return []

    def find_positions(self, magic):
        return []

    def sanity_checks(self, expected_equity=None):
        return []

    def manage_position(self, *args, **kwargs):
        return ExecutionLog()

    def place_signal(self, signal, plan):
        return self.place_log or ExecutionLog()

    def warn_on_unknown(self, known_magics):
        return []

    def all_alive_magics(self):
        return set()


class _FakeForensic:
    enabled = False

    def start_cycle(self, **kwargs):
        pass

    def decision(self, **kwargs):
        pass

    def end_cycle(self, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


@pytest.fixture(autouse=True)
def patch_auto_dependencies(monkeypatch):
    _FakeRegistry.entries = []
    _FakeExecutor.place_log = None
    monkeypatch.setattr(cli, "_make_notifier", lambda args: SimpleNamespace(path=None))
    monkeypatch.setattr(cli, "_make_forensic", lambda args: _FakeForensic())
    monkeypatch.setattr(cli, "_handle_closures", lambda *args, **kwargs: None)
    monkeypatch.setattr("xauusd_trading.mt5_equity", lambda conn: 1000.0)
    monkeypatch.setattr("xauusd_trading.SignalRegistry", _FakeRegistry)
    monkeypatch.setattr("xauusd_trading.Mt5Executor", _FakeExecutor)


def _args(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        mt5_symbol="XAUUSD",
        mt5_server_offset=3,
        positions_json=str(tmp_path / "positions.json"),
        no_notifications=True,
        notifications=None,
        no_forensic=True,
        forensic_log=None,
    )


def _signal():
    return parse_one_signal(
        "1. BUY XAUUSD 4518 - 4516 SL 4511 TP1 4526 TP2 4536 TP3 4551 8:00 AM",
        source_date="2026-06-02",
        source_offset=7,
    )


def _follow_rec():
    plan = SimpleNamespace(
        action="FOLLOW",
        orders=[],
        replay_position=None,
        rationale="",
        pending_expires_at=datetime(2026, 6, 2, 14, 30),
    )
    return SimpleNamespace(new_signal=plan)


def _run_auto_once(tmp_path: Path, monkeypatch, *, signals, place_log=None, config=DEFAULT_CONFIG, state=None):
    signals_path = tmp_path / "signals.txt"
    signals_path.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(cli, "parse_signals_file", lambda path: list(signals))
    monkeypatch.setattr(cli, "decide", lambda *args, **kwargs: _follow_rec())
    if place_log is not None:
        _FakeExecutor.place_log = place_log
    return cli._auto_pass(
        _args(tmp_path),
        config,
        _FakeConn(),
        _FakeChart(datetime(2026, 6, 2, 6, 0)),
        signals_path,
        iteration=1,
        candidate_console_state=state,
    )


def test_auto_idle_cycle_is_quiet(tmp_path, monkeypatch, capsys):
    exit_code = _run_auto_once(tmp_path, monkeypatch, signals=[])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert "XAUUSD AUTO MODE" not in captured.out
    assert "Tracked signals" not in captured.out
    assert "TOTAL FLOATING" not in captured.out
    assert "(no tracked signals)" not in captured.out


def test_auto_placement_prints_single_event_line(tmp_path, monkeypatch, capsys):
    signal = _signal()
    plog = ExecutionLog(actions=["  BUY LIMIT placed for test"], placed=1)

    exit_code = _run_auto_once(tmp_path, monkeypatch, signals=[signal], place_log=plog)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.count("BUY LIMIT placed for test") == 1
    assert "[auto heartbeat" not in captured.out
    assert "recorded executed_at" not in captured.out
    assert "XAUUSD AUTO MODE" not in captured.out
    assert "TOTAL FLOATING" not in captured.out


def test_auto_waiting_candidate_status_is_deduped_across_cycles(tmp_path, monkeypatch, capsys):
    signal = _signal()
    cfg = replace(DEFAULT_CONFIG, trailing_open_distance=5.0)
    waiting = "Signal 2026-06-02#01: trailing-open waiting; no broker LIMIT is placed."
    plog = ExecutionLog(actions=[waiting])
    state: dict[str, str] = {}

    first = _run_auto_once(tmp_path, monkeypatch, signals=[signal], place_log=plog, config=cfg, state=state)
    second = _run_auto_once(tmp_path, monkeypatch, signals=[signal], place_log=plog, config=cfg, state=state)

    captured = capsys.readouterr()
    assert first == 0
    assert second == 0
    assert captured.out.count(waiting) == 1
    assert "[auto heartbeat" not in captured.out
    assert "XAUUSD AUTO MODE" not in captured.out
    assert "TOTAL FLOATING" not in captured.out


def test_auto_watch_prints_hourly_heartbeat(tmp_path, monkeypatch, capsys):
    signals_path = tmp_path / "signals.txt"
    signals_path.write_text("placeholder", encoding="utf-8")
    calls = {"count": 0}
    ticks = iter([0.0, 10.0, 3601.0])

    def fake_auto_pass(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise KeyboardInterrupt
        return 0

    monkeypatch.setattr(cli, "_auto_pass", fake_auto_pass)
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(cli.time, "sleep", lambda interval: None)

    exit_code = cli._run_auto_watch(
        SimpleNamespace(watch_interval=5.0),
        DEFAULT_CONFIG,
        _FakeConn(),
        _FakeChart(datetime(2026, 6, 2, 6, 0)),
        signals_path,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.count("[auto heartbeat #1") == 1
