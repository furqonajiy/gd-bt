#!/usr/bin/env python3
"""Generate high-frequency XAUUSD scalping signals from MT5 M1 candles.

The output is the same human-readable signal format consumed by the existing
backtest/live engine, so the workflow is:

    python tools/generate_scalper_signals.py \
      --charts data/XAUUSD_M1_*.csv \
      --output generated/scalper_pullback_v1.txt \
      --diagnostics generated/scalper_pullback_v1.csv

    python tools/backtest_configurable.py \
      --signals generated/scalper_pullback_v1.txt \
      --charts data/XAUUSD_M1_*.csv \
      --output-dir reports/scalper_pullback_v1 \
      --max-drawdown-limit-pct 40

Design goal: produce many scalping candidates, then let the existing backtest
engine reject weak parameter sets. This first generator is deliberately simple
and parameterized: EMA trend + EMA21 pullback + confirming candle, with ATR /
swing based SL and risk-multiple TP levels.
"""
from __future__ import annotations

import argparse
import csv
import glob
import math
import sys
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

# Allow running as ``python tools/generate_scalper_signals.py`` from repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading import CsvChartSource, parse_signals_file  # noqa: E402


@dataclass(frozen=True)
class GeneratedSignal:
    time: datetime
    side: str
    r1: float
    r2: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    reason: str
    entry_ref: float
    risk: float
    atr: float
    spread_points: int
    ema_fast: float
    ema_mid: float
    ema_slow: float


class _Heartbeat:
    """Periodic stderr progress message while a blocking step is running."""

    def __init__(self, label: str, interval_seconds: float, *, enabled: bool = True):
        self.label = label
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.enabled = enabled
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = 0.0

    def __enter__(self):
        if not self.enabled:
            return self
        self._start = time.time()
        print(f"[{self.label}] started", file=sys.stderr, flush=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.enabled:
            return False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        elapsed = time.time() - self._start
        print(f"[{self.label}] finished after {_fmt_duration(elapsed)}", file=sys.stderr, flush=True)
        return False

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            elapsed = time.time() - self._start
            print(f"[{self.label}] still running... elapsed {_fmt_duration(elapsed)}", file=sys.stderr, flush=True)


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}h {m:02d}m {s:02d}s"
    if m:
        return f"{m:d}m {s:02d}s"
    return f"{s:d}s"


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


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    return round(math.floor(value / step + 1e-9) * step, 2)


def _ceil_to_step(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    return round(math.ceil(value / step - 1e-9) * step, 2)


def _price(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _time_ampm(t: datetime) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def _add_indicators(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]
    high = out["high"]
    low = out["low"]
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    out["atr"] = tr.rolling(args.atr_period, min_periods=args.atr_period).mean()
    out["ema_fast"] = close.ewm(span=args.ema_fast, adjust=False).mean()
    out["ema_mid"] = close.ewm(span=args.ema_mid, adjust=False).mean()
    out["ema_slow"] = close.ewm(span=args.ema_slow, adjust=False).mean()
    out["ema_mid_slope"] = out["ema_mid"] - out["ema_mid"].shift(args.slope_bars)
    out["swing_low"] = low.shift(1).rolling(args.swing_lookback, min_periods=args.swing_lookback).min()
    out["swing_high"] = high.shift(1).rolling(args.swing_lookback, min_periods=args.swing_lookback).max()
    out["body"] = (out["close"] - out["open"]).abs()
    out["range"] = out["high"] - out["low"]
    return out


def _in_session(t: datetime, session_start: int, session_end: int) -> bool:
    """Inclusive start, exclusive end. Supports sessions crossing midnight."""
    h = t.hour
    if session_start == session_end:
        return True
    if session_start < session_end:
        return session_start <= h < session_end
    return h >= session_start or h < session_end


def _build_buy(row, args: argparse.Namespace) -> GeneratedSignal | None:
    atr = float(row.atr)
    close = float(row.close)
    entry_offset = max(args.min_entry_offset, atr * args.entry_offset_atr)
    entry_ref = _floor_to_step(close - entry_offset, args.price_step)
    high_entry = entry_ref
    low_entry = round(high_entry - args.range_width, 2)

    swing_sl = float(row.swing_low) - atr * args.sl_buffer_atr
    raw_risk = high_entry - swing_sl
    if raw_risk <= 0:
        return None
    if raw_risk < args.min_risk:
        risk = args.min_risk
    elif raw_risk > args.max_risk:
        if not args.cap_oversized_risk:
            return None
        risk = args.max_risk
    else:
        risk = raw_risk

    sl = _floor_to_step(high_entry - risk, args.price_step)
    if sl >= low_entry:
        sl = _floor_to_step(low_entry - args.price_step, args.price_step)
        risk = high_entry - sl
    if not (args.min_risk <= risk <= args.max_risk + 1e-9):
        return None

    tp1 = _ceil_to_step(high_entry + risk * args.rr1, args.price_step)
    tp2 = _ceil_to_step(high_entry + risk * args.rr2, args.price_step)
    tp3 = _ceil_to_step(high_entry + risk * args.rr3, args.price_step)
    if not (tp1 > high_entry and tp1 < tp2 < tp3):
        return None

    return GeneratedSignal(
        time=row.time.to_pydatetime() if hasattr(row.time, "to_pydatetime") else row.time,
        side="BUY", r1=high_entry, r2=low_entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
        reason="ema_pullback_buy", entry_ref=entry_ref, risk=risk, atr=atr,
        spread_points=int(row.spread), ema_fast=float(row.ema_fast),
        ema_mid=float(row.ema_mid), ema_slow=float(row.ema_slow),
    )


def _build_sell(row, args: argparse.Namespace) -> GeneratedSignal | None:
    atr = float(row.atr)
    close = float(row.close)
    entry_offset = max(args.min_entry_offset, atr * args.entry_offset_atr)
    entry_ref = _ceil_to_step(close + entry_offset, args.price_step)
    low_entry = entry_ref
    high_entry = round(low_entry + args.range_width, 2)

    swing_sl = float(row.swing_high) + atr * args.sl_buffer_atr
    raw_risk = swing_sl - low_entry
    if raw_risk <= 0:
        return None
    if raw_risk < args.min_risk:
        risk = args.min_risk
    elif raw_risk > args.max_risk:
        if not args.cap_oversized_risk:
            return None
        risk = args.max_risk
    else:
        risk = raw_risk

    sl = _ceil_to_step(low_entry + risk, args.price_step)
    if sl <= high_entry:
        sl = _ceil_to_step(high_entry + args.price_step, args.price_step)
        risk = sl - low_entry
    if not (args.min_risk <= risk <= args.max_risk + 1e-9):
        return None

    tp1 = _floor_to_step(low_entry - risk * args.rr1, args.price_step)
    tp2 = _floor_to_step(low_entry - risk * args.rr2, args.price_step)
    tp3 = _floor_to_step(low_entry - risk * args.rr3, args.price_step)
    if not (tp1 < low_entry and tp1 > tp2 > tp3):
        return None

    return GeneratedSignal(
        time=row.time.to_pydatetime() if hasattr(row.time, "to_pydatetime") else row.time,
        side="SELL", r1=low_entry, r2=high_entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
        reason="ema_pullback_sell", entry_ref=entry_ref, risk=risk, atr=atr,
        spread_points=int(row.spread), ema_fast=float(row.ema_fast),
        ema_mid=float(row.ema_mid), ema_slow=float(row.ema_slow),
    )


def _print_scan_progress(i: int, total: int, start: float, signals: int, row_time: datetime) -> None:
    elapsed = time.time() - start
    pct = i / total * 100.0 if total else 0.0
    rate = i / elapsed if elapsed > 0 else 0.0
    eta = (total - i) / rate if rate > 0 else None
    eta_text = _fmt_duration(eta) if eta is not None else "calculating"
    print(
        f"[generate] {i:,}/{total:,} rows ({pct:5.1f}%) | "
        f"signals={signals:,} | candle={row_time} | elapsed={_fmt_duration(elapsed)} | ETA={eta_text}",
        file=sys.stderr,
        flush=True,
    )


def generate_signals(df: pd.DataFrame, args: argparse.Namespace) -> list[GeneratedSignal]:
    progress_enabled = args.progress_interval_seconds > 0 and args.progress_every_rows > 0
    with _Heartbeat("indicator calculation", args.progress_interval_seconds, enabled=args.progress_interval_seconds > 0):
        df = _add_indicators(df, args)

    signals: list[GeneratedSignal] = []
    last_signal_time: datetime | None = None
    per_day_count: dict[str, int] = {}

    start_time = pd.Timestamp(args.start) if args.start else None
    end_time = pd.Timestamp(args.end) if args.end else None
    total = len(df)
    start_clock = time.time()
    next_time_print = start_clock + max(1.0, args.progress_interval_seconds)

    if progress_enabled:
        print(f"[generate] scanning {total:,} candles...", file=sys.stderr, flush=True)

    for i, row in enumerate(df.itertuples(index=False), start=1):
        t = row.time.to_pydatetime() if hasattr(row.time, "to_pydatetime") else row.time
        now_clock = time.time()
        due_by_rows = i == 1 or i % args.progress_every_rows == 0 or i == total
        due_by_time = args.progress_interval_seconds > 0 and now_clock >= next_time_print
        if progress_enabled and (due_by_rows or due_by_time):
            _print_scan_progress(i, total, start_clock, len(signals), t)
            while next_time_print <= now_clock:
                next_time_print += max(1.0, args.progress_interval_seconds)

        if start_time is not None and pd.Timestamp(t) < start_time:
            continue
        if end_time is not None and pd.Timestamp(t) >= end_time:
            continue
        if args.weekdays_only and t.weekday() >= 5:
            continue
        if not _in_session(t, args.session_start, args.session_end):
            continue
        if int(row.spread) > args.max_spread_points:
            continue
        if any(pd.isna(v) for v in (row.atr, row.ema_fast, row.ema_mid, row.ema_slow, row.swing_low, row.swing_high)):
            continue

        atr = float(row.atr)
        if atr < args.min_atr or atr > args.max_atr:
            continue
        if last_signal_time is not None:
            gap_min = (t - last_signal_time).total_seconds() / 60.0
            if gap_min < args.cooldown_minutes:
                continue
        day_key = t.strftime("%Y-%m-%d")
        if args.max_signals_per_day > 0 and per_day_count.get(day_key, 0) >= args.max_signals_per_day:
            continue

        body = float(row.body)
        if body < atr * args.min_body_atr:
            continue

        close = float(row.close)
        open_ = float(row.open)
        high = float(row.high)
        low = float(row.low)
        ema_fast = float(row.ema_fast)
        ema_mid = float(row.ema_mid)
        ema_slow = float(row.ema_slow)
        slope = float(row.ema_mid_slope)

        buy_trend = ema_fast > ema_mid > ema_slow and slope >= args.min_slope and close >= ema_fast
        buy_pullback = low <= ema_mid + atr * args.pullback_atr
        buy_confirm = close > open_ and close > ema_mid

        sell_trend = ema_fast < ema_mid < ema_slow and slope <= -args.min_slope and close <= ema_fast
        sell_pullback = high >= ema_mid - atr * args.pullback_atr
        sell_confirm = close < open_ and close < ema_mid

        sig: GeneratedSignal | None = None
        if buy_trend and buy_pullback and buy_confirm:
            sig = _build_buy(row, args)
        elif sell_trend and sell_pullback and sell_confirm:
            sig = _build_sell(row, args)

        if sig is None:
            continue

        signals.append(sig)
        last_signal_time = t
        per_day_count[day_key] = per_day_count.get(day_key, 0) + 1

    if progress_enabled:
        elapsed = time.time() - start_clock
        print(
            f"[generate] completed scan: {total:,} rows, {len(signals):,} signals, elapsed {_fmt_duration(elapsed)}",
            file=sys.stderr,
            flush=True,
        )
    return signals


def _write_signal_file(signals: list[GeneratedSignal], output: Path,
                       signal_tz: int = 3) -> None:
    """Write the feed in `signal_tz` display time (header ``GMT+{signal_tz}``).

    Bars/times are generated in chart time (GMT+3); shifting by (signal_tz-3)
    before grouping moves near-midnight signals into the correct shifted day
    section. The engine parses the GMT+N header and converts back, so the
    chart-time semantics are identical regardless of display tz -- this is
    presentation only (e.g. GMT+7 to match Victor's feed).
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    shift = timedelta(hours=signal_tz - 3)
    grouped: dict[str, list[tuple[datetime, GeneratedSignal]]] = {}
    for sig in signals:
        disp = sig.time + shift
        grouped.setdefault(disp.strftime("%Y-%m-%d"), []).append((disp, sig))

    lines: list[str] = []
    for day in sorted(grouped):
        lines.append(f"{day} GMT+{signal_tz}")
        for idx, (disp, sig) in enumerate(sorted(grouped[day], key=lambda x: x[0]), start=1):
            lines.append(
                f"{idx}. {sig.side} XAUUSD "
                f"{_price(sig.r1)} - {_price(sig.r2)} "
                f"SL {_price(sig.sl)} "
                f"TP1 {_price(sig.tp1)} "
                f"TP2 {_price(sig.tp2)} "
                f"TP3 {_price(sig.tp3)} "
                f"{_time_ampm(disp)}"
            )
        lines.append("")
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_diagnostics(signals: list[GeneratedSignal], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = list(asdict(signals[0]).keys()) if signals else [
            "time", "side", "r1", "r2", "sl", "tp1", "tp2", "tp3", "reason",
            "entry_ref", "risk", "atr", "spread_points", "ema_fast", "ema_mid", "ema_slow",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for sig in signals:
            row = asdict(sig)
            row["time"] = sig.time.isoformat(sep=" ")
            writer.writerow(row)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="generate_scalper_signals",
        description="Generate XAUUSD EMA-pullback scalping signals from M1 chart data.",
    )
    p.add_argument("--charts", required=True, nargs="+", help="MT5 M1 chart CSV files or globs.")
    p.add_argument("--output", required=True, help="Output signal text file.")
    p.add_argument("--diagnostics", default=None, help="Optional CSV with generated signal features.")
    p.add_argument("--start", default=None, help="Optional inclusive chart-time start, e.g. 2024-01-01.")
    p.add_argument("--end", default=None, help="Optional exclusive chart-time end, e.g. 2026-01-01.")
    p.add_argument("--progress-every-rows", type=int, default=100_000, help="Print scan progress every N candles. Use 0 to disable.")
    p.add_argument("--progress-interval-seconds", type=float, default=15.0, help="Print progress heartbeat every N seconds. Use 0 to disable.")

    p.add_argument("--cooldown-minutes", type=float, default=3.0)
    p.add_argument("--max-signals-per-day", type=int, default=0, help="0 = unlimited.")
    p.add_argument("--signal-tz", type=int, default=3,
                   help="Display timezone for the feed (header GMT+N and times). Default 3 = chart time; 7 matches the Victor feed. Presentation only -- the engine converts back via the header.")
    p.add_argument("--session-start", type=int, default=7, help="Chart-time hour, GMT+3. Default 07:00.")
    p.add_argument("--session-end", type=int, default=23, help="Chart-time hour, GMT+3. Default 23:00.")
    p.add_argument("--weekdays-only", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--max-spread-points", type=int, default=60)

    p.add_argument("--ema-fast", type=int, default=9)
    p.add_argument("--ema-mid", type=int, default=21)
    p.add_argument("--ema-slow", type=int, default=50)
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--swing-lookback", type=int, default=12)
    p.add_argument("--slope-bars", type=int, default=5)
    p.add_argument("--min-slope", type=float, default=0.03)
    p.add_argument("--pullback-atr", type=float, default=0.25)
    p.add_argument("--min-body-atr", type=float, default=0.08)
    p.add_argument("--min-atr", type=float, default=0.25)
    p.add_argument("--max-atr", type=float, default=8.0)

    p.add_argument("--price-step", type=float, default=0.5)
    p.add_argument("--range-width", type=float, default=2.0)
    p.add_argument("--min-entry-offset", type=float, default=0.5)
    p.add_argument("--entry-offset-atr", type=float, default=0.10)
    p.add_argument("--sl-buffer-atr", type=float, default=0.20)
    p.add_argument("--min-risk", type=float, default=4.0)
    p.add_argument("--max-risk", type=float, default=12.0)
    p.add_argument("--cap-oversized-risk", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--rr1", type=float, default=1.0)
    p.add_argument("--rr2", type=float, default=1.5)
    p.add_argument("--rr3", type=float, default=2.0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.range_width != 2.0:
        raise SystemExit("range-width must remain 2.0 for the current signal parser validation rules.")
    if not (0 <= args.session_start <= 23 and 0 <= args.session_end <= 23):
        raise SystemExit("session-start and session-end must be hours in 0..23")
    if not (args.rr1 > 0 and args.rr1 < args.rr2 < args.rr3):
        raise SystemExit("Require 0 < rr1 < rr2 < rr3")

    progress_enabled = args.progress_interval_seconds > 0
    chart_paths = _expand_chart_paths(args.charts)
    print(f"Loading chart files: {len(chart_paths):,}", file=sys.stderr, flush=True)
    with _Heartbeat("chart load", args.progress_interval_seconds, enabled=progress_enabled):
        chart = CsvChartSource(chart_paths)
    print(
        f"Loaded chart rows: {len(chart.dataframe):,} | range: {chart.first_time()} -> {chart.last_time()}",
        file=sys.stderr,
        flush=True,
    )

    signals = generate_signals(chart.dataframe, args)

    output = Path(args.output)
    print(f"Writing signals to {output}", file=sys.stderr, flush=True)
    _write_signal_file(signals, output, signal_tz=args.signal_tz)
    if args.diagnostics:
        print(f"Writing diagnostics to {args.diagnostics}", file=sys.stderr, flush=True)
        _write_diagnostics(signals, Path(args.diagnostics))

    parsed = parse_signals_file(output)
    if len(parsed) != len(signals):
        raise SystemExit(
            f"Generated {len(signals)} signals but parser read {len(parsed)}. "
            f"Check signal formatting in {output}."
        )

    if signals:
        first = min(s.time for s in signals)
        last = max(s.time for s in signals)
        days = len({s.time.date() for s in signals})
    else:
        first = last = None
        days = 0

    print(f"Generated signals: {len(signals)}")
    print(f"Active days:        {days}")
    print(f"First signal:       {first}")
    print(f"Last signal:        {last}")
    print(f"Output:             {output.resolve()}")
    if args.diagnostics:
        print(f"Diagnostics:        {Path(args.diagnostics).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
