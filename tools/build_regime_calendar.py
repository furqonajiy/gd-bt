#!/usr/bin/env python3
"""Build a data-driven XAUUSD regime calendar from M1 chart files.

The output is a daily CSV with three labels:

* ``sweep_regime``: broad buckets for optimizer/GitHub Actions splits.
* ``behavior_regime``: stricter unsupervised cluster diagnostics.
* ``old_threshold_regime``: the current ATR-threshold router for comparison.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading.strategy.regime_calendar import (  # noqa: E402
    REGIME_ORDER,
    build_regime_calendar,
    expand_chart_paths,
    load_m1,
    monthly_regime_summary,
)


DAILY_COLUMNS = [
    "date",
    "month",
    "sweep_regime",
    "behavior_regime",
    "old_threshold_regime",
    "open",
    "high",
    "low",
    "close",
    "rows",
    "m15_tr_mean",
    "m15_tr_mean_pct",
    "m15_tr_p90_pct",
    "daily_range_pct",
    "trend_20d",
    "trend_efficiency_20d",
    "choppiness_20d",
    "shock_rate_20d",
    "vol_of_vol_20d",
    "vol_intensity",
    "directionality",
]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--charts", nargs="+", default=["data/XAUUSD_M1_*_ELEV8.csv"],
                   help="M1 chart CSV path(s)/glob(s). Default: data/XAUUSD_M1_*_ELEV8.csv")
    p.add_argument("--output", default="reports/regime_calendar.csv",
                   help="Daily regime calendar CSV output.")
    p.add_argument("--monthly-output", default=None,
                   help="Optional monthly summary CSV output.")
    args = p.parse_args(argv)

    paths = expand_chart_paths(args.charts)
    if not paths:
        raise SystemExit("no chart files matched --charts")
    m1 = load_m1(paths)
    calendar = build_regime_calendar(m1)
    out = calendar.reset_index(names="date")
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    cols = [c for c in DAILY_COLUMNS if c in out.columns]

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    out[cols].to_csv(output, index=False, float_format="%.10g")

    usable = out[out["sweep_regime"].notna()]
    counts = usable["sweep_regime"].value_counts().to_dict()
    print(f"wrote {output} | files={len(paths)} rows={len(m1):,} days={len(out)} usable={len(usable)}")
    for regime in REGIME_ORDER:
        print(f"  {regime}: {int(counts.get(regime, 0))} days")

    if args.monthly_output:
        monthly = monthly_regime_summary(calendar)
        monthly_out = Path(args.monthly_output)
        monthly_out.parent.mkdir(parents=True, exist_ok=True)
        monthly.to_csv(monthly_out, index=False, float_format="%.10g")
        print(f"wrote {monthly_out} | months={len(monthly)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
