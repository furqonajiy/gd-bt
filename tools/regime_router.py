"""Backward-compatible shim. The regime detector now lives in the package at
``xauusd_trading.strategy.regime`` (re-exported from ``xauusd_trading``) so the
backtest report, the live ``auto --adaptive`` router, and this CLI all share one
implementation. Existing imports of ``tools.regime_router`` keep working.
"""
from __future__ import annotations

from xauusd_trading.strategy.regime import (  # noqa: F401
    BULL_TREND_MIN,
    DEFAULT_LIVE_REGIME,
    RegimeReading,
    VOL_TIER_LOW_MAX,
    VOL_TIER_MID_MAX,
    detect_regime,
    m15_atr,
    read_current_regime,
    trend_score,
)
