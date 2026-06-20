#!/usr/bin/env python3
"""Filter a PROVIDER feed (e.g. Victor's victor_signals.txt) by entry-feature
indicators computed from the chart at each signal's bar.

Victor signals are received text, not generated -- so we cannot regenerate them
with the scalper generator's filters. Instead we compute the SAME indicators
(RSI / Bollinger %B+bandwidth / ADX / VWAP / HTF-EMA / S-R, via
generate_scalper_signals._add_indicators) on the chart, look up the bar at each
signal's chart_time, and keep the signal only if it passes the SAME
_entry_filters_ok logic. Output preserves the provider feed format (same writer
as the live provider filter), so a filtered feed is a drop-in for the backtest /
sweep / live auto.

All entry-feature flags default to no-op, so with no flags the output is the
provider feed unchanged (modulo the --preset hour/side filter, default "all").

Usage (one variant):
  python tools/filter_provider_signals_by_indicator.py \
    --input victor_signals.txt --output generated/victor_rsi.txt \
    --charts "data/XAUUSD_M1_2026*_ELEV8.csv" --rsi-buy-max 70 --rsi-sell-min 30
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from trading.xauusd import CsvChartSource  # noqa: E402
import generate_scalper_signals as gen  # noqa: E402
import live_provider_signal_filter as pf  # noqa: E402


def build_parser():
    # Reuse the generator's parser so the entry-feature flags + their indicator
    # parameters (periods, k, ema spans, ...) are IDENTICAL to the scalper feed.
    # It already exposes --charts and --output; we add the provider --input and
    # the hour/side --preset. Generation-only flags are simply ignored here.
    p = gen.build_parser()
    p.description = "Filter a provider feed by entry-feature indicators (RSI/BB/ADX/VWAP/HTF/S-R)."
    p.add_argument("--input", required=True, help="Raw provider feed (e.g. victor_signals.txt).")
    p.add_argument("--preset", default="all",
                   choices=["all", "no_bad_hours", "best_hours", "high_growth_hour_side",
                            "research_month_hour_side"],
                   help="Hour/side preset applied BEFORE the indicator filter (default all = none).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    kept_preset, total = pf.parse_and_filter(Path(args.input), args.preset)

    # Always restrict to the chart range (drop signals with no chart bar) so that
    # the unfiltered "base" feed and every filtered variant cover the SAME signals
    # -- only the indicator filter differs. Indicators are only computed when a
    # filter is active (cheap base path otherwise).
    have_filter = gen._any_entry_filter(args)
    chart = CsvChartSource(gen._expand_chart_paths(args.charts))
    df = gen._add_indicators(chart.dataframe, args) if have_filter else chart.dataframe
    df = df.set_index("time", drop=False).sort_index()
    chart_start, chart_end = df.index[0], df.index[-1]

    passed: list = []
    no_bar = 0
    filtered_out = 0
    for sig in kept_preset:
        ts = pd.Timestamp(sig.chart_time)
        # Restrict to the chart range at BOTH ends (asof alone would map an
        # after-chart-end signal onto the last bar). Matches the backtest, which
        # excludes signals outside [chart_start, chart_end].
        if ts < chart_start or ts > chart_end:
            no_bar += 1
            continue
        bar_t = df.index.asof(ts)
        if bar_t is pd.NaT or pd.isna(bar_t):
            no_bar += 1
            continue
        if have_filter:
            row = df.loc[bar_t]
            if isinstance(row, pd.DataFrame):  # duplicate timestamp -> use the last bar
                row = row.iloc[-1]
            if not gen._entry_filters_ok(row, sig.side, args):
                filtered_out += 1
                continue
        passed.append(sig)

    pf.write_filtered(passed, Path(args.output))
    in_range = len(passed) + filtered_out
    print(f"[provider-indicator-filter] input={total:,} after-preset={len(kept_preset):,} "
          f"in-chart-range={in_range:,} filtered-out={filtered_out:,} kept={len(passed):,} "
          f"(preset={args.preset}, filter={'on' if have_filter else 'off'}) -> {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
