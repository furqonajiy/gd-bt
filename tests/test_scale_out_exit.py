"""Multi-entry scale-out exit mode.

Worst leg (furthest from signal SL) is closed at TP1 and again at TP2; the
remaining legs lock to BEP+buffer after TP1 and trail after TP2, capped at the
final target or pure-trailing past it. All flags default off so the DD40 /
TRAILING-0.5 contract is unchanged (covered by test_smoke.py).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trading.engine import Bar, DEFAULT_CONFIG, advance_bars, open_position, parse_one_signal
from trading.engine.core.positions import _scale_out_mode

ROOT = Path(__file__).resolve().parents[1]


def _bar(t: datetime, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(t, o, h, l, c, 0, 0.0)


def _buy_signal():
    return parse_one_signal(
        "1. BUY XAUUSD 4750 - 4748 SL 4744 TP1 4760 TP2 4770 TP3 4780 10:00 AM",
        source_date="2026-06-01",
        source_offset=3,
    )


def _scale_cfg(**over):
    base = dict(
        entry_count=3,
        entry_ladder="range_to_sl",
        entry_sl_gap=2.0,
        sl_multiplier=1.0,            # base_stop_distance == raw range, clean stops
        activation_delay_minutes=0,
        sizing_mode="fixed",
        lot_per_entry=0.10,           # deterministic lots
        lock_after_tp1=False,
        lock_after_tp2=False,
        trailing_open_distance=0.0,
    )
    base.update(over)
    return replace(DEFAULT_CONFIG, **base)


def _worst_index(pos) -> int:
    return max(range(len(pos.entries)),
               key=lambda i: abs(pos.entries[i].entry_price - pos.signal.sl))


# --- defaults stay off -------------------------------------------------------

def test_default_config_scale_out_is_off():
    assert _scale_out_mode(DEFAULT_CONFIG) is False
    for name in ("scale_out_at_tp1", "scale_out_at_tp2", "bep_after_tp1", "runner_no_final_cap"):
        assert getattr(DEFAULT_CONFIG, name) is False
    assert DEFAULT_CONFIG.bep_buffer == 0.0
    assert DEFAULT_CONFIG.trailing_close_after_stage == 0


# --- scale-out at TP1 --------------------------------------------------------

def test_scale_out_closes_worst_buy_leg_at_tp1():
    sig = _buy_signal()
    cfg = _scale_cfg(scale_out_at_tp1=True)
    pos = open_position(sig, 5000.0, cfg)
    assert [e.entry_price for e in pos.entries] == [4750.0, 4748.0, 4746.0]
    t = sig.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4752, 4752, 4745, 4747),                  # fill all three
        _bar(t + timedelta(minutes=1), 4747, 4762, 4747, 4760),  # touch TP1 only
    ], cfg)

    worst = pos.entries[_worst_index(pos)]                 # highest BUY fill = 4750
    assert worst.entry_price == 4750.0
    assert worst.status == "TP1"
    assert worst.exit_price == 4760.0
    assert pos.scaled_tp1 is True
    assert sum(1 for e in pos.entries if e.status == "OPEN") == 2


def test_scale_out_closes_worst_sell_leg_at_tp1():
    sig = parse_one_signal(
        "1. SELL XAUUSD 4750 - 4752 SL 4756 TP1 4740 TP2 4730 TP3 4720 10:00 AM",
        source_date="2026-06-01", source_offset=3,
    )
    cfg = _scale_cfg(scale_out_at_tp1=True)
    pos = open_position(sig, 5000.0, cfg)
    t = sig.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4748, 4755, 4748, 4750),                  # fill all three (sell fills on the way up)
        _bar(t + timedelta(minutes=1), 4750, 4750, 4738, 4740),  # touch TP1 only
    ], cfg)

    worst = pos.entries[_worst_index(pos)]                 # lowest SELL fill
    assert worst.status == "TP1"
    assert worst.exit_price == 4740.0
    assert worst.entry_price == min(e.entry_price for e in pos.entries)


def test_single_leg_does_not_scale_out_at_tp1():
    sig = _buy_signal()
    cfg = _scale_cfg(entry_count=1, scale_out_at_tp1=True, bep_after_tp1=True, bep_buffer=0.5)
    pos = open_position(sig, 5000.0, cfg)
    t = sig.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4752, 4752, 4749, 4750),
        _bar(t + timedelta(minutes=1), 4750, 4762, 4750, 4760),  # TP1 with a lone leg
    ], cfg)

    assert pos.scaled_tp1 is False
    assert pos.entries[0].status == "OPEN"                 # the lone leg runs, not closed at TP1


# --- BEP+buffer after TP1 ----------------------------------------------------

def test_bep_after_tp1_locks_remaining_legs_with_small_profit():
    sig = _buy_signal()
    cfg = _scale_cfg(scale_out_at_tp1=True, bep_after_tp1=True, bep_buffer=0.5,
                     trailing_close_after_stage=2)
    pos = open_position(sig, 5000.0, cfg)
    t = sig.signal_time_chart

    advance_bars(pos, [
        _bar(t, 4752, 4752, 4745, 4747),
        _bar(t + timedelta(minutes=1), 4747, 4762, 4747, 4760),  # TP1 -> stage 1, BEP arms
    ], cfg)

    # remaining legs now sit at entry + buffer, not the initial SL or TP1 lock
    survivors = [e for e in pos.entries if e.status == "OPEN"]
    assert len(survivors) == 2
    for e in survivors:
        assert pos.effective_stop_for(e, cfg) == pytest.approx(e.entry_price + 0.5)

    # a shallow retrace that only reaches the higher BEP stop closes that leg green
    advance_bars(pos, [
        _bar(t + timedelta(minutes=2), 4760, 4760, 4748, 4750),  # low 4748 hits 4748.5 only
    ], cfg)
    closed = sorted((e for e in pos.entries if e.status == "BEP"), key=lambda e: e.entry_price)
    assert len(closed) == 1
    assert closed[0].entry_price == 4748.0
    assert closed[0].exit_price == pytest.approx(4748.5)
    assert closed[0].pnl > 0                               # locked a small profit, not breakeven


# --- full flow: TP1 -> TP2 -> trailing ---------------------------------------

def _run_to_single_trailing_leg(cfg):
    sig = _buy_signal()
    pos = open_position(sig, 5000.0, cfg)
    t = sig.signal_time_chart
    advance_bars(pos, [
        _bar(t, 4752, 4752, 4745, 4747),                          # fill all
        _bar(t + timedelta(minutes=1), 4747, 4762, 4747, 4760),   # TP1 -> close 4750
        _bar(t + timedelta(minutes=2), 4760, 4772, 4760, 4770),   # TP2 -> close 4748, trail 4746
    ], cfg)
    return pos, t


def test_full_scale_out_flow_then_trailing_exit():
    cfg = _scale_cfg(scale_out_at_tp1=True, scale_out_at_tp2=True, bep_after_tp1=True,
                     bep_buffer=0.5, trailing_close_after_stage=2, trailing_close_distance=0.5)
    pos, t = _run_to_single_trailing_leg(cfg)

    assert pos.entries[0].status == "TP1" and pos.entries[0].exit_price == 4760.0
    assert pos.entries[1].status == "TP2" and pos.entries[1].exit_price == 4770.0
    runner = pos.entries[2]
    assert runner.status == "OPEN"
    assert pos.stage == 2
    assert runner.trailing_stop == pytest.approx(4771.5)
    assert pos.effective_stop_for(runner, cfg) == pytest.approx(4771.5)  # trailing dominates BEP

    advance_bars(pos, [
        _bar(t + timedelta(minutes=3), 4772, 4773, 4771, 4771),  # retrace into trailing stop
    ], cfg)
    assert runner.status == "TRAILING_STOP"
    assert runner.exit_price == pytest.approx(4771.5)
    assert runner.pnl > 0


def test_runner_final_cap_tp3_force_closes_at_target():
    cfg = _scale_cfg(scale_out_at_tp1=True, scale_out_at_tp2=True, bep_after_tp1=True,
                     bep_buffer=0.5, trailing_close_after_stage=2, trailing_close_distance=0.5)
    pos, t = _run_to_single_trailing_leg(cfg)

    advance_bars(pos, [
        _bar(t + timedelta(minutes=3), 4772, 4782, 4772, 4781),  # reach TP3 without hitting trail
    ], cfg)
    runner = pos.entries[2]
    assert runner.status == "TP3"
    assert runner.exit_price == 4780.0


def test_runner_final_cap_none_pure_trails_past_tp3():
    cfg = _scale_cfg(scale_out_at_tp1=True, scale_out_at_tp2=True, bep_after_tp1=True,
                     bep_buffer=0.5, trailing_close_after_stage=2, trailing_close_distance=0.5,
                     runner_no_final_cap=True)
    pos, t = _run_to_single_trailing_leg(cfg)

    advance_bars(pos, [
        _bar(t + timedelta(minutes=3), 4772, 4782, 4772, 4781),  # TP3 touched, no force-close
    ], cfg)
    runner = pos.entries[2]
    assert runner.status == "OPEN"                          # rides past TP3
    assert runner.trailing_stop == pytest.approx(4781.5)


# --- CLI wiring --------------------------------------------------------------

def _load(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "tools" / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_COMMON_STRATEGY = [
    "--initial-capital", "5000", "--sizing-mode", "risk", "--lot", "0.5",
    "--risk", "0.05", "--minimum-lot", "0.01", "--lot-step", "0.01",
    "--bonus-per-closed-lot", "3", "--entries", "4",
    "--entry-ladder", "range_to_sl", "--entry-sl-gap", "2",
    "--activation-delay", "0", "--pending-expiry", "630", "--max-hold", "90",
    "--sl-multiplier", "2", "--final-target", "TP3",
    "--lock-after-tp1", "false", "--lock-after-tp2", "false",
    "--tp1-lock-delay-minutes", "0", "--tp2-lock-delay-minutes", "0",
    "--profit-lock-mode", "tp_levels", "--bep-trigger-distance", "3",
    "--tp1-lock-fraction", "0.5", "--tp2-lock-target", "TP1",
    "--runner-after-tp3", "false", "--tp3-lock-target", "TP2",
    "--trailing-open-distance", "0", "--trailing-close-distance", "0.5",
]

_SCALE_FLAGS = [
    "--scale-out-at-tp1", "true", "--scale-out-at-tp2", "true",
    "--bep-after-tp1", "true", "--bep-buffer", "0.5",
    "--trailing-close-after-stage", "2", "--runner-final-cap", "none",
]


def _assert_scale_config(cfg):
    assert cfg.scale_out_at_tp1 is True
    assert cfg.scale_out_at_tp2 is True
    assert cfg.bep_after_tp1 is True
    assert cfg.bep_buffer == 0.5
    assert cfg.trailing_close_after_stage == 2
    assert cfg.runner_no_final_cap is True


def test_backtest_explicit_scale_out_flows_into_config():
    m = _load("backtest_explicit")
    argv = ["--signals", "s.txt", "--charts", "c.csv", "--output-dir", "out",
            "--max-drawdown-limit-pct", "40", "--progress-interval-seconds", "0",
            *_COMMON_STRATEGY, *_SCALE_FLAGS]
    _assert_scale_config(m.config_from_args(m.build_parser().parse_args(argv)))


def test_auto_explicit_scale_out_flows_into_config():
    m = _load("auto_explicit")
    argv = ["--signals", "s.txt", "--positions-json", "p.json", "--watch-interval", "5",
            "--mt5-symbol", "XAUUSD", "--mt5-server-offset", "3", "--mt5-history-bars", "5000",
            *_COMMON_STRATEGY, *_SCALE_FLAGS]
    _assert_scale_config(m.config_from_args(m.build_parser().parse_args(argv)))


def test_explicit_scale_out_defaults_off_when_omitted():
    m = _load("backtest_explicit")
    argv = ["--signals", "s.txt", "--charts", "c.csv", "--output-dir", "out",
            "--max-drawdown-limit-pct", "40", "--progress-interval-seconds", "0",
            *_COMMON_STRATEGY]
    cfg = m.config_from_args(m.build_parser().parse_args(argv))
    assert _scale_out_mode(cfg) is False


def test_trailing_close_after_stage_out_of_range_rejected():
    m = _load("backtest_explicit")
    argv = ["--signals", "s.txt", "--charts", "c.csv", "--output-dir", "out",
            "--max-drawdown-limit-pct", "40", "--progress-interval-seconds", "0",
            *_COMMON_STRATEGY, "--trailing-close-after-stage", "4"]
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        m.config_from_args(m.build_parser().parse_args(argv))
