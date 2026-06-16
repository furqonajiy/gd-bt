#!/usr/bin/env python3
"""Slice a Victor-style signals feed to a date range.

The feed is date-header blocks::

    2024-04-22 GMT+7
    1. SELL XAUUSD ...
    2. BUY  XAUUSD ...

    2024-04-23 GMT+7
    ...

A *block* is a ``YYYY-MM-DD ...`` header line plus the numbered signal lines
that follow it (up to the next header / blank run). We keep a block when its
header date falls in ``[--start-date, --end-date]`` (inclusive, ``YYYY-MM-DD``).

Why this exists: the per-regime Victor sweep must hold out the last N months of
*that regime's* signals as OOS. ``sweep.split_train_validate`` slices the last N
months of whatever feed it is given, so feeding it a regime-sliced file (e.g.
2025-only for R3) makes the OOS window correct instead of leaking the whole
feed's 2026 tail into a 2025 sweep.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")


def slice_feed(text: str, start: str, end: str) -> str:
    """Return only the date blocks whose header date is in [start, end]."""
    out: list[str] = []
    keep = False
    for line in text.splitlines():
        m = _DATE_RE.match(line)
        if m:
            keep = start <= m.group(1) <= end
        if keep:
            out.append(line)
    # one trailing newline, no leading blank lines
    return "\n".join(out).strip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--signals", required=True, help="input feed path")
    p.add_argument("--start-date", required=True, help="YYYY-MM-DD inclusive")
    p.add_argument("--end-date", required=True, help="YYYY-MM-DD inclusive")
    p.add_argument("--output", required=True, help="output feed path")
    args = p.parse_args(argv)

    text = Path(args.signals).read_text()
    sliced = slice_feed(text, args.start_date, args.end_date)
    Path(args.output).write_text(sliced)
    n_blocks = sum(1 for ln in sliced.splitlines() if _DATE_RE.match(ln))
    n_signals = sum(1 for ln in sliced.splitlines() if re.match(r"^\d+\.\s", ln))
    print(f"sliced {args.signals} -> {args.output}: "
          f"{n_blocks} days, {n_signals} signals in [{args.start_date}..{args.end_date}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
