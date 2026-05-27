"""Smoke test: balanced default backtest must run end-to-end.

The exact P&L changes whenever the strategy preset changes. This smoke test
therefore protects parser/chart/backtest integration rather than freezing one
historical optimization result.

Run from repo root:
    python -m pytest tests/ -s
"""
from __future__ import annotations
from pathlib import Path

from xauusd_trading import (
    BALANCED_LIVE_CONFIG, CsvChartSource, parse_signals_file, run_backtest,
)

REPO = Path(__file__).resolve().parents[1]
CHART_FILE = REPO / "data" / "XAUUSD_M1_202604.csv"


def test_backtest_runs_balanced_default():
    assert CHART_FILE.exists(), (
        f"Missing chart file: {CHART_FILE}. "
        f"Place your April-2026 MT5 export there before running this test."
    )

    signals = parse_signals_file(REPO / "signals.txt")
    chart = CsvChartSource([CHART_FILE])
    result = run_backtest(signals, chart, BALANCED_LIVE_CONFIG)

    actuals = {
        "final_equity": result["final_equity"],
        "wins": result["wins"],
        "losses": result["losses"],
        "no_fills": result["no_fills"],
        "open": result["open"],
        "signals_included": result["signals_included"],
        "win_rate_pct": result["win_rate_pct"],
    }
    print("\nActuals:", actuals)

    assert result["signals_included"] > 0
    assert result["wins"] + result["losses"] + result["no_fills"] + result["open"] == result["signals_included"]
    assert result["final_equity"] is not None
    assert result["max_drawdown_pct"] <= 0.0
