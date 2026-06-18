#!/usr/bin/env python3
"""Compose one chronological feed from regime-specific signal feeds."""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.filter_signals_by_regime import (  # noqa: E402
    DATE_RE,
    SIGNAL_RE,
    allowed_dates_from_calendar,
)
from xauusd_trading.strategy.regime_calendar import normalize_regime  # noqa: E402


@dataclass(frozen=True)
class ComposeStats:
    regime: str
    source: str
    calendar_days: int
    feed_days: int
    signal_count: int


def parse_regime_feed(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError(f"expected REGIME=feed_path, got {value!r}")
    regime, path = value.split("=", 1)
    regime = normalize_regime(regime.strip())
    path = path.strip()
    if not regime or not path:
        raise ValueError(f"expected REGIME=feed_path, got {value!r}")
    return regime, Path(path)


def feed_blocks_by_date(text: str) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {}
    current_date: str | None = None
    current: list[str] = []

    def flush() -> None:
        if current_date is None:
            return
        block = list(current)
        while block and not block[-1].strip():
            block.pop()
        if block:
            blocks[current_date] = block

    for line in text.splitlines():
        m = DATE_RE.match(line)
        if m:
            flush()
            current_date = m.group(1)
            current = [line]
        elif current_date is not None:
            current.append(line)
    flush()
    return blocks


def compose_regime_feeds(
        calendar_text: str,
        regime_feeds: dict[str, tuple[str, str]],
        layer: str = "sweep_regime") -> tuple[str, list[ComposeStats]]:
    dated_blocks: dict[str, tuple[str, list[str]]] = {}
    stats: list[ComposeStats] = []

    for regime, (source, feed_text) in regime_feeds.items():
        allowed_dates = allowed_dates_from_calendar(calendar_text, {regime}, layer)
        blocks = feed_blocks_by_date(feed_text)
        selected_dates = sorted(set(blocks) & allowed_dates)
        signal_count = 0
        for date in selected_dates:
            if date in dated_blocks:
                other_regime, _ = dated_blocks[date]
                raise ValueError(f"date {date} selected by both {other_regime} and {regime}")
            block = blocks[date]
            dated_blocks[date] = (regime, block)
            signal_count += sum(1 for line in block if SIGNAL_RE.match(line))
        stats.append(
            ComposeStats(
                regime=regime,
                source=source,
                calendar_days=len(allowed_dates),
                feed_days=len(selected_dates),
                signal_count=signal_count,
            )
        )

    pieces = ["\n".join(block) for _date, (_regime, block) in sorted(dated_blocks.items())]
    text = "\n\n".join(pieces)
    return (text + "\n") if text else "", stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--calendar", required=True, help="CSV from tools/build_regime_calendar.py.")
    p.add_argument("--regime-feed", action="append", required=True,
                   help="Mapping REGIME=feed_path. Repeat for each regime to include.")
    p.add_argument("--layer", default="sweep_regime",
                   choices=["sweep_regime", "behavior_regime", "old_threshold_regime"])
    p.add_argument("--output", required=True, help="Composed signal feed output.")
    p.add_argument("--allow-empty-regime", action="store_true",
                   help="Allow a mapped regime to contribute zero feed days.")
    args = p.parse_args(argv)

    calendar_text = Path(args.calendar).read_text()
    regime_feeds: dict[str, tuple[str, str]] = {}
    for item in args.regime_feed:
        regime, path = parse_regime_feed(item)
        if regime in regime_feeds:
            raise SystemExit(f"duplicate regime mapping: {regime}")
        regime_feeds[regime] = (str(path), path.read_text())

    try:
        text, stats = compose_regime_feeds(calendar_text, regime_feeds, args.layer)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    empty = [s.regime for s in stats if s.feed_days == 0]
    if empty and not args.allow_empty_regime:
        raise SystemExit(f"mapped regime(s) produced no feed days: {', '.join(empty)}")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text)

    total_days = sum(s.feed_days for s in stats)
    total_signals = sum(s.signal_count for s in stats)
    print(f"wrote {output}: {total_days} days, {total_signals} signals via {args.layer}")
    for s in stats:
        print(
            f"  {s.regime}: {s.feed_days}/{s.calendar_days} days, "
            f"{s.signal_count} signals from {s.source}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
