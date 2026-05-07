"""Smoke test: refactored backtest must reproduce the validated baseline.

Run from repo root:
    python -m pytest tests/
"""
from __future__ import annotations
import math
from pathlib import Path

from xauusd_trading import (
    CsvChartSource, DEFAULT_CONFIG, parse_signals_file, run_backtest,
)


REPO = Path(__file__).resolve().parents[1]


def test_backtest_matches_validated_baseline():
    signals = parse_signals_file(REPO / "xauusd_signals_corrected_all.txt")
    chart = CsvChartSource([
        REPO / "XAUUSD_M1_202601221044_202604302359.csv",
        REPO / "XAUUSD_M1_202605010100_202605052359.csv",
    ])
    result = run_backtest(signals, chart, DEFAULT_CONFIG)

    # Locked numbers from the original backtester. Any drift here means the
    # refactor has changed strategy behavior. Don't relax these without
    # re-validating the strategy.
    assert math.isclose(result["final_equity"], 8748.884641596967, rel_tol=0, abs_tol=1e-6)
    assert result["wins"] == 180
    assert result["losses"] == 114
    assert result["no_fills"] == 550
    assert result["open"] == 0
    assert result["signals_included"] == 844
    assert math.isclose(result["win_rate_pct"], 61.224489795918366, abs_tol=1e-9)
