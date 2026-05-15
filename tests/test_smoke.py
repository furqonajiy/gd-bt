"""Smoke test: backtest must reproduce the locked April-2026 baseline.

If this fails, the strategy has drifted. Either revert the change or
re-validate via tools/sweep.py, then update EXPECTED below from the
"Actuals:" line the test prints on failure.

Run from repo root:
    python -m pytest tests/ -s
"""
from __future__ import annotations
import math
from pathlib import Path

from xauusd_trading import (
    CsvChartSource, DEFAULT_CONFIG, parse_signals_file, run_backtest,
)

REPO = Path(__file__).resolve().parents[1]
CHART_FILE = REPO / "data" / "XAUUSD_M1_202604.csv"

EXPECTED = {
    'final_equity': 50493.00999999989,
    'wins': 115,
    'losses': 56,
    'no_fills': 61,
    'open': 0,
    'signals_included': 232,
    'win_rate_pct': 67.2514619883041,
}


def test_backtest_matches_validated_baseline():
    assert CHART_FILE.exists(), (
        f"Missing chart file: {CHART_FILE}. "
        f"Place your April-2026 MT5 export there before running this test."
    )

    signals = parse_signals_file(REPO / "signals.txt")
    chart = CsvChartSource([CHART_FILE])
    result = run_backtest(signals, chart, DEFAULT_CONFIG)

    actuals = {
        "final_equity":     result["final_equity"],
        "wins":             result["wins"],
        "losses":           result["losses"],
        "no_fills":         result["no_fills"],
        "open":             result["open"],
        "signals_included": result["signals_included"],
        "win_rate_pct":     result["win_rate_pct"],
    }
    print("\nActuals:", actuals)

    assert math.isclose(result["final_equity"], EXPECTED["final_equity"], abs_tol=1e-6)
    assert result["wins"]             == EXPECTED["wins"]
    assert result["losses"]           == EXPECTED["losses"]
    assert result["no_fills"]         == EXPECTED["no_fills"]
    assert result["open"]             == EXPECTED["open"]
    assert result["signals_included"] == EXPECTED["signals_included"]
    assert math.isclose(result["win_rate_pct"], EXPECTED["win_rate_pct"], abs_tol=1e-9)