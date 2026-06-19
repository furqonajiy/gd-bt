"""Volatility-regime detection (canonical home; re-exported from the package).

The 2021-2026 analysis showed XAUUSD splits into four volatility regimes whose
typical M15 ATR was roughly:

    R1 quiet   ~$2.2     R2 bull     ~$2.8
    R3 strong  ~$5.7     R4 parabolic ~$13.2

Live, we never know the regime by *date* -- we classify the *current* market by
its M15 ATR. The same ATR that sizes the adaptive SL/TP also picks the regime,
so the router and the signal generator stay consistent. Boundaries sit between
adjacent regime ATR levels.

Used by the backtest report (label each month's regime), the live `auto
--adaptive` router (pick the regime's champion config), and the `regime_auto`
CLI. Dependency-light (pandas only) so it can run inside the live loop.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# Calibrated against the 2021-2026 monthly-mean M15 ATR distribution:
#   R1 quiet  $1.4-3.6   R2 bull   $1.6-4.0   (overlap -> trend separates them)
#   R3 strong $2.9-10.3  R4 parab  $9.6-17.4  (clean ATR break ~$9.5)
# ATR alone gives three clean VOLATILITY TIERS; the low tier (quiet vs bull) is
# split by trend, since R1 and R2 share volatility and differ only in direction.
VOL_TIER_LOW_MAX = 4.0     # below this = low-vol tier (R1 or R2)
VOL_TIER_MID_MAX = 9.5     # [4.0, 9.5) = R3 strong; >= 9.5 = R4 parabolic
# Recent return (fraction) above which the low-vol tier is a trending bull (R2).
BULL_TREND_MIN = 0.015

# Today's live regime (2026-06) for reference / the "RUN THIS NOW" pointer.
DEFAULT_LIVE_REGIME = "R4parab"


@dataclass(frozen=True)
class RegimeThresholds:
    """ATR/trend cutoffs used by the live regime router."""
    vol_tier_low_max: float = VOL_TIER_LOW_MAX
    vol_tier_mid_max: float = VOL_TIER_MID_MAX
    bull_trend_min: float = BULL_TREND_MIN

    def __post_init__(self) -> None:
        if self.vol_tier_low_max <= 0:
            raise ValueError("vol_tier_low_max must be > 0")
        if self.vol_tier_mid_max <= self.vol_tier_low_max:
            raise ValueError("vol_tier_mid_max must be > vol_tier_low_max")


DEFAULT_REGIME_THRESHOLDS = RegimeThresholds()


def _thresholds(t: RegimeThresholds | None) -> RegimeThresholds:
    return t or DEFAULT_REGIME_THRESHOLDS


def regime_thresholds_from_mapping(
        data: dict, *, use_learned_boundaries: bool = False) -> RegimeThresholds:
    """Build thresholds from a plain dict or a regime-calibration report JSON.

    Plain threshold dictionaries may contain ``vol_tier_low_max``,
    ``vol_tier_mid_max`` and ``bull_trend_min``. A calibration report JSON may be
    passed directly; by default its ``router_thresholds`` are read. With
    ``use_learned_boundaries=True``, the learned R2/R3 and R3/R4 ATR boundaries
    are used as ``vol_tier_low_max`` and ``vol_tier_mid_max`` while the low-tier
    R1/R2 split remains trend-based.
    """
    source = data.get("meta", data)
    if use_learned_boundaries:
        boundaries = source.get("learned_boundaries") or data.get("learned_boundaries")
        if not isinstance(boundaries, list) or len(boundaries) < 3:
            raise ValueError("learned_boundaries must contain at least 3 values")
        base = source.get("router_thresholds") or {}
        return RegimeThresholds(
            vol_tier_low_max=float(boundaries[1]),
            vol_tier_mid_max=float(boundaries[2]),
            bull_trend_min=float(base.get("bull_trend_min", BULL_TREND_MIN)),
        )

    values = source.get("router_thresholds") or source.get("thresholds") or source
    return RegimeThresholds(
        vol_tier_low_max=float(values.get("vol_tier_low_max", VOL_TIER_LOW_MAX)),
        vol_tier_mid_max=float(values.get("vol_tier_mid_max", VOL_TIER_MID_MAX)),
        bull_trend_min=float(values.get("bull_trend_min", BULL_TREND_MIN)),
    )


def regime_thresholds_from_json_file(
        path: str | Path, *, use_learned_boundaries: bool = False) -> RegimeThresholds:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("regime thresholds JSON must contain an object")
    return regime_thresholds_from_mapping(data, use_learned_boundaries=use_learned_boundaries)


def detect_regime(
        smoothed_m15_atr: float, trend: float = 0.0,
        thresholds: RegimeThresholds | None = None) -> str:
    """Classify the current market from smoothed M15 ATR (USD) and a trend score.

    `trend` is the recent fractional price change; it only matters in the
    low-vol tier, where it separates a quiet range (R1) from a trending bull
    (R2). At mid/high volatility the ATR alone decides.
    """
    t = _thresholds(thresholds)
    if smoothed_m15_atr >= t.vol_tier_mid_max:
        return "R4parab"
    if smoothed_m15_atr >= t.vol_tier_low_max:
        return "R3strong"
    return "R2bull" if trend >= t.bull_trend_min else "R1quiet"


def _m15(m1: pd.DataFrame) -> pd.DataFrame:
    return m1.resample("15min").agg(
        high=("high", "max"), low=("low", "min"), close=("close", "last")
    ).dropna()


def m15_atr(m1: pd.DataFrame, period: int = 14) -> float:
    """Smoothed M15 ATR (USD) -- the mean ATR over the supplied window, so a
    single noisy bar can't flip the regime. Pass a recent window (e.g. the last
    ~5-20 trading days of M1) for a stable read."""
    m15 = _m15(m1)
    prev_close = m15["close"].shift()
    true_range = pd.concat(
        [m15["high"] - m15["low"],
         (m15["high"] - prev_close).abs(),
         (m15["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = true_range.rolling(period, min_periods=period).mean()
    return float(atr.mean())


def trend_score(m1: pd.DataFrame) -> float:
    """Recent fractional price change across the supplied window (close-to-close)."""
    m15 = _m15(m1)
    if len(m15) < 2:
        return 0.0
    first, last = m15["close"].iloc[0], m15["close"].iloc[-1]
    return float((last - first) / first) if first else 0.0


@dataclass
class RegimeReading:
    """What the router knows about the current market this cycle."""
    regime: str
    m15_atr: float
    trend: float
    thresholds: RegimeThresholds = DEFAULT_REGIME_THRESHOLDS

    @property
    def is_live_default(self) -> bool:
        return self.regime == DEFAULT_LIVE_REGIME


def read_current_regime(
        m1: pd.DataFrame, period: int = 14,
        thresholds: RegimeThresholds | None = None) -> RegimeReading:
    atr = m15_atr(m1, period=period)
    tr = trend_score(m1)
    t = _thresholds(thresholds)
    return RegimeReading(regime=detect_regime(atr, tr, t), m15_atr=atr, trend=tr, thresholds=t)
