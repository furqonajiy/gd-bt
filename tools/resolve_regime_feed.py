#!/usr/bin/env python3
"""Materialize a regime-specific signal feed for the grid sweep."""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.champions_report import feed_signals  # noqa: E402
from tools.scalper_feed_variants import scalper_variant_args  # noqa: E402
from tools.slice_signals import slice_feed  # noqa: E402


REGIME_WINDOWS: dict[str, tuple[str, str]] = {
    "R1quiet": ("2021-11-01", "2023-09-30"),
    "R2bull": ("2023-10-01", "2024-12-31"),
    "R3strong": ("2025-01-01", "2025-12-31"),
    "R4parab": ("2026-01-01", "2026-12-31"),
}


def _split_flags(flags: list[str] | None) -> list[str]:
    out: list[str] = []
    for item in flags or []:
        out.extend(shlex.split(item))
    return out


def scalper_variant_command(feed: str, charts: list[str], output: Path,
                            regime: str) -> list[str] | None:
    variant_args = scalper_variant_args(feed)
    if variant_args is None:
        return None
    start, _end = REGIME_WINDOWS[regime]
    return [
        "--charts", *charts,
        "--output", str(output),
        "--start", start,
        "--session-start", "0",
        "--session-end", "0",
        "--signal-tz", "7",
        *_split_flags(variant_args),
    ]


def materialize_regime_feed(feed: str, regime: str, output: Path,
                            charts: list[str]) -> Path:
    if regime not in REGIME_WINDOWS:
        raise SystemExit(f"unknown regime {regime}")
    output.parent.mkdir(parents=True, exist_ok=True)

    variant_argv = scalper_variant_command(feed, charts, output, regime)
    if variant_argv is not None:
        from tools import generate_scalper_signals  # noqa: E402

        rc = generate_scalper_signals.main(variant_argv)
        if rc:
            raise SystemExit(rc)
        return output

    source = Path(feed_signals(feed))
    if not source.exists():
        raise SystemExit(f"feed file {source} is missing")
    start, end = REGIME_WINDOWS[regime]
    output.write_text(slice_feed(source.read_text(encoding="utf-8"), start, end),
                      encoding="utf-8")
    return output


def _count_signals(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines()
               if line.lstrip().split(".", 1)[0].isdigit())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--feed", required=True)
    p.add_argument("--regime", required=True, choices=sorted(REGIME_WINDOWS))
    p.add_argument("--output", required=True)
    p.add_argument("--charts", nargs="+", required=True)
    args = p.parse_args(argv)

    output = materialize_regime_feed(
        args.feed, args.regime, Path(args.output), list(args.charts))
    n = _count_signals(output)
    if n <= 0:
        raise SystemExit(f"ERROR: materialized feed empty for {args.feed} {args.regime}")
    print(f"feed {args.feed} -> {output} ({n} signals for {args.regime})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
