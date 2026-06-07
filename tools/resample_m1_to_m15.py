#!/usr/bin/env python3
"""Resample M1 archive CSVs to M15, written in the same archive format.

M15 OHLC is an exact aggregate of fifteen M1 bars (open=first, high=max, low=min,
close=last), so when a broker M15 archive isn't available -- or doesn't reach as
far back as the M1 archive -- this reconstructs M15 from the M1 you already have,
across the full M1 range (better coverage than a fresh M15 fetch limited to MT5's
window). Bars are timestamped at the bucket OPEN on :00/:15/:30/:45 boundaries
(MT5 convention), which is what the chart loader and the M15 momentum generator
(bar_minutes=15, signal at bar close) expect.

PowerShell (conda env `trading`):
    python tools/resample_m1_to_m15.py --in "data/BTCUSD_M1_*_ELEV8.csv" --out-dir data --symbol BTCUSD
"""
from __future__ import annotations

import argparse
import glob
from pathlib import Path

import pandas as pd

_COLS = ["<DATE>", "<TIME>", "<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>", "<TICKVOL>", "<VOL>", "<SPREAD>"]


def _read_m1(paths: list[str]) -> pd.DataFrame:
    frames = []
    for p in paths:
        df = pd.read_csv(p, sep="\t")
        df.columns = [c.strip("<>").upper() for c in df.columns]
        df["time"] = pd.to_datetime(
            df["DATE"].astype(str) + " " + df["TIME"].astype(str), format="%Y.%m.%d %H:%M:%S"
        )
        frames.append(df)
    return pd.concat(frames, ignore_index=True).set_index("time").sort_index()


def resample_to_m15(m1: pd.DataFrame) -> pd.DataFrame:
    # label/closed='left' => the [t, t+15min) bucket is stamped at its open t.
    agg = m1.resample("15min", label="left", closed="left").agg(
        OPEN=("OPEN", "first"), HIGH=("HIGH", "max"), LOW=("LOW", "min"),
        CLOSE=("CLOSE", "last"), TICKVOL=("TICKVOL", "sum"), VOL=("VOL", "sum"),
        SPREAD=("SPREAD", "last"),
    )
    return agg.dropna(subset=["OPEN"])  # drop empty buckets (weekends / data gaps)


def _write_month(rows: pd.DataFrame, out_dir: Path, symbol: str, ym: str) -> Path:
    path = out_dir / f"{symbol}_M15_{ym}_ELEV8.csv"
    lines = ["\t".join(_COLS)]
    for ts, r in rows.iterrows():
        lines.append("\t".join([
            ts.strftime("%Y.%m.%d"), ts.strftime("%H:%M:%S"),
            f"{r.OPEN:.2f}", f"{r.HIGH:.2f}", f"{r.LOW:.2f}", f"{r.CLOSE:.2f}",
            f"{r.TICKVOL:.1f}", f"{r.VOL:.1f}", str(int(round(r.SPREAD))),
        ]))
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Resample M1 archive CSVs to M15 (same format).")
    ap.add_argument("--in", dest="inp", nargs="+", required=True, help="M1 CSV path(s)/glob(s).")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--symbol", default="BTCUSD")
    args = ap.parse_args(argv)

    files: list[str] = []
    for pat in args.inp:
        files.extend(sorted(glob.glob(pat)) if any(c in pat for c in "*?[") else [pat])
    if not files:
        raise SystemExit("No M1 files matched --in")

    m15 = resample_to_m15(_read_m1(files))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    for ym, grp in m15.groupby(m15.index.strftime("%Y%m")):
        path = _write_month(grp, out_dir, args.symbol, ym)
        total += len(grp)
        print(f"[m15] {path.name}: {len(grp):,} bars")
    print(f"[done] {total:,} M15 bars from {len(files)} M1 file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())