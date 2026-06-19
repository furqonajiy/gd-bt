"""Regime-adaptive config resolution, shared by the live ``auto --adaptive``
loop and the ``backtest --adaptive`` path.

Both need the same two things:
  * load a regime's published champion config (``CHAMPION_<regime>.json`` under a
    champions dir) as a :class:`StrategyConfig`, falling back to an incumbent
    when none exists or anything fails (never raises);
  * map a moment in time to the regime in effect, from a *trailing* window of M1
    ending at that moment -- so the backtest classifies the regime exactly as the
    live loop would (no lookahead into bars the live executor wouldn't have seen).
"""
from __future__ import annotations

import json
from dataclasses import fields as _dc_fields
from pathlib import Path

import pandas as pd

from xauusd_trading.core.config import StrategyConfig
from xauusd_trading.strategy.regime import (
    RegimeThresholds,
    detect_regime,
    m15_atr,
    read_current_regime,
    trend_score,
)


def champion_config(regime: str, champions_dir: str | Path | None,
                    fallback: StrategyConfig) -> StrategyConfig:
    """The regime's published champion as a StrategyConfig, or ``fallback``.

    Reads ``<champions_dir>/CHAMPION_<regime>.json`` (the sweep's deploy file).
    Any missing file / parse error / bad field returns ``fallback`` unchanged, so
    callers never have to guard -- an absent champion just means "run the
    incumbent for this regime".
    """
    path = Path(champions_dir or ".") / f"CHAMPION_{regime}.json"
    if not path.exists():
        return fallback
    try:
        cfg = json.loads(path.read_text()).get("config") or {}
    except (OSError, json.JSONDecodeError):
        return fallback
    valid = {f.name for f in _dc_fields(StrategyConfig)}
    try:
        return StrategyConfig(**{k: v for k, v in cfg.items() if k in valid})
    except (TypeError, ValueError):
        return fallback


def make_regime_config_resolver(chart_df: pd.DataFrame, *, champions_dir,
                                base_config: StrategyConfig,
                                window_days: int = 20,
                                thresholds: RegimeThresholds | None = None):
    """Build ``resolver(signal) -> StrategyConfig`` for ``run_backtest``'s
    ``config_resolver``. For each signal it classifies the regime from the
    ``window_days`` of M1 ending at the signal's chart time (no lookahead) and
    returns that regime's champion config, falling back to ``base_config``.

    Both classifications are cached: the regime by the signal's date (it is stable
    intraday), and the champion config by regime -- so a full backtest costs at
    most one classification per day and one JSON load per regime.
    """
    idx = chart_df[["time", "high", "low", "close"]].set_index("time").sort_index()
    win = pd.Timedelta(days=int(window_days))
    regime_by_day: dict[object, str] = {}
    config_by_regime: dict[str, StrategyConfig] = {}

    def _regime_at(ts) -> str:
        day = pd.Timestamp(ts).normalize()
        cached = regime_by_day.get(day)
        if cached is not None:
            return cached
        window = idx[(idx.index <= ts) & (idx.index > ts - win)]
        if len(window) < 60:
            regime = (
                read_current_regime(idx[idx.index <= ts], thresholds=thresholds).regime
                if len(idx[idx.index <= ts]) else "R4parab"
            )
        else:
            regime = detect_regime(m15_atr(window), trend_score(window), thresholds=thresholds)
        regime_by_day[day] = regime
        return regime

    def resolver(signal):
        regime = _regime_at(signal.signal_time_chart)
        cfg = config_by_regime.get(regime)
        if cfg is None:
            cfg = champion_config(regime, champions_dir, base_config)
            config_by_regime[regime] = cfg
        return cfg

    resolver.regime_at = _regime_at  # exposed for reporting / debugging
    return resolver
