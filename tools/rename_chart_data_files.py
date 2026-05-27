#!/usr/bin/env python3
"""Rename chart CSV files under data/ to the canonical source-aware format.

Canonical format:

    XAUUSD_M1_YYYYMM_ELEV8.csv
    XAUUSD_M1_YYYYMM_INTERNET.csv

Inference rules:

- Names containing INTERNET, DAT_MT, or SHIFTED are treated as INTERNET.
- Names containing ELEV8 or MT5 are treated as ELEV8.
- Legacy project files named XAUUSD_M1_YYYYMM.csv are treated as ELEV8.
- Already-canonical names are left unchanged.

Examples:

    data/XAUUSD_M1_202604.csv
      -> data/XAUUSD_M1_202604_ELEV8.csv

    data/DAT_MT_SHIFTED_XAUUSD_M1_202401.csv
      -> data/XAUUSD_M1_202401_INTERNET.csv

Preview only:

    python tools/rename_chart_data_files.py

Apply renames:

    python tools/rename_chart_data_files.py --apply
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path


CANONICAL_RE = re.compile(r"^XAUUSD_M1_(?P<yyyymm>\d{6})_(?P<source>ELEV8|INTERNET)\.csv$", re.I)
MONTH_RE = re.compile(r"(?P<yyyymm>20\d{4})")
LEGACY_ELEV8_RE = re.compile(r"^XAUUSD_M1_20\d{4}\.CSV$", re.I)


def infer_source(path: Path) -> str | None:
    name = path.name.upper()
    if "INTERNET" in name or "DAT_MT" in name or "SHIFTED" in name:
        return "INTERNET"
    if "ELEV8" in name or "MT5" in name:
        return "ELEV8"
    # Legacy project files named XAUUSD_M1_YYYYMM.csv came from MT5/ELEV8.
    if LEGACY_ELEV8_RE.match(name):
        return "ELEV8"
    return None


def canonical_name(path: Path) -> str | None:
    if CANONICAL_RE.match(path.name):
        return path.name

    month = MONTH_RE.search(path.name)
    if not month:
        return None

    source = infer_source(path)
    if source is None:
        return None

    return f"XAUUSD_M1_{month.group('yyyymm')}_{source}.csv"


def plan_renames(data_dir: Path) -> tuple[list[tuple[Path, Path]], list[Path], list[Path]]:
    renames: list[tuple[Path, Path]] = []
    unchanged: list[Path] = []
    skipped: list[Path] = []

    for path in sorted(data_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".csv":
            continue

        target_name = canonical_name(path)
        if target_name is None:
            skipped.append(path)
            continue

        target = path.with_name(target_name)
        if target == path:
            unchanged.append(path)
        else:
            renames.append((path, target))

    return renames, unchanged, skipped


def apply_renames(renames: list[tuple[Path, Path]], *, overwrite: bool = False) -> None:
    target_counts: dict[Path, int] = {}
    for _src, dst in renames:
        target_counts[dst] = target_counts.get(dst, 0) + 1
    duplicates = sorted(path for path, count in target_counts.items() if count > 1)
    if duplicates:
        dup_text = "\n".join(f"  - {p}" for p in duplicates)
        raise SystemExit(f"Refusing: multiple source files map to the same target:\n{dup_text}")

    for src, dst in renames:
        if dst.exists() and not overwrite:
            raise SystemExit(
                f"Refusing to overwrite existing file: {dst}\n"
                f"Source: {src}\n"
                f"Use --overwrite only if you intentionally want to replace it."
            )

    for src, dst in renames:
        src.rename(dst)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data", help="Directory containing chart CSV files.")
    parser.add_argument("--apply", action="store_true", help="Actually rename files. Default is dry-run.")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing target file.")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        raise SystemExit(f"Data directory does not exist: {data_dir}")
    if not data_dir.is_dir():
        raise SystemExit(f"Not a directory: {data_dir}")

    renames, unchanged, skipped = plan_renames(data_dir)

    print("Canonical chart filename migration")
    print(f"Data dir: {data_dir.resolve()}")
    print()

    if renames:
        print("Renames:")
        for src, dst in renames:
            print(f"  {src.name} -> {dst.name}")
    else:
        print("Renames: none")

    if unchanged:
        print("\nAlready canonical:")
        for path in unchanged:
            print(f"  {path.name}")

    if skipped:
        print("\nSkipped / unknown source:")
        for path in skipped:
            print(f"  {path.name}")

    if args.apply:
        apply_renames(renames, overwrite=args.overwrite)
        print(f"\nApplied {len(renames)} rename(s).")
    else:
        print("\nDry-run only. Re-run with --apply to rename files.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
