"""End-to-end wiring of `auto --apply-signal-edits` through cli._auto_pass.

The override-consumer logic itself is unit-tested in
test_signal_overrides_executor.py against the real executor + registry. Here we
only pin the wiring inside the live `_auto_pass`: with the flag on, an amend
flattens + re-places, a revoke flattens + is held out of the candidate pass, and
with the flag off the journal is ignored entirely.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import trading.xauusd.cli as cli
from trading.xauusd import DEFAULT_CONFIG, ExecutionLog, SignalRegistry, parse_one_signal


class _Tick:
    bid = 4500.0
    ask = 4500.2


class _Mt5:
    def symbol_info_tick(self, symbol):
        return _Tick()


class _Conn:
    def __init__(self):
        self.mt5 = _Mt5()


class _Chart:
    def __init__(self, now):
        self.now = now

    def last_time(self):
        return self.now

    def latest(self):
        return None

    def bars_between(self, start, end):
        return []


class _Exec:
    last: "_Exec | None" = None

    def __init__(self, *args, **kwargs):
        self.flattened: list[tuple[str, str]] = []
        self.placed: list[str] = []
        type(self).last = self

    def reconcile_with_mt5(self, *args, **kwargs):
        return ExecutionLog()

    def manage_position(self, *args, **kwargs):
        return ExecutionLog()

    def sanity_checks(self, expected_equity=None):
        return []

    def find_orders(self, magic):
        return []

    def find_positions(self, magic):
        return []

    def warn_on_unknown(self, known):
        return []

    def all_alive_magics(self):
        return set()

    def flatten_signal(self, key, *, reason="amend"):
        self.flattened.append((key, reason))
        return ExecutionLog(cancelled=1)

    def place_signal(self, signal, plan):
        self.placed.append(signal.signal_key)
        return ExecutionLog(actions=[f"placed {signal.signal_key}"], placed=1)


class _Forensic:
    enabled = False

    def start_cycle(self, **k):
        pass

    def decision(self, **k):
        pass

    def end_cycle(self, **k):
        pass

    def error(self, *a, **k):
        pass


class _Notifier:
    path = None

    def __getattr__(self, name):
        return lambda *a, **k: None


def _follow_rec():
    plan = SimpleNamespace(
        action="FOLLOW", orders=[], replay_position=None, rationale="",
        pending_expires_at=datetime(2099, 1, 1),
        pending_activates_at=datetime(2000, 1, 1),
    )
    return SimpleNamespace(new_signal=plan)


def _signal():
    sig = parse_one_signal(
        "2. BUY XAUUSD 4321 - 4319 SL 4314 TP1 4329 TP2 4339 TP3 4359 11:15 AM",
        source_date="2026-06-17", source_offset=7)
    sig.tag = "VIC"
    return sig


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_make_notifier", lambda args: _Notifier())
    monkeypatch.setattr(cli, "_make_forensic", lambda args: _Forensic())
    monkeypatch.setattr(cli, "_handle_closures", lambda *a, **k: None)
    monkeypatch.setattr(cli, "report_entry_closures", lambda *a, **k: None)
    monkeypatch.setattr("trading.xauusd.mt5_equity", lambda conn: 5000.0)
    monkeypatch.setattr("trading.xauusd.Mt5Executor", _Exec)
    monkeypatch.setattr(cli, "decide", lambda *a, **k: _follow_rec())
    monkeypatch.setattr(cli, "parse_signals_file", lambda path, **kw: [_signal()])
    _Exec.last = None

    overrides = tmp_path / "signal_overrides.jsonl"
    overrides.write_text("", encoding="utf-8")
    signals = tmp_path / "victor_live.txt"
    signals.write_text("placeholder", encoding="utf-8")
    registry_path = tmp_path / "positions_victor.json"

    def _args(apply_edits):
        return SimpleNamespace(
            mt5_symbol="XAUUSD", mt5_server_offset=3,
            positions_json=str(registry_path),
            strategy_tag="VIC",
            apply_signal_edits=apply_edits,
            signal_overrides_file=str(overrides),
            replace_missing_entries="false", reopen_missing_positions="false",
            adaptive="false",
            no_notifications=True, notifications=None,
            no_forensic=True, forensic_log=None,
        )

    def _run(apply_edits):
        return cli._auto_pass(
            _args(apply_edits), DEFAULT_CONFIG, _Conn(),
            _Chart(datetime(2026, 6, 17, 8, 30)), signals, iteration=1,
        )

    def _append(obj):
        with overrides.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, separators=(",", ":")) + "\n")

    return SimpleNamespace(run=_run, append=_append,
                           registry=SignalRegistry(registry_path))


def test_revoke_flattens_and_is_held_out_of_placement(harness):
    harness.run(True)  # anchor the journal offset at EOF
    harness.append({"signal_key": "2026-06-17#02", "action": "revoke"})

    harness.run(True)

    ex = _Exec.last
    assert ("VIC-2026-06-17#02", "revoke") in ex.flattened
    assert ex.placed == []                              # not re-placed
    assert harness.registry.load() == []               # untracked


def test_amend_flattens_and_re_places_corrected(harness):
    harness.run(True)
    harness.append({"signal_key": "2026-06-17#02", "action": "amend"})

    harness.run(True)

    ex = _Exec.last
    assert ("VIC-2026-06-17#02", "amend") in ex.flattened
    assert ex.placed == ["VIC-2026-06-17#02"]          # re-placed from corrected feed
    assert "VIC-2026-06-17#02" in ex._amended_force_replace_keys


def test_flag_off_ignores_the_journal(harness):
    harness.append({"signal_key": "2026-06-17#02", "action": "revoke"})

    harness.run(False)

    ex = _Exec.last
    assert ex.flattened == []
    # Journal ignored, so the feed signal is simply placed as a normal candidate.
    assert ex.placed == ["VIC-2026-06-17#02"]
