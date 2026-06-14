from __future__ import annotations

import numpy as np
import pandas as pd

from tools.regime_router import detect_regime, read_current_regime


def test_detect_regime_vol_tiers():
    # low-vol tier: trend decides quiet vs bull
    assert detect_regime(2.2, trend=0.0) == "R1quiet"
    assert detect_regime(2.2, trend=0.05) == "R2bull"
    assert detect_regime(3.9, trend=0.0) == "R1quiet"
    # mid tier = R3 regardless of trend
    assert detect_regime(4.0, trend=0.5) == "R3strong"
    assert detect_regime(9.4) == "R3strong"
    # high tier = R4
    assert detect_regime(9.5) == "R4parab"
    assert detect_regime(15.0, trend=-0.2) == "R4parab"


def _synth_m1(price: float, wiggle: float, drift: float = 0.0, minutes: int = 60 * 24) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=minutes, freq="1min")
    base = price + np.arange(minutes) * drift + (np.arange(minutes) % 2) * wiggle
    return pd.DataFrame(
        {"open": base, "high": base + wiggle, "low": base - wiggle, "close": base},
        index=idx,
    )


def test_reading_quiet_vs_parabolic():
    quiet = read_current_regime(_synth_m1(1800.0, 0.3))
    wild = read_current_regime(_synth_m1(4500.0, 6.0))
    assert quiet.regime == "R1quiet"
    assert wild.regime == "R4parab"
    assert wild.m15_atr > quiet.m15_atr


def test_low_vol_bull_detected_by_trend():
    # low volatility but a sustained climb -> R2 bull, not R1 quiet
    bull = read_current_regime(_synth_m1(1900.0, 0.3, drift=0.02))
    assert bull.trend >= 0.015
    assert bull.regime == "R2bull"
