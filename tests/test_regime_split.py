"""Tests for the Phase-1 regime-split analyzer's pure functions.

These use synthetic in-memory inputs only, so they run in a fresh clone with no
``data/``. Run from repo root:  python -m pytest tests/test_regime_split.py
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "regime_split_analysis", ROOT / "tools" / "regime_split_analysis.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rs = _load_tool()


def _make_chart() -> pd.DataFrame:
    """30 flat bars then 30 steadily rising bars, all GMT+3 1-minute."""
    t0 = datetime(2025, 1, 2, 1, 0, 0)
    rows = []
    for i in range(60):
        close = (2000.0 + (0.05 if i % 2 else -0.05)) if i < 30 else (2000.0 + (i - 29) * 1.0)
        rows.append({
            "time": t0 + timedelta(minutes=i),
            "open": close, "high": close + 0.2, "low": close - 0.2, "close": close,
            "spread": 20, "spread_price": 0.20,
        })
    return pd.DataFrame(rows)


def test_uptrend_scores_higher_than_flat():
    df = _make_chart()
    t0 = df["time"].iloc[0].to_pydatetime()
    series = rs.compute_indicator_series(df, ema_fast_period=5, ema_slow_period=20, atr_period=5)

    flat = rs.label_signal_regime(series, t0 + timedelta(minutes=29), "BUY", warmup=20)
    up = rs.label_signal_regime(series, t0 + timedelta(minutes=59), "BUY", warmup=20)

    assert flat["classified"] and up["classified"]
    assert up["direction"] == "UP"
    assert up["trend_strength"] > flat["trend_strength"]


def test_with_trend_direction_agreement():
    df = _make_chart()
    t0 = df["time"].iloc[0].to_pydatetime()
    series = rs.compute_indicator_series(df, ema_fast_period=5, ema_slow_period=20, atr_period=5)

    buy = rs.label_signal_regime(series, t0 + timedelta(minutes=59), "BUY", warmup=20)
    sell = rs.label_signal_regime(series, t0 + timedelta(minutes=59), "SELL", warmup=20)

    assert buy["with_trend"] is True       # BUY in an uptrend agrees
    assert sell["with_trend"] is False     # SELL in an uptrend does not


def test_unclassified_when_no_prior_or_unwarmed():
    df = _make_chart()
    t0 = df["time"].iloc[0].to_pydatetime()
    series = rs.compute_indicator_series(df, ema_fast_period=5, ema_slow_period=20, atr_period=5)

    before = rs.label_signal_regime(series, t0 - timedelta(minutes=1), "BUY", warmup=20)
    early = rs.label_signal_regime(series, t0 + timedelta(minutes=3), "BUY", warmup=20)

    assert before == {"classified": False, "reason": "no_prior_bar"}
    assert early["classified"] is False and early["reason"] == "unwarmed"


def test_summarize_rates_and_medians():
    rows = [
        {"filled": True, "mfe": 10.0, "mae": 2.0, "tp1": True, "tp2": False,
         "tp3": False, "sl": False, "near_then_sl": False},
        {"filled": True, "mfe": 4.0, "mae": 6.0, "tp1": False, "tp2": False,
         "tp3": False, "sl": True, "near_then_sl": True},
        {"filled": False, "mfe": None, "mae": None, "tp1": False, "tp2": False,
         "tp3": False, "sl": False, "near_then_sl": False},
    ]
    s = rs.summarize(rows)
    assert s["signals"] == 3
    assert s["fill_rate"] == 2 / 3
    assert s["tp1_rate"] == 0.5          # over the 2 filled
    assert s["sl_rate"] == 0.5
    assert s["mfe_median"] == 7.0        # median(10, 4)
    assert s["mae_median"] == 4.0        # median(2, 6)
    assert s["mfe_to_mae_median"] == 7.0 / 4.0


def test_summarize_empty_bucket_is_safe():
    s = rs.summarize([])
    assert s["signals"] == 0
    assert s["fill_rate"] is None
    assert s["mfe_median"] is None


def test_terciles_partition_all_rows_in_order():
    rows = [{"trend_strength": float(v)} for v in range(1, 10)]  # 9 rows
    buckets = rs.tercile_buckets(rows)
    names = [n for n, _ in buckets]
    assert names == ["T1_low_trend", "T2_mid", "T3_high_trend"]
    sizes = [len(b) for _, b in buckets]
    assert sizes == [3, 3, 3]
    assert sum(sizes) == len(rows)
    t1_max = max(r["trend_strength"] for r in buckets[0][1])
    t3_min = min(r["trend_strength"] for r in buckets[2][1])
    assert t1_max < t3_min