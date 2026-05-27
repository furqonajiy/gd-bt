#!/usr/bin/env python3
"""Generate breakout-retest signals filtered by EMA, MACD, and RSI.

This is the first regime-aware improvement step:

- Keep the proven breakout-retest signal logic.
- Keep BUY signals only when trend indicators are bullish.
- Keep SELL signals only when trend indicators are bearish.

It intentionally does not add sideways S/R yet. That comes after we compare this
clean trend-filtered version against the balanced breakout-retest baseline.
"""
from __future__ import annotations

import argparse
import csv
import glob
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
for p in (ROOT, TOOLS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from xauusd_trading import CsvChartSource, parse_signals_file  # noqa: E402
from generate_breakout_retest_signals import generate_signals, _write_signal_file  # noqa: E402


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


def add_indicators(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    out = df.copy().sort_values("time").reset_index(drop=True)
    close = out["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - prev_close).abs(),
        (out["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    out["atr"] = tr.rolling(args.atr_period, min_periods=args.atr_period).mean()
    out["ema_fast"] = close.ewm(span=args.ema_fast, adjust=False).mean()
    out["ema_mid"] = close.ewm(span=args.ema_mid, adjust=False).mean()
    out["ema_slow"] = close.ewm(span=args.ema_slow, adjust=False).mean()
    out["ema_mid_slope"] = out["ema_mid"] - out["ema_mid"].shift(args.slope_bars)
    macd = close.ewm(span=args.macd_fast, adjust=False).mean() - close.ewm(span=args.macd_slow, adjust=False).mean()
    out["macd_hist"] = macd - macd.ewm(span=args.macd_signal, adjust=False).mean()
    out["rsi"] = _rsi(close, args.rsi_period)
    return out


def breakout_args(args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        charts=args.charts,
        output="",
        diagnostics=None,
        start=args.start,
        end=args.end,
        progress_every_rows=args.progress_every_rows,
        progress_interval_seconds=args.progress_interval_seconds,
        direction="both",
        cooldown_minutes=args.cooldown_minutes,
        level_cooldown_minutes=args.level_cooldown_minutes,
        session_start=args.session_start,
        session_end=args.session_end,
        weekdays_only=True,
        max_spread_points=args.max_spread_points,
        asian_start=0,
        asian_end=7,
        atr_period=14,
        min_atr=0.25,
        max_atr=8.0,
        require_body=True,
        min_body_atr=args.min_body_atr,
        max_body_atr=args.max_body_atr,
        price_step=0.5,
        range_width=2.0,
        breakout_buffer=args.breakout_buffer,
        entry_buffer=args.entry_buffer,
        stop_distance=args.stop_distance,
        min_risk=4.0,
        max_risk=12.0,
        rr1=1.0,
        rr2=1.5,
        rr3=args.rr3,
    )


def keep_signal(sig, row, args: argparse.Namespace) -> bool:
    if any(pd.isna(row[x]) for x in ["atr", "ema_fast", "ema_mid", "ema_slow", "ema_mid_slope", "macd_hist", "rsi"]):
        return False
    atr = float(row["atr"])
    if atr <= 0:
        return False
    ema_fast = float(row["ema_fast"])
    ema_mid = float(row["ema_mid"])
    ema_slow = float(row["ema_slow"])
    slope = float(row["ema_mid_slope"])
    macd_hist = float(row["macd_hist"])
    rsi = float(row["rsi"])

    if sig.side == "BUY":
        return (
            ema_fast > ema_mid > ema_slow
            and slope >= atr * args.min_slope_atr
            and macd_hist >= args.min_macd_hist
            and args.buy_rsi_min <= rsi <= args.buy_rsi_max
        )
    return (
        ema_fast < ema_mid < ema_slow
        and slope <= -atr * args.min_slope_atr
        and macd_hist <= -args.min_macd_hist
        and args.sell_rsi_min <= rsi <= args.sell_rsi_max
    )


def write_diagnostics(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate indicator-filtered breakout-retest XAUUSD signals.")
    p.add_argument("--charts", required=True, nargs="+")
    p.add_argument("--output", default="generated/breakout_retest_indicator_filtered.txt")
    p.add_argument("--diagnostics", default="generated/breakout_retest_indicator_filtered.csv")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--progress-every-rows", type=int, default=100_000)
    p.add_argument("--progress-interval-seconds", type=float, default=15.0)

    p.add_argument("--cooldown-minutes", type=float, default=3.0)
    p.add_argument("--level-cooldown-minutes", type=float, default=45.0)
    p.add_argument("--max-spread-points", type=int, default=40)
    p.add_argument("--session-start", type=int, default=7)
    p.add_argument("--session-end", type=int, default=23)
    p.add_argument("--breakout-buffer", type=float, default=1.0)
    p.add_argument("--entry-buffer", type=float, default=0.0)
    p.add_argument("--stop-distance", type=float, default=3.0)
    p.add_argument("--rr3", type=float, default=2.0)
    p.add_argument("--min-body-atr", type=float, default=0.1)
    p.add_argument("--max-body-atr", type=float, default=2.0)

    p.add_argument("--ema-fast", type=int, default=20)
    p.add_argument("--ema-mid", type=int, default=50)
    p.add_argument("--ema-slow", type=int, default=100)
    p.add_argument("--slope-bars", type=int, default=10)
    p.add_argument("--min-slope-atr", type=float, default=0.02)
    p.add_argument("--macd-fast", type=int, default=12)
    p.add_argument("--macd-slow", type=int, default=26)
    p.add_argument("--macd-signal", type=int, default=9)
    p.add_argument("--min-macd-hist", type=float, default=0.02)
    p.add_argument("--rsi-period", type=int, default=14)
    p.add_argument("--buy-rsi-min", type=float, default=52)
    p.add_argument("--buy-rsi-max", type=float, default=75)
    p.add_argument("--sell-rsi-min", type=float, default=25)
    p.add_argument("--sell-rsi-max", type=float, default=48)
    p.add_argument("--atr-period", type=int, default=14)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    chart = CsvChartSource(_expand_chart_paths(args.charts))
    indicators = add_indicators(chart.dataframe, args).set_index("time")
    raw = generate_signals(chart.dataframe, breakout_args(args))

    kept = []
    diag = []
    for sig in raw:
        if sig.time not in indicators.index:
            continue
        row = indicators.loc[sig.time]
        ok = keep_signal(sig, row, args)
        if ok:
            kept.append(sig)
        d = asdict(sig)
        d["time"] = sig.time.isoformat(sep=" ")
        d["kept"] = ok
        for col in ["ema_fast", "ema_mid", "ema_slow", "ema_mid_slope", "macd_hist", "rsi", "atr"]:
            d[col] = float(row[col]) if not pd.isna(row[col]) else None
        diag.append(d)

    _write_signal_file(kept, Path(args.output))
    if args.diagnostics:
        write_diagnostics(Path(args.diagnostics), diag)
    parsed = parse_signals_file(Path(args.output))
    if len(parsed) != len(kept):
        raise SystemExit(f"Generated {len(kept)} signals but parser read {len(parsed)}. Check {args.output}.")

    print(f"Raw breakout signals: {len(raw)}")
    print(f"Kept after indicators: {len(kept)}")
    print(f"Output: {Path(args.output).resolve()}")
    if args.diagnostics:
        print(f"Diagnostics: {Path(args.diagnostics).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
