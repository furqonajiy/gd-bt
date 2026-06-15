#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xauusd_trading.core import chart_tz


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


def _add_indicators(m15: pd.DataFrame, ema_fast: int, ema_slow: int, atr_period: int) -> pd.DataFrame:
    out = m15.copy()
    out["ema_fast"] = out["close"].ewm(span=ema_fast, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=ema_slow, adjust=False).mean()

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
        entry_offset: float,
        range_width: float,
        sl_gap: float,
        tp1_distance: float,
        tp2_distance: float,
        tp3_distance: float,
) -> SignalRow:
    close = round(float(row.close), 2)

    if side == "BUY":
        r1 = round(close - entry_offset, 2)
        r2 = round(r1 - range_width, 2)
        sl = round(r2 - sl_gap, 2)
        tp1 = round(r1 + tp1_distance, 2)
        tp2 = round(r1 + tp2_distance, 2)
        tp3 = round(r1 + tp3_distance, 2)
    else:
        r1 = round(close + entry_offset, 2)
        r2 = round(r1 + range_width, 2)
        sl = round(r2 + sl_gap, 2)
        tp1 = round(r1 - tp1_distance, 2)
        tp2 = round(r1 - tp2_distance, 2)
        tp3 = round(r1 - tp3_distance, 2)

    return SignalRow(row.signal_time.to_pydatetime(), side, r1, r2, sl, tp1, tp2, tp3)


def generate_signals(args: argparse.Namespace) -> list[SignalRow]:
    m15 = _load_working_m15(args.m15_charts, args.m1_charts)
    data = _add_indicators(m15, args.ema_fast, args.ema_slow, args.atr_period)
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
        if not (args.min_atr <= float(row.atr) <= args.max_atr):
            continue

        signal_date = signal_time.date()
        if daily_count[signal_date] >= args.max_signals_per_day:
            continue

        ema_delta = float(row.ema_fast) - float(data.iloc[i - 1].ema_fast) if i > 0 else 0.0
        side: str | None = None
        if row.ema_fast > row.ema_slow and ema_delta > 0 and row.close > row.ema_fast:
            side = "BUY"
        elif row.ema_fast < row.ema_slow and ema_delta < 0 and row.close < row.ema_fast:
            side = "SELL"

        if side is None:
            continue

        previous_same_side = last_side_time[side]
        if previous_same_side is not None and signal_time - previous_same_side < pd.Timedelta(minutes=args.same_side_spacing_minutes):
            continue

        # Volatility-adaptive sizing: every distance is an ATR MULTIPLE of this
        # bar's M15 ATR, so the stop/target widths self-scale with the regime
        # instead of being fixed dollars. Defaults are chosen so a quiet-regime
        # ATR (~$2.22) reproduces the legacy fixed-$ `better` distances; a
        # parabolic ATR (~$13) widens them proportionally.
        atr = float(row.atr)
        range_width = args.range_atr * atr
        sl_gap = args.sl_gap_atr * atr
        tp1_distance = args.tp1_atr * atr
        tp2_distance = args.tp2_atr * atr
        tp3_distance = args.tp3_atr * atr

        rows.append(
            _build_signal(
                row,
                side,
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

    # Signals are detected in CHART time (GMT+3); the feed is DISPLAYED in
    # source_tz_offset (header GMT+N + per-line clock), so shift by
    # (source_tz_offset - CHART_TZ_OFFSET). The engine parses the GMT+N header and
    # converts back to GMT+3, so the round-trip lands on the exact source bar.
    # DST-aware: from_chart_tz honors EET/EEST so winter (+2) round-trips too.
    grouped: dict[str, list[tuple[datetime, SignalRow]]] = defaultdict(list)
    for signal in signals:
        disp = chart_tz.from_chart_tz(signal.signal_time, source_tz_offset)
        grouped[disp.strftime("%Y-%m-%d")].append((disp, signal))

    lines: list[str] = []
    tz_label = f"GMT+{source_tz_offset}" if source_tz_offset >= 0 else f"GMT{source_tz_offset}"

    for date_key in sorted(grouped):
        if lines:
            lines.append("")
        lines.append(f"{date_key} {tz_label}")

        for day_id, (disp, signal) in enumerate(sorted(grouped[date_key], key=lambda ds: ds[0]), start=1):
            lines.append(
                f"{day_id}. {signal.side} XAUUSD "
                f"{_fmt_price(signal.r1)} - {_fmt_price(signal.r2)} "
                f"SL {_fmt_price(signal.sl)} "
                f"TP1 {_fmt_price(signal.tp1)} "
                f"TP2 {_fmt_price(signal.tp2)} "
                f"TP3 {_fmt_price(signal.tp3)} "
                f"{_fmt_time(disp)}"
            )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate volatility-adaptive (ATR-scaled) XAUUSD M15 trend-pullback signals.")
    p.add_argument("--m15-charts", nargs="+", default=["data/XAUUSD_M15_*_ELEV8.csv"])
    p.add_argument("--m1-charts", nargs="+", default=["data/XAUUSD_M1_*_ELEV8.csv"])
    p.add_argument("--output", default="generated/live_provider_all.txt")
    p.add_argument("--alias-output", default="generated/adaptive_self_m15_trend_pullback.txt")
    p.add_argument("--start-date", default="2025-01-01")
    p.add_argument("--end-date", default=None)
    p.add_argument("--ema-fast", type=int, default=21)
    p.add_argument("--ema-slow", type=int, default=55)
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--min-atr", type=float, default=0.30)
    p.add_argument("--max-atr", type=float, default=80.00)
    p.add_argument("--same-side-spacing-minutes", type=int, default=60)
    p.add_argument("--max-signals-per-day", type=int, default=20)
    p.add_argument("--entry-offset", type=float, default=1.00)
    # ATR-multiple sizing. Defaults reproduce the fixed-$ `better` distances at
    # a quiet-regime ATR of ~$2.22 (0.9*2.22=2.0 range, 1.6*2.22=3.5 sl gap,
    # 1.8*2.22=4.0 tp1, 3.2*2.22=7.1 tp2, 5.4*2.22=12.0 tp3).
    p.add_argument("--range-atr", type=float, default=0.90)
    p.add_argument("--sl-gap-atr", type=float, default=1.60)
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
