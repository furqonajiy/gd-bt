#!/usr/bin/env python3
"""Emit chart/env settings for a regime calendar split.

GitHub Actions needs shell variables for the active regime.  This helper reads
``tools/build_regime_calendar.py`` output and emits the chart months containing
that regime's days, plus start/end metadata.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading.strategy.regime_calendar import normalize_regime  # noqa: E402


def calendar_dates(text: str, regimes: set[str], layer: str) -> list[str]:
    wanted = {normalize_regime(r) for r in regimes}
    out: list[str] = []
    for row in csv.DictReader(text.splitlines()):
        date = (row.get("date") or "").strip()
        regime = normalize_regime((row.get(layer) or "").strip())
        if date and regime in wanted:
            out.append(date)
    return sorted(dict.fromkeys(out))


def chart_paths_for_dates(dates: list[str], charts_dir: str = "data") -> list[str]:
    months = sorted({d[:7].replace("-", "") for d in dates})
    return [f"{charts_dir.rstrip('/')}/XAUUSD_M1_{month}_ELEV8.csv" for month in months]


def env_lines(dates: list[str], charts: list[str]) -> list[str]:
    if not dates:
        raise ValueError("no dates supplied")
    return [
        f"CHARTS={' '.join(charts)}",
        f"REGIME_START={dates[0]}",
        f"REGIME_END={dates[-1]}",
        f"REGIME_DAYS={len(dates)}",
        f"REGIME_MONTHS={len(charts)}",
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--calendar", required=True, help="CSV from tools/build_regime_calendar.py.")
    p.add_argument("--regime", action="append", required=True,
                   help="Regime to keep. Repeat to combine regimes.")
    p.add_argument("--layer", default="sweep_regime",
                   choices=["sweep_regime", "behavior_regime", "old_threshold_regime"],
                   help="Calendar label column to use.")
    p.add_argument("--charts-dir", default="data", help="Directory prefix for chart CSV paths.")
    p.add_argument("--github-env", default=None,
                   help="Optional GITHUB_ENV file path to append key=value lines.")
    args = p.parse_args(argv)

    text = Path(args.calendar).read_text()
    dates = calendar_dates(text, set(args.regime), args.layer)
    if not dates:
        raise SystemExit(f"no calendar dates matched regime={args.regime} layer={args.layer}")
    charts = chart_paths_for_dates(dates, args.charts_dir)
    lines = env_lines(dates, charts)
    for line in lines:
        print(line)
    if args.github_env:
        with Path(args.github_env).open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
