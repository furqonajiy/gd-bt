#!/usr/bin/env python3
"""
Filter provider-style XAUUSD signal archive by side + signal hour.

Usage:
  python tools/filter_signals_hour_side.py \
    --input signals/self_m15_archive.txt \
    --output signals/self_m15_tick_best_filtered_top_profit.txt \
    --keys SELL_11 BUY_17 SELL_19 SELL_08 BUY_07 BUY_09 SELL_17 BUY_05 SELL_16 SELL_10 SELL_04 BUY_21 SELL_00 BUY_19 SELL_21 SELL_22 SELL_06 BUY_04 SELL_09

The input must look like:
  2026-05-22 GMT+3
  1. BUY XAUUSD ... 5:30 PM
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+GMT[+-]\d+\s*$")
SIGNAL_RE = re.compile(
    r"^\s*(\d+)\.\s+(BUY|SELL)\s+XAUUSD\b.*?\s+(\d{1,2}):(\d{2})\s*(AM|PM)\s*$",
    re.IGNORECASE,
)


def to_hour_24(hour: int, ampm: str) -> int:
    ampm = ampm.upper()
    if ampm == "AM":
        return 0 if hour == 12 else hour
    return 12 if hour == 12 else hour + 12


def filter_signals(input_path: Path, output_path: Path, allowed_keys: set[str]) -> tuple[int, int]:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    current_header: str | None = None
    kept_by_day: list[str] = []
    total = 0
    kept = 0
    output_lines: list[str] = []

    def flush_day() -> None:
        nonlocal kept_by_day, current_header, output_lines
        if current_header and kept_by_day:
            if output_lines:
                output_lines.append("")
            output_lines.append(current_header)
            # Renumber inside the filtered backtest file.
            # If you prefer to preserve original day IDs/magic, replace this loop with:
            # output_lines.extend(kept_by_day)
            for i, line in enumerate(kept_by_day, start=1):
                output_lines.append(re.sub(r"^\s*\d+\.", f"{i}.", line, count=1))
        kept_by_day = []

    for raw_line in input_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if DATE_RE.match(line):
            flush_day()
            current_header = line
            continue

        match = SIGNAL_RE.match(line)
        if not match:
            continue

        total += 1
        side = match.group(2).upper()
        hour = to_hour_24(int(match.group(3)), match.group(5))
        key = f"{side}_{hour:02d}"

        if key in allowed_keys:
            kept += 1
            kept_by_day.append(line)

    flush_day()
    output_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    return total, kept


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--keys", nargs="+", required=True)
    args = parser.parse_args()

    allowed_keys = {key.upper() for key in args.keys}
    total, kept = filter_signals(args.input, args.output, allowed_keys)

    print(f"Input signals : {total}")
    print(f"Kept signals  : {kept}")
    print(f"Output file   : {args.output}")


if __name__ == "__main__":
    main()
