"""Data-driven regime calendar for XAUUSD sweeps.

This module is intentionally separate from the live ``regime.py`` router.  The
live router must classify the current market from a short trailing window.  The
calendar builder is an offline research/sweep tool: it labels every historical
day from M1 data using a richer set of causal daily features, then exposes a
broader ``sweep_regime`` that has enough samples for optimizer jobs.
"""
from __future__ import annotations

import glob
import math
from collections import Counter
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

REGIME_ORDER = ("R1quiet", "R2bull", "R3strong", "R4parab")
REGIME_ALIASES = {
    "R1quiet": "R1quiet",
    "R2bull": "R2bull",
    "R2trend": "R2bull",
    "R3strong": "R3strong",
    "R4parab": "R4parab",
}

FEATURE_COLUMNS = [
    "m15_tr_mean_pct",
    "m15_tr_p90_pct",
    "daily_range_pct",
    "vol_of_vol_20d",
    "trend_20d",
    "abs_trend_20d",
    "trend_efficiency_20d",
    "choppiness_20d",
    "shock_rate_20d",
    "wick_ratio_20d",
    "spread_pct",
]


def normalize_regime(regime: str) -> str:
    """Return the canonical repo regime name, preserving unknown names."""
    return REGIME_ALIASES.get(str(regime), str(regime))


def expand_chart_paths(patterns: Iterable[str]) -> list[str]:
    """Expand shell-independent chart globs while preserving explicit paths."""
    paths: list[str] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        paths.extend(matches if matches else [pattern])
    return sorted(dict.fromkeys(paths))


def load_m1(paths: Iterable[str | Path]) -> pd.DataFrame:
    """Load ELEV8-style tab-separated M1 files into an indexed dataframe."""
    frames = []
    for path in sorted(str(p) for p in paths):
        df = pd.read_csv(path, sep="\t")
        df.columns = [c.strip("<>").lower() for c in df.columns]
        dt = pd.to_datetime(df["date"] + " " + df["time"], format="%Y.%m.%d %H:%M:%S")
        df = df.assign(time=dt)
        frames.append(df[["time", "open", "high", "low", "close", "tickvol", "spread"]])
    if not frames:
        raise ValueError("no chart files supplied")
    out = pd.concat(frames, ignore_index=True).drop_duplicates("time").sort_values("time")
    return out.set_index("time")


def true_range(ohlc: pd.DataFrame) -> pd.Series:
    prev_close = ohlc["close"].shift()
    parts = pd.concat(
        [
            ohlc["high"] - ohlc["low"],
            (ohlc["high"] - prev_close).abs(),
            (ohlc["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return parts.max(axis=1)


def _choppiness(high: pd.Series, low: pd.Series, tr_sum: pd.Series, n: int) -> pd.Series:
    denom = high - low
    raw = np.log10((tr_sum / denom.replace(0, np.nan)).clip(lower=1.0)) / math.log10(n) * 100.0
    return raw.replace([np.inf, -np.inf], np.nan)


def build_daily_features(m1: pd.DataFrame) -> pd.DataFrame:
    """Compute causal daily features from M1/M15 bars."""
    m15 = m1.resample("15min").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        tickvol=("tickvol", "sum"),
        spread=("spread", "median"),
    ).dropna()
    m15["tr"] = true_range(m15)

    daily = m1.resample("1D").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        rows=("close", "size"),
        spread=("spread", "median"),
    ).dropna()
    daily["daily_range"] = daily["high"] - daily["low"]
    daily["daily_range_pct"] = daily["daily_range"] / daily["close"]
    daily["daily_return"] = daily["close"].pct_change()
    daily["m15_tr_mean"] = m15["tr"].resample("1D").mean()
    daily["m15_tr_p90"] = m15["tr"].resample("1D").quantile(0.90)
    daily["m15_tr_mean_pct"] = daily["m15_tr_mean"] / daily["close"]
    daily["m15_tr_p90_pct"] = daily["m15_tr_p90"] / daily["close"]
    daily["spread_pct"] = (daily["spread"] / 100.0) / daily["close"]

    abs_daily_move = daily["close"].diff().abs()
    daily["trend_20d"] = daily["close"].pct_change(20)
    daily["abs_trend_20d"] = daily["trend_20d"].abs()
    daily["path_length_20d"] = abs_daily_move.rolling(20, min_periods=12).sum()
    daily["net_move_20d"] = (daily["close"] - daily["close"].shift(20)).abs()
    daily["trend_efficiency_20d"] = daily["net_move_20d"] / daily["path_length_20d"].replace(0, np.nan)
    daily["tr_sum_20d"] = true_range(daily).rolling(20, min_periods=12).sum()
    daily["roll_high_20d"] = daily["high"].rolling(20, min_periods=12).max()
    daily["roll_low_20d"] = daily["low"].rolling(20, min_periods=12).min()
    daily["choppiness_20d"] = _choppiness(daily["roll_high_20d"], daily["roll_low_20d"], daily["tr_sum_20d"], 20)
    daily["vol_of_vol_20d"] = daily["m15_tr_mean"].rolling(20, min_periods=12).std() / daily["m15_tr_mean"].rolling(20, min_periods=12).mean()

    shock_threshold = m15["tr"].rolling(20 * 24 * 4, min_periods=24 * 4).quantile(0.95)
    daily["shock_rate_20d"] = (m15["tr"] > shock_threshold).resample("1D").mean().rolling(20, min_periods=12).mean()

    candle_body = (daily["close"] - daily["open"]).abs()
    candle_range = (daily["high"] - daily["low"]).replace(0, np.nan)
    daily["wick_ratio_20d"] = (1.0 - candle_body / candle_range).rolling(20, min_periods=12).mean()
    return daily


def _robust_scale(df: pd.DataFrame) -> pd.DataFrame:
    med = df.median()
    iqr = (df.quantile(0.75) - df.quantile(0.25)).replace(0, np.nan).fillna(1.0)
    return (df - med) / iqr


def _kmeans(x: np.ndarray, k: int = 4, seed: int = 20260618, max_iter: int = 200) -> np.ndarray:
    if len(x) < k:
        raise ValueError(f"need at least {k} rows for k-means regime clustering")
    rng = np.random.default_rng(seed)
    center_idx = [int(np.argmin(np.linalg.norm(x - np.median(x, axis=0), axis=1)))]
    for _ in range(1, k):
        dist2 = np.min(((x[:, None, :] - x[center_idx][None, :, :]) ** 2).sum(axis=2), axis=1)
        probs = dist2 / dist2.sum() if dist2.sum() > 0 else np.ones(len(x)) / len(x)
        center_idx.append(int(rng.choice(len(x), p=probs)))
    centers = x[center_idx].copy()
    labels = np.zeros(len(x), dtype=int)
    for _ in range(max_iter):
        new_labels = np.argmin(((x[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2), axis=1)
        if np.array_equal(labels, new_labels):
            break
        labels = new_labels
        for j in range(k):
            if np.any(labels == j):
                centers[j] = x[labels == j].mean(axis=0)
    return labels


def _map_behavior_clusters(daily: pd.DataFrame) -> dict[int, str]:
    stats = daily.groupby("cluster").agg(
        atr=("m15_tr_mean_pct", "mean"),
        p90=("m15_tr_p90_pct", "mean"),
        range_pct=("daily_range_pct", "mean"),
        trend=("trend_20d", "mean"),
        efficiency=("trend_efficiency_20d", "mean"),
        chop=("choppiness_20d", "mean"),
        shock=("shock_rate_20d", "mean"),
    )
    stats["vol_score"] = stats[["atr", "p90", "range_pct", "shock"]].rank(pct=True).mean(axis=1)
    ordered = list(stats.sort_values("vol_score").index)
    high = ordered[-1]
    mid = ordered[-2]
    low_two = ordered[:2]
    low_sorted = sorted(
        low_two,
        key=lambda c: (stats.loc[c, "trend"], stats.loc[c, "efficiency"], -stats.loc[c, "chop"]),
    )
    return {
        int(low_sorted[0]): "R1quiet",
        int(low_sorted[1]): "R2bull",
        int(mid): "R3strong",
        int(high): "R4parab",
    }


def _old_threshold_regime(row: pd.Series) -> str:
    atr = row["m15_tr_mean"]
    trend = row["trend_20d"] if pd.notna(row["trend_20d"]) else 0.0
    if atr >= 9.5:
        return "R4parab"
    if atr >= 4.0:
        return "R3strong"
    return "R2bull" if trend >= 0.015 else "R1quiet"


def _assign_sweep_regime(daily: pd.DataFrame, usable_index: pd.Index) -> None:
    rank_cols = [
        "m15_tr_mean_pct",
        "m15_tr_p90_pct",
        "daily_range_pct",
        "shock_rate_20d",
        "vol_of_vol_20d",
    ]
    daily.loc[usable_index, "vol_intensity"] = daily.loc[usable_index, rank_cols].rank(pct=True).mean(axis=1)

    directional = pd.concat(
        [
            daily.loc[usable_index, "abs_trend_20d"].rank(pct=True),
            daily.loc[usable_index, "trend_efficiency_20d"].rank(pct=True),
            1.0 - daily.loc[usable_index, "choppiness_20d"].rank(pct=True),
        ],
        axis=1,
    )
    daily.loc[usable_index, "directionality"] = directional.mean(axis=1)

    def label(row: pd.Series) -> str:
        vol = row["vol_intensity"]
        directionality = row["directionality"]
        trend = row["trend_20d"]
        if vol >= 0.90:
            return "R4parab"
        if vol >= 0.70 or (vol >= 0.58 and directionality >= 0.68):
            return "R3strong"
        if directionality >= 0.52 and trend > 0.0:
            return "R2bull"
        return "R1quiet"

    daily.loc[usable_index, "sweep_regime"] = daily.loc[usable_index].apply(label, axis=1)


def build_regime_calendar(m1: pd.DataFrame) -> pd.DataFrame:
    """Return a daily dataframe with behavior, sweep, and old threshold labels."""
    daily = build_daily_features(m1)
    usable = daily[FEATURE_COLUMNS].replace([np.inf, -np.inf], np.nan).dropna()
    labels = _kmeans(_robust_scale(usable).to_numpy(), k=4)
    daily.loc[usable.index, "cluster"] = labels
    mapping = _map_behavior_clusters(daily.loc[usable.index])
    daily["behavior_regime"] = daily["cluster"].map(mapping)
    _assign_sweep_regime(daily, usable.index)
    daily["old_threshold_regime"] = daily.apply(_old_threshold_regime, axis=1)
    daily["month"] = daily.index.to_period("M").astype(str)
    return daily


def monthly_regime_summary(calendar: pd.DataFrame) -> pd.DataFrame:
    """Summarize dominant regime labels per month for reports."""
    usable = calendar.dropna(subset=["sweep_regime", "behavior_regime"]).copy()
    return usable.groupby("month").agg(
        sweep_regime=("sweep_regime", lambda s: Counter(s).most_common(1)[0][0]),
        behavior_regime=("behavior_regime", lambda s: Counter(s).most_common(1)[0][0]),
        old_threshold_regime=("old_threshold_regime", lambda s: Counter(s).most_common(1)[0][0]),
        days=("close", "size"),
        r4_sweep_days=("sweep_regime", lambda s: int((s == "R4parab").sum())),
        r3_r4_sweep_share=("sweep_regime", lambda s: float(s.isin(["R3strong", "R4parab"]).mean())),
        close_start=("open", "first"),
        close_end=("close", "last"),
        vol_intensity=("vol_intensity", "mean"),
        directionality=("directionality", "mean"),
        m15_tr_mean=("m15_tr_mean", "mean"),
        m15_tr_mean_pct=("m15_tr_mean_pct", "mean"),
    ).reset_index()
