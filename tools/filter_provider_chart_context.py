#!/usr/bin/env python3
"""Filter provider signals using chart context: daily S/R and RSI.

This is a research filter for ideas like:

- BUY near daily support: keep the position longer / test runner behavior.
- SELL near daily resistance: keep the position longer / test runner behavior.
- Include RSI constraints so support/resistance entries are not blindly accepted.

The output is a normal GMT+3 signal file, so it can be used by the same
backtest/live engine.
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path
from typing import Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
for p in (ROOT, TOOLS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from xauusd_trading import CsvChartSource  # noqa: E402
from filter_provider_signals import parse_provider_signals, keep_signal, write_signals  # noqa: E402


def _expand_chart_paths(patterns: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            matches = sorted(glob.glob(pat))
            if not matches:
                raise SystemExit(f"No files match pattern: {pat}")
            out.extend(Path(m) for m in matches)
        else:
            path = Path(pat)
            if not path.exists():
                raise SystemExit(f"Chart file not found: {pat}")
            out.append(path)
    if not out:
        raise SystemExit("No chart files provided")
    return out


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def prepare_context(df: pd.DataFrame, rsi_period: int) -> pd.DataFrame:
    out = df.copy().sort_values("time").reset_index(drop=True)
    out["date"] = out["time"].dt.strftime("%Y-%m-%d")
    out["rsi"] = _rsi(out["close"], rsi_period)

    daily = out.groupby("date", sort=True).agg(
        day_high=("high", "max"),
        day_low=("low", "min"),
    )
    daily["prev_day_high"] = daily["day_high"].shift(1)
    daily["prev_day_low"] = daily["day_low"].shift(1)
    out = out.merge(daily[["prev_day_high", "prev_day_low"]], left_on="date", right_index=True, how="left")
    return out.set_index("time")


def _nearest_context_row(ctx: pd.DataFrame, t) -> pd.Series | None:
    # Signals are minute timestamps, so exact match should usually work.
    if t in ctx.index:
        row = ctx.loc[t]
        if isinstance(row, pd.DataFrame):
            return row.iloc[-1]
        return row
    prior = ctx.loc[:t]
    if prior.empty:
        return None
    return prior.iloc[-1]


def _entry_reference(row) -> float:
    # For BUY range_uniform / signal_range_3, higher entry is normally first.
    # For SELL, lower entry is normally first. This is enough for proximity checks.
    r1 = float(row.r1)
    r2 = float(row.r2)
    return max(r1, r2) if row.side == "BUY" else min(r1, r2)


def keep_by_context(row, ctx_row, args: argparse.Namespace) -> tuple[bool, str]:
    if ctx_row is None:
        return False, "no chart context"
    rsi = ctx_row.get("rsi")
    prev_low = ctx_row.get("prev_day_low")
    prev_high = ctx_row.get("prev_day_high")
    if pd.isna(rsi) or pd.isna(prev_low) or pd.isna(prev_high):
        return False, "missing RSI/daily S/R"

    entry = _entry_reference(row)
    rsi = float(rsi)
    prev_low = float(prev_low)
    prev_high = float(prev_high)
    support_dist = abs(entry - prev_low)
    resistance_dist = abs(entry - prev_high)

    if row.side == "BUY":
        near = support_dist <= args.daily_sr_distance
        rsi_ok = args.buy_rsi_min <= rsi <= args.buy_rsi_max
        return near and rsi_ok, f"BUY support_dist={support_dist:.2f} rsi={rsi:.2f}"

    near = resistance_dist <= args.daily_sr_distance
    rsi_ok = args.sell_rsi_min <= rsi <= args.sell_rsi_max
    return near and rsi_ok, f"SELL resistance_dist={resistance_dist:.2f} rsi={rsi:.2f}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Filter provider signals using daily support/resistance and RSI.")
    p.add_argument("--signals", required=True)
    p.add_argument("--charts", required=True, nargs="+")
    p.add_argument("--output", required=True)
    p.add_argument("--base-preset", default="high_growth_hour_side", choices=["all", "no_bad_hours", "best_hours", "high_growth_hour_side", "research_month_hour_side"])
    p.add_argument("--mode", default="base_and_context", choices=["context_only", "base_and_context", "base_or_context"])
    p.add_argument("--daily-sr-distance", type=float, default=5.0, help="Max $ distance from prev-day low/high.")
    p.add_argument("--rsi-period", type=int, default=14)
    p.add_argument("--buy-rsi-min", type=float, default=25.0)
    p.add_argument("--buy-rsi-max", type=float, default=55.0)
    p.add_argument("--sell-rsi-min", type=float, default=45.0)
    p.add_argument("--sell-rsi-max", type=float, default=75.0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rows = parse_provider_signals(Path(args.signals))
    chart = CsvChartSource(_expand_chart_paths(args.charts))
    ctx = prepare_context(chart.dataframe, args.rsi_period)

    kept = []
    context_hits = 0
    base_hits = 0
    for row in rows:
        base_ok = keep_signal(row, args.base_preset)
        if base_ok:
            base_hits += 1
        ctx_row = _nearest_context_row(ctx, row.chart_time)
        context_ok, _reason = keep_by_context(row, ctx_row, args)
        if context_ok:
            context_hits += 1

        if args.mode == "context_only":
            keep = context_ok
        elif args.mode == "base_and_context":
            keep = base_ok and context_ok
        else:
            keep = base_ok or context_ok
        if keep:
            kept.append(row)

    write_signals(kept, Path(args.output))
    print(f"Input signals:       {len(rows):,}")
    print(f"Base preset hits:    {base_hits:,}")
    print(f"Context hits:        {context_hits:,}")
    print(f"Kept signals:        {len(kept):,}")
    print(f"Mode:                {args.mode}")
    print(f"Output:              {Path(args.output).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
