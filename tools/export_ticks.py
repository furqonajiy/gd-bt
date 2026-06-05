#!/usr/bin/env python3
from __future__ import annotations

import argparse
import calendar
import csv
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading import Mt5Connection, POINT_VALUE  # noqa: E402


UTC = timezone.utc
FIELDNAMES = [
    "<DATE>",
    "<TIME>",
    "<TIME_MSC>",
    "<BID>",
    "<ASK>",
    "<LAST>",
    "<VOLUME>",
    "<VOLUME_REAL>",
    "<FLAGS>",
    "<SPREAD>",
]
HEADER_LINE = "\t".join(FIELDNAMES)


def _parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def _month_start(value: datetime) -> datetime:
    return datetime(value.year, value.month, 1)


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return datetime(value.year + 1, 1, 1)
    return datetime(value.year, value.month + 1, 1)


def _iter_months(start: datetime, end: datetime) -> Iterable[tuple[datetime, datetime]]:
    cur = _month_start(start)
    while cur < end:
        nxt = min(_next_month(cur), end)
        yield max(cur, start), nxt
        cur = _next_month(cur)


def _iter_chunks(start: datetime, end: datetime, chunk_hours: int) -> Iterable[tuple[datetime, datetime]]:
    cur = start
    step = timedelta(hours=chunk_hours)
    while cur < end:
        nxt = min(cur + step, end)
        yield cur, nxt
        cur = nxt


def _chart_to_mt5_epoch(chart_naive: datetime, server_offset_hours: int) -> int:
    shift = timedelta(hours=3 - server_offset_hours)
    broker_naive = chart_naive - shift
    return calendar.timegm(broker_naive.timetuple())


def _mt5_msc_to_chart_time(epoch_msc: int, server_offset_hours: int) -> datetime:
    shift = timedelta(hours=3 - server_offset_hours)
    broker_naive = datetime.fromtimestamp(int(epoch_msc) / 1000.0, UTC).replace(tzinfo=None)
    return broker_naive + shift


def _is_header_only_tick_file(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return False
    return len(lines) == 1 and lines[0].strip() == HEADER_LINE


def _tick_rows(ticks, server_offset_hours: int) -> Iterable[dict[str, str]]:
    for tick in ticks:
        bid = float(tick["bid"])
        ask = float(tick["ask"])
        last = float(tick["last"])
        volume = int(tick["volume"])
        time_msc = int(tick["time_msc"])
        flags = int(tick["flags"])
        volume_real = float(tick["volume_real"]) if "volume_real" in tick.dtype.names else 0.0

        chart_dt = _mt5_msc_to_chart_time(time_msc, server_offset_hours)
        spread_points = round((ask - bid) / POINT_VALUE) if ask > 0 and bid > 0 else ""

        yield {
            "<DATE>": chart_dt.strftime("%Y.%m.%d"),
            "<TIME>": chart_dt.strftime("%H:%M:%S.%f")[:-3],
            "<TIME_MSC>": str(time_msc),
            "<BID>": f"{bid:.2f}" if bid > 0 else "",
            "<ASK>": f"{ask:.2f}" if ask > 0 else "",
            "<LAST>": f"{last:.2f}" if last > 0 else "",
            "<VOLUME>": str(volume),
            "<VOLUME_REAL>": f"{volume_real:.8f}".rstrip("0").rstrip("."),
            "<FLAGS>": str(flags),
            "<SPREAD>": str(spread_points),
        }


def _write_rows(path: Path, rows: Iterable[dict[str, str]], *, write_header: bool) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if write_header else "a"
    count = 0
    with path.open(mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, delimiter="\t")
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def _export_month(conn: Mt5Connection, args: argparse.Namespace, month_start: datetime, month_end: datetime) -> int:
    mt5 = conn.mt5
    out_path = Path(args.output_dir) / f"{args.symbol}_TICK_{month_start:%Y%m}_ELEV8.csv"

    if _is_header_only_tick_file(out_path):
        out_path.unlink()
        print(f"[empty] removed header-only tick file: {out_path}")

    if out_path.exists() and out_path.stat().st_size > 0 and not args.overwrite:
        print(f"[skip] {out_path} exists; use --overwrite to replace.")
        return 0
    if out_path.exists() and args.overwrite:
        out_path.unlink()

    total = 0
    wrote_header = False

    for chunk_start, chunk_end in _iter_chunks(month_start, month_end, args.chunk_hours):
        start_epoch = _chart_to_mt5_epoch(chunk_start, args.mt5_server_offset)
        end_epoch = _chart_to_mt5_epoch(chunk_end, args.mt5_server_offset)

        ticks = mt5.copy_ticks_range(args.symbol, start_epoch, end_epoch, mt5.COPY_TICKS_ALL)
        if ticks is None:
            raise RuntimeError(
                f"copy_ticks_range failed for {args.symbol} "
                f"{chunk_start:%Y-%m-%d %H:%M} to {chunk_end:%Y-%m-%d %H:%M}: "
                f"{mt5.last_error()}"
            )
        if len(ticks) == 0:
            if args.progress:
                print(
                    f"[ticks] {chunk_start:%Y-%m-%d %H:%M} -> {chunk_end:%Y-%m-%d %H:%M}: "
                    "0 ticks (skipped)"
                )
            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)
            continue

        wrote = _write_rows(out_path, _tick_rows(ticks, args.mt5_server_offset), write_header=not wrote_header)
        wrote_header = True
        total += wrote

        if args.progress:
            print(
                f"[ticks] {chunk_start:%Y-%m-%d %H:%M} -> {chunk_end:%Y-%m-%d %H:%M}: "
                f"{wrote:,} ticks"
            )

        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    if total == 0:
        if out_path.exists():
            out_path.unlink()
        print(f"[empty] {args.symbol} {month_start:%Y-%m}: no ticks; skipped file creation.")
        return 0

    print(f"[done] {out_path}: {total:,} ticks")
    return total


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Export real MT5 tick data into monthly tab-separated CSV files.")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--start-date", required=True, help="Chart-time GMT+3 date, e.g. 2024-01-01")
    p.add_argument("--end-date", required=True, help="Exclusive chart-time GMT+3 date, e.g. 2026-06-06")
    p.add_argument("--output-dir", default="data/ticks")
    p.add_argument("--mt5-server-offset", type=int, default=3)
    p.add_argument("--chunk-hours", type=int, default=6)
    p.add_argument("--sleep-seconds", type=float, default=0.2)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--progress", action="store_true")

    p.add_argument("--mt5-path", default=None)
    p.add_argument("--mt5-login", type=int, default=None)
    p.add_argument("--mt5-password", default=None)
    p.add_argument("--mt5-server", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.chunk_hours < 1:
        raise SystemExit("--chunk-hours must be >= 1")

    start = _parse_date(args.start_date)
    end = _parse_date(args.end_date)
    if end <= start:
        raise SystemExit("--end-date must be after --start-date")

    conn = Mt5Connection(
        path=args.mt5_path,
        login=args.mt5_login,
        password=args.mt5_password,
        server=args.mt5_server,
    )
    conn.initialize()
    try:
        mt5 = conn.mt5
        if not mt5.symbol_select(args.symbol, True):
            raise SystemExit(f"Symbol {args.symbol!r} not found in MT5 Market Watch.")

        grand_total = 0
        for month_start, month_end in _iter_months(start, end):
            grand_total += _export_month(conn, args, month_start, month_end)

        print(f"[all done] exported {grand_total:,} ticks")
        return 0
    finally:
        conn.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
