#!/usr/bin/env python3
"""Filter provider-style XAUUSD signal files by tested time/side presets.

This tool keeps the original signal levels, SL, and TP levels unchanged.  It only
filters which signals are allowed through, based on chart-time hour and side.

The default preset is ``high_growth_hour_side``.  It was selected from the
uploaded provider signal file after full-chart validation because it improved
win rate and allowed much higher risk while keeping drawdown below 50%.

Important: this script converts the input source timezone to chart timezone
GMT+3 and writes the output as ``GMT+3`` so it can be backtested directly.
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


HEADER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+GMT\s*([+-]\d+)")
SIGNAL_RE = re.compile(
    r"^\s*(\d+)\.\s*(BUY|SELL)\s+XAUUSD\s+"
    r"([0-9.]+)\s*-\s*([0-9.]+)\s+SL\s+([0-9.]+)\s+"
    r"TP1\s+([0-9.]+)\s+TP2\s+([0-9.]+)\s+TP3\s+([0-9.]+)\s+(.+?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SignalRow:
    source_date: str
    source_id: int
    chart_time: datetime
    side: str
    r1: str
    r2: str
    sl: str
    tp1: str
    tp2: str
    tp3: str


def _parse_time(text: str):
    text = text.strip().upper()
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    raise ValueError(f"Unsupported signal time: {text}")


def parse_provider_signals(path: Path) -> list[SignalRow]:
    source_date: str | None = None
    source_tz_offset = 7
    rows: list[SignalRow] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        header = HEADER_RE.match(line.strip())
        if header:
            source_date = header.group(1)
            source_tz_offset = int(header.group(2))
            continue
        match = SIGNAL_RE.match(line.strip())
        if not match or source_date is None:
            continue
        source_id = int(match.group(1))
        side = match.group(2).upper()
        src_dt = datetime.combine(
            datetime.strptime(source_date, "%Y-%m-%d").date(),
            _parse_time(match.group(9)),
        )
        chart_time = src_dt + timedelta(hours=3 - source_tz_offset)
        rows.append(
            SignalRow(
                source_date=source_date,
                source_id=source_id,
                chart_time=chart_time,
                side=side,
                r1=match.group(3),
                r2=match.group(4),
                sl=match.group(5),
                tp1=match.group(6),
                tp2=match.group(7),
                tp3=match.group(8),
            )
        )
    return rows


def keep_signal(row: SignalRow, preset: str) -> bool:
    hour = row.chart_time.hour
    month = row.chart_time.month

    if preset == "all":
        return True

    if preset == "no_bad_hours":
        return hour not in {5, 6, 8, 12, 13, 19, 21, 22}

    if preset == "best_hours":
        return hour in {9, 10, 11, 14, 15, 16, 17, 18, 20}

    if preset == "high_growth_hour_side":
        buy_hours = {4, 9, 10, 11, 12, 13, 14, 17, 18, 20}
        sell_hours = {7, 8, 9, 10, 11, 14, 15, 16, 17, 18, 19}
        return (row.side == "BUY" and hour in buy_hours) or (row.side == "SELL" and hour in sell_hours)

    if preset == "research_month_hour_side":
        # Overfit/research-only: excludes historically weak calendar months in
        # the uploaded sample. Do not use live without more out-of-sample data.
        return month not in {7, 11, 12} and keep_signal(row, "high_growth_hour_side")

    raise ValueError(f"Unknown preset: {preset}")


def _time_ampm(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def write_signals(rows: list[SignalRow], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[SignalRow]] = defaultdict(list)
    for row in rows:
        grouped[row.chart_time.strftime("%Y-%m-%d")].append(row)

    lines: list[str] = []
    for date_key in sorted(grouped):
        lines.append(f"{date_key} GMT+3")
        for idx, row in enumerate(sorted(grouped[date_key], key=lambda r: r.chart_time), start=1):
            lines.append(
                f"{idx}. {row.side} XAUUSD {row.r1} - {row.r2} "
                f"SL {row.sl} TP1 {row.tp1} TP2 {row.tp2} TP3 {row.tp3} "
                f"{_time_ampm(row.chart_time)}"
            )
        lines.append("")
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Filter provider XAUUSD signals with tested presets.")
    parser.add_argument("--signals", required=True, help="Input provider signal file, usually GMT+7.")
    parser.add_argument("--output", required=True, help="Filtered output signal file, written as GMT+3.")
    parser.add_argument(
        "--preset",
        default="high_growth_hour_side",
        choices=["all", "no_bad_hours", "best_hours", "high_growth_hour_side", "research_month_hour_side"],
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rows = parse_provider_signals(Path(args.signals))
    kept = [row for row in rows if keep_signal(row, args.preset)]
    write_signals(kept, Path(args.output))
    print(f"Input signals:  {len(rows):,}")
    print(f"Kept signals:   {len(kept):,}")
    print(f"Preset:         {args.preset}")
    print(f"Output:         {Path(args.output).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
