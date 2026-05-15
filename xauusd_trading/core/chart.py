"""Chart loading — MT5 1-minute bars in tab-separated CSV.

CSV columns: <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>

All prices are Bid. SPREAD is in points; 1 point = $0.01. Ask = Bid + spread.
Times are GMT+3 (chart timezone).
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

import pandas as pd

from xauusd_trading import POINT_VALUE


@dataclass(frozen=True)
class Bar:
    """One 1-minute bar. Prices are Bid; spread_price is dollars."""
    time: datetime
    open: float
    high: float
    low: float
    close: float
    spread_points: int
    spread_price: float


def load_chart(paths: Iterable[Path]) -> pd.DataFrame:
    """Load and concatenate one or more MT5 M1 CSV files into a DataFrame
    with columns: time, open, high, low, close, spread, spread_price.
    """
    frames = []
    for p in paths:
        df = pd.read_csv(p, sep="\t")
        df.columns = [c.strip("<>").upper() for c in df.columns]
        required = {"DATE", "TIME", "OPEN", "HIGH", "LOW", "CLOSE", "SPREAD"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"Chart file {p} missing columns: {sorted(missing)}")
        df["time"] = pd.to_datetime(
            df["DATE"].astype(str) + " " + df["TIME"].astype(str),
            format="%Y.%m.%d %H:%M:%S",
            )
        for c in ("OPEN", "HIGH", "LOW", "CLOSE", "SPREAD"):
            df[c.lower()] = pd.to_numeric(df[c], errors="coerce")
        df["spread_price"] = df["spread"] * POINT_VALUE
        frames.append(df[["time", "open", "high", "low", "close", "spread", "spread_price"]])
    if not frames:
        raise ValueError("No chart files provided")
    chart = pd.concat(frames, ignore_index=True).dropna(
        subset=["time", "open", "high", "low", "close", "spread_price"]
    )
    return chart.drop_duplicates(subset=["time"], keep="last").sort_values("time").reset_index(drop=True)


def slice_bars(chart: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    """Bars in [start, end] inclusive."""
    return chart[(chart["time"] >= start) & (chart["time"] <= end)]


def iter_bars(df: pd.DataFrame) -> Iterator[Bar]:
    for r in df.itertuples(index=False):
        t = r.time.to_pydatetime() if hasattr(r.time, "to_pydatetime") else r.time
        yield Bar(
            time=t, open=float(r.open), high=float(r.high), low=float(r.low),
            close=float(r.close), spread_points=int(r.spread),
            spread_price=float(r.spread_price),
        )


def latest_bar(chart: pd.DataFrame, at_or_before: Optional[datetime] = None) -> Optional[Bar]:
    """Most recent bar at or before the given time (defaults to the last bar)."""
    if chart.empty:
        return None
    df = chart if at_or_before is None else chart[chart["time"] <= at_or_before]
    if df.empty:
        return None
    last = df.iloc[-1]
    return Bar(
        time=last["time"].to_pydatetime() if hasattr(last["time"], "to_pydatetime") else last["time"],
        open=float(last["open"]), high=float(last["high"]), low=float(last["low"]),
        close=float(last["close"]), spread_points=int(last["spread"]),
        spread_price=float(last["spread_price"]),
    )