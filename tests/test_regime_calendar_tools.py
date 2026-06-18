from __future__ import annotations

import numpy as np
import pandas as pd

from tools.filter_signals_by_regime import allowed_dates_from_calendar, filter_feed_by_dates
from tools.regime_calendar_env import calendar_dates, chart_paths_for_dates, env_lines
from xauusd_trading.strategy.regime_calendar import REGIME_ORDER, build_regime_calendar, normalize_regime


def test_regime_alias_and_calendar_feed_filter() -> None:
    calendar = "\n".join([
        "date,sweep_regime,behavior_regime,old_threshold_regime",
        "2026-01-01,R4parab,R4parab,R4parab",
        "2026-01-02,R2bull,R2trend,R2bull",
        "2026-01-03,R2trend,R2trend,R2bull",
    ])
    assert normalize_regime("R2trend") == "R2bull"

    allowed = allowed_dates_from_calendar(calendar, {"R2trend"}, "sweep_regime")
    assert allowed == {"2026-01-02", "2026-01-03"}

    feed = "\n".join([
        "2026-01-01 GMT+7",
        "1. SELL XAUUSD 4500 - 4502 SL 4510 TP1 4490 TP2 4480 TP3 4460 10:00 AM",
        "",
        "2026-01-02 GMT+7",
        "1. BUY XAUUSD 4500 - 4498 SL 4490 TP1 4510 TP2 4520 TP3 4540 10:00 AM",
        "",
        "2026-01-03 GMT+7",
        "1. SELL XAUUSD 4500 - 4502 SL 4510 TP1 4490 TP2 4480 TP3 4460 10:00 AM",
    ]) + "\n"
    filtered = filter_feed_by_dates(feed, allowed)
    assert "2026-01-01" not in filtered
    assert "2026-01-02" in filtered
    assert "2026-01-03" in filtered
    assert filtered.endswith("\n")


def test_regime_calendar_env_derives_chart_months() -> None:
    calendar = "\n".join([
        "date,sweep_regime,behavior_regime,old_threshold_regime",
        "2025-10-17,R4parab,R4parab,R4parab",
        "2025-10-20,R4parab,R4parab,R4parab",
        "2026-02-02,R4parab,R3strong,R4parab",
        "2026-03-10,R3strong,R3strong,R4parab",
    ])
    dates = calendar_dates(calendar, {"R4parab"}, "sweep_regime")
    assert dates == ["2025-10-17", "2025-10-20", "2026-02-02"]
    charts = chart_paths_for_dates(dates)
    assert charts == [
        "data/XAUUSD_M1_202510_ELEV8.csv",
        "data/XAUUSD_M1_202602_ELEV8.csv",
    ]
    lines = env_lines(dates, charts)
    assert "REGIME_START=2025-10-17" in lines
    assert "REGIME_END=2026-02-02" in lines
    assert "REGIME_DAYS=3" in lines
    assert "REGIME_MONTHS=2" in lines


def test_build_regime_calendar_has_canonical_sweep_labels() -> None:
    idx = pd.date_range("2022-01-01", periods=160 * 96, freq="15min")
    block = np.arange(len(idx)) // 96
    intraday = (np.arange(len(idx)) % 96) / 96.0
    vol = np.select(
        [block < 40, block < 80, block < 120],
        [0.4, 1.0, 2.0],
        default=5.0,
    )
    drift = np.select(
        [block < 40, block < 80, block < 120],
        [0.00, 0.04, 0.07],
        default=-0.02,
    )
    base = 1800.0 + block * drift + np.sin(intraday * np.pi * 2.0) * vol
    m1_like = pd.DataFrame(
        {
            "open": base,
            "high": base + vol,
            "low": base - vol,
            "close": base + np.cos(intraday * np.pi * 2.0) * vol * 0.25,
            "tickvol": 1,
            "spread": 20,
        },
        index=idx,
    )

    calendar = build_regime_calendar(m1_like)
    labels = set(calendar["sweep_regime"].dropna())
    assert labels
    assert labels.issubset(set(REGIME_ORDER))
    assert calendar["behavior_regime"].dropna().isin(REGIME_ORDER).all()
    assert calendar["old_threshold_regime"].isin(REGIME_ORDER).all()
