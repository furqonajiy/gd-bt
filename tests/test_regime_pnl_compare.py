"""Tests for the Phase-2 P&L comparison tool.

The fixed-lot edge arithmetic is tested with synthetic dicts (always runs). The
per-signal-risk loop is checked for parity against run_backtest using real
data/ charts when present, and skips otherwise (same policy as the smoke test).

Run from repo root:  python -m pytest tests/test_regime_pnl_compare.py
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "regime_pnl_compare", ROOT / "tools" / "regime_pnl_compare.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rp = _load_tool()


# ---------------------------------------------------------------------------
# fixed-lot edge arithmetic (pure, no data)
# ---------------------------------------------------------------------------
def _fixed_rows():
    return [
        {"signal_key": "A", "pnl": 100.0, "signal_time_chart": datetime(2025, 3, 1)},
        {"signal_key": "B", "pnl": -40.0, "signal_time_chart": datetime(2025, 4, 1)},
        {"signal_key": "C", "pnl": 60.0, "signal_time_chart": datetime(2025, 5, 1)},
        {"signal_key": "D", "pnl": -20.0, "signal_time_chart": datetime(2026, 1, 1)},
        {"signal_key": "E", "pnl": 50.0, "signal_time_chart": datetime(2026, 2, 1)},
        {"signal_key": "F", "pnl": 30.0, "signal_time_chart": datetime(2026, 3, 1)},
    ]


_LABELS = {
    "A": {"classified": True, "with_trend": True},
    "B": {"classified": True, "with_trend": False},
    "C": {"classified": True, "with_trend": True},
    "D": {"classified": True, "with_trend": False},
    "E": {"classified": True, "with_trend": True},
    "F": {"classified": False, "with_trend": False},   # unclassified -> incumbent only
}


def _by_fold(edge):
    return {r["fold"]: r for r in edge["rows"]}


def test_fixed_lot_edge_arithmetic():
    edge = rp.fixed_lot_edge(_fixed_rows(), _LABELS, derate_factor=0.5)
    rows = _by_fold(edge)

    allr = rows["ALL"]
    assert allr["signals"] == 6
    assert allr["with/counter"] == "3/2"          # F is unclassified, excluded from both
    assert allr["incumbent_net"] == 180.0
    assert allr["with_trend_net"] == 210.0        # 100 + 60 + 50
    assert allr["derate_net"] == 210.0            # 180 - 0.5*(-60)
    assert allr["d_with_trend"] == 30.0
    assert allr["d_derate"] == 30.0

    assert rows["2025"]["incumbent_net"] == 120.0
    assert rows["2025"]["derate_net"] == 140.0    # 120 - 0.5*(-40)
    assert rows["2026"]["incumbent_net"] == 60.0
    assert rows["2026"]["derate_net"] == 70.0     # 60 - 0.5*(-20)


def test_derate_factor_one_equals_incumbent():
    edge = rp.fixed_lot_edge(_fixed_rows(), _LABELS, derate_factor=1.0)
    for r in edge["rows"]:
        assert r["derate_net"] == r["incumbent_net"]


# ---------------------------------------------------------------------------
# per-signal-risk parity with run_backtest (needs data/, else skip)
# ---------------------------------------------------------------------------
DATA_DIR = ROOT / "data"
CHART_FILES = sorted(DATA_DIR.glob("XAUUSD_M1_*.csv"))
SIGNALS_FILE = ROOT / "signals.txt"


@pytest.mark.skipif(not CHART_FILES or not SIGNALS_FILE.exists(),
                    reason="no data/ charts or signals.txt in this checkout")
def test_per_signal_risk_parity_with_run_backtest():
    from trading.engine import DEFAULT_CONFIG, CsvChartSource, parse_signals_file, run_backtest

    signals = parse_signals_file(SIGNALS_FILE)[-500:]   # recent slice keeps the test quick
    chart = CsvChartSource(CHART_FILES)

    baseline = run_backtest(signals, chart, DEFAULT_CONFIG)
    # No counter-trend signals derated and factor 1.0 -> must reproduce run_backtest.
    composed = rp.run_per_signal_risk(signals, chart.dataframe, DEFAULT_CONFIG,
                                      counter_keys=set(), derate_factor=1.0)

    assert abs(composed["net"] - baseline["net_profit"]) < 1e-6
    assert abs(composed["max_dd_pct"] - baseline["max_drawdown_pct"]) < 1e-9