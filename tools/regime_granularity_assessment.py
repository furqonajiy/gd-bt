#!/usr/bin/env python3
"""Regime-granularity assessment for XAUUSD (research).

Question: is the live 4-regime scheme (R1/R2/R3/R4, ``strategy/regime.py``) the
right determination, or should it be finer / defined differently?

The live scheme classifies on **absolute** smoothed M15 ATR (USD): >=$9.5 R4,
[$4.0,$9.5) R3, <$4.0 split by trend into R2(bull)/R1(quiet). This script tests
that against the full ELEV8 M1 archive (2021-11 .. 2026-06) and shows:

  1. The absolute threshold is PRICE-BIASED. Gold ran $1,800 -> $5,000 over the
     archive, so a fixed $9.5 cutoff is 0.53% of price in 2021 but 0.20% in 2026
     -- the regime label drifts with price, not just volatility.
  2. On price-normalized volatility (M15 ATR / price), 1-D k-means shows the data
     supports ~4 tiers (elbow at k=3-4), and the membership differs from the
     absolute scheme (months get re-ranked across price eras).
  3. Today's single R4 band is internally heterogeneous (~1.8x abs-ATR spread);
     splitting it (high vs extreme) tightens it to ~1.3-1.4x.
  4. Locked-exit SLIPPAGE scales with ABSOLUTE ATR (dollars through the fill
     window), so a per-tier abs-ATR scaler spans ~0.15x..1.56x of the R4 anchor
     -- the flat 2.0/1.0 is only right near mid-R4.

Read-only; prints tables. Run: ``python tools/regime_granularity_assessment.py``.
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading.engine.strategy.regime import detect_regime, m15_atr, trend_score  # noqa: E402

# Price-normalized volatility tiers (M15 ATR as % of price). Bands chosen from the
# k-means breaks below; the two calmest are direction-split (quiet vs bull).
PCT_BANDS = [(0.10, "V0 dead"), (0.15, "V1 normal"), (0.20, "V2 elevated"),
             (0.26, "V3 high"), (float("inf"), "V4 extreme")]
BULL_TREND_MIN = 1.5  # % monthly move above which a calm tier is a trending bull


def _load_month(path: str) -> pd.DataFrame:
    rows = []
    for line in open(path):
        p = line.rstrip("\n").split("\t")
        if len(p) < 6:
            continue
        try:
            rows.append((pd.Timestamp(f"{p[0]} {p[1]}"),
                         float(p[3]), float(p[4]), float(p[5])))
        except ValueError:
            continue
    return pd.DataFrame(rows, columns=["time", "high", "low", "close"]).set_index("time")


def _vol_tier(pct: float) -> str:
    for hi, name in PCT_BANDS:
        if pct < hi:
            return name
    return PCT_BANDS[-1][1]


def _kmeans1d(x: np.ndarray, k: int, iters: int = 300) -> tuple[np.ndarray, float]:
    x = np.sort(x)
    c = np.quantile(x, np.linspace(0.1, 0.9, k))
    for _ in range(iters):
        lab = np.argmin(np.abs(x[:, None] - c[None, :]), axis=1)
        newc = np.array([x[lab == j].mean() if (lab == j).any() else c[j] for j in range(k)])
        if np.allclose(newc, c):
            break
        c = newc
    lab = np.argmin(np.abs(x[:, None] - c[None, :]), axis=1)
    wss = sum(((x[lab == j] - c[j]) ** 2).sum() for j in range(k))
    return np.sort(c), float(wss)


def build_table(charts_glob: str = "data/XAUUSD_M1_*_ELEV8.csv") -> pd.DataFrame:
    recs = []
    for f in sorted(glob.glob(charts_glob)):
        ym = Path(f).stem.split("_")[2]
        df = _load_month(f)
        if len(df) < 500:
            continue
        atr = m15_atr(df, period=14)
        tr = trend_score(df) * 100.0
        px = float(df["close"].median())
        recs.append(dict(month=f"{ym[:4]}-{ym[4:]}", price=px, atr=atr,
                         atr_pct=100.0 * atr / px, trend=tr,
                         current=detect_regime(atr, tr / 100.0)))
    R = pd.DataFrame(recs)
    R["vol_tier"] = R["atr_pct"].apply(_vol_tier)

    def _refined(row):
        t = row["vol_tier"].split()[0]
        if row["vol_tier"] in ("V0 dead", "V1 normal"):
            return t + ("-bull" if row["trend"] >= BULL_TREND_MIN else "-quiet")
        return t
    R["proposed"] = R.apply(_refined, axis=1)
    return R


def main() -> int:
    R = build_table()
    pd.set_option("display.width", 200)

    print("=== ARCHIVE per-month regime metric (smoothed M15 ATR) ===")
    print(R.to_string(index=False, formatters={
        "price": "${:,.0f}".format, "atr": "${:.2f}".format,
        "atr_pct": "{:.3f}%".format, "trend": "{:+.1f}%".format}))

    print("\n=== PRICE BIAS of the absolute thresholds ===")
    for thr in (4.0, 9.5):
        print(f"  abs ${thr:.1f} = {100*thr/1800:.2f}% of price @ $1,800 (2021)  "
              f"vs {100*thr/4700:.2f}% @ $4,700 (2026)")

    print("\n=== how many tiers does price-normalized %-ATR support? (1-D k-means) ===")
    x = R["atr_pct"].values
    tss = ((x - x.mean()) ** 2).sum()
    for k in range(2, 7):
        c, wss = _kmeans1d(x, k)
        print(f"  k={k}  variance explained {1-wss/tss:6.1%}  centers "
              + "  ".join(f"{v:.3f}%" for v in c))

    print("\n=== proposed %-normalized tiers: n / median %-ATR / median ABS ATR / price span ===")
    g = R.groupby("vol_tier").agg(n=("month", "size"), med_pct=("atr_pct", "median"),
                                  med_abs=("atr", "median"),
                                  pmin=("price", "min"), pmax=("price", "max"))
    print(g.to_string(formatters={"med_pct": "{:.3f}%".format, "med_abs": "${:.2f}".format,
                                  "pmin": "${:,.0f}".format, "pmax": "${:,.0f}".format}))

    anchor = R[R["current"] == "R4parab"]["atr"].median()
    print(f"\n=== SLIPPAGE scaler per tier (abs ATR / R4 anchor ${anchor:.2f}; R4 lock slip 2.0/1.0) ===")
    for t, row in g.iterrows():
        sc = row["med_abs"] / anchor
        print(f"  {t:12} abs ${row['med_abs']:5.2f}  {sc:.2f}x  -> TP1 {2.0*sc:.2f} / TP2 {1.0*sc:.2f} pt")

    print("\n=== current absolute regime  x  proposed %-tier ===")
    print(pd.crosstab(R["current"], R["vol_tier"]).to_string())

    print("\n=== within-regime ABS-ATR spread (max/min; lower = more homogeneous) ===")
    for col, lab in [("current", "CURRENT"), ("vol_tier", "PROPOSED")]:
        print(f"  {lab}")
        for grp, sub in R.groupby(col):
            a = sub["atr"]
            if len(a) >= 2:
                print(f"     {grp:11} n={len(a):2}  ${a.min():5.2f}-${a.max():5.2f}  {a.max()/a.min():.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
