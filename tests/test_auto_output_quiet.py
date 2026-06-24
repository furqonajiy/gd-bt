from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import trading.engine.cli as cli
from trading.engine import DEFAULT_CONFIG, ExecutionLog, parse_one_signal, signal_to_magic


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


class _NoopNotifier:
    """Tolerates any emit_* call; these tests assert console output, not
    notifier payloads (those are covered in test_notification_emits)."""
    path = None

    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            return None
        return _noop


@pytest.fixture(autouse=True)
def patch_auto_dependencies(monkeypatch):
    _FakeRegistry.entries = []
    _FakeExecutor.place_log = None
    monkeypatch.setattr(cli, "_make_notifier", lambda args: _NoopNotifier())
    monkeypatch.setattr(cli, "_make_forensic", lambda args: _FakeForensic())
    monkeypatch.setattr(cli, "_handle_closures", lambda *args, **kwargs: None)
    monkeypatch.setattr("trading.engine.mt5_equity", lambda conn: 1000.0)
    monkeypatch.setattr("trading.engine.SignalRegistry", _FakeRegistry)
    monkeypatch.setattr("trading.engine.Mt5Executor", _FakeExecutor)


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
    monkeypatch.setattr(cli, "parse_signals_file", lambda path, **kw: list(signals))
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
    ticks = iter([0.0, 3601.0])

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

def test_auto_played_out_status_dedupes_despite_flapping_realized(tmp_path, monkeypatch, capsys):
    """A fully played-out signal is terminal; its line carries a replay realized
    P&L that re-computes each cycle. The line must print once, not re-spam when
    only that dollar figure changes."""
    signal = _signal()
    signals_path = tmp_path / "signals.txt"
    signals_path.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(cli, "parse_signals_file", lambda path, **kw: [signal])

    realized = {"v": 67.60}

    def fake_decide(*args, **kwargs):
        entries = [
            SimpleNamespace(
                entry_index=0, lot=0.01, status="TP1",
                fill_time=datetime(2026, 6, 2, 8, 5, 10), entry_price=4470.10,
                exit_time=datetime(2026, 6, 2, 8, 20, 30), exit_price=4478.00, pnl=7.90,
            ),
            SimpleNamespace(
                entry_index=1, lot=0.01, status="NO_FILL",
                fill_time=None, entry_price=4467.40,
                exit_time=None, exit_price=None, pnl=None,
            ),
        ]
        rp = SimpleNamespace(
            entries=entries, signal=SimpleNamespace(side="BUY"),
            realized_pnl=lambda: realized["v"],
        )
        plan = SimpleNamespace(
            action="SKIP_INVALIDATED", orders=[], replay_position=rp,
            rationale="", pending_expires_at=datetime(2026, 6, 2, 14, 30),
        )
        return SimpleNamespace(new_signal=plan)

    monkeypatch.setattr(cli, "decide", fake_decide)

    state: dict[str, str] = {}

    def run_once():
        return cli._auto_pass(
            _args(tmp_path), DEFAULT_CONFIG, _FakeConn(),
            _FakeChart(datetime(2026, 6, 2, 6, 0)), signals_path,
            iteration=1, candidate_console_state=state,
        )

    assert run_once() == 0
    realized["v"] = 64.22  # replay re-computes the realized P&L next cycle
    assert run_once() == 0

    captured = capsys.readouterr()
    # Header + per-entry breakdown all dedupe to a single print despite the flap.
    assert captured.out.count("every entry has already played out in backtest replay") == 1
    assert captured.out.count("#01.1 BUY 0.01 lot  filled 08:05:10 @4470.10 -> closed 08:20:30 @4478.00 TP1 | move +7.90 | $+7.90") == 1
    assert captured.out.count("#01.2 BUY 0.01 lot  no fill | move -- | $0.00") == 1
    assert "[auto heartbeat" not in captured.out


def test_auto_expired_skip_dedupes_across_minute_change(tmp_path, monkeypatch, capsys):
    """The expired-skip line embeds a 'now HH:MM' clock; it must print once even
    as that clock ticks to a new minute across cycles (expiry is terminal)."""
    signal = _signal()
    signals_path = tmp_path / "signals.txt"
    signals_path.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(cli, "parse_signals_file", lambda path, **kw: [signal])

    def fake_decide(*args, **kwargs):
        plan = SimpleNamespace(
            action="SKIP_EXPIRED", orders=[], replay_position=None,
            rationale="", pending_expires_at=datetime(2026, 6, 2, 5, 50),
        )
        return SimpleNamespace(new_signal=plan)

    monkeypatch.setattr(cli, "decide", fake_decide)

    state: dict[str, str] = {}

    def run_once(now):
        return cli._auto_pass(
            _args(tmp_path), DEFAULT_CONFIG, _FakeConn(),
            _FakeChart(now), signals_path,
            iteration=1, candidate_console_state=state,
        )

    assert run_once(datetime(2026, 6, 2, 6, 0)) == 0
    assert run_once(datetime(2026, 6, 2, 6, 1)) == 0  # 'now' advances a minute

    captured = capsys.readouterr()
    assert captured.out.count("pending window already closed") == 1

def test_auto_reopen_tracked_partial_survives_same_cycle_prune(tmp_path, monkeypatch):
    """A trailing partial ladder (placed=0) whose replay still holds OPEN legs is
    tracked for reopen; it must survive the SAME-cycle prune. Before the fix it was
    added then pruned the same cycle (alive was built from the pre-loop registry),
    so it churned in/out every interval and re-logged its 'partial placement' +
    'Pruned' lines. Gated on --reopen-missing-positions, so default mode is
    unchanged."""
    signal = _signal()
    signals_path = tmp_path / "signals.txt"
    signals_path.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(cli, "parse_signals_file", lambda path, **kw: [signal])

    def fake_decide(*a, **k):
        plan = SimpleNamespace(
            action="FOLLOW",
            orders=[SimpleNamespace(entry_index=0, entry_price=4518.0,
                                    lot=0.01, initial_sl=4511.0)],
            replay_position=SimpleNamespace(entries=[
                SimpleNamespace(status="OPEN"), SimpleNamespace(status="CLOSED")]),
            rationale="",
            pending_expires_at=datetime(2026, 6, 2, 14, 30),
            pending_activates_at=datetime(2026, 6, 2, 6, 0),
        )
        return SimpleNamespace(new_signal=plan)
    monkeypatch.setattr(cli, "decide", fake_decide)

    class _PruneReg:
        rows: list[dict] = []

        def __init__(self, path):
            pass

        def load(self):
            return list(_PruneReg.rows)

        def add(self, sig, equity, executed_at=None):
            if not any(r["signal_key"] == sig.signal_key for r in _PruneReg.rows):
                _PruneReg.rows.append({"signal_key": sig.signal_key})

        def prune(self, alive):
            before = len(_PruneReg.rows)
            _PruneReg.rows[:] = [r for r in _PruneReg.rows
                                 if signal_to_magic(r["signal_key"]) in alive]
            return before - len(_PruneReg.rows)
    _PruneReg.rows = []

    class _Exec(_FakeExecutor):
        def reopen_missing_open_positions(self, actual, config):
            return ExecutionLog()

        def replace_missing_pending_entries(self, actual, config, replay_end):
            return ExecutionLog()
    monkeypatch.setattr("trading.engine.SignalRegistry", _PruneReg)
    monkeypatch.setattr("trading.engine.Mt5Executor", _Exec)

    args = _args(tmp_path)
    args.reopen_missing_positions = "true"

    rc = cli._auto_pass(
        args, DEFAULT_CONFIG, _FakeConn(), _FakeChart(datetime(2026, 6, 2, 6, 0)),
        signals_path, iteration=1, candidate_console_state={},
    )
    assert rc == 0
    # Survived: still tracked after the cycle (no add/prune churn).
    assert _PruneReg.rows == [{"signal_key": signal.signal_key}]


def test_auto_startup_feed_scan_prints_once_on_first_cycle(tmp_path, monkeypatch, capsys):
    """First cycle prints a one-line feed scan (placeable vs played-out) so a cold
    start reads clearly; later cycles don't repeat it, and an idle (no-candidate)
    start stays silent."""
    signal = _signal()
    signals_path = tmp_path / "signals.txt"
    signals_path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(cli, "parse_signals_file", lambda path, **kw: [signal])
    monkeypatch.setattr(cli, "decide", lambda *a, **k: _follow_rec())
    state: dict[str, str] = {}

    def run(it):
        return cli._auto_pass(
            _args(tmp_path), DEFAULT_CONFIG, _FakeConn(),
            _FakeChart(datetime(2026, 6, 2, 6, 0)), signals_path,
            iteration=it, candidate_console_state=state,
        )

    assert run(1) == 0
    out1 = capsys.readouterr().out
    assert "Startup feed scan:" in out1
    assert "1 placeable" in out1

    assert run(2) == 0
    assert "Startup feed scan:" not in capsys.readouterr().out


def _skip_invalidated_rec(exp=datetime(2026, 6, 2, 14, 30)):
    plan = SimpleNamespace(
        action="SKIP_INVALIDATED",
        orders=[SimpleNamespace(entry_index=0)],
        replay_position=None,
        rationale="",
        pending_expires_at=exp,
        pending_activates_at=datetime(2026, 6, 2, 6, 0),
    )
    return SimpleNamespace(new_signal=plan)


def test_trailing_live_entry_places_a_replay_played_out_signal(tmp_path, monkeypatch, capsys):
    """--trailing-live-entry: a trailing-open signal the replay marks played-out is
    still placed off the live price (executor decides arm/skip); no 'played out' line."""
    signal = _signal()
    sp = tmp_path / "signals.txt"; sp.write_text("x", encoding="utf-8")
    monkeypatch.setattr(cli, "parse_signals_file", lambda p, **k: [signal])
    monkeypatch.setattr(cli, "decide", lambda *a, **k: _skip_invalidated_rec())
    cfg = replace(DEFAULT_CONFIG, trailing_open_distance=0.2)
    _FakeExecutor.place_log = ExecutionLog(actions=["  placed trailing-open STOP #0"], placed=1)
    args = _args(tmp_path); args.trailing_live_entry = "true"

    rc = cli._auto_pass(args, cfg, _FakeConn(), _FakeChart(datetime(2026, 6, 2, 6, 0)),
                        sp, iteration=2, candidate_console_state={})
    out = capsys.readouterr().out
    assert rc == 0
    assert "placed trailing-open STOP" in out
    assert "already played out" not in out
    assert _FakeRegistry.entries  # tracked after a live placement


def test_trailing_live_entry_off_keeps_replay_played_out_skip(tmp_path, monkeypatch, capsys):
    """Default (flag off): the replay verdict stands -- played-out signal is skipped,
    place_signal is not called."""
    signal = _signal()
    sp = tmp_path / "signals.txt"; sp.write_text("x", encoding="utf-8")
    monkeypatch.setattr(cli, "parse_signals_file", lambda p, **k: [signal])
    monkeypatch.setattr(cli, "decide", lambda *a, **k: _skip_invalidated_rec())
    cfg = replace(DEFAULT_CONFIG, trailing_open_distance=0.2)
    _FakeExecutor.place_log = ExecutionLog(actions=["  placed trailing-open STOP #0"], placed=1)
    args = _args(tmp_path)  # no trailing_live_entry -> off

    rc = cli._auto_pass(args, cfg, _FakeConn(), _FakeChart(datetime(2026, 6, 2, 6, 0)),
                        sp, iteration=2, candidate_console_state={})
    out = capsys.readouterr().out
    assert rc == 0
    assert "already played out" in out
    assert "placed trailing-open STOP" not in out
