#!/usr/bin/env python3
"""Filter a date-header signal feed by a generated regime calendar.

This is the calendar-aware replacement for workflow regexes such as
``SIG_RE='^2026-'``.  It keeps a feed block when the block's header date appears
in the calendar with the requested regime label.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading.strategy.regime_calendar import normalize_regime  # noqa: E402

DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")
SIGNAL_RE = re.compile(r"^\d+\.\s")


def allowed_dates_from_calendar(text: str, regimes: set[str], layer: str) -> set[str]:
    """Read calendar CSV text and return date strings matching ``regimes``."""
    wanted = {normalize_regime(r) for r in regimes}
    out: set[str] = set()
    for row in csv.DictReader(text.splitlines()):
        date = (row.get("date") or "").strip()
        label = normalize_regime((row.get(layer) or "").strip())
        if date and label in wanted:
            out.add(date)
    return out


def filter_feed_by_dates(text: str, allowed_dates: set[str]) -> str:
    """Return only feed blocks whose header date is in ``allowed_dates``."""
    out: list[str] = []
    keep = False
    for line in text.splitlines():
        m = DATE_RE.match(line)
        if m:
            keep = m.group(1) in allowed_dates
        if keep:
            out.append(line)
    return "\n".join(out).strip("\n") + ("\n" if out else "")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--signals", required=True, help="Input signal feed.")
    p.add_argument("--calendar", required=True, help="CSV from tools/build_regime_calendar.py.")
    p.add_argument("--regime", action="append", required=True,
                   help="Regime to keep. Repeat to combine regimes.")
    p.add_argument("--layer", default="sweep_regime",
                   choices=["sweep_regime", "behavior_regime", "old_threshold_regime"],
                   help="Calendar label column to use.")
    p.add_argument("--output", required=True, help="Filtered feed output.")
    args = p.parse_args(argv)

    calendar_text = Path(args.calendar).read_text()
    allowed_dates = allowed_dates_from_calendar(calendar_text, set(args.regime), args.layer)
    if not allowed_dates:
        raise SystemExit(f"no calendar dates matched regime={args.regime} layer={args.layer}")

    feed_text = Path(args.signals).read_text()
    filtered = filter_feed_by_dates(feed_text, allowed_dates)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(filtered)

    n_days = sum(1 for ln in filtered.splitlines() if DATE_RE.match(ln))
    n_signals = sum(1 for ln in filtered.splitlines() if SIGNAL_RE.match(ln))
    regimes = ",".join(args.regime)
    print(f"filtered {args.signals} -> {args.output}: {n_days} days, {n_signals} signals "
          f"for {regimes} via {args.layer} ({len(allowed_dates)} calendar days)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
