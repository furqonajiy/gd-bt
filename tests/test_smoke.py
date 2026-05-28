"""Smoke test: DD40 command backtest must run end-to-end.

This test mirrors the selected DD40 command contract:
filtered provider signals, XAUUSD_M1_*.csv charts, $1000 initial capital,
risk sizing at 0.05575, 3 range_to_sl entries, entry_sl_gap=2, activation delay
3, pending expiry 630, max hold 90, SL multiplier 1.61, TP3 final target,
no TP2 lock, and $3 closed-lot bonus.

Run from repo root:
    python -m pytest tests/ -s
"""
from __future__ import annotations
from dataclasses import replace
from pathlib import Path

import pytest

from xauusd_trading import (
    DEFAULT_CONFIG, CsvChartSource, parse_signals_file, run_backtest,
)

REPO = Path(__file__).resolve().parents[1]
DATA_DIR = REPO / "data"
SIGNALS_FILE = REPO / "generated" / "live_provider_high_growth.txt"
CHART_FILES = sorted(DATA_DIR.glob("XAUUSD_M1_*.csv"))

DD40_COMMAND_CONFIG = replace(
    DEFAULT_CONFIG,
    initial_capital=1000.0,
    sizing_mode="risk",
    risk_per_signal=0.05575,
    entry_count=3,
    entry_ladder="range_to_sl",
    entry_sl_gap=2.0,
    activation_delay_minutes=3,
    pending_expiry_minutes=630,
    max_hold_minutes=90,
    sl_multiplier=1.61,
    final_target="TP3",
    lock_after_tp2=False,
    bonus_per_closed_lot=3.0,
)


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
    result = run_backtest(signals, chart, DD40_COMMAND_CONFIG)

    actuals = {
        "initial_capital": DD40_COMMAND_CONFIG.initial_capital,
        "risk_per_signal": DD40_COMMAND_CONFIG.risk_per_signal,
        "entry_count": DD40_COMMAND_CONFIG.entry_count,
        "entry_ladder": DD40_COMMAND_CONFIG.entry_ladder,
        "entry_sl_gap": DD40_COMMAND_CONFIG.entry_sl_gap,
        "activation_delay_minutes": DD40_COMMAND_CONFIG.activation_delay_minutes,
        "pending_expiry_minutes": DD40_COMMAND_CONFIG.pending_expiry_minutes,
        "max_hold_minutes": DD40_COMMAND_CONFIG.max_hold_minutes,
        "sl_multiplier": DD40_COMMAND_CONFIG.sl_multiplier,
        "final_target": DD40_COMMAND_CONFIG.final_target,
        "lock_after_tp2": DD40_COMMAND_CONFIG.lock_after_tp2,
        "bonus_per_closed_lot": DD40_COMMAND_CONFIG.bonus_per_closed_lot,
        "final_equity": result["final_equity"],
        "wins": result["wins"],
        "losses": result["losses"],
        "no_fills": result["no_fills"],
        "open": result["open"],
        "signals_included": result["signals_included"],
        "win_rate_pct": result["win_rate_pct"],
        "max_drawdown_pct": result["max_drawdown_pct"],
    }
    print("\nDD40 command smoke actuals:", actuals)

    assert result["signals_included"] > 0
    assert result["wins"] + result["losses"] + result["no_fills"] + result["open"] == result["signals_included"]
    assert result["final_equity"] is not None
    assert result["max_drawdown_pct"] <= 0.0
