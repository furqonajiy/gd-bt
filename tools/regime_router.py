"""Backward-compatible shim. The regime detector now lives in the package at
``xauusd_trading.strategy.regime`` (re-exported from ``xauusd_trading``) so the
backtest report, the live ``auto --adaptive`` router, and this CLI all share one
implementation. Existing imports of ``tools.regime_router`` keep working.
"""
from __future__ import annotations

from xauusd_trading.strategy.regime import (  # noqa: F401
    BULL_TREND_MIN,
    DEFAULT_LIVE_REGIME,
    DEFAULT_REGIME_THRESHOLDS,
    RegimeReading,
    RegimeThresholds,
    VOL_TIER_LOW_MAX,
    VOL_TIER_MID_MAX,
    detect_regime,
    m15_atr,
    read_current_regime,
    regime_thresholds_from_json_file,
    regime_thresholds_from_mapping,
    trend_score,
)
