#!/usr/bin/env python3
"""Split monthly tick files into half-month parts, fetching missing months first.

Each month's full tick file (the one tools/export_ticks.py writes) is split into
two by-date parts -- H1 = days 1-15, H2 = days 16-end -- so a ~600 MB month becomes
two roughly-300 MB files. A month with no full file yet is fetched from MT5 first
(by delegating to export_ticks, so the on-disk schema and the GMT+3<->epoch handling
stay identical) and then split.

Why split by calendar date rather than by row count: a tick's day never changes, so
a given tick always lands in the same part. That makes the per-part merge
deterministic -- a re-run can only append strictly-newer ticks to each part, never
reshuffle them -- which is what keeps a re-sync idempotent and growth-only, mirroring
export_ticks' own resume-append.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse export_ticks' primitives so the on-disk schema, the <TIME_MSC> resume key,
# and the GMT+3<->epoch conversion are shared rather than re-derived -- re-deriving
# the epoch math is exactly the kind of drift that silently corrupts a tick archive.
from tools.export_ticks import (  # noqa: E402
    FIELDNAMES,
    HEADER_LINE,
    _export_month,
    _is_header_only_tick_file,
    _last_tick_msc,
)
from xauusd_trading import Mt5Connection  # noqa: E402

# Days 1-15 -> H1, 16-end -> H2. Fixed boundary so part membership is stable across
# re-syncs (see module docstring); it is not a tunable.
_HALF_BOUNDARY_DAY = 15


def _parse_yyyymm(value: str) -> tuple[int, int]:
    if len(value) != 6 or not value.isdigit():
        raise SystemExit(f"month must be YYYYMM, got {value!r}")
    year, month = int(value[:4]), int(value[4:])
    if not 1 <= month <= 12:
        raise SystemExit(f"month out of range in {value!r}")
    return year, month


def _iter_months(start: tuple[int, int], end: tuple[int, int]) -> Iterable[tuple[int, int]]:
    year, month = start
    while (year, month) <= end:
        yield year, month
        month += 1
        if month == 13:
            year, month = year + 1, 1


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return start, end


def _full_path(output_dir: str, symbol: str, year: int, month: int) -> Path:
    return Path(output_dir) / f"{symbol}_TICK_{year:04d}{month:02d}_ELEV8.csv"


def _half_paths(output_dir: str, symbol: str, year: int, month: int) -> tuple[Path, Path]:
    stem = f"{symbol}_TICK_{year:04d}{month:02d}"
    base = Path(output_dir)
    return base / f"{stem}_H1_ELEV8.csv", base / f"{stem}_H2_ELEV8.csv"


def _needs_fetch(full_path: Path) -> bool:
    if not full_path.exists() or full_path.stat().st_size == 0:
        return True
    # A header-only file is a stale no-tick artifact, not real data.
    return _is_header_only_tick_file(full_path)


class _PartWriter:
    """Append-or-create writer for one half file, with grow-only dedup.

    Skips any row whose <TIME_MSC> is not strictly greater than the part's last
    stored tick, so re-splitting the same (or a grown) full file appends only the
    new ticks and never duplicates. The handle is opened lazily so a part that
    receives nothing is left byte-for-byte untouched.
    """

    def __init__(self, path: Path, *, overwrite: bool):
        self._path = path
        if overwrite and path.exists():
            path.unlink()
        # Resume point and append-vs-create decision are read once, before any
        # write, so they reflect the file as it stood at the start of this split.
        self._last_msc: Optional[int] = _last_tick_msc(path)
        self._append = path.exists() and path.stat().st_size > 0
        self._fh = None
        self._writer = None
        self.written = 0

    def _ensure_open(self) -> None:
        if self._writer is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" + csv.writer with a tab delimiter matches export_ticks'
        # writer byte-for-byte (same delimiter, same "\r\n" line terminator).
        self._fh = self._path.open("a" if self._append else "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh, delimiter="\t")
        if not self._append:
            self._writer.writerow(FIELDNAMES)

    def add(self, parts: list[str], msc: int) -> None:
        if self._last_msc is not None and msc <= self._last_msc:
            return
        self._ensure_open()
        self._writer.writerow(parts)
        self.written += 1

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()


def split_month_file(
        full_path: Path, h1_path: Path, h2_path: Path, *, overwrite: bool = False
) -> tuple[int, int]:
    """Route every tick row of full_path into its half file; return (h1_written, h2_written)."""
    if not full_path.exists() or full_path.stat().st_size == 0:
        return (0, 0)

    h1 = _PartWriter(h1_path, overwrite=overwrite)
    h2 = _PartWriter(h2_path, overwrite=overwrite)
    try:
        # Stream line-by-line: a month of ticks is millions of rows, so the full
        # file is never loaded into memory.
        with full_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if not line or line == HEADER_LINE or line.startswith("<DATE>"):
                    continue
                parts = line.split("\t")
                if len(parts) < 3 or not parts[2].isdigit():
                    continue
                day = int(parts[0].rsplit(".", 1)[1])  # <DATE> is YYYY.MM.DD
                msc = int(parts[2])                    # <TIME_MSC>
                (h1 if day <= _HALF_BOUNDARY_DAY else h2).add(parts, msc)
    finally:
        h1.close()
        h2.close()
    return (h1.written, h2.written)


def _fetch_full_month(conn, args: argparse.Namespace, year: int, month: int) -> None:
    """Create/extend the full monthly file via export_ticks (identical schema + epoch path)."""
    month_start, month_end = _month_bounds(year, month)
    etex_args = argparse.Namespace(
        symbol=args.symbol,
        output_dir=args.output_dir,
        mt5_server_offset=args.mt5_server_offset,
        chunk_hours=args.chunk_hours,
        sleep_seconds=args.sleep_seconds,
        progress=args.progress,
        overwrite=False,  # never wholesale re-fetch: ticks aged out of the window would be lost
        merge=True,       # grow an existing partial month forward instead of skipping it
    )
    _export_month(conn, etex_args, month_start, month_end)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Split monthly MT5 tick files into H1 (days 1-15) and H2 (days 16-end); "
                    "fetch missing months from MT5 first."
    )
    p.add_argument("--symbol", default="BTCUSD")
    p.add_argument("--from", dest="from_ym", required=True, help="First month, YYYYMM, e.g. 202401")
    p.add_argument("--to", dest="to_ym", required=True, help="Last month inclusive, YYYYMM, e.g. 202606")
    p.add_argument("--output-dir", default="data/ticks")
    p.add_argument("--no-fetch", action="store_true",
                   help="Split only full files already on disk; do not fetch missing months from MT5.")
    p.add_argument("--overwrite", action="store_true",
                   help="Rebuild the H1/H2 parts from scratch (default appends only newer ticks).")
    p.add_argument("--remove-source", action="store_true",
                   help="Delete each full monthly file after it is split (prevents double-counting "
                        "if a glob ever matches both the full file and its parts).")
    p.add_argument("--chunk-hours", type=int, default=6)
    p.add_argument("--sleep-seconds", type=float, default=0.2)
    p.add_argument("--mt5-server-offset", type=int, default=3)
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

    start = _parse_yyyymm(args.from_ym)
    end = _parse_yyyymm(args.to_ym)
    if end < start:
        raise SystemExit("--to must not be before --from")

    months = list(_iter_months(start, end))

    # Only open MT5 if at least one month is actually missing and fetching is on,
    # so split-only runs over existing files need no terminal.
    fetch_needed = (not args.no_fetch) and any(
        _needs_fetch(_full_path(args.output_dir, args.symbol, y, m)) for y, m in months
    )

    conn = None
    if fetch_needed:
        conn = Mt5Connection(
            path=args.mt5_path, login=args.mt5_login,
            password=args.mt5_password, server=args.mt5_server,
        )
        conn.initialize()
        if not conn.mt5.symbol_select(args.symbol, True):
            conn.shutdown()
            raise SystemExit(f"Symbol {args.symbol!r} not found in MT5 Market Watch.")

    try:
        for year, month in months:
            full = _full_path(args.output_dir, args.symbol, year, month)

            if conn is not None and _needs_fetch(full):
                _fetch_full_month(conn, args, year, month)

            if _needs_fetch(full):
                # Still nothing: the broker has no ticks for this month (or --no-fetch).
                print(f"[skip] {year:04d}{month:02d}: no full tick file to split.")
                continue

            h1_path, h2_path = _half_paths(args.output_dir, args.symbol, year, month)
            w1, w2 = split_month_file(full, h1_path, h2_path, overwrite=args.overwrite)
            print(f"[split] {year:04d}{month:02d}: H1 +{w1:,}  H2 +{w2:,}")

            if args.remove_source:
                full.unlink()
                print(f"[removed source] {full}")
    finally:
        if conn is not None:
            conn.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())