"""Smoke test: DD40 command backtest must run end-to-end.

This test mirrors the selected DD40 command contract:
filtered provider signals, XAUUSD_M1_*.csv charts, $5000 initial capital,
risk sizing at 0.05575, 3 range_to_sl entries, entry_sl_gap=2, activation delay
3, pending expiry 630, max hold 90, SL multiplier 1.61, TP3 final target,
no TP2 lock, and $3 closed-lot bonus.

Run from repo root:
    python -m pytest tests/ -s
"""
from __future__ import annotations
from pathlib import Path

import pytest

from trading.xauusd import (
    DEFAULT_CONFIG, CsvChartSource, parse_signals_file, run_backtest,
)

REPO = Path(__file__).resolve().parents[1]
DATA_DIR = REPO / "data"
SIGNALS_FILE = REPO / "generated" / "live_provider_high_growth.txt"
CHART_FILES = sorted(DATA_DIR.glob("XAUUSD_M1_*.csv"))


DD40_DEFAULT_EXPECTED = {
    "initial_capital": 50000.0,
    "sizing_mode": "risk",
    "risk_per_signal": 0.05575,
    "entry_count": 3,
    "entry_ladder": "range_to_sl",
    "entry_sl_gap": 2.0,
    "activation_delay_minutes": 3,
    "pending_expiry_minutes": 630,
    "max_hold_minutes": 90,
    "sl_multiplier": 1.61,
    "final_target": "TP3",
    "lock_after_tp1": True,
    "lock_after_tp2": False,
    "tp1_lock_delay_minutes": 0,
    "tp2_lock_delay_minutes": 0,
    "profit_lock_mode": "tp_levels",
    "bonus_per_closed_lot": 3.0,
    # Research toggles must default OFF for DD40. Pinning them here makes the
    # contract test fail fast on XAUUSD_* env-var drift instead of letting it
    # cascade into unrelated executor/fill test failures.
    "trailing_open_distance": 0.0,
    "trailing_close_distance": 0.0,
    "trend_runner_enabled": False,
}


def test_default_config_matches_dd40_contract():
    actuals = {
        key: getattr(DEFAULT_CONFIG, key)
        for key in DD40_DEFAULT_EXPECTED
    }
    assert actuals == DD40_DEFAULT_EXPECTED


def test_dd40_command_backtest_runs_end_to_end():
    if not SIGNALS_FILE.exists():
        pytest.skip(f"Missing filtered provider signals: {SIGNALS_FILE}")
    if not CHART_FILES:
        pytest.skip(
            f"No chart files found under {DATA_DIR}. "
            "Place at least one XAUUSD_M1_*.csv export before running this smoke test."
        )

    signals = parse_signals_file(SIGNALS_FILE)
    chart = CsvChartSource(CHART_FILES)
    result = run_backtest(signals, chart, DEFAULT_CONFIG)

    actuals = {
        "initial_capital": DEFAULT_CONFIG.initial_capital,
        "risk_per_signal": DEFAULT_CONFIG.risk_per_signal,
        "entry_count": DEFAULT_CONFIG.entry_count,
        "entry_ladder": DEFAULT_CONFIG.entry_ladder,
        "entry_sl_gap": DEFAULT_CONFIG.entry_sl_gap,
        "activation_delay_minutes": DEFAULT_CONFIG.activation_delay_minutes,
        "pending_expiry_minutes": DEFAULT_CONFIG.pending_expiry_minutes,
        "max_hold_minutes": DEFAULT_CONFIG.max_hold_minutes,
        "sl_multiplier": DEFAULT_CONFIG.sl_multiplier,
        "final_target": DEFAULT_CONFIG.final_target,
        "lock_after_tp2": DEFAULT_CONFIG.lock_after_tp2,
        "bonus_per_closed_lot": DEFAULT_CONFIG.bonus_per_closed_lot,
        "final_equity": result["final_equity"],
        "wins": result["wins"],
        "losses": result["losses"],
        "breakevens": result["breakevens"],
        "no_fills": result["no_fills"],
        "open": result["open"],
        "signals_included": result["signals_included"],
        "win_rate_pct": result["win_rate_pct"],
        "max_drawdown_pct": result["max_drawdown_pct"],
    }
    print("\nDD40 command smoke actuals:", actuals)

    assert result["signals_included"] > 0
    # Every replayed signal lands in exactly one terminal bucket. position_status
    # returns WIN | LOSS | BREAKEVEN | NO_FILL | OPEN, so all five must be summed.
    assert (
                   result["wins"] + result["losses"] + result["breakevens"]
                   + result["no_fills"] + result["open"]
           ) == result["signals_included"]
    assert result["final_equity"] is not None
    assert result["max_drawdown_pct"] <= 0.0


def test_every_terminal_status_has_a_bucket():
    """Guard against the BREAKEVEN-style orphan: every status position_status can
    return must map to a counted bucket present in the bucket template. Runs
    without market data, so it always executes (the end-to-end test skips when the
    provider feed or charts are absent)."""
    from trading.xauusd.strategy.backtest import _STATUS_TO_KEY, _new_bucket

    terminal_statuses = {"WIN", "LOSS", "BREAKEVEN", "NO_FILL", "OPEN"}
    assert set(_STATUS_TO_KEY) == terminal_statuses

    bucket = _new_bucket("month", "1970-01", 0.0)
    for key in _STATUS_TO_KEY.values():
        assert key in bucket