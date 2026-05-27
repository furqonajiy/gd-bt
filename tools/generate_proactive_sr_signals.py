#!/usr/bin/env python3
"""Generate proactive XAUUSD support/resistance bounce signals.

Unlike the EMA and rejection generators, this script does not wait for a full
confirmation candle after the level reacts.  It predicts likely bounce zones from
known levels and places pending limit ranges before price reaches them.

Levels used, without look-ahead:
- previous trading day's high / low
- current day's Asian-session high / low, used only after the Asian session ends

Workflow:

    python tools/generate_proactive_sr_signals.py \
      --charts data/XAUUSD_M1_*.csv \
      --output generated/proactive_sr_v1.txt \
      --diagnostics generated/proactive_sr_v1.csv

    python tools/backtest_configurable.py \
      --signals generated/proactive_sr_v1.txt \
      --charts data/XAUUSD_M1_*.csv \
      --output-dir reports/proactive_sr_v1 \
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
    level_name: str
    level: float
    opposite_level: float | None
    distance_to_level: float
    risk: float
    room_rr: float | None
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


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "calculating"
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


def _prepare_levels(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    out = df.copy().sort_values("time").reset_index(drop=True)
    out["date"] = out["time"].dt.strftime("%Y-%m-%d")
    out["hour"] = out["time"].dt.hour

    prev_close = out["close"].shift(1)
    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - prev_close).abs(),
        (out["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    out["atr"] = tr.rolling(args.atr_period, min_periods=args.atr_period).mean()
    out["body"] = (out["close"] - out["open"]).abs()

    daily = out.groupby("date", sort=True).agg(day_high=("high", "max"), day_low=("low", "min"))
    daily["prev_day_high"] = daily["day_high"].shift(1)
    daily["prev_day_low"] = daily["day_low"].shift(1)
    out = out.merge(daily[["prev_day_high", "prev_day_low"]], left_on="date", right_index=True, how="left")

    asian = out[(out["hour"] >= args.asian_start) & (out["hour"] < args.asian_end)]
    asian_levels = asian.groupby("date", sort=True).agg(asian_high=("high", "max"), asian_low=("low", "min"))
    out = out.merge(asian_levels, left_on="date", right_index=True, how="left")
    return out


def _build_buy(row, args: argparse.Namespace, *, level_name: str, level: float, opposite: float | None) -> GeneratedSignal | None:
    atr = float(row.atr)
    close = float(row.close)
    entry = _ceil_to_step(level + args.entry_buffer, args.price_step)
    high_entry = entry
    low_entry = round(high_entry - args.range_width, 2)
    distance = close - level

    risk = max(args.min_risk, min(args.max_risk, args.stop_distance if args.stop_distance > 0 else atr * args.stop_atr))
    sl = _floor_to_step(level - risk + args.entry_buffer, args.price_step)
    if sl >= low_entry:
        sl = _floor_to_step(low_entry - args.price_step, args.price_step)
    risk = high_entry - sl
    if not (args.min_risk <= risk <= args.max_risk + 1e-9):
        return None

    room_rr = None
    if opposite is not None and opposite > high_entry:
        room_rr = (opposite - high_entry) / risk
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
        reason="proactive_support_buy", level_name=level_name, level=level,
        opposite_level=opposite, distance_to_level=distance, risk=risk, room_rr=room_rr,
        atr=atr, spread_points=int(row.spread),
    )


def _build_sell(row, args: argparse.Namespace, *, level_name: str, level: float, opposite: float | None) -> GeneratedSignal | None:
    atr = float(row.atr)
    close = float(row.close)
    entry = _floor_to_step(level - args.entry_buffer, args.price_step)
    low_entry = entry
    high_entry = round(low_entry + args.range_width, 2)
    distance = level - close

    risk = max(args.min_risk, min(args.max_risk, args.stop_distance if args.stop_distance > 0 else atr * args.stop_atr))
    sl = _ceil_to_step(level + risk - args.entry_buffer, args.price_step)
    if sl <= high_entry:
        sl = _ceil_to_step(high_entry + args.price_step, args.price_step)
    risk = sl - low_entry
    if not (args.min_risk <= risk <= args.max_risk + 1e-9):
        return None

    room_rr = None
    if opposite is not None and opposite < low_entry:
        room_rr = (low_entry - opposite) / risk
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
        reason="proactive_resistance_sell", level_name=level_name, level=level,
        opposite_level=opposite, distance_to_level=distance, risk=risk, room_rr=room_rr,
        atr=atr, spread_points=int(row.spread),
    )


def _print_progress(i: int, total: int, started: float, n_signals: int, t: datetime) -> None:
    elapsed = time.time() - started
    pct = i / total * 100.0 if total else 0.0
    rate = i / elapsed if elapsed > 0 else 0.0
    eta = (total - i) / rate if rate > 0 else None
    print(
        f"[proactive-sr] {i:,}/{total:,} rows ({pct:5.1f}%) | signals={n_signals:,} | "
        f"candle={t} | elapsed={_fmt_duration(elapsed)} | ETA={_fmt_duration(eta)}",
        file=sys.stderr,
        flush=True,
    )


def generate_signals(df: pd.DataFrame, args: argparse.Namespace) -> list[GeneratedSignal]:
    with _Heartbeat("level preparation", args.progress_interval_seconds, enabled=args.progress_interval_seconds > 0):
        df = _prepare_levels(df, args)

    signals: list[GeneratedSignal] = []
    per_day_count: dict[str, int] = {}
    last_signal_time: datetime | None = None
    last_level_seen: dict[tuple[str, str], datetime] = {}

    start_time = pd.Timestamp(args.start) if args.start else None
    end_time = pd.Timestamp(args.end) if args.end else None
    total = len(df)
    started = time.time()
    next_time_print = started + max(1.0, args.progress_interval_seconds)
    progress_enabled = args.progress_interval_seconds > 0 and args.progress_every_rows > 0
    if progress_enabled:
        print(f"[proactive-sr] scanning {total:,} candles...", file=sys.stderr, flush=True)

    for i, row in enumerate(df.itertuples(index=False), start=1):
        t = row.time.to_pydatetime() if hasattr(row.time, "to_pydatetime") else row.time
        now_clock = time.time()
        if progress_enabled and (i == 1 or i % args.progress_every_rows == 0 or i == total or now_clock >= next_time_print):
            _print_progress(i, total, started, len(signals), t)
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
        if pd.isna(row.atr):
            continue
        atr = float(row.atr)
        if atr < args.min_atr or atr > args.max_atr:
            continue
        if float(row.body) > atr * args.max_body_atr:
            # Avoid entering immediately after a news/spike candle.
            continue
        if last_signal_time is not None:
            if (t - last_signal_time).total_seconds() / 60.0 < args.cooldown_minutes:
                continue
        day_key = t.strftime("%Y-%m-%d")
        if args.max_signals_per_day > 0 and per_day_count.get(day_key, 0) >= args.max_signals_per_day:
            continue

        close = float(row.close)
        open_ = float(row.open)
        min_distance = max(args.min_distance, atr * args.min_distance_atr)
        max_distance = max(args.max_distance, atr * args.max_distance_atr)

        candidates: list[GeneratedSignal] = []
        support_levels = []
        resistance_levels = []
        if not pd.isna(row.prev_day_low):
            support_levels.append(("prev_day_low", float(row.prev_day_low), float(row.prev_day_high) if not pd.isna(row.prev_day_high) else None))
        if not pd.isna(row.prev_day_high):
            resistance_levels.append(("prev_day_high", float(row.prev_day_high), float(row.prev_day_low) if not pd.isna(row.prev_day_low) else None))
        # Asian levels only after the Asian range is complete.
        if int(row.hour) >= args.asian_end:
            if not pd.isna(row.asian_low):
                support_levels.append(("asian_low", float(row.asian_low), float(row.asian_high) if not pd.isna(row.asian_high) else None))
            if not pd.isna(row.asian_high):
                resistance_levels.append(("asian_high", float(row.asian_high), float(row.asian_low) if not pd.isna(row.asian_low) else None))

        if args.direction in {"both", "buy"}:
            for level_name, level, opposite in support_levels:
                distance = close - level
                approaching = close < open_ if args.require_approach_candle else True
                if min_distance <= distance <= max_distance and approaching:
                    sig = _build_buy(row, args, level_name=level_name, level=level, opposite=opposite)
                    if sig is not None:
                        candidates.append(sig)
        if args.direction in {"both", "sell"}:
            for level_name, level, opposite in resistance_levels:
                distance = level - close
                approaching = close > open_ if args.require_approach_candle else True
                if min_distance <= distance <= max_distance and approaching:
                    sig = _build_sell(row, args, level_name=level_name, level=level, opposite=opposite)
                    if sig is not None:
                        candidates.append(sig)

        if not candidates:
            continue

        # Prefer the nearest level; if tied, prefer levels with better room to target.
        candidates.sort(key=lambda s: (s.distance_to_level, -(s.room_rr or 0.0)))
        sig = candidates[0]
        level_key = (sig.side, sig.level_name)
        prior_time = last_level_seen.get(level_key)
        if prior_time is not None and (t - prior_time).total_seconds() / 60.0 < args.level_cooldown_minutes:
            continue

        signals.append(sig)
        last_signal_time = t
        last_level_seen[level_key] = t
        per_day_count[day_key] = per_day_count.get(day_key, 0) + 1

    if progress_enabled:
        print(
            f"[proactive-sr] completed scan: {total:,} rows, {len(signals):,} signals, elapsed {_fmt_duration(time.time() - started)}",
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
            "level_name", "level", "opposite_level", "distance_to_level", "risk", "room_rr", "atr", "spread_points",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for sig in signals:
            row = asdict(sig)
            row["time"] = sig.time.isoformat(sep=" ")
            writer.writerow(row)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate proactive support/resistance bounce signals.")
    p.add_argument("--charts", required=True, nargs="+")
    p.add_argument("--output", required=True)
    p.add_argument("--diagnostics", default=None)
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--progress-every-rows", type=int, default=100_000)
    p.add_argument("--progress-interval-seconds", type=float, default=15.0)

    p.add_argument("--direction", choices=["both", "buy", "sell"], default="both")
    p.add_argument("--cooldown-minutes", type=float, default=10.0)
    p.add_argument("--level-cooldown-minutes", type=float, default=45.0)
    p.add_argument("--max-signals-per-day", type=int, default=0)
    p.add_argument("--session-start", type=int, default=7)
    p.add_argument("--session-end", type=int, default=23)
    p.add_argument("--weekdays-only", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--max-spread-points", type=int, default=60)

    p.add_argument("--asian-start", type=int, default=0)
    p.add_argument("--asian-end", type=int, default=7)
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--min-atr", type=float, default=0.25)
    p.add_argument("--max-atr", type=float, default=8.0)
    p.add_argument("--min-distance", type=float, default=1.0)
    p.add_argument("--max-distance", type=float, default=5.0)
    p.add_argument("--min-distance-atr", type=float, default=0.4)
    p.add_argument("--max-distance-atr", type=float, default=1.8)
    p.add_argument("--max-body-atr", type=float, default=1.5)
    p.add_argument("--require-approach-candle", action=argparse.BooleanOptionalAction, default=True)

    p.add_argument("--price-step", type=float, default=0.5)
    p.add_argument("--range-width", type=float, default=2.0)
    p.add_argument("--entry-buffer", type=float, default=0.5)
    p.add_argument("--stop-distance", type=float, default=6.0, help="Fixed level stop distance. <=0 uses ATR stop.")
    p.add_argument("--stop-atr", type=float, default=2.0)
    p.add_argument("--min-risk", type=float, default=4.0)
    p.add_argument("--max-risk", type=float, default=12.0)
    p.add_argument("--min-room-rr", type=float, default=1.0)
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
    if not (0 <= args.asian_start <= 23 and 0 <= args.asian_end <= 23):
        raise SystemExit("asian-start and asian-end must be hours in 0..23")
    if not (0 < args.rr1 < args.rr2 < args.rr3):
        raise SystemExit("Require 0 < rr1 < rr2 < rr3")

    chart_paths = _expand_chart_paths(args.charts)
    print(f"Loading chart files: {len(chart_paths):,}", file=sys.stderr, flush=True)
    with _Heartbeat("chart load", args.progress_interval_seconds, enabled=args.progress_interval_seconds > 0):
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
        raise SystemExit(f"Generated {len(signals)} signals but parser read {len(parsed)}. Check {output}.")

    days = len({s.time.date() for s in signals}) if signals else 0
    print(f"Generated signals: {len(signals)}")
    print(f"Active days:        {days}")
    print(f"First signal:       {min((s.time for s in signals), default=None)}")
    print(f"Last signal:        {max((s.time for s in signals), default=None)}")
    print(f"Output:             {output.resolve()}")
    if args.diagnostics:
        print(f"Diagnostics:        {Path(args.diagnostics).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
