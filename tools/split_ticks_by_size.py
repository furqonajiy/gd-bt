#!/usr/bin/env python3
"""Split a tick CSV into size-capped parts so each fits under GitHub's limit.

GitHub rejects any file larger than 100 MiB on push (and warns above 50 MiB). A
month -- or even a few days -- of XAUUSD ticks easily exceeds that, so this
splits one full tick file into sequential parts, each at most ``--max-mb`` MiB,
cutting only on line boundaries (never mid-row). The header line is repeated at
the top of every part, so each part is independently loadable -- ``tick_backtest``
globs them and concatenates, same as it does the ``_H1``/``_H2`` halves.

Naming mirrors the archive convention: ``XAUUSD_TICK_202606_ELEV8.csv`` becomes
``XAUUSD_TICK_202606_p1_ELEV8.csv``, ``..._p2_ELEV8.csv``, ... (the ``_pN`` is
inserted before the ``_ELEV8`` tag; dot-free, like every other artifact name).

Unlike split_ticks.py (which splits by calendar date into stable H1/H2 halves),
this splits by accumulated bytes -- part boundaries depend on tick volume, so
they are NOT stable across re-syncs. Use it as a final packaging step before
committing, not as an incremental store.

Usage:
  python tools/split_ticks_by_size.py `
    --input data/ticks/XAUUSD_TICK_202606_ELEV8.csv `
    --max-mb 95 `
    --remove-source
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

_MIB = 1024 * 1024


def _part_path(src: Path, n: int) -> Path:
    """Insert _p{n} before the _ELEV8 tag (or before .csv if there's no tag)."""
    name = src.name
    if name.endswith("_ELEV8.csv"):
        base = name[: -len("_ELEV8.csv")]
        return src.with_name(f"{base}_p{n}_ELEV8.csv")
    return src.with_name(f"{src.stem}_p{n}.csv")


def split_file(src: Path, max_bytes: int, *, remove_source: bool = False) -> list[Path]:
    """Split src into <= max_bytes parts on line boundaries; header in each part.

    Returns the list of part paths written. If the whole file already fits in one
    part, it is left untouched and an empty list is returned (nothing to split).
    """
    if max_bytes <= 0:
        raise SystemExit("--max-mb must be > 0")
    if src.stat().st_size <= max_bytes:
        print(f"[skip] {src.name} is {src.stat().st_size / _MIB:.1f} MiB, already under the cap.")
        return []

    with src.open("r", encoding="utf-8", newline="") as f:
        header = f.readline()
        header_bytes = len(header.encode("utf-8"))
        if header_bytes >= max_bytes:
            raise SystemExit(f"--max-mb too small: header alone is {header_bytes} bytes.")

        parts: list[Path] = []
        part_n = 0
        out = None
        size = 0

        def _open_next():
            nonlocal out, size, part_n
            if out is not None:
                out.close()
            part_n += 1
            path = _part_path(src, part_n)
            out = path.open("w", encoding="utf-8", newline="")
            out.write(header)
            size = header_bytes
            parts.append(path)

        _open_next()
        for line in f:
            line_bytes = len(line.encode("utf-8"))
            # Roll to a new part when this line would overflow the cap (but keep
            # at least one data line per part so we always make progress).
            if size + line_bytes > max_bytes and size > header_bytes:
                _open_next()
            out.write(line)
            size += line_bytes
        if out is not None:
            out.close()

    for p in parts:
        print(f"[part] {p.name}: {p.stat().st_size / _MIB:.1f} MiB")
    if remove_source:
        src.unlink()
        print(f"[removed source] {src.name}")
    return parts


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Split tick CSV(s) into size-capped parts (each <= --max-mb MiB) "
                    "so they fit under GitHub's 100 MiB file limit.")
    p.add_argument("--input", required=True, nargs="+",
                   help="Tick CSV path(s) or glob(s) to split.")
    p.add_argument("--max-mb", type=float, default=95.0,
                   help="Max size per part in MiB (default 95; stay under GitHub's 100).")
    p.add_argument("--remove-source", action="store_true",
                   help="Delete each source file after it is split (avoids a glob "
                        "matching the full file AND its parts).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths: list[Path] = []
    for pat in args.input:
        hits = [Path(p) for p in glob.glob(pat)]
        paths.extend(hits if hits else [Path(pat)])
    max_bytes = int(args.max_mb * _MIB)
    total_parts = 0
    for src in paths:
        if not src.exists():
            print(f"[skip] {src} does not exist.")
            continue
        total_parts += len(split_file(src, max_bytes, remove_source=args.remove_source))
    print(f"[all done] wrote {total_parts} part(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
