"""CI-safe tests for the 1H-trend-gate helpers in tools/generate_self_signals.py."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from pytest import approx

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.generate_self_signals import _resample_ohlc, _build_h1_trend  # noqa: E402


def _m15_hour(hour: int, close: float) -> list[dict]:
    base = pd.Timestamp("2025-01-02") + pd.Timedelta(hours=hour)
    return [{"time": base + pd.Timedelta(minutes=15 * k), "open": close, "high": close + 0.5,
             "low": close - 0.5, "close": close, "spread": 20} for k in range(4)]


def test_resample_drops_incomplete_bucket_and_aggregates():
    rows = []
    for k in range(4):  # full hour 0: 4 M15 bars
        t = pd.Timestamp("2025-01-02 00:00") + pd.Timedelta(minutes=15 * k)
        rows.append({"time": t, "open": 100 + k, "high": 105 + k, "low": 95 + k, "close": 101 + k, "spread": 20})
    for k in range(2):  # partial hour 1: only 2 bars -> must be dropped (min_bars=4)
        t = pd.Timestamp("2025-01-02 01:00") + pd.Timedelta(minutes=15 * k)
        rows.append({"time": t, "open": 200, "high": 205, "low": 195, "close": 201, "spread": 20})
    h1 = _resample_ohlc(pd.DataFrame(rows), "1h", 4)
    assert len(h1) == 1                       # the 2-bar hour is dropped
    assert h1.iloc[0]["open"] == approx(100)  # first
    assert h1.iloc[0]["high"] == approx(108)  # max (105+3)
    assert h1.iloc[0]["low"] == approx(95)    # min
    assert h1.iloc[0]["close"] == approx(104)  # last (101+3)


def test_build_h1_trend_signs_and_close_stamping():
    # Rising then falling hourly closes; tiny EMAs make the cross immediate.
    closes = [100.0, 101.0, 102.0, 103.0, 102.0, 101.0]
    rows = [r for h, c in enumerate(closes) for r in _m15_hour(h, c)]
    trend = _build_h1_trend(pd.DataFrame(rows), None, ema_fast=1, ema_slow=2)

    assert list(trend.columns) == ["trend_known_time", "h1_trend"]
    assert len(trend) == len(closes)                       # one row per closed hour
    assert set(trend["h1_trend"].unique()) <= {-1, 0, 1}
    # Trend is stamped at the bar CLOSE (open + 1h), not the open -> no look-ahead.
    first_open = pd.Timestamp("2025-01-02 00:00")
    assert trend["trend_known_time"].iloc[0] == first_open + pd.Timedelta(hours=1)
    # Rising stretch goes long; the falling tail goes short.
    assert (trend["h1_trend"].iloc[1:4] == 1).any()
    assert (trend["h1_trend"].iloc[4:] == -1).any()