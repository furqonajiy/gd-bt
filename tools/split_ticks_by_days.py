#!/usr/bin/env python3
"""Split a tick CSV into fixed N-day calendar windows (default 3 days/part).

This is the DATE-based alternative to split_ticks_by_size.py. Instead of cutting
on accumulated bytes (part boundaries that move every re-sync), it cuts on the
calendar: each part holds a fixed window of `--days` days-of-month, named by its
START day so the filename is self-describing, with a `_pN` sub-index so a window
too large for GitHub is sub-split while a normal window is just `_p1`. With the
default 3-day window:

    _D1_p1  = days 01-03   _D4_p1 = days 04-06   ...   _D31_p1 = day 31

i.e. ``XAUUSD_TICK_202606_D1_p1_ELEV8.csv``, ``..._D4_p1_ELEV8.csv``, ... A given
tick ALWAYS lands in the same date window regardless of when you re-split, so
membership is deterministic and grow-only.

Every window ALWAYS carries at least ``_p1``. A window whose ticks exceed
``--max-mb`` MiB (default 95, GitHub-safe) is size-split WITHIN the window into
``_D16_p1``, ``_D16_p2``, ... so the archive is always pushable (GitHub rejects
files >= 100 MiB). June's volatile 16-18 / 22-24 windows are why this matters: at
strict 3-day-no-cap they were 147 / 180 MiB and would bounce a push.

Consumers are unaffected: tick_backtest/backtest_hybrid glob ``..._*_ELEV8.csv``
and ``load_ticks`` re-sorts every row by timestamp after concatenating, so the
part name/order never matters. (Legacy size-only ``_pN`` parts and these
``_D<start>_pN`` parts can even coexist under the same glob.)

The committed archive stores only the parts (no full month file), so this tool
reassembles the parts into the full month first when the full file is absent --
recognizing BOTH an existing ``_D<start>_pN`` date archive (re-run) and a legacy
``_pN`` size archive (first migration) -- then re-splits by day.

Usage (pass the FULL base path; the parts are reassembled if needed):
  python tools/split_ticks_by_days.py \
    --input data/ticks/XAUUSD_TICK_202606_ELEV8.csv \
    --days 3 --max-mb 95 --remove-source
"""
from __future__ import annotations

import argparse
import glob
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse the size splitter's reassembly (join_parts is naming-agnostic), its
# within-window size sub-split (split_file -> _pN), its legacy _pN finder
# (parts_for) and its source-tag splitter (_split_tag) so the FIRST migration from
# a size archive can rebuild the full month AND a non-ELEV8 archive (e.g. _DEMO)
# splits/joins with the same machinery; never re-derive the join or the size cut.
from tools.split_ticks_by_size import _split_tag, join_parts, parts_for, split_file  # noqa: E402

_MIB = 1024 * 1024
# _D<start>_p<sub>_<TAG>: a date window (start day-of-month) carrying a size
# sub-index, for ANY source tag (ELEV8, DEMO, ...) -- group(1)/group(2) are the
# start day and the sub-index.
_DAY_PART_RE = re.compile(r"_D(\d+)_p(\d+)_[A-Za-z0-9]+\.csv$")


def _window_start_day(day: int, days: int) -> int:
    """Start day-of-month of the window a given day falls in (day 1..3 -> 1,
    4..6 -> 4, ... for days=3). This start day IS the part's _D<start> label."""
    return ((day - 1) // days) * days + 1


def _window_base_path(base: Path, start_day: int) -> Path:
    """The per-window FULL temp path (no _pN yet): insert _D{start_day} before the
    source tag (_ELEV8, _DEMO, ...). split_file then size-splits this into
    _D{start_day}_pN_<TAG>."""
    stem, tag = _split_tag(base.name)
    if tag is not None:
        return base.with_name(f"{stem}_D{start_day}_{tag}.csv")
    return base.with_name(f"{base.stem}_D{start_day}.csv")


def day_parts_for(base: Path) -> list[Path]:
    """The _D<start>_p<sub> date parts of ``base`` (..._YYYYMM_<TAG>.csv), ordered
    by (start day, sub-index) -- numeric, not lexicographic (D31 after D4; p10
    after p2). Works for any source tag (ELEV8, DEMO, ...)."""
    stem, tag = _split_tag(base.name)
    if tag is None:
        return []
    hits = [p for p in base.parent.glob(f"{stem}_D*_p*_{tag}.csv") if _DAY_PART_RE.search(p.name)]
    return sorted(hits, key=lambda p: (int(_DAY_PART_RE.search(p.name).group(1)),
                                       int(_DAY_PART_RE.search(p.name).group(2))))


def split_file_by_days(src: Path, days: int = 3, *, max_mb: float = 95.0,
                       remove_source: bool = False) -> list[Path]:
    """Split src into fixed `days`-day windows keyed on the <DATE> day, then
    size-split each window at `max_mb` MiB so every part is GitHub-safe.

    Header is repeated in each part; rows are streamed (a month is millions of
    rows, never loaded whole). Window _D{start} holds days [start .. start+days-1]
    and is written as _D{start}_p1 (+ _p2, ... only if it exceeds max_mb).
    Returns the part paths written, ordered by (start day, sub-index).

    Any pre-existing parts for this month (both _D<start>_pN date parts and legacy
    _pN size parts) are removed first, so a re-split never leaves a stale part
    behind when the part set changes shape.
    """
    if days < 1:
        raise SystemExit("--days must be >= 1")
    if max_mb <= 0:
        raise SystemExit("--max-mb must be > 0")
    if not src.name.endswith(".csv"):
        raise SystemExit(f"unexpected tick filename: {src.name}")

    # Clean slate: drop existing date parts AND legacy size parts for this base.
    for stale in (*day_parts_for(src), *parts_for(src)):
        if stale != src and stale.exists():
            stale.unlink()

    # Pass 1: route rows into one full temp file per date window.
    windows: dict[int, "_PartHandle"] = {}
    with src.open("r", encoding="utf-8", newline="") as f:
        header = f.readline()
        if not header:
            raise SystemExit(f"{src.name} is empty")
        for line in f:
            if not line.strip():
                continue
            date_field = line.split("\t", 1)[0]  # <DATE> = YYYY.MM.DD
            try:
                day = int(date_field.rsplit(".", 1)[1])
            except (IndexError, ValueError):
                continue  # not a data row (stray header / blank)
            start_day = _window_start_day(day, days)
            w = windows.get(start_day)
            if w is None:
                w = _PartHandle(_window_base_path(src, start_day), header)
                windows[start_day] = w
            w.write(line)
    for w in windows.values():
        w.close()

    # Pass 2: size-split each window's temp into _D{start}_pN (force=True so even
    # a sub-cap window becomes a single _p1), then drop the temp window file.
    max_bytes = int(max_mb * _MIB)
    parts: list[Path] = []
    for start_day in sorted(windows):
        window_base = windows[start_day].path
        sub = split_file(window_base, max_bytes, remove_source=True, force=True)
        hi = start_day + days - 1
        for p in sub:
            print(f"[part] {p.name}: days {start_day:02d}-{hi:02d}, {p.stat().st_size / _MIB:.1f} MiB")
        parts.extend(sub)
    if remove_source:
        src.unlink()
        print(f"[removed source] {src.name}")
    return parts


class _PartHandle:
    """Write handle for one date window's full temp file (headed once)."""

    def __init__(self, path: Path, header: str):
        self.path = path
        self._fh = path.open("w", encoding="utf-8", newline="")
        self._fh.write(header)

    def write(self, line: str) -> None:
        self._fh.write(line)

    def close(self) -> None:
        self._fh.close()


def _resolve_full(base: Path) -> Path | None:
    """Return a full month file to split: ``base`` itself if present, else the
    reassembly of its parts -- an existing _D<start> date archive (re-run) first,
    then a legacy _pN size archive (first migration). The reassembled file is
    byte-identical to the pre-split original; callers pass remove_source=True so
    it doesn't linger and get double-globbed."""
    if base.exists() and base.stat().st_size > 0:
        return base
    parts = day_parts_for(base) or parts_for(base)
    if parts:
        join_parts(parts, base, remove_parts=True)  # parts are replaced by the new date parts
        return base
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Split tick CSV(s) into fixed N-day calendar windows (default 3 "
                    "days), named by start day with a size sub-index: _D1_p1=days "
                    "1-3, _D4_p1=4-6, ... (a window over --max-mb adds _p2, _p3).")
    p.add_argument("--input", required=True, nargs="+",
                   help="FULL month base path(s) or glob(s), e.g. "
                        "data/ticks/XAUUSD_TICK_202606_ELEV8.csv. If only the parts "
                        "exist on disk they are reassembled first, then re-split.")
    p.add_argument("--days", type=int, default=3,
                   help="Calendar days per window (default 3).")
    p.add_argument("--max-mb", type=float, default=95.0,
                   help="Size cap per part in MiB (default 95; stay under GitHub's "
                        "100). A window bigger than this is split into _pN sub-parts.")
    p.add_argument("--remove-source", action="store_true",
                   help="Delete the (reassembled) full month file after splitting "
                        "so a glob never matches both it and its parts. Recommended.")
    return p


def _expand_inputs(patterns: list[str]) -> list[Path]:
    """Expand globs to FULL base paths. A glob that only matches parts is
    collapsed to the base (..._YYYYMM_ELEV8.csv) so we reassemble + re-split once
    per month rather than once per part."""
    bases: list[Path] = []
    seen: set[str] = set()
    for pat in patterns:
        hits = glob.glob(pat)
        for c in (hits if hits else [pat]):
            cp = Path(c)
            name = cp.name
            # ..._YYYYMM_{D<start>_pN | pN}_<TAG>.csv -> ..._YYYYMM_<TAG>.csv (base),
            # preserving the source tag (ELEV8, DEMO, ...).
            base_name = re.sub(r"_(?:D\d+_p\d+|p\d+)_([A-Za-z][A-Za-z0-9]*)\.csv$",
                               r"_\1.csv", name)
            cp = cp.with_name(base_name)
            if str(cp) not in seen:
                seen.add(str(cp))
                bases.append(cp)
    return bases


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    total = 0
    for base in _expand_inputs(args.input):
        full = _resolve_full(base)
        if full is None:
            print(f"[skip] no full file or parts for {base.name}")
            continue
        total += len(split_file_by_days(full, args.days, remove_source=args.remove_source))
    print(f"[all done] wrote {total} date-window part(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
