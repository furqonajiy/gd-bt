#!/usr/bin/env python3
"""Generate volatility-adaptive (ATR-scaled) XAUUSD M15 BREAKOUT/MOMENTUM signals.

The third distinct mechanism. Where the range-fade `self` generator trades a
shallow trend pullback and `generate_meanrev_signals.py` fades a stretch, this
one trades WITH a fresh momentum break of recent structure:

  * Track the rolling `--lookback` bar HIGH and LOW (excluding the current bar).
  * When the close breaks ABOVE the prior high by >= `--break-atr` * ATR AND the
    bar's body (|close - open|) is >= `--min-body-atr` * ATR (real momentum, not
    a wick), BUY the upside breakout. Symmetric SELL on a downside break.
  * Entries ladder a touch ABOVE the breakout level for a BUY (buy-stop-style
    continuation, expressed as the same r1/r2 ladder the engine consumes), the
    stop sits `--sl-atr` * ATR BELOW the broken level (back inside the range),
    and TP1/2/3 run `--tpN-atr` * ATR FURTHER in the breakout direction.

Every dollar dimension is an ATR MULTIPLE of this bar's M15 ATR, so widths
self-scale with the regime (quiet 2021 ~$2-5 stops, parabolic 2026 ~$15-30).
Output format is byte-identical to the existing feeds so it drops straight into
`tools/sweep_self_limit.py` / `backtest_explicit.py`.
"""
from __future__ import annotations

import argparse
import glob
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd


CHART_TZ_OFFSET = 3


@dataclass(frozen=True)
class SignalRow:
    signal_time: datetime
    side: str
    r1: float
    r2: float
    sl: float
    tp1: float
    tp2: float
    tp3: float


def _expand_patterns(patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern)) if any(ch in pattern for ch in "*?[") else [pattern]
        for match in matches:
            path = Path(match)
            if path.exists():
                paths.append(path)
    return sorted({p.resolve(): p for p in paths}.values())


def _read_mt5_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, sep="\t")
    df.columns = [str(c).strip("<>").upper() for c in df.columns]
    required = {"DATE", "TIME", "OPEN", "HIGH", "LOW", "CLOSE", "SPREAD"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path} missing required columns: {sorted(missing)}")

    out = pd.DataFrame()
    out["time"] = pd.to_datetime(
        df["DATE"].astype(str) + " " + df["TIME"].astype(str),
        format="%Y.%m.%d %H:%M:%S",
    )
    for src, dst in (("OPEN", "open"), ("HIGH", "high"), ("LOW", "low"), ("CLOSE", "close"), ("SPREAD", "spread")):
        out[dst] = pd.to_numeric(df[src], errors="coerce")
    out["source_file"] = path.name
    return out.dropna(subset=["time", "open", "high", "low", "close", "spread"])


def _load_csvs(paths: list[Path]) -> pd.DataFrame:
    if not paths:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "spread", "source_file"])
    frames = [_read_mt5_csv(path) for path in paths]
    df = pd.concat(frames, ignore_index=True)
    return df.sort_values(["time", "source_file"]).drop_duplicates("time", keep="last").reset_index(drop=True)


def _m1_to_m15(m1: pd.DataFrame) -> pd.DataFrame:
    if m1.empty:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "spread"])
    indexed = m1.sort_values("time").set_index("time")
    m15 = indexed.resample("15min", label="left", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        spread=("spread", "last"),
    )
    return m15.dropna(subset=["open", "high", "low", "close", "spread"]).reset_index()


def _load_working_m15(m15_patterns: list[str], m1_patterns: list[str]) -> pd.DataFrame:
    m15_paths = _expand_patterns(m15_patterns)
    m1_paths = _expand_patterns(m1_patterns)

    direct_m15 = _load_csvs(m15_paths)
    from_m1 = _m1_to_m15(_load_csvs(m1_paths))

    frames: list[pd.DataFrame] = []
    if not from_m1.empty:
        temp = from_m1.copy()
        temp["source_priority"] = 0
        frames.append(temp)
    if not direct_m15.empty:
        temp = direct_m15.copy()
        temp["source_priority"] = 1
        frames.append(temp)
    if not frames:
        raise SystemExit("No chart files found. Provide --m15-charts and/or --m1-charts.")

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values(["time", "source_priority"])
    combined = combined.drop_duplicates("time", keep="last")
    combined = combined.sort_values("time").reset_index(drop=True)
    return combined[["time", "open", "high", "low", "close", "spread"]]


def _add_indicators(m15: pd.DataFrame, lookback: int, atr_period: int) -> pd.DataFrame:
    out = m15.copy()
    # Prior-structure high/low EXCLUDING the current bar (shift(1)) so the break
    # is measured against bars that closed before this one.
    out["prior_high"] = out["high"].shift(1).rolling(lookback, min_periods=lookback).max()
    out["prior_low"] = out["low"].shift(1).rolling(lookback, min_periods=lookback).min()
    out["body"] = (out["close"] - out["open"]).abs()

    prev_close = out["close"].shift(1)
    true_range = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr"] = true_range.rolling(atr_period, min_periods=atr_period).mean()
    return out


def _fmt_price(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _fmt_time(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _build_signal(
        row,
        side: str,
        break_level: float,
        entry_offset: float,
        range_width: float,
        sl_gap: float,
        tp1_distance: float,
        tp2_distance: float,
        tp3_distance: float,
) -> SignalRow:
    """Lay a continuation ladder in the breakout direction.

    For a BUY (upside break) the entries sit just ABOVE the close (buy more of
    the momentum on a shallow continuation pullback, in the same r1/r2 ladder
    shape the engine consumes); the stop sits sl-atr below the BROKEN level
    (i.e. back inside the prior range, which invalidates the break) and the
    targets run further UP. SELL is the mirror.
    """
    close = round(float(row.close), 2)

    if side == "BUY":
        r1 = round(close - entry_offset, 2)
        r2 = round(r1 - range_width, 2)
        # Stop back inside the range: below the broken high by sl_gap. Never let
        # it sit above the ladder.
        sl = round(min(break_level, r2) - sl_gap, 2)
        tp1 = round(r1 + tp1_distance, 2)
        tp2 = round(r1 + tp2_distance, 2)
        tp3 = round(r1 + tp3_distance, 2)
    else:
        r1 = round(close + entry_offset, 2)
        r2 = round(r1 + range_width, 2)
        sl = round(max(break_level, r2) + sl_gap, 2)
        tp1 = round(r1 - tp1_distance, 2)
        tp2 = round(r1 - tp2_distance, 2)
        tp3 = round(r1 - tp3_distance, 2)

    return SignalRow(row.signal_time.to_pydatetime(), side, r1, r2, sl, tp1, tp2, tp3)


def generate_signals(args: argparse.Namespace) -> list[SignalRow]:
    m15 = _load_working_m15(args.m15_charts, args.m1_charts)
    data = _add_indicators(m15, args.lookback, args.atr_period)
    data["signal_time"] = data["time"] + pd.Timedelta(minutes=15)

    start = pd.Timestamp(args.start_date) if args.start_date else pd.Timestamp.min
    end = pd.Timestamp(args.end_date) + pd.Timedelta(days=1) if args.end_date else pd.Timestamp.max

    last_side_time: dict[str, pd.Timestamp | None] = {"BUY": None, "SELL": None}
    daily_count: defaultdict[object, int] = defaultdict(int)
    rows: list[SignalRow] = []

    for i, row in data.iterrows():
        signal_time = row.signal_time
        if signal_time < start or signal_time >= end:
            continue
        atr = float(row.atr)
        if not (args.min_atr <= atr <= args.max_atr):
            continue
        if pd.isna(row.prior_high) or pd.isna(row.prior_low):
            continue

        signal_date = signal_time.date()
        if daily_count[signal_date] >= args.max_signals_per_day:
            continue

        close = float(row.close)
        body = float(row.body)
        prior_high = float(row.prior_high)
        prior_low = float(row.prior_low)

        # Momentum filter: the breaking bar must have a real body, not a wick.
        if body < args.min_body_atr * atr:
            continue

        side: str | None = None
        break_level: float | None = None
        if close - prior_high >= args.break_atr * atr:
            side = "BUY"
            break_level = round(prior_high, 2)
        elif prior_low - close >= args.break_atr * atr:
            side = "SELL"
            break_level = round(prior_low, 2)

        if side is None or break_level is None:
            continue

        previous_same_side = last_side_time[side]
        if previous_same_side is not None and signal_time - previous_same_side < pd.Timedelta(minutes=args.same_side_spacing_minutes):
            continue

        # Volatility-adaptive sizing: every distance is an ATR MULTIPLE of this
        # bar's M15 ATR so widths self-scale with the regime.
        range_width = args.range_atr * atr
        sl_gap = args.sl_atr * atr
        tp1_distance = args.tp1_atr * atr
        tp2_distance = args.tp2_atr * atr
        tp3_distance = args.tp3_atr * atr

        rows.append(
            _build_signal(
                row,
                side,
                break_level,
                args.entry_offset,
                range_width,
                sl_gap,
                tp1_distance,
                tp2_distance,
                tp3_distance,
            )
        )
        last_side_time[side] = signal_time
        daily_count[signal_date] += 1

    return rows


def write_signal_file(signals: list[SignalRow], output_path: Path, source_tz_offset: int) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    grouped: dict[str, list[SignalRow]] = defaultdict(list)
    for signal in signals:
        grouped[signal.signal_time.strftime("%Y-%m-%d")].append(signal)

    lines: list[str] = []
    tz_label = f"GMT+{source_tz_offset}" if source_tz_offset >= 0 else f"GMT{source_tz_offset}"

    for date_key in sorted(grouped):
        if lines:
            lines.append("")
        lines.append(f"{date_key} {tz_label}")

        for day_id, signal in enumerate(sorted(grouped[date_key], key=lambda s: s.signal_time), start=1):
            lines.append(
                f"{day_id}. {signal.side} XAUUSD "
                f"{_fmt_price(signal.r1)} - {_fmt_price(signal.r2)} "
                f"SL {_fmt_price(signal.sl)} "
                f"TP1 {_fmt_price(signal.tp1)} "
                f"TP2 {_fmt_price(signal.tp2)} "
                f"TP3 {_fmt_price(signal.tp3)} "
                f"{_fmt_time(signal.signal_time)}"
            )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate volatility-adaptive (ATR-scaled) XAUUSD M15 breakout/momentum signals.")
    p.add_argument("--m15-charts", nargs="+", default=["data/XAUUSD_M15_*_ELEV8.csv"])
    p.add_argument("--m1-charts", nargs="+", default=["data/XAUUSD_M1_*_ELEV8.csv"])
    p.add_argument("--output", default="generated/adaptive_breakout.txt")
    p.add_argument("--alias-output", default=None)
    p.add_argument("--start-date", default="2021-11-01")
    p.add_argument("--end-date", default=None)
    p.add_argument("--lookback", type=int, default=12, help="Rolling window (M15 bars) for the prior high/low to break.")
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--min-atr", type=float, default=0.30, help="ATR filter band (lower): skip dead/illiquid bars.")
    p.add_argument("--max-atr", type=float, default=80.00, help="ATR filter band (upper): skip data glitches.")
    p.add_argument("--same-side-spacing-minutes", type=int, default=45)
    p.add_argument("--max-signals-per-day", type=int, default=20)
    p.add_argument("--entry-offset", type=float, default=1.00, help="Fixed $ nudge of entry #1 for the continuation ladder.")
    # Trigger thresholds (ATR multiples).
    p.add_argument("--break-atr", type=float, default=0.05,
                   help="Break must clear the prior high/low by break-atr * ATR.")
    p.add_argument("--min-body-atr", type=float, default=0.25,
                   help="Momentum filter: bar body >= min-body-atr * ATR.")
    # ATR-multiple sizing. Tuned for a comparable signal count to the ~21-24k
    # range-fade feed with a momentum R:R (stop back inside the range, targets
    # running with the break).
    p.add_argument("--range-atr", type=float, default=0.60)
    p.add_argument("--sl-atr", type=float, default=0.90,
                   help="Stop placed sl-atr * ATR BELOW (BUY) / ABOVE (SELL) the broken level.")
    p.add_argument("--tp1-atr", type=float, default=1.80)
    p.add_argument("--tp2-atr", type=float, default=3.20)
    p.add_argument("--tp3-atr", type=float, default=5.40)
    p.add_argument("--source-tz-offset", type=int, default=CHART_TZ_OFFSET)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    signals = generate_signals(args)
    output_path = Path(args.output)
    write_signal_file(signals, output_path, args.source_tz_offset)

    if args.alias_output:
        alias_path = Path(args.alias_output)
        alias_path.parent.mkdir(parents=True, exist_ok=True)
        if alias_path.resolve() != output_path.resolve():
            shutil.copyfile(output_path, alias_path)

    print(f"signals_written={len(signals)}")
    print(f"output={output_path}")
    if args.alias_output:
        print(f"alias_output={args.alias_output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
