#!/usr/bin/env python3
"""Generate supply/demand zone signals for XAUUSD.

Reads the M1 ELEV8 archive, resamples to H1, detects pivot-anchored
demand/supply zones, and writes resting-limit signals in the canonical
signal-file schema. The same output file feeds both the existing backtest
(`backtest --signals ...`) and live auto. Execution stays on the M1 engine.
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path
from typing import Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading import Bar, POINT_VALUE, format_generated_signals  # noqa: E402
from xauusd_trading.strategy.zone_signals import (  # noqa: E402
    ZoneConfig,
    detect_zones,
    generate_zone_signals,
)


def _expand(patterns: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            matches = sorted(glob.glob(pat))
            if not matches:
                raise SystemExit(f"No files match pattern: {pat}")
            out.extend(Path(m) for m in matches)
        else:
            p = Path(pat)
            if not p.exists():
                raise SystemExit(f"Chart file not found: {pat}")
            out.append(p)
    if not out:
        raise SystemExit("No chart files provided")
    return out


def _load_m1(paths: list[Path]) -> pd.DataFrame:
    frames = []
    for path in paths:
        raw = pd.read_csv(path, sep="\t")
        raw.columns = [c.strip("<>").upper() for c in raw.columns]
        missing = {"DATE", "TIME", "OPEN", "HIGH", "LOW", "CLOSE", "SPREAD"} - set(raw.columns)
        if missing:
            raise SystemExit(f"{path} missing columns: {sorted(missing)}")
        df = pd.DataFrame()
        df["time"] = pd.to_datetime(
            raw["DATE"].astype(str) + " " + raw["TIME"].astype(str),
            format="%Y.%m.%d %H:%M:%S",
            )
        for col in ("OPEN", "HIGH", "LOW", "CLOSE", "SPREAD"):
            df[col.lower()] = pd.to_numeric(raw[col], errors="coerce")
        frames.append(df)
    out = pd.concat(frames, ignore_index=True).dropna()
    return out.drop_duplicates("time", keep="last").sort_values("time").reset_index(drop=True)


def _resample_h1(m1: pd.DataFrame) -> list[Bar]:
    idx = m1.set_index("time")
    h1 = idx.resample("1h").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        spread=("spread", "mean"),
    ).dropna()
    # Drop the final bucket: at the dataset boundary it may be a partially
    # formed hour, and a half-bar must never seed a zone.
    if len(h1) > 0:
        h1 = h1.iloc[:-1]
    bars: list[Bar] = []
    for ts, row in h1.iterrows():
        sp = int(round(float(row.spread)))
        bars.append(Bar(
            time=ts.to_pydatetime(),
            open=float(row.open), high=float(row.high),
            low=float(row.low), close=float(row.close),
            spread_points=sp, spread_price=sp * POINT_VALUE,
        ))
    return bars


def build_config(args: argparse.Namespace) -> ZoneConfig:
    return ZoneConfig(
        swing_len=args.swing_len,
        atr_period=args.atr_period,
        max_zone_atr=args.max_zone_atr,
        min_zone_atr=args.min_zone_atr,
        sl_buffer=args.sl_buffer,
        broker_stops_level=args.broker_stops_level,
        min_target_atr=args.min_target_atr,
        max_target_atr=args.max_target_atr,
        min_separation_bars=args.min_separation_bars,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate XAUUSD supply/demand zone signals (H1 zones, M1 execution).")
    p.add_argument("--m1-charts", required=True, nargs="+", help="M1 CSV path(s) or glob(s).")
    p.add_argument("--output", required=True, help="Output signal file (NOT signals.txt).")
    p.add_argument("--swing-len", type=int, default=5)
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--max-zone-atr", type=float, default=2.0)
    p.add_argument("--min-zone-atr", type=float, default=0.0)
    p.add_argument("--sl-buffer", type=float, default=0.40)
    p.add_argument("--broker-stops-level", type=float, default=0.40)
    p.add_argument("--min-target-atr", type=float, default=1.0)
    p.add_argument("--max-target-atr", type=float, default=12.0)
    p.add_argument("--min-separation-bars", type=int, default=6)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = _expand(args.m1_charts)
    m1 = _load_m1(paths)
    bars = _resample_h1(m1)
    config = build_config(args)

    zones = detect_zones(bars, config)
    signals = generate_zone_signals(bars, config)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(format_generated_signals(signals), encoding="utf-8")

    demand = sum(1 for z in zones if z.kind == "demand")
    supply = sum(1 for z in zones if z.kind == "supply")
    buys = sum(1 for s in signals if s.side == "BUY")
    sells = sum(1 for s in signals if s.side == "SELL")
    span = f"{bars[0].time:%Y-%m-%d} .. {bars[-1].time:%Y-%m-%d}" if bars else "(no bars)"
    print(
        f"[gen_zone_signals] H1 bars={len(bars)} ({span}); "
        f"zones: {demand} demand / {supply} supply; "
        f"signals: {len(signals)} ({buys} BUY / {sells} SELL) -> {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())