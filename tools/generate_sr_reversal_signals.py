#!/usr/bin/env python3
"""Generate XAUUSD scalping signals from support/resistance reactions.

This generator is intentionally price-action based instead of indicator-led.
It looks for liquidity sweeps or rejections around prior rolling support and
resistance, then places a limit-entry range around that level for a retest.

Workflow:

    python tools/generate_sr_reversal_signals.py \
      --charts data/XAUUSD_M1_*.csv \
      --output generated/sr_reversal_v1.txt \
      --diagnostics generated/sr_reversal_v1.csv

    python tools/backtest_configurable.py \
      --signals generated/sr_reversal_v1.txt \
      --charts data/XAUUSD_M1_*.csv \
      --output-dir reports/sr_reversal_v1 \
      --max-drawdown-limit-pct 40
"""
from __future__ import annotations

import argparse
import csv
import glob
import math
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

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
    level: float
    opposite_level: float
    entry_ref: float
    risk: float
    room_rr: float
    atr: float
    spread_points: int


class _Heartbeat:
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
        print(f"[{self.label}] finished after {_fmt_duration(time.time() - self._start)}", file=sys.stderr, flush=True)
        return False

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            print(f"[{self.label}] still running... elapsed {_fmt_duration(time.time() - self._start)}", file=sys.stderr, flush=True)


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


def _in_session(t: datetime, session_start: int, session_end: int) -> bool:
    h = t.hour
    if session_start == session_end:
        return True
    if session_start < session_end:
        return session_start <= h < session_end
    return h >= session_start or h < session_end


def _add_levels(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    out = df.copy()
    high = out["high"]
    low = out["low"]
    close = out["close"]
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    out["atr"] = tr.rolling(args.atr_period, min_periods=args.atr_period).mean()

    # Use only prior candles for levels to prevent look-ahead bias.
    out["support"] = low.shift(1).rolling(args.level_lookback, min_periods=args.level_lookback).min()
    out["resistance"] = high.shift(1).rolling(args.level_lookback, min_periods=args.level_lookback).max()
    out["recent_swing_low"] = low.shift(1).rolling(args.swing_lookback, min_periods=args.swing_lookback).min()
    out["recent_swing_high"] = high.shift(1).rolling(args.swing_lookback, min_periods=args.swing_lookback).max()
    out["bar_range"] = high - low
    out["close_pos"] = (close - low) / out["bar_range"].replace(0, pd.NA)
    out["body"] = (close - out["open"]).abs()
    out["box_width"] = out["resistance"] - out["support"]
    return out


def _mode_allows(mode: str, sweep: bool, hold: bool) -> bool:
    if mode == "sweep":
        return sweep
    if mode == "hold":
        return hold
    return sweep or hold


def _buy_signal(row, args: argparse.Namespace, reason: str) -> GeneratedSignal | None:
    atr = float(row.atr)
    support = float(row.support)
    resistance = float(row.resistance)
    entry_ref = _ceil_to_step(support + args.entry_buffer, args.price_step)
    high_entry = entry_ref
    low_entry = round(high_entry - args.range_width, 2)

    raw_sl = min(float(row.recent_swing_low), support) - atr * args.sl_buffer_atr
    raw_risk = high_entry - raw_sl
    if raw_risk <= 0:
        return None
    risk = min(max(raw_risk, args.min_risk), args.max_risk) if args.cap_oversized_risk else raw_risk
    if not args.cap_oversized_risk and raw_risk > args.max_risk:
        return None

    sl = _floor_to_step(high_entry - risk, args.price_step)
    if sl >= low_entry:
        sl = _floor_to_step(low_entry - args.price_step, args.price_step)
        risk = high_entry - sl
    if not (args.min_risk <= risk <= args.max_risk + 1e-9):
        return None

    room = resistance - high_entry
    room_rr = room / risk if risk > 0 else 0.0
    if args.min_room_rr > 0 and room_rr < args.min_room_rr:
        return None

    tp1 = _ceil_to_step(high_entry + risk * args.rr1, args.price_step)
    tp2 = _ceil_to_step(high_entry + risk * args.rr2, args.price_step)
    tp3 = _ceil_to_step(high_entry + risk * args.rr3, args.price_step)
    if not (tp1 > high_entry and tp1 < tp2 < tp3):
        return None

    return GeneratedSignal(
        time=row.time.to_pydatetime() if hasattr(row.time, "to_pydatetime") else row.time,
        side="BUY", r1=high_entry, r2=low_entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
        reason=reason, level=support, opposite_level=resistance, entry_ref=entry_ref,
        risk=risk, room_rr=room_rr, atr=atr, spread_points=int(row.spread),
    )


def _sell_signal(row, args: argparse.Namespace, reason: str) -> GeneratedSignal | None:
    atr = float(row.atr)
    support = float(row.support)
    resistance = float(row.resistance)
    entry_ref = _floor_to_step(resistance - args.entry_buffer, args.price_step)
    low_entry = entry_ref
    high_entry = round(low_entry + args.range_width, 2)

    raw_sl = max(float(row.recent_swing_high), resistance) + atr * args.sl_buffer_atr
    raw_risk = raw_sl - low_entry
    if raw_risk <= 0:
        return None
    risk = min(max(raw_risk, args.min_risk), args.max_risk) if args.cap_oversized_risk else raw_risk
    if not args.cap_oversized_risk and raw_risk > args.max_risk:
        return None

    sl = _ceil_to_step(low_entry + risk, args.price_step)
    if sl <= high_entry:
        sl = _ceil_to_step(high_entry + args.price_step, args.price_step)
        risk = sl - low_entry
    if not (args.min_risk <= risk <= args.max_risk + 1e-9):
        return None

    room = low_entry - support
    room_rr = room / risk if risk > 0 else 0.0
    if args.min_room_rr > 0 and room_rr < args.min_room_rr:
        return None

    tp1 = _floor_to_step(low_entry - risk * args.rr1, args.price_step)
    tp2 = _floor_to_step(low_entry - risk * args.rr2, args.price_step)
    tp3 = _floor_to_step(low_entry - risk * args.rr3, args.price_step)
    if not (tp1 < low_entry and tp1 > tp2 > tp3):
        return None

    return GeneratedSignal(
        time=row.time.to_pydatetime() if hasattr(row.time, "to_pydatetime") else row.time,
        side="SELL", r1=low_entry, r2=high_entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
        reason=reason, level=resistance, opposite_level=support, entry_ref=entry_ref,
        risk=risk, room_rr=room_rr, atr=atr, spread_points=int(row.spread),
    )


def _print_scan_progress(i: int, total: int, start: float, signals: int, row_time: datetime) -> None:
    elapsed = time.time() - start
    pct = i / total * 100.0 if total else 0.0
    rate = i / elapsed if elapsed > 0 else 0.0
    eta = (total - i) / rate if rate > 0 else None
    print(
        f"[sr-generate] {i:,}/{total:,} rows ({pct:5.1f}%) | signals={signals:,} | "
        f"candle={row_time} | elapsed={_fmt_duration(elapsed)} | ETA={_fmt_duration(eta) if eta else 'calculating'}",
        file=sys.stderr,
        flush=True,
    )


def generate_signals(df: pd.DataFrame, args: argparse.Namespace) -> list[GeneratedSignal]:
    progress_enabled = args.progress_interval_seconds > 0 and args.progress_every_rows > 0
    with _Heartbeat("level calculation", args.progress_interval_seconds, enabled=args.progress_interval_seconds > 0):
        df = _add_levels(df, args)

    signals: list[GeneratedSignal] = []
    per_day_count: dict[str, int] = {}
    last_signal_time: datetime | None = None
    last_level_by_side: dict[str, tuple[datetime, float]] = {}

    start_time = pd.Timestamp(args.start) if args.start else None
    end_time = pd.Timestamp(args.end) if args.end else None
    total = len(df)
    started = time.time()
    next_time_print = started + max(1.0, args.progress_interval_seconds)

    if progress_enabled:
        print(f"[sr-generate] scanning {total:,} candles...", file=sys.stderr, flush=True)

    for i, row in enumerate(df.itertuples(index=False), start=1):
        t = row.time.to_pydatetime() if hasattr(row.time, "to_pydatetime") else row.time
        now_clock = time.time()
        due_by_rows = i == 1 or i % args.progress_every_rows == 0 or i == total
        due_by_time = args.progress_interval_seconds > 0 and now_clock >= next_time_print
        if progress_enabled and (due_by_rows or due_by_time):
            _print_scan_progress(i, total, started, len(signals), t)
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
        required = (row.atr, row.support, row.resistance, row.recent_swing_low, row.recent_swing_high, row.close_pos)
        if any(pd.isna(v) for v in required):
            continue

        atr = float(row.atr)
        if atr < args.min_atr or atr > args.max_atr:
            continue
        box_width = float(row.box_width)
        if box_width < args.min_box_width or box_width > args.max_box_width:
            continue
        bar_range = float(row.bar_range)
        if bar_range <= 0 or bar_range > atr * args.max_signal_bar_atr:
            continue
        if float(row.body) < atr * args.min_body_atr:
            continue

        if last_signal_time is not None:
            gap_min = (t - last_signal_time).total_seconds() / 60.0
            if gap_min < args.cooldown_minutes:
                continue
        day_key = t.strftime("%Y-%m-%d")
        if args.max_signals_per_day > 0 and per_day_count.get(day_key, 0) >= args.max_signals_per_day:
            continue

        support = float(row.support)
        resistance = float(row.resistance)
        high = float(row.high)
        low = float(row.low)
        close = float(row.close)
        open_ = float(row.open)
        close_pos = float(row.close_pos)

        touch_buffer = max(args.min_touch_buffer, atr * args.touch_atr)
        sweep_buffer = max(args.min_sweep_buffer, atr * args.sweep_atr)
        reclaim_buffer = max(args.min_reclaim_buffer, atr * args.reclaim_atr)

        buy_sweep = low <= support - sweep_buffer and close >= support + reclaim_buffer
        buy_hold = low <= support + touch_buffer and close >= support + reclaim_buffer
        buy_confirm = close > open_ and close_pos >= args.buy_close_pos

        sell_sweep = high >= resistance + sweep_buffer and close <= resistance - reclaim_buffer
        sell_hold = high >= resistance - touch_buffer and close <= resistance - reclaim_buffer
        sell_confirm = close < open_ and close_pos <= args.sell_close_pos

        sig: GeneratedSignal | None = None
        if buy_confirm and _mode_allows(args.mode, buy_sweep, buy_hold):
            reason = "support_sweep_buy" if buy_sweep else "support_reject_buy"
            sig = _buy_signal(row, args, reason)
        elif sell_confirm and _mode_allows(args.mode, sell_sweep, sell_hold):
            reason = "resistance_sweep_sell" if sell_sweep else "resistance_reject_sell"
            sig = _sell_signal(row, args, reason)

        if sig is None:
            continue

        prior = last_level_by_side.get(sig.side)
        if prior is not None:
            prior_time, prior_level = prior
            if (t - prior_time).total_seconds() / 60.0 < args.level_cooldown_minutes:
                if abs(sig.level - prior_level) <= args.level_repeat_distance:
                    continue

        signals.append(sig)
        last_signal_time = t
        last_level_by_side[sig.side] = (t, sig.level)
        per_day_count[day_key] = per_day_count.get(day_key, 0) + 1

    if progress_enabled:
        print(
            f"[sr-generate] completed scan: {total:,} rows, {len(signals):,} signals, elapsed {_fmt_duration(time.time() - started)}",
            file=sys.stderr,
            flush=True,
        )
    return signals


def _write_signal_file(signals: list[GeneratedSignal], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[GeneratedSignal]] = {}
    for sig in signals:
        grouped.setdefault(sig.time.strftime("%Y-%m-%d"), []).append(sig)

    lines: list[str] = []
    for day in sorted(grouped):
        lines.append(f"{day} GMT+3")
        for idx, sig in enumerate(sorted(grouped[day], key=lambda s: s.time), start=1):
            lines.append(
                f"{idx}. {sig.side} XAUUSD {_price(sig.r1)} - {_price(sig.r2)} "
                f"SL {_price(sig.sl)} TP1 {_price(sig.tp1)} TP2 {_price(sig.tp2)} "
                f"TP3 {_price(sig.tp3)} {_time_ampm(sig.time)}"
            )
        lines.append("")
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_diagnostics(signals: list[GeneratedSignal], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = list(asdict(signals[0]).keys()) if signals else [
            "time", "side", "r1", "r2", "sl", "tp1", "tp2", "tp3", "reason",
            "level", "opposite_level", "entry_ref", "risk", "room_rr", "atr", "spread_points",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for sig in signals:
            row = asdict(sig)
            row["time"] = sig.time.isoformat(sep=" ")
            writer.writerow(row)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="generate_sr_reversal_signals",
        description="Generate support/resistance rejection and liquidity-sweep scalping signals.",
    )
    p.add_argument("--charts", required=True, nargs="+", help="MT5 M1 chart CSV files or globs.")
    p.add_argument("--output", required=True, help="Output signal text file.")
    p.add_argument("--diagnostics", default=None, help="Optional CSV with generated signal features.")
    p.add_argument("--start", default=None, help="Optional inclusive chart-time start, e.g. 2024-01-01.")
    p.add_argument("--end", default=None, help="Optional exclusive chart-time end, e.g. 2026-01-01.")
    p.add_argument("--progress-every-rows", type=int, default=100_000)
    p.add_argument("--progress-interval-seconds", type=float, default=15.0)

    p.add_argument("--mode", choices=["sweep", "hold", "both"], default="both")
    p.add_argument("--cooldown-minutes", type=float, default=8.0)
    p.add_argument("--level-cooldown-minutes", type=float, default=60.0)
    p.add_argument("--level-repeat-distance", type=float, default=2.0)
    p.add_argument("--max-signals-per-day", type=int, default=0, help="0 = unlimited.")
    p.add_argument("--session-start", type=int, default=7, help="Chart-time hour, GMT+3.")
    p.add_argument("--session-end", type=int, default=23, help="Chart-time hour, GMT+3.")
    p.add_argument("--weekdays-only", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--max-spread-points", type=int, default=60)

    p.add_argument("--level-lookback", type=int, default=180, help="Prior M1 candles for support/resistance.")
    p.add_argument("--swing-lookback", type=int, default=30)
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--min-atr", type=float, default=0.25)
    p.add_argument("--max-atr", type=float, default=8.0)
    p.add_argument("--min-box-width", type=float, default=8.0)
    p.add_argument("--max-box-width", type=float, default=80.0)
    p.add_argument("--max-signal-bar-atr", type=float, default=3.0)
    p.add_argument("--min-body-atr", type=float, default=0.05)

    p.add_argument("--touch-atr", type=float, default=0.10)
    p.add_argument("--sweep-atr", type=float, default=0.10)
    p.add_argument("--reclaim-atr", type=float, default=0.05)
    p.add_argument("--min-touch-buffer", type=float, default=0.5)
    p.add_argument("--min-sweep-buffer", type=float, default=0.3)
    p.add_argument("--min-reclaim-buffer", type=float, default=0.2)
    p.add_argument("--buy-close-pos", type=float, default=0.55, help="Close location in candle range, 0 low to 1 high.")
    p.add_argument("--sell-close-pos", type=float, default=0.45, help="Close location in candle range, 0 low to 1 high.")

    p.add_argument("--price-step", type=float, default=0.5)
    p.add_argument("--range-width", type=float, default=2.0)
    p.add_argument("--entry-buffer", type=float, default=0.5)
    p.add_argument("--sl-buffer-atr", type=float, default=0.20)
    p.add_argument("--min-risk", type=float, default=4.0)
    p.add_argument("--max-risk", type=float, default=12.0)
    p.add_argument("--cap-oversized-risk", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--min-room-rr", type=float, default=1.0, help="Require room to opposite S/R level. 0 disables.")
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
    if not (0 < args.rr1 < args.rr2 < args.rr3):
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
    _write_signal_file(signals, output)
    if args.diagnostics:
        print(f"Writing diagnostics to {args.diagnostics}", file=sys.stderr, flush=True)
        _write_diagnostics(signals, Path(args.diagnostics))

    parsed = parse_signals_file(output)
    if len(parsed) != len(signals):
        raise SystemExit(
            f"Generated {len(signals)} signals but parser read {len(parsed)}. Check formatting in {output}."
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
