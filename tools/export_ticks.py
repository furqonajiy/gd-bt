#!/usr/bin/env python3
from __future__ import annotations

import argparse
import calendar
import csv
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading.engine import Mt5Connection, POINT_VALUE  # noqa: E402


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


def _last_tick_msc(path: Path) -> Optional[int]:
    """Last recorded tick's <TIME_MSC>, or None if the file holds no ticks.

    Tail-reads a small trailing window rather than the whole file: a month of
    ticks is millions of rows and merge only needs the final timestamp. The
    final data line is always complete because every row this tool writes ends
    in a line terminator, so it sits well inside the trailing window.
    """
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        window = min(size, 65536)
        f.seek(size - window)
        tail = f.read().decode("utf-8", errors="replace")
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line or line == HEADER_LINE:
            continue
        cols = line.split("\t")
        if len(cols) >= 3 and cols[2].isdigit():
            return int(cols[2])
    return None


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
    out_path = Path(args.output_dir) / f"{args.symbol}_TICK_{month_start:%Y%m}_{getattr(args, 'source', 'ELEV8')}.csv"

    # Continue-from-latest with a SPLIT archive: --split-mb deletes the full month
    # file, leaving only _pN parts, so --merge would otherwise see "no file" and
    # re-fetch the whole month (losing ticks the broker has since aged out). When
    # merging and only parts exist, reassemble them back into the full file first
    # so merge can resume from the last recorded tick (a later --split-mb re-packs).
    if getattr(args, "merge", False) and not (out_path.exists() and out_path.stat().st_size > 0):
        from tools.split_ticks_by_days import day_parts_for
        from tools.split_ticks_by_size import join_parts, parts_for
        # Recognize BOTH archive shapes: _D<start> date parts (--split-days) and
        # legacy _pN size parts (--split-mb). Whichever is on disk is reassembled
        # so --merge resumes from the last tick; a later --split-* re-packs.
        parts = day_parts_for(out_path) or parts_for(out_path)
        if parts:
            join_parts(parts, out_path, remove_parts=True)
            print(f"[reassembled] {len(parts)} part(s) -> {out_path.name} to resume merge")

    # A header-only file is a stale artifact of a prior run that hit a no-tick
    # window; drop it so it neither blocks a re-fetch nor counts as data.
    if _is_header_only_tick_file(out_path):
        out_path.unlink()
        print(f"[empty] removed header-only tick file: {out_path}")

    out_exists = out_path.exists() and out_path.stat().st_size > 0

    if out_exists and args.overwrite:
        out_path.unlink()
        out_exists = False

    # Merge resumes from the last stored tick and appends only newer ones, so
    # earlier ticks -- which may already have aged out of the broker's tick
    # window -- are never re-fetched. --overwrite would re-pull and thus lose
    # them; merge is the safe way to grow a rolling-month tick file.
    resume_msc: Optional[int] = None
    fetch_start = month_start
    append = False
    if out_exists and args.merge:
        resume_msc = _last_tick_msc(out_path)
        if resume_msc is not None:
            append = True
            # copy_ticks_range is second-granular on input, so resume at the
            # whole second holding the last tick and drop time_msc <= last_msc
            # below: that re-pulls only the boundary second and de-dupes it.
            resume_chart = _mt5_msc_to_chart_time((resume_msc // 1000) * 1000, args.mt5_server_offset)
            fetch_start = max(resume_chart, month_start)
    elif out_exists and not args.overwrite:
        print(f"[skip] {out_path} exists; use --overwrite to replace or --merge to extend.")
        return 0

    total = 0
    wrote_header = False

    for chunk_start, chunk_end in _iter_chunks(fetch_start, month_end, args.chunk_hours):
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

        rows = _tick_rows(ticks, args.mt5_server_offset)
        if resume_msc is not None:
            rows = (r for r in rows if int(r["<TIME_MSC>"]) > resume_msc)

        wrote = _write_rows(out_path, rows, write_header=(not append and not wrote_header))
        if wrote:
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
        if append:
            # Existing file is already current; a no-op merge must never delete it.
            print(f"[merge] {out_path}: up to date (+0 ticks).")
        else:
            if out_path.exists():
                out_path.unlink()
            print(f"[empty] {args.symbol} {month_start:%Y-%m}: no ticks; skipped file creation.")
        return 0

    if append:
        print(f"[merge] {out_path}: +{total:,} ticks (resumed {fetch_start:%Y-%m-%d %H:%M:%S}).")
    else:
        print(f"[done] {out_path}: {total:,} ticks")
    return total


def _split_exported(output_dir: str, symbol: str, months: list[tuple[int, int]],
                    max_mb: float, source: str = "ELEV8") -> int:
    """Split each exported month's file into <= max_mb MiB parts, removing the
    full file. Reuses split_ticks_by_size so the _pN naming and line-aligned
    cutting are identical; imported lazily to avoid a load-time dependency.
    Returns the number of parts written."""
    from tools.split_ticks_by_size import split_file
    max_bytes = int(max_mb * 1024 * 1024)
    written = 0
    for year, month in months:
        src = Path(output_dir) / f"{symbol}_TICK_{year:04d}{month:02d}_{source}.csv"
        if src.exists():
            written += len(split_file(src, max_bytes, remove_source=True))
    return written


def _split_exported_days(output_dir: str, symbol: str, months: list[tuple[int, int]],
                         days: int, max_mb: float = 95.0, source: str = "ELEV8") -> int:
    """Split each exported month's file into fixed `days`-day calendar windows
    (_D<start>_pN naming, sub-split at max_mb so each part is GitHub-safe),
    removing the full file. Reuses split_ticks_by_days so the date-window cutting
    is shared; honors the `source` tag so a non-ELEV8 archive (e.g. DEMO) splits
    into _D<start>_pN_<source> parts too. Returns the number of parts written."""
    from tools.split_ticks_by_days import split_file_by_days
    written = 0
    for year, month in months:
        src = Path(output_dir) / f"{symbol}_TICK_{year:04d}{month:02d}_{source}.csv"
        if src.exists():
            written += len(split_file_by_days(src, days, max_mb=max_mb, remove_source=True))
    return written


def _fetch_new_rows(conn, args, fetch_start: datetime, month_end: datetime,
                    resume_msc: int) -> list[dict]:
    """Fetch ticks in [fetch_start, month_end), keep only those strictly newer than
    resume_msc (de-dupes the re-pulled boundary second). Returns row dicts."""
    mt5 = conn.mt5
    out: list[dict] = []
    for chunk_start, chunk_end in _iter_chunks(fetch_start, month_end, args.chunk_hours):
        ticks = mt5.copy_ticks_range(
            args.symbol, _chart_to_mt5_epoch(chunk_start, args.mt5_server_offset),
            _chart_to_mt5_epoch(chunk_end, args.mt5_server_offset), mt5.COPY_TICKS_ALL)
        if ticks is None:
            raise RuntimeError(f"copy_ticks_range failed for {args.symbol} "
                               f"{chunk_start:%Y-%m-%d %H:%M}: {mt5.last_error()}")
        if len(ticks):
            out.extend(r for r in _tick_rows(ticks, args.mt5_server_offset)
                       if int(r["<TIME_MSC>"]) > resume_msc)
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)
    return out


def _merge_append_split_month(conn, args, month_start: datetime, month_end: datetime):
    """Incremental tick append against a SIZE-SPLIT archive (committed _pN parts),
    WITHOUT reassembling p1..p(N-1).

    Resumes from the last tick in the LAST part, fetches only newer ticks, and
    re-splits just (last part + new ticks) into parts numbered from the last index.
    So a completed past month with no new ticks is a cheap no-op (a tail read + an
    empty fetch), and the current month only ever rewrites its tail -- never the
    whole ~600 MiB archive. Returns the number of ticks appended, or None when this
    path does not apply (a full file already exists, no parts yet, or the resume
    point can't be read) so the caller falls back to the full-file export."""
    from tools.split_ticks_by_size import _PART_RE, parts_for, split_file
    out_path = Path(args.output_dir) / f"{args.symbol}_TICK_{month_start:%Y%m}_{getattr(args, 'source', 'ELEV8')}.csv"
    if out_path.exists() and out_path.stat().st_size > 0:
        return None  # full file present -> the normal merge path handles it
    parts = parts_for(out_path)
    if not parts:
        return None  # fresh month -> normal export creates (and splits) it
    last_part = parts[-1]
    last_n = int(_PART_RE.search(last_part.name).group(1))
    resume_msc = _last_tick_msc(last_part)
    if resume_msc is None:
        return None  # can't resume from the tail -> fall back (may reassemble)

    resume_chart = _mt5_msc_to_chart_time((resume_msc // 1000) * 1000, args.mt5_server_offset)
    fetch_start = max(resume_chart, month_start)
    new_rows = _fetch_new_rows(conn, args, fetch_start, month_end, resume_msc)
    if not new_rows:
        print(f"[merge] {args.symbol} {month_start:%Y-%m}: up to date (+0 ticks); "
              f"{len(parts)} part(s) untouched.")
        return 0

    # Re-split ONLY (last part + new rows), numbered from last_n, so p1..p(N-1) are
    # never touched. force=True writes a single _p{last_n} even if the tail is sub-cap.
    shutil.copyfile(last_part, out_path)
    _write_rows(out_path, new_rows, write_header=False)
    last_part.unlink()
    written = split_file(out_path, int(args.split_mb * 1024 * 1024),
                         remove_source=True, start_part=last_n, force=True)
    print(f"[merge] {args.symbol} {month_start:%Y-%m}: +{len(new_rows):,} ticks appended; "
          f"tail re-split into {len(written)} part(s) from p{last_n} "
          f"(p1..p{last_n - 1} untouched).")
    return len(new_rows)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Export real MT5 tick data into monthly tab-separated CSV files.")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--start-date", required=True, help="Chart-time GMT+3 date, e.g. 2024-01-01")
    p.add_argument("--end-date", required=True, help="Exclusive chart-time GMT+3 date, e.g. 2026-06-06")
    p.add_argument("--output-dir", default="data/ticks")
    p.add_argument("--source", default="ELEV8",
                   help="Broker/source tag baked into the filename "
                        "(XAUUSD_TICK_YYYYMM[_pN]_<SOURCE>.csv; default ELEV8). Use a "
                        "distinct tag (e.g. DEMO) so a demo broker's tick archive "
                        "never mixes with the live one.")
    p.add_argument("--mt5-server-offset", type=int, default=3)
    p.add_argument("--chunk-hours", type=int, default=6)
    p.add_argument("--sleep-seconds", type=float, default=0.2)
    p.add_argument("--progress", action="store_true")

    # Re-running on an existing monthly file: extend it forward (--merge) or
    # rebuild it wholesale (--overwrite). Mutually exclusive; default is skip.
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--overwrite", action="store_true",
                      help="Delete and re-fetch the whole month (loses ticks aged out of the broker window).")
    mode.add_argument("--merge", action="store_true",
                      help="Append ticks newer than the last recorded one to an existing monthly file.")

    p.add_argument("--split-mb", type=float, default=None,
                   help="After fetching, split each month's file into parts of at most "
                        "this many MiB and delete the full file, so every part fits under "
                        "GitHub's 100 MiB limit (parts: XAUUSD_TICK_YYYYMM_p1_ELEV8.csv, "
                        "_p2, ...). NOTE: with the full file removed, a later --merge "
                        "re-fetches the window instead of resuming -- use it as a final "
                        "packaging step, not for an incrementally-grown archive.")
    p.add_argument("--split-days", type=int, default=None,
                   help="After fetching, split each month into fixed N-day calendar "
                        "parts named by start day (_D1=days 1-3, _D4=4-6, ... for 3) "
                        "and delete the full file. DATE-based alternative to "
                        "--split-mb: membership is deterministic (a tick always lands "
                        "in the same part), but a window has NO size cap -- a volatile "
                        "stretch can exceed GitHub's 100 MiB (the splitter warns). "
                        "Mutually exclusive with --split-mb.")

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
    if args.split_mb and args.split_days:
        raise SystemExit("--split-mb and --split-days are mutually exclusive.")
    if args.split_days is not None and args.split_days < 1:
        raise SystemExit("--split-days must be >= 1")

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
        months_to_split: list[tuple[int, int]] = []
        for month_start, month_end in _iter_months(start, end):
            # --merge + --split-mb: APPEND only the new ticks to the split tail
            # (no whole-archive reassemble). Falls back to the full-file export
            # when the month isn't a committed split archive.
            handled = None
            if args.merge and args.split_mb:
                handled = _merge_append_split_month(conn, args, month_start, month_end)
            if handled is None:
                grand_total += _export_month(conn, args, month_start, month_end)
                months_to_split.append((month_start.year, month_start.month))
            else:
                grand_total += handled

        print(f"[all done] exported {grand_total:,} ticks")
        if args.split_mb and months_to_split:
            parts = _split_exported(args.output_dir, args.symbol, months_to_split, args.split_mb, getattr(args, "source", "ELEV8"))
            print(f"[split] wrote {parts} size-capped part(s) (<= {args.split_mb:g} MiB each)")
        if args.split_days and months_to_split:
            parts = _split_exported_days(args.output_dir, args.symbol, months_to_split,
                                         args.split_days, source=getattr(args, "source", "ELEV8"))
            print(f"[split] wrote {parts} date-window part(s) ({args.split_days} day(s) each)")
        return 0
    finally:
        conn.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())