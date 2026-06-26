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
import re
import sys
from pathlib import Path

_MIB = 1024 * 1024
# Match a _pN part of ANY source tag (ELEV8, DEMO, ...); group(1) is the part num.
_PART_RE = re.compile(r"_p(\d+)_[A-Za-z0-9]+\.csv$")
# Trailing _<TAG>.csv source tag (e.g. _ELEV8, _DEMO); the live archive is ELEV8.
# The tag must START with a letter so a purely-numeric suffix (a legacy untagged
# ``XAUUSD_TICK_202606.csv``) is NOT mistaken for a source tag.
_TAG_RE = re.compile(r"_([A-Za-z][A-Za-z0-9]*)\.csv$")


def _split_tag(name: str) -> tuple[str, str | None]:
    """Split a tick filename into (base-without-tag, source-tag). The tag is the
    trailing ``_<TAG>.csv`` (e.g. ``XAUUSD_TICK_202606_ELEV8.csv`` -> base
    ``XAUUSD_TICK_202606``, tag ``ELEV8``); a legacy untagged ``...csv`` -> tag
    None. Generalises the historical ELEV8-only naming so a separate broker
    archive (e.g. DEMO) splits/joins with the same machinery."""
    m = _TAG_RE.search(name)
    if not m:
        return (name[:-4] if name.endswith(".csv") else name, None)
    return (name[: m.start()], m.group(1))


def _part_path(src: Path, n: int) -> Path:
    """Insert _p{n} before the source tag (or before .csv if there's no tag)."""
    base, tag = _split_tag(src.name)
    if tag is not None:
        return src.with_name(f"{base}_p{n}_{tag}.csv")
    return src.with_name(f"{src.stem}_p{n}.csv")


def parts_for(full: Path) -> list[Path]:
    """The size-split _pN parts that a split of ``full`` produced, in NUMERIC
    order (p2 before p10). ``full`` is the un-split path
    (``..._YYYYMM_<TAG>.csv``); its parts are ``..._YYYYMM_pN_<TAG>.csv``."""
    base, tag = _split_tag(full.name)
    if tag is None:
        return []
    hits = [p for p in full.parent.glob(f"{base}_p*_{tag}.csv") if _PART_RE.search(p.name)]
    return sorted(hits, key=lambda p: int(_PART_RE.search(p.name).group(1)))


def join_parts(parts: list[Path], dest: Path, *, remove_parts: bool = False) -> Path:
    """Reassemble size-split _pN parts into one file (the inverse of split_file).

    The header from the first part is kept; the repeated header line at the top of
    every later part is dropped, so ``dest`` is byte-identical to the original
    pre-split file. Parts are written in the order given (use ``parts_for`` for
    numeric order). Optionally deletes the consumed parts afterwards."""
    if not parts:
        raise SystemExit("join_parts: no parts to join")
    with dest.open("w", encoding="utf-8", newline="") as out:
        for i, part in enumerate(parts):
            with part.open("r", encoding="utf-8", newline="") as f:
                if i != 0:
                    f.readline()  # drop the repeated header
                out.write(f.read())
    print(f"[joined] {len(parts)} part(s) -> {dest.name} ({dest.stat().st_size / _MIB:.1f} MiB)")
    if remove_parts:
        for p in parts:
            p.unlink()
        print(f"[removed parts] {len(parts)}")
    return dest


def split_file(src: Path, max_bytes: int, *, remove_source: bool = False,
               start_part: int = 1, force: bool = False) -> list[Path]:
    """Split src into <= max_bytes parts on line boundaries; header in each part.

    Returns the list of part paths written. If the whole file already fits in one
    part it is left untouched and an empty list is returned (nothing to split) --
    UNLESS ``force`` is set, in which case it is still (re)written as a single
    part. ``start_part`` numbers the first part (use N to APPEND a re-split tail
    after existing parts p1..p(N-1), leaving them untouched)."""
    if max_bytes <= 0:
        raise SystemExit("--max-mb must be > 0")
    if not force and src.stat().st_size <= max_bytes:
        print(f"[skip] {src.name} is {src.stat().st_size / _MIB:.1f} MiB, already under the cap.")
        return []

    with src.open("r", encoding="utf-8", newline="") as f:
        header = f.readline()
        header_bytes = len(header.encode("utf-8"))
        if header_bytes >= max_bytes:
            raise SystemExit(f"--max-mb too small: header alone is {header_bytes} bytes.")

        parts: list[Path] = []
        part_n = start_part - 1
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
                        "matching the full file AND its parts). In --join mode, "
                        "delete the consumed _pN parts after reassembly.")
    p.add_argument("--join", action="store_true",
                   help="REASSEMBLE mode (inverse of split): each --input is a FULL "
                        "target path (e.g. data/ticks/XAUUSD_TICK_202606_ELEV8.csv); "
                        "its _pN parts are joined back into it byte-identically. Use "
                        "to restore an incremental working file from committed parts "
                        "before `export_ticks --merge`.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths: list[Path] = []
    for pat in args.input:
        hits = [Path(p) for p in glob.glob(pat)]
        paths.extend(hits if hits else [Path(pat)])

    if args.join:
        total = 0
        for full in paths:
            parts = parts_for(full)
            if not parts:
                print(f"[skip] no _pN parts found for {full.name}")
                continue
            join_parts(parts, full, remove_parts=args.remove_source)
            total += 1
        print(f"[all done] reassembled {total} file(s)")
        return 0

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
