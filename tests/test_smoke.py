"""Smoke test: backtest must reproduce the re-validated April-2026 baseline.

Run from repo root:
    python -m pytest tests/ -s

Re-validation flow (one-time, after data refresh):
    1. Make sure data/XAUUSD_M1_202604.csv is in place.
    2. Run:   python -m pytest tests/test_smoke.py -s
       The test will fail and print the actual numbers under "Actuals:".
    3. Paste those numbers into the EXPECTED dict below.
    4. Re-run pytest; it should now pass and stay locked.
"""
from __future__ import annotations
import math
from pathlib import Path

from xauusd_trading import (
    CsvChartSource, DEFAULT_CONFIG, parse_signals_file, run_backtest,
)

REPO = Path(__file__).resolve().parents[1]
CHART_FILE = REPO / "data" / "XAUUSD_M1_202604.csv"


# ============================================================================
# RE-VALIDATED BASELINE — April 2026 only
# Replace the placeholders below after the first run. See module docstring.
# ============================================================================
EXPECTED = {
    "final_equity":     None,   # TODO: paste actual after first run
    "wins":             None,
    "losses":           None,
    "no_fills":         None,
    "open":             None,
    "signals_included": None,
    "win_rate_pct":     None,
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

    if any(v is None for v in EXPECTED.values()):
        raise AssertionError(
            "Baseline placeholders are still None. Paste 'Actuals' (above) "
            "into EXPECTED in tests/test_smoke.py and re-run."
        )

    assert math.isclose(result["final_equity"], EXPECTED["final_equity"], abs_tol=1e-6)
    assert result["wins"]             == EXPECTED["wins"]
    assert result["losses"]           == EXPECTED["losses"]
    assert result["no_fills"]         == EXPECTED["no_fills"]
    assert result["open"]             == EXPECTED["open"]
    assert result["signals_included"] == EXPECTED["signals_included"]
    assert math.isclose(result["win_rate_pct"], EXPECTED["win_rate_pct"], abs_tol=1e-9)
