#!/usr/bin/env python3
"""Continuously filter provider signals for live execution.

Recommended live architecture:

1. Telegram listener writes raw provider signals to ``signals.txt``.
2. This process reads ``signals.txt`` and writes a filtered GMT+3 signal file.
3. The live auto-executor reads only the filtered file.

This keeps backtest/live parity simple: the exact same filtered signal file can
be passed to ``tools/backtest_configurable.py`` and to the live runner.

Watch mode is intentionally quiet: it prints only when a new signal is kept by
the filter, not on every polling interval.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading.strategy.provider_filter import decide_provider_signal_filter  # noqa: E402

HEADER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+GMT\s*([+-]\d+)")
SIGNAL_RE = re.compile(
    r"^\s*(\d+)\.\s*(BUY|SELL)\s+XAUUSD\s+"
    r"([0-9.]+)\s*-\s*([0-9.]+)\s+SL\s+([0-9.]+)\s+"
    r"TP1\s+([0-9.]+)\s+TP2\s+([0-9.]+)\s+TP3\s+([0-9.]+)\s+(.+?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProviderSignal:
    source_date: str
    source_tz: int
    source_id: int
    source_time: datetime
    chart_time: datetime
    side: str
    r1: str
    r2: str
    sl: str
    tp1: str
    tp2: str
    tp3: str
    filter_reason: str


def _parse_time(text: str):
    text = text.strip().upper()
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    raise ValueError(f"Unsupported signal time: {text}")


def _time_ampm(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _signal_key(sig: ProviderSignal) -> tuple:
    return (
        sig.source_date,
        sig.source_id,
        sig.side,
        sig.r1,
        sig.r2,
        sig.sl,
        sig.tp1,
        sig.tp2,
        sig.tp3,
        sig.chart_time.isoformat(sep=" "),
    )


def _describe_signal(sig: ProviderSignal) -> str:
    # Lead with the engine-style signal key — chart date + the provider day-id
    # that is emitted as `N.` in the filtered feed — so the operator can tell
    # at a glance which signal this is (#10 vs #11) and match it to
    # positions.json / MT5 order comments, which use the same key.
    return (
        f"{sig.chart_time:%Y-%m-%d}#{sig.source_id:02d} GMT+3 {sig.side} XAUUSD "
        f"{sig.r1}-{sig.r2} SL {sig.sl} TP1 {sig.tp1} TP2 {sig.tp2} TP3 {sig.tp3} "
        f"at {_time_ampm(sig.chart_time)} | {sig.filter_reason}"
    )


def _input_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def parse_and_filter(input_path: Path, preset: str) -> tuple[list[ProviderSignal], int]:
    if not input_path.exists():
        return [], 0
    current_date: str | None = None
    current_tz = 7
    kept: list[ProviderSignal] = []
    total = 0

    for line in input_path.read_text(encoding="utf-8").splitlines():
        header = HEADER_RE.match(line.strip())
        if header:
            current_date = header.group(1)
            current_tz = int(header.group(2))
            continue
        match = SIGNAL_RE.match(line.strip())
        if not match or current_date is None:
            continue
        total += 1
        side = match.group(2).upper()
        source_time = datetime.combine(
            datetime.strptime(current_date, "%Y-%m-%d").date(),
            _parse_time(match.group(9)),
        )
        decision = decide_provider_signal_filter(
            side=side,
            source_time=source_time,
            preset=preset,
            source_tz_offset=current_tz,
            chart_tz_offset=3,
        )
        if not decision.keep:
            continue
        kept.append(
            ProviderSignal(
                source_date=current_date,
                source_tz=current_tz,
                source_id=int(match.group(1)),
                source_time=source_time,
                chart_time=decision.chart_time,
                side=side,
                r1=match.group(3),
                r2=match.group(4),
                sl=match.group(5),
                tp1=match.group(6),
                tp2=match.group(7),
                tp3=match.group(8),
                filter_reason=decision.reason,
            )
        )
    return kept, total


def write_filtered(signals: list[ProviderSignal], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[ProviderSignal]] = defaultdict(list)
    for sig in signals:
        grouped[sig.chart_time.strftime("%Y-%m-%d")].append(sig)

    lines: list[str] = []
    for date_key in sorted(grouped):
        lines.append(f"{date_key} GMT+3")
        # Emit the provider's own per-day number (source_id), not a fresh 1..N
        # count. The engine derives day_id -> signal_key from this prefix, so
        # keeping it aligns signal_key with signals.txt / the Telegram channel
        # for operator cross-reference; gaps (e.g. only 2 and 3 kept) are fine.
        for sig in sorted(grouped[date_key], key=lambda s: s.chart_time):
            lines.append(
                f"{sig.source_id}. {sig.side} XAUUSD {sig.r1} - {sig.r2} "
                f"SL {sig.sl} TP1 {sig.tp1} TP2 {sig.tp2} TP3 {sig.tp3} "
                f"{_time_ampm(sig.chart_time)}"
            )
        lines.append("")

    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    tmp.replace(output_path)


def run_once(input_path: Path, output_path: Path, preset: str) -> tuple[int, int, list[ProviderSignal]]:
    kept, total = parse_and_filter(input_path, preset)
    write_filtered(kept, output_path)
    return total, len(kept), kept


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Continuously filter provider signals for live execution.")
    p.add_argument("--input", default="signals.txt", help="Raw provider signal file written by Telegram listener.")
    p.add_argument("--output", default="generated/live_provider_high_growth.txt", help="Filtered GMT+3 signal file for backtest/live auto.")
    p.add_argument("--preset", default="high_growth_hour_side", choices=["all", "no_bad_hours", "best_hours", "high_growth_hour_side", "research_month_hour_side"])
    p.add_argument("--watch", action="store_true", help="Keep filtering every --interval seconds.")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument(
        "--print-existing-on-start",
        action="store_true",
        help="In watch mode, also print existing kept signals at startup. Default is quiet startup.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_path = Path(args.input)
    output_path = Path(args.output)

    if not args.watch:
        total, kept_count, _kept = run_once(input_path, output_path, args.preset)
        print(
            f"[provider-filter] input={total:,} kept={kept_count:,} preset={args.preset} output={output_path}",
            flush=True,
        )
        return 0

    seen: set[tuple] = set()
    last_mtime: int | None = None
    first_run = True
    print(
        f"[provider-filter] watching {input_path} -> {output_path} | preset={args.preset} | quiet until a new kept signal appears",
        flush=True,
    )

    while True:
        current_mtime = _input_mtime_ns(input_path)
        if first_run or current_mtime != last_mtime:
            total, kept_count, kept = run_once(input_path, output_path, args.preset)
            kept_keys = {_signal_key(sig) for sig in kept}

            if first_run:
                seen = kept_keys
                if args.print_existing_on_start:
                    for sig in kept:
                        print(f"[provider-filter][KEPT] {_describe_signal(sig)}", flush=True)
                print(
                    f"[provider-filter] ready | input={total:,} kept={kept_count:,} output={output_path}",
                    flush=True,
                )
                first_run = False
            else:
                new_kept = [sig for sig in kept if _signal_key(sig) not in seen]
                for sig in new_kept:
                    print(f"[provider-filter][NEW KEPT] {_describe_signal(sig)}", flush=True)
                seen = kept_keys

            last_mtime = current_mtime

        time.sleep(max(0.5, float(args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())