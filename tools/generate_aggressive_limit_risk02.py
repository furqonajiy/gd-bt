#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class GeneratedSignal:
    signal_time: datetime
    side: str
    r1: float
    r2: float
    sl: float
    tp1: float
    tp2: float
    tp3: float


def _price(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _time_text(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _expand_paths(patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    for pattern in patterns:
        if any(ch in pattern for ch in "*?["):
            matches = sorted(glob.glob(pattern))
            if not matches:
                raise SystemExit(f"No files match pattern: {pattern}")
            out.extend(Path(path) for path in matches)
        else:
            path = Path(pattern)
            if not path.exists():
                raise SystemExit(f"Chart file not found: {path}")
            out.append(path)
    if not out:
        raise SystemExit("No chart files provided")
    return out


def _load_m1(paths: list[Path]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in paths:
        df = pd.read_csv(path, sep="\t")
        df.columns = [str(col).strip("<>").upper() for col in df.columns]
        missing = {"DATE", "TIME", "OPEN", "HIGH", "LOW", "CLOSE", "SPREAD"} - set(df.columns)
        if missing:
            raise SystemExit(f"{path} missing columns: {sorted(missing)}")
        df["time"] = pd.to_datetime(
            df["DATE"].astype(str) + " " + df["TIME"].astype(str),
            format="%Y.%m.%d %H:%M:%S",
            )
        for col in ("OPEN", "HIGH", "LOW", "CLOSE", "SPREAD"):
            df[col.lower()] = pd.to_numeric(df[col], errors="coerce")
        frames.append(df[["time", "open", "high", "low", "close", "spread"]])

    chart = pd.concat(frames, ignore_index=True).dropna()
    chart = chart.drop_duplicates(subset=["time"], keep="last")
    chart = chart.sort_values("time").reset_index(drop=True)
    return chart.set_index("time")


def _ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return (
        df.resample(rule, label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "spread": "last"})
        .dropna()
    )


def _parse_hours(raw: str) -> set[int]:
    hours = {int(part.strip()) for part in raw.split(",") if part.strip()}
    bad = [hour for hour in hours if hour < 0 or hour > 23]
    if bad:
        raise SystemExit(f"Invalid execution hour(s): {bad}")
    return hours


def generate_signals(
        chart: pd.DataFrame,
        *,
        start_date: datetime,
        end_date: datetime | None,
        allowed_hours: set[int],
        ema_span: int,
        cooldown_minutes: int,
        anchor_distance: float,
        signal_width: float,
        raw_sl_distance: float,
        tp1_distance: float,
        tp2_distance: float,
        tp3_distance: float,
) -> list[GeneratedSignal]:
    m1 = chart[chart.index >= start_date]
    if end_date is not None:
        m1 = m1[m1.index < end_date + timedelta(days=1)]
    if m1.empty:
        return []

    m5 = _ohlc(m1, "5min")
    m15 = _ohlc(m1, "15min")
    m5["ema"] = m5["close"].ewm(span=ema_span, adjust=False).mean()
    m15["ema"] = m15["close"].ewm(span=ema_span, adjust=False).mean()

    completed_m15 = m15[["close", "ema"]].copy()
    completed_m15.index = completed_m15.index + timedelta(minutes=15)

    decision_times = m5.index + timedelta(minutes=5)
    m15_asof = completed_m15.reindex(decision_times, method="ffill")
    m15_asof.index = m5.index

    signals: list[GeneratedSignal] = []
    last_by_side: dict[str, datetime] = {}

    for i in range(1, len(m5)):
        row = m5.iloc[i]
        previous = m5.iloc[i - 1]
        bar_start = m5.index[i].to_pydatetime()
        signal_time = bar_start + timedelta(minutes=5)

        if signal_time < start_date:
            continue
        if end_date is not None and signal_time >= end_date + timedelta(days=1):
            continue
        if signal_time.hour not in allowed_hours:
            continue

        higher = m15_asof.iloc[i]
        if pd.isna(higher["close"]) or pd.isna(higher["ema"]):
            continue

        close = float(row["close"])
        buy = close > float(row["ema"]) and close > float(previous["close"]) and float(higher["close"]) > float(higher["ema"])
        sell = close < float(row["ema"]) and close < float(previous["close"]) and float(higher["close"]) < float(higher["ema"])

        side = "BUY" if buy else "SELL" if sell else None
        if side is None:
            continue

        last = last_by_side.get(side)
        if last is not None and signal_time - last < timedelta(minutes=cooldown_minutes):
            continue
        last_by_side[side] = signal_time

        if side == "BUY":
            anchor = round(close - anchor_distance, 2)
            signal = GeneratedSignal(
                signal_time=signal_time,
                side=side,
                r1=anchor,
                r2=round(anchor - signal_width, 2),
                sl=round(anchor - raw_sl_distance, 2),
                tp1=round(anchor + tp1_distance, 2),
                tp2=round(anchor + tp2_distance, 2),
                tp3=round(anchor + tp3_distance, 2),
            )
        else:
            anchor = round(close + anchor_distance, 2)
            signal = GeneratedSignal(
                signal_time=signal_time,
                side=side,
                r1=anchor,
                r2=round(anchor + signal_width, 2),
                sl=round(anchor + raw_sl_distance, 2),
                tp1=round(anchor - tp1_distance, 2),
                tp2=round(anchor - tp2_distance, 2),
                tp3=round(anchor - tp3_distance, 2),
            )
        signals.append(signal)

    return sorted(signals, key=lambda item: item.signal_time)


def write_signal_file(signals: list[GeneratedSignal], output: Path) -> None:
    by_day: dict[str, list[GeneratedSignal]] = defaultdict(list)
    for signal in signals:
        by_day[signal.signal_time.strftime("%Y-%m-%d")].append(signal)

    lines: list[str] = []
    for day in sorted(by_day):
        lines.append(f"{day} GMT+3")
        for day_id, signal in enumerate(by_day[day], start=1):
            lines.append(
                f"{day_id}. {signal.side} XAUUSD "
                f"{_price(signal.r1)} - {_price(signal.r2)} "
                f"SL {_price(signal.sl)} "
                f"TP1 {_price(signal.tp1)} "
                f"TP2 {_price(signal.tp2)} "
                f"TP3 {_price(signal.tp3)} "
                f"{_time_text(signal.signal_time)}"
            )
        lines.append("")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate aggressive plain-LIMIT XAUUSD signals from M1 chart data.")
    parser.add_argument("--charts", nargs="+", required=True)
    parser.add_argument("--output", default="signals/aggressive_limit_risk02.txt")
    parser.add_argument("--start-date", default="2025-01-01")
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--execution-hours", default="5,8,10,11,12,13,15,16,18,19,21")
    parser.add_argument("--ema-span", type=int, default=100)
    parser.add_argument("--cooldown-minutes", type=int, default=60)
    parser.add_argument("--anchor-distance", type=float, default=7.0)
    parser.add_argument("--signal-width", type=float, default=2.0)
    parser.add_argument("--raw-sl-distance", type=float, default=10.0)
    parser.add_argument("--tp1-distance", type=float, default=5.0)
    parser.add_argument("--tp2-distance", type=float, default=10.0)
    parser.add_argument("--tp3-distance", type=float, default=15.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    start_date = datetime.strptime(args.start_date, "%Y-%m-%d")
    end_date = datetime.strptime(args.end_date, "%Y-%m-%d") if args.end_date else None

    chart = _load_m1(_expand_paths(args.charts))
    signals = generate_signals(
        chart,
        start_date=start_date,
        end_date=end_date,
        allowed_hours=_parse_hours(args.execution_hours),
        ema_span=args.ema_span,
        cooldown_minutes=args.cooldown_minutes,
        anchor_distance=args.anchor_distance,
        signal_width=args.signal_width,
        raw_sl_distance=args.raw_sl_distance,
        tp1_distance=args.tp1_distance,
        tp2_distance=args.tp2_distance,
        tp3_distance=args.tp3_distance,
    )
    write_signal_file(signals, Path(args.output))
    print(f"Wrote {len(signals)} signals to {Path(args.output).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())