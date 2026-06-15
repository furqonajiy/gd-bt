#!/usr/bin/env python3
"""Generate volatility-adaptive (ATR-scaled) XAUUSD M15 MEAN-REVERSION signals.

A deliberately *different* mechanism from the range-fade `self` generator. The
range-fade generator (`generate_adaptive_self_signals.py`) signals WITH the M15
EMA trend on a shallow pullback; this one signals AGAINST a stretched move:

  * Compute an M15 EMA (the "mean") and the M15 ATR.
  * When price has run >= `--band-atr` * ATR ABOVE the mean (overextended up),
    SELL back toward the mean. Symmetric BUY when price is `--band-atr` * ATR
    BELOW the mean (overextended down).
  * Stop is placed `--sl-atr` * ATR BEYOND the stretched extreme (above the
    high for a SELL, below the low for a BUY) so a continued blow-off stops it
    out; targets walk back toward / through the mean as ATR multiples.

Everything that has a dollar dimension is an ATR MULTIPLE of this bar's M15 ATR,
so the stop/target widths self-scale with the regime exactly like the existing
generator: a quiet 2021 bar (~$2 ATR) yields ~$2-5 stops, a 2026 parabolic bar
(~$10-13 ATR) yields ~$15-30 stops, with no fixed-dollar assumptions.

Output is byte-identical in format to the existing feeds (a `YYYY-MM-DD GMT+3`
date header, then `N. SIDE XAUUSD <e1> - <e2> SL <sl> TP1 .. TP2 .. TP3 .. time`),
so it drops straight into `tools/sweep_self_limit.py` / `backtest_explicit.py`.
"""
from __future__ import annotations

import argparse
import glob
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
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


def _add_indicators(m15: pd.DataFrame, ema_period: int, atr_period: int) -> pd.DataFrame:
    out = m15.copy()
    # The "mean" price is a single configurable EMA; distance from it (in ATRs)
    # is the stretch we fade.
    out["ema"] = out["close"].ewm(span=ema_period, adjust=False).mean()

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
    """Lay a LIMIT ladder that fades FURTHER into the stretch.

    For a SELL (price overextended up) the entries sit ABOVE the close so a
    little more push-up fills us at a better price; the stop sits beyond the
    extreme high and the targets walk back DOWN toward the mean. BUY is the
    mirror. This matches the `self` generator's r1/r2 ladder shape so the engine
    treats the legs identically.
    """
    close = round(float(row.close), 2)
    high = round(float(row.high), 2)
    low = round(float(row.low), 2)

    if side == "BUY":
        # Overextended DOWN: buy the snap-back. Fade a touch lower.
        r1 = round(close - entry_offset, 2)
        r2 = round(r1 - range_width, 2)
        # Stop BEYOND the stretched low (the extreme), not just below the ladder.
        sl = round(min(r2, low) - sl_gap, 2)
        tp1 = round(r1 + tp1_distance, 2)
        tp2 = round(r1 + tp2_distance, 2)
        tp3 = round(r1 + tp3_distance, 2)
    else:
        # Overextended UP: sell the snap-back. Fade a touch higher.
        r1 = round(close + entry_offset, 2)
        r2 = round(r1 + range_width, 2)
        sl = round(max(r2, high) + sl_gap, 2)
        tp1 = round(r1 - tp1_distance, 2)
        tp2 = round(r1 - tp2_distance, 2)
        tp3 = round(r1 - tp3_distance, 2)

    return SignalRow(row.signal_time.to_pydatetime(), side, r1, r2, sl, tp1, tp2, tp3)


def generate_signals(args: argparse.Namespace) -> list[SignalRow]:
    m15 = _load_working_m15(args.m15_charts, args.m1_charts)
    data = _add_indicators(m15, args.ema_period, args.atr_period)
    # The signal is acted on at the NEXT bar's open (close of bar i confirms the
    # stretch), so stamp it 15 minutes ahead like the existing generator.
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

        signal_date = signal_time.date()
        if daily_count[signal_date] >= args.max_signals_per_day:
            continue

        # Stretch = how many ATRs the close sits away from the EMA mean.
        stretch = (float(row.close) - float(row.ema)) / atr if atr > 0 else 0.0

        side: str | None = None
        if stretch >= args.band_atr:
            # Overextended UP -> fade with a SELL back toward the mean.
            side = "SELL"
        elif stretch <= -args.band_atr:
            # Overextended DOWN -> fade with a BUY back toward the mean.
            side = "BUY"

        if side is None:
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
    shift = timedelta(hours=source_tz_offset - CHART_TZ_OFFSET)

    grouped: dict[str, list[tuple[datetime, SignalRow]]] = defaultdict(list)
    for signal in signals:
        disp = signal.signal_time + shift
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
    p = argparse.ArgumentParser(description="Generate volatility-adaptive (ATR-scaled) XAUUSD M15 mean-reversion signals.")
    p.add_argument("--m15-charts", nargs="+", default=["data/XAUUSD_M15_*_ELEV8.csv"])
    p.add_argument("--m1-charts", nargs="+", default=["data/XAUUSD_M1_*_ELEV8.csv"])
    p.add_argument("--output", default="generated/adaptive_meanrev.txt")
    p.add_argument("--alias-output", default=None)
    p.add_argument("--start-date", default="2021-11-01")
    p.add_argument("--end-date", default=None)
    p.add_argument("--ema-period", type=int, default=34, help="M15 EMA used as the mean to fade back to.")
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--min-atr", type=float, default=0.30, help="ATR filter band (lower): skip dead/illiquid bars.")
    p.add_argument("--max-atr", type=float, default=80.00, help="ATR filter band (upper): skip data glitches.")
    p.add_argument("--same-side-spacing-minutes", type=int, default=60)
    p.add_argument("--max-signals-per-day", type=int, default=20)
    p.add_argument("--entry-offset", type=float, default=1.00, help="Fixed $ nudge of entry #1 deeper into the stretch.")
    # Trigger: how stretched (in ATRs from the EMA) before we fade.
    p.add_argument("--band-atr", type=float, default=1.60,
                   help="Fade when |close - EMA| >= band-atr * ATR (the overextension threshold).")
    # ATR-multiple sizing. Tuned for a comparable signal count to the ~21-24k
    # range-fade feed while keeping a mean-reversion R:R (SL beyond the extreme,
    # targets walking back toward/through the mean).
    p.add_argument("--range-atr", type=float, default=0.60)
    p.add_argument("--sl-atr", type=float, default=0.90,
                   help="Stop placed sl-atr * ATR BEYOND the stretched extreme.")
    p.add_argument("--tp1-atr", type=float, default=1.40)
    p.add_argument("--tp2-atr", type=float, default=2.60)
    p.add_argument("--tp3-atr", type=float, default=4.20)
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
