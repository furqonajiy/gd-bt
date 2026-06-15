#!/usr/bin/env python3
"""Generate Dukascopy XAUUSD tick data into monthly ELEV8-schema CSV files.

Dukascopy serves one LZMA-compressed ``.bi5`` file per hour (UTC); each holds
fixed 20-byte big-endian tick records: (ms-since-hour, ask_pts, bid_pts,
ask_vol, bid_vol). This tool downloads, decodes, scales prices, shifts UTC ->
broker GMT+3, and writes the exact 10-column tab schema ``export_ticks.py``
emits -- so ``tick_backtest``'s ``--ticks`` consumes Dukascopy and ELEV8 files
interchangeably (its loader keys on DATE+TIME as GMT+3 naive and ignores the
rest).

Source vs parity: Dukascopy is a third-party ECN feed -- tighter spreads and
slightly different prices than ELEV8. Use it for the months ELEV8 lacks
(pre-May-2026): it validates execution mechanics over deep history, NOT exact
ELEV8 P&L. Derate accordingly.

Price scale: Dukascopy prices are integer "points"; XAUUSD is points/1000
(default --price-scale). The first decoded bid/ask are printed so you can
confirm ~gold price; cross-check one overlapping hour against an ELEV8 file
before trusting a full run.

Stdlib only (urllib + lzma). NOTE: Dukascopy URL months are 0-indexed.

Usage:
  python tools/export_ticks_dukascopy.py --symbol XAUUSD \
      --start-date 2026-01-01 --end-date 2026-05-01 \
      --output-dir data/ticks --server-offset 3 --progress
"""
from __future__ import annotations

import argparse
import calendar
import csv
import lzma
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading import POINT_VALUE  # noqa: E402


UTC = timezone.utc
BASE_URL = "https://datafeed.dukascopy.com/datafeed"
_RECORD = struct.Struct(">IIIff")  # ms-in-hour, ask_pts, bid_pts, ask_vol, bid_vol

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


def _iter_hours(start: datetime, end: datetime) -> Iterable[datetime]:
    cur = start.replace(minute=0, second=0, microsecond=0)
    while cur < end:
        yield cur
        cur += timedelta(hours=1)


def _hour_url(symbol: str, url_dt_utc: datetime) -> str:
    # Dukascopy paths are 0-indexed on the month -- the classic gotcha.
    return (
        f"{BASE_URL}/{symbol}/{url_dt_utc.year:04d}/{url_dt_utc.month - 1:02d}/"
        f"{url_dt_utc.day:02d}/{url_dt_utc.hour:02d}h_ticks.bi5"
    )


def _decompress_bi5(buf: bytes) -> bytes:
    # Dukascopy .bi5 are LZMA-alone; FORMAT_AUTO decodes both alone and xz, but
    # fall back explicitly and then to a stream tolerant of a missing end marker.
    for fmt in (lzma.FORMAT_AUTO, lzma.FORMAT_ALONE):
        try:
            return lzma.decompress(buf, format=fmt)
        except lzma.LZMAError:
            continue
    return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(buf)


def _bi5_to_rows(data: bytes, scale: float, chart_hour: datetime) -> Iterable[dict[str, str]]:
    """Decode one hour's raw tick bytes into ELEV8-schema rows in GMT+3.

    chart_hour is the broker GMT+3 hour these ticks belong to; each record's
    ms-offset is measured from the UTC hour start, which (integer offset) is the
    same wall-clock minute/second within the GMT+3 hour -- so adding the offset
    to chart_hour yields the broker timestamp directly.
    """
    if len(data) % _RECORD.size != 0:
        raise ValueError(f"tick payload {len(data)} bytes is not a multiple of {_RECORD.size}")
    base_ms = calendar.timegm(chart_hour.timetuple()) * 1000
    for ms_off, ask_pts, bid_pts, ask_vol, bid_vol in _RECORD.iter_unpack(data):
        ask = ask_pts / scale
        bid = bid_pts / scale
        t = chart_hour + timedelta(milliseconds=ms_off)
        vol_real = float(ask_vol) + float(bid_vol)
        spread = round((ask - bid) / POINT_VALUE) if ask > 0 and bid > 0 else ""
        yield {
            "<DATE>": t.strftime("%Y.%m.%d"),
            "<TIME>": t.strftime("%H:%M:%S.%f")[:-3],
            "<TIME_MSC>": str(base_ms + ms_off),
            "<BID>": f"{bid:.2f}",
            "<ASK>": f"{ask:.2f}",
            "<LAST>": "",
            "<VOLUME>": "0",
            "<VOLUME_REAL>": f"{vol_real:.8f}".rstrip("0").rstrip("."),
            "<FLAGS>": "6",  # bid+ask present on every Dukascopy tick
            "<SPREAD>": str(spread),
        }


def _is_header_only_tick_file(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return False
    return len(lines) == 1 and lines[0].strip() == HEADER_LINE


def _last_tick_msc(path: Path) -> Optional[int]:
    """Last recorded tick's <TIME_MSC>, or None if the file holds no ticks."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(size - min(size, 65536))
        tail = f.read().decode("utf-8", errors="replace")
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line or line == HEADER_LINE:
            continue
        cols = line.split("\t")
        if len(cols) >= 3 and cols[2].isdigit():
            return int(cols[2])
    return None


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


def _fetch_hour(url: str, *, timeout: float, retries: int, sleep: float) -> Optional[bytes]:
    """Return the hour's raw bytes, or None for a 404 (no file = closed market)."""
    req = urllib.request.Request(url, headers={"User-Agent": "xauusd-tick-fetch/1.0"})
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last_err = e
        except urllib.error.URLError as e:
            last_err = e
        if attempt < retries and sleep > 0:
            time.sleep(sleep * (attempt + 1))
    raise RuntimeError(f"fetch failed: {url}: {last_err}")


def _export_month(
        args: argparse.Namespace,
        month_start: datetime,
        month_end: datetime,
        *,
        fetch: Callable[[str], Optional[bytes]],
        stats: dict,
) -> int:
    out_path = Path(args.output_dir) / f"{args.symbol}_TICK_{month_start:%Y%m}_DUKASCOPY.csv"

    if _is_header_only_tick_file(out_path):
        out_path.unlink()
        print(f"[empty] removed header-only tick file: {out_path}")

    out_exists = out_path.exists() and out_path.stat().st_size > 0

    if out_exists and args.overwrite:
        out_path.unlink()
        out_exists = False

    resume_msc: Optional[int] = None
    fetch_start = month_start
    append = False
    if out_exists and args.merge:
        resume_msc = _last_tick_msc(out_path)
        if resume_msc is not None:
            append = True
            # <TIME_MSC> is the GMT+3 wall clock encoded as a UTC epoch, so
            # fromtimestamp(...UTC) recovers the broker time; resume at that hour
            # and drop time_msc <= last below -- re-pulls only the boundary hour.
            last_chart = datetime.fromtimestamp(resume_msc / 1000.0, UTC).replace(tzinfo=None)
            fetch_start = max(last_chart.replace(minute=0, second=0, microsecond=0), month_start)
    elif out_exists and not args.overwrite:
        print(f"[skip] {out_path} exists; use --overwrite to rebuild or --merge to extend.")
        return 0

    total = 0
    wrote_header = False

    for chart_hour in _iter_hours(fetch_start, month_end):
        url_dt_utc = chart_hour - timedelta(hours=args.server_offset)
        url = _hour_url(args.symbol, url_dt_utc)
        try:
            raw = fetch(url)
        except RuntimeError as e:
            stats["failed"] += 1
            print(f"[warn] {e}")
            continue

        if not raw:
            if args.progress:
                print(f"[ticks] {chart_hour:%Y-%m-%d %H:00}: no file (closed)")
            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)
            continue

        rows = list(_bi5_to_rows(_decompress_bi5(raw), args.price_scale, chart_hour))
        if resume_msc is not None:
            rows = [r for r in rows if int(r["<TIME_MSC>"]) > resume_msc]

        if rows and not stats["scale_checked"]:
            sample = ", ".join(f"{r['<BID>']}/{r['<ASK>']}" for r in rows[:3])
            print(f"[scale-check] first bid/ask: {sample}  (expect ~gold price; if 10x off, set --price-scale)")
            stats["scale_checked"] = True

        wrote = _write_rows(out_path, rows, write_header=(not append and not wrote_header))
        if wrote:
            wrote_header = True
        total += wrote

        if args.progress:
            print(f"[ticks] {chart_hour:%Y-%m-%d %H:00}: {wrote:,} ticks")
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    if total == 0:
        if append:
            print(f"[merge] {out_path}: up to date (+0 ticks).")
        else:
            if out_path.exists():
                out_path.unlink()
            print(f"[empty] {args.symbol} {month_start:%Y-%m}: no ticks; skipped file creation.")
        return 0

    if append:
        print(f"[merge] {out_path}: +{total:,} ticks (resumed {fetch_start:%Y-%m-%d %H:00}).")
    else:
        print(f"[done] {out_path}: {total:,} ticks")
    return total


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate Dukascopy tick data into monthly ELEV8-schema CSV files.")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--start-date", required=True, help="Chart-time GMT+3 date, e.g. 2026-01-01")
    p.add_argument("--end-date", required=True, help="Exclusive chart-time GMT+3 date, e.g. 2026-05-01")
    p.add_argument("--output-dir", default="data/ticks")
    p.add_argument("--server-offset", type=int, default=3,
                   help="Broker tz offset from UTC; shifts Dukascopy UTC to chart time to match ELEV8 files.")
    p.add_argument("--price-scale", type=float, default=1000.0,
                   help="Integer-point divisor for XAUUSD (default 1000). Watch the [scale-check] line.")
    p.add_argument("--sleep-seconds", type=float, default=0.1)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--progress", action="store_true")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--overwrite", action="store_true", help="Delete and re-download the whole month.")
    mode.add_argument("--merge", action="store_true",
                      help="Append ticks newer than the last recorded one to an existing monthly file.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    start = _parse_date(args.start_date)
    end = _parse_date(args.end_date)
    if end <= start:
        raise SystemExit("--end-date must be after --start-date")
    if args.price_scale <= 0:
        raise SystemExit("--price-scale must be > 0")

    def fetch(url: str) -> Optional[bytes]:
        return _fetch_hour(url, timeout=args.timeout, retries=args.retries, sleep=args.sleep_seconds)

    stats = {"failed": 0, "scale_checked": False}
    grand_total = 0
    for month_start, month_end in _iter_months(start, end):
        grand_total += _export_month(args, month_start, month_end, fetch=fetch, stats=stats)

    print(f"[all done] exported {grand_total:,} ticks"
          + (f"; {stats['failed']} hour(s) failed after retries" if stats["failed"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())