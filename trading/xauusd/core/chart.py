"""Chart loading — MT5-compatible 1-minute bars in tab-separated CSV.

CSV columns: <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>

All prices are Bid. SPREAD is in points; 1 point = $0.01. Ask = Bid + spread.
Times are GMT+3 (chart timezone).

Preferred file naming under data/:

    XAUUSD_M1_YYYYMM_ELEV8.csv     # canonical broker/MT5 source
    XAUUSD_M1_YYYYMM_INTERNET.csv  # shifted/reconciled internet fallback

When both sources contain the same timestamp, ELEV8 wins over INTERNET.
Legacy names like XAUUSD_M1_YYYYMM.csv are still accepted.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

import pandas as pd

from trading.xauusd import POINT_VALUE


SOURCE_PRIORITY = {
    "UNKNOWN": 0,
    "INTERNET": 10,
    "ELEV8": 20,
}


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


def chart_source_from_path(path: Path) -> str:
    """Infer chart source label from filename.

    Expected names:
        XAUUSD_M1_202604_ELEV8.csv
        XAUUSD_M1_202401_INTERNET.csv

    Unknown/legacy files are accepted with lower priority.
    """
    stem = Path(path).stem.upper()
    if stem.endswith("_ELEV8"):
        return "ELEV8"
    if stem.endswith("_INTERNET"):
        return "INTERNET"
    return "UNKNOWN"


def load_chart(paths: Iterable[Path], point_value: float = POINT_VALUE) -> pd.DataFrame:
    """Load and concatenate one or more MT5-compatible M1 CSV files.

    Returns columns: time, open, high, low, close, spread, spread_price,
    source, source_file.

    Duplicate timestamp rule:
        ELEV8 > INTERNET > UNKNOWN; within the same source, later input file
        wins. This lets backtests safely load both:
            data/XAUUSD_M1_*_INTERNET.csv
            data/XAUUSD_M1_*_ELEV8.csv
        and use broker MT5/ELEV8 candles wherever available.
    """
    frames = []
    for input_order, p in enumerate(paths):
        p = Path(p)
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
        df["spread_price"] = df["spread"] * point_value
        df["source"] = chart_source_from_path(p)
        df["source_file"] = p.name
        df["source_priority"] = df["source"].map(SOURCE_PRIORITY).fillna(0).astype(int)
        df["input_order"] = input_order
        frames.append(df[[
            "time", "open", "high", "low", "close", "spread", "spread_price",
            "source", "source_file", "source_priority", "input_order",
        ]])
    if not frames:
        raise ValueError("No chart files provided")
    chart = pd.concat(frames, ignore_index=True).dropna(
        subset=["time", "open", "high", "low", "close", "spread_price"]
    )
    chart = chart.sort_values(["time", "source_priority", "input_order"])
    chart = chart.drop_duplicates(subset=["time"], keep="last")
    return chart.sort_values("time").reset_index(drop=True)


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
