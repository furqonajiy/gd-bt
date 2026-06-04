#!/usr/bin/env python3
"""Generate research-only self signals from MT5 M1 chart exports."""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading import (  # noqa: E402
    CsvChartSource,
    RejectionSignalConfig,
    format_generated_signals,
    generate_rejection_signals,
    iter_bars,
)


def _expand_chart_paths(patterns: list[str]) -> list[Path]:
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


def _parse_date(value: str | None) -> datetime | None:
    return datetime.strptime(value, "%Y-%m-%d") if value else None


def _hour_or_none(raw: str) -> int | None:
    value = int(raw)
    if value < 0:
        return None
    if value > 24:
        raise argparse.ArgumentTypeError("hour must be -1, or 0..24")
    return value


def _spread_or_none(raw: str) -> int | None:
    value = int(raw)
    if value < 0:
        return None
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate closed-candle rejection-zone self signals for trailing-open research."
    )
    p.add_argument("--charts", required=True, nargs="+", help="MT5 M1 chart CSV path(s), supports globs.")
    p.add_argument("--output", required=True, help="Output signal file, e.g. generated/self_rejection_v1.txt.")
    p.add_argument("--start-date", default=None, metavar="YYYY-MM-DD")
    p.add_argument("--end-date", default=None, metavar="YYYY-MM-DD")
    p.add_argument("--lookback-bars", type=int, default=20)
    p.add_argument("--min-wick", type=float, default=1.0)
    p.add_argument("--min-bar-range", type=float, default=1.5)
    p.add_argument("--wick-body-ratio", type=float, default=1.2)
    p.add_argument("--zone-buffer", type=float, default=0.25)
    p.add_argument("--zone-size", type=float, default=1.0)
    p.add_argument("--cooldown-minutes", type=int, default=20)
    p.add_argument("--same-zone-cooldown-minutes", type=int, default=120)
    p.add_argument("--max-spread-points", type=_spread_or_none, default=35, help="-1 disables spread filter.")
    p.add_argument("--session-start-hour", type=_hour_or_none, default=7, help="-1 disables session filter.")
    p.add_argument("--session-end-hour", type=_hour_or_none, default=22, help="-1 disables session filter.")
    p.add_argument("--entry-range-width", type=float, default=2.0)
    p.add_argument("--sl-distance", type=float, default=5.0)
    p.add_argument("--tp1-distance", type=float, default=10.0)
    p.add_argument("--tp2-distance", type=float, default=20.0)
    p.add_argument("--tp3-distance", type=float, default=40.0)
    p.add_argument("--price-digits", type=int, default=2)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    chart = CsvChartSource(_expand_chart_paths(args.charts))
    df = chart.dataframe

    start = _parse_date(args.start_date)
    end = _parse_date(args.end_date)
    if end is not None:
        end = end + timedelta(days=1)

    if start is not None:
        df = df[df["time"] >= start]
    if end is not None:
        df = df[df["time"] < end]

    config = RejectionSignalConfig(
        lookback_bars=args.lookback_bars,
        min_wick=args.min_wick,
        min_bar_range=args.min_bar_range,
        wick_body_ratio=args.wick_body_ratio,
        zone_buffer=args.zone_buffer,
        zone_size=args.zone_size,
        cooldown_minutes=args.cooldown_minutes,
        same_zone_cooldown_minutes=args.same_zone_cooldown_minutes,
        max_spread_points=args.max_spread_points,
        session_start_hour=args.session_start_hour,
        session_end_hour=args.session_end_hour,
        entry_range_width=args.entry_range_width,
        sl_distance=args.sl_distance,
        tp1_distance=args.tp1_distance,
        tp2_distance=args.tp2_distance,
        tp3_distance=args.tp3_distance,
        price_digits=args.price_digits,
    )

    signals = generate_rejection_signals(iter_bars(df), config)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        format_generated_signals(signals, source_tz_offset=3, price_digits=args.price_digits),
        encoding="utf-8",
    )

    side_counts = Counter(signal.side for signal in signals)
    summary = {
        "output": str(output),
        "signals": len(signals),
        "buy": side_counts.get("BUY", 0),
        "sell": side_counts.get("SELL", 0),
        "chart_start": str(df["time"].iloc[0]) if not df.empty else None,
        "chart_end": str(df["time"].iloc[-1]) if not df.empty else None,
        "config": config.__dict__,
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
