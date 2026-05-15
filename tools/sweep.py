#!/usr/bin/env python3
"""Parameter sweep for the XAUUSD engine.

Every configuration runs through the same `advance_one_bar` simulator
the smoke test locks — no lookahead, strict-touch arming, same-bar
worst-case stop wins, spread-aware triggers. The v2 baseline is always
included as an anchor.

Usage:
    python tools/sweep.py --signals signals.txt \\
                          --charts data/XAUUSD_M1_*.csv \\
                          --output sweep_results.csv

    # Smaller grid:
    python tools/sweep.py --signals signals.txt --charts data/*.csv \\
                          --risks 0.05 --entry-counts 3,5

    # Count combinations without running:
    python tools/sweep.py --signals signals.txt --charts data/*.csv --dry-run
"""
from __future__ import annotations

import argparse
import glob
import itertools
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

# Make `xauusd_trading` importable when running this script directly.
# sweep.py lives at <repo>/tools/, so the repo root is two parents up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xauusd_trading import (  # noqa: E402
    CsvChartSource, StrategyConfig, parse_signals_file, run_backtest,
)
from xauusd_trading import Signal  # noqa: E402


# ---------------------------------------------------------------------------
# default grid — override via CLI flags
# ---------------------------------------------------------------------------

DEFAULT_GRID: dict[str, list[Any]] = {
    "risk_per_signal":          [0.02, 0.05, 0.10],
    "entry_count":              [3, 5, 7],
    "entry_ladder":             ["range_uniform", "range_to_sl"],
    "entry_sl_gap":             [1.0, 2.0],          # range_to_sl only
    "activation_delay_minutes": [0, 2],
    "pending_expiry_minutes":   [60, 120, 240],
    "max_hold_minutes":         [60, 90, 120],
    "sl_multiplier":            [1.0, 1.25, 1.5],
    "final_target":             ["TP2", "TP3"],
    "lock_after_tp1":           [True],
}

# v2 baseline — always included as an anchor for comparison.
BASELINE = {
    "risk_per_signal": 0.05,
    "entry_count": 3,
    "entry_ladder": "range_to_sl",
    "entry_sl_gap": 2.0,
    "activation_delay_minutes": 0,
    "pending_expiry_minutes": 240,
    "max_hold_minutes": 90,
    "sl_multiplier": 1.0,
    "final_target": "TP2",
    "lock_after_tp1": True,
}

_STRATEGY_FIELDS = {
    "risk_per_signal", "entry_count", "entry_ladder", "entry_sl_gap",
    "activation_delay_minutes", "pending_expiry_minutes", "max_hold_minutes",
    "sl_multiplier", "final_target", "lock_after_tp1",
}


# ---------------------------------------------------------------------------
# worker pool plumbing
# ---------------------------------------------------------------------------

# Globals populated by the multiprocessing initializer. Each worker loads
# the chart and signals once at startup; subsequent configs reuse the cache.
_CHART: CsvChartSource | None = None
_SIGNALS: list[Signal] | None = None
_SPLIT_TIME: pd.Timestamp | None = None


def _init_worker(chart_paths: list[str], signals_path: str, split_iso: str | None):
    global _CHART, _SIGNALS, _SPLIT_TIME
    _CHART = CsvChartSource([Path(p) for p in chart_paths])
    _SIGNALS = parse_signals_file(Path(signals_path))
    _SPLIT_TIME = pd.Timestamp(split_iso) if split_iso else None


def _backtest_subset(signals: list[Signal], config: StrategyConfig) -> dict:
    """run_backtest may early-exit on equity <= 0; defend against empty input."""
    if not signals:
        return {
            "final_equity": float(config.initial_capital),
            "net_profit": 0.0, "wins": 0, "losses": 0, "no_fills": 0,
            "open": 0, "win_rate_pct": 0.0, "max_drawdown_pct": 0.0,
            "signals_included": 0,
        }
    return run_backtest(signals, _CHART, config)


def _eval_config(combo: dict) -> dict:
    """Evaluate one configuration: full-period + IS + OOS = 3 backtests."""
    assert _CHART is not None and _SIGNALS is not None

    config = StrategyConfig(**{k: combo[k] for k in _STRATEGY_FIELDS})

    full = _backtest_subset(_SIGNALS, config)
    out = dict(combo)
    out.update({
        "full_final_equity":   full["final_equity"],
        "full_net_profit":     full["net_profit"],
        "full_wins":           full["wins"],
        "full_losses":         full["losses"],
        "full_no_fills":       full["no_fills"],
        "full_open":           full["open"],
        "full_win_rate_pct":   full["win_rate_pct"],
        "full_max_dd_pct":     full["max_drawdown_pct"],
        "full_signals":        full["signals_included"],
    })

    if _SPLIT_TIME is not None:
        train = [s for s in _SIGNALS if s.signal_time_chart < _SPLIT_TIME]
        test  = [s for s in _SIGNALS if s.signal_time_chart >= _SPLIT_TIME]
        is_  = _backtest_subset(train, config)
        oos  = _backtest_subset(test,  config)
        out.update({
            "is_final_equity":   is_["final_equity"],
            "is_win_rate_pct":   is_["win_rate_pct"],
            "is_wins":           is_["wins"],
            "is_losses":         is_["losses"],
            "is_signals":        is_["signals_included"],
            "is_max_dd_pct":     is_["max_drawdown_pct"],
            "oos_final_equity":  oos["final_equity"],
            "oos_win_rate_pct":  oos["win_rate_pct"],
            "oos_wins":          oos["wins"],
            "oos_losses":        oos["losses"],
            "oos_signals":       oos["signals_included"],
            "oos_max_dd_pct":    oos["max_drawdown_pct"],
        })
        # Overfit signal: large IS gain, OOS loss or small gain.
        is_ret = (is_["final_equity"] - config.initial_capital) / config.initial_capital
        oos_ret = (oos["final_equity"] - config.initial_capital) / config.initial_capital
        out["is_return_pct"]  = is_ret * 100.0
        out["oos_return_pct"] = oos_ret * 100.0
        out["overfit_flag"] = bool(is_ret > 0.5 and oos_ret < 0.05)
    return out


# ---------------------------------------------------------------------------
# grid expansion
# ---------------------------------------------------------------------------

def expand_grid(grid: dict[str, list]) -> list[dict]:
    """Cartesian product of grid values, with redundant combos removed.

    entry_sl_gap is irrelevant for range_uniform, so all range_uniform
    variants collapse to a single sl_gap value (the first in the grid).
    """
    keys = list(grid.keys())
    seen: set[tuple] = set()
    out: list[dict] = []
    for values in itertools.product(*[grid[k] for k in keys]):
        d = dict(zip(keys, values))
        if d.get("entry_ladder") == "range_uniform":
            d["entry_sl_gap"] = grid["entry_sl_gap"][0]
        key = tuple(d[k] for k in keys)
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _parse_csv_list(spec: str | None, type_):
    """Parse a comma-separated CLI value into a typed list, or None."""
    if spec is None:
        return None
    items = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if type_ is bool:
            items.append(token.lower() in ("1", "true", "yes", "y", "t"))
        else:
            items.append(type_(token))
    return items


def _expand_chart_paths(patterns: list[str]) -> list[str]:
    paths: list[str] = []
    for pat in patterns:
        if any(c in pat for c in "*?["):
            matches = sorted(glob.glob(pat))
            if not matches:
                raise SystemExit(f"No files match pattern: {pat}")
            paths.extend(matches)
        else:
            if not Path(pat).exists():
                raise SystemExit(f"Chart file not found: {pat}")
            paths.append(pat)
    return paths


def _print_top(df: pd.DataFrame, n: int, title: str, sort_col: str) -> None:
    sub = df.sort_values(sort_col, ascending=False).head(n)
    cols = [
        "risk_per_signal", "entry_count", "entry_ladder", "entry_sl_gap",
        "activation_delay_minutes", "pending_expiry_minutes",
        "max_hold_minutes", "sl_multiplier", "final_target", "lock_after_tp1",
        "full_final_equity", "full_win_rate_pct", "full_max_dd_pct",
    ]
    if "is_final_equity" in df.columns:
        cols += [
            "is_final_equity", "oos_final_equity",
            "is_win_rate_pct", "oos_win_rate_pct",
            "oos_max_dd_pct", "overfit_flag",
        ]
    available = [c for c in cols if c in df.columns]
    print()
    print("=" * 110)
    print(title)
    print("=" * 110)
    with pd.option_context("display.max_rows", None,
                           "display.max_columns", None,
                           "display.width", 220,
                           "display.float_format", lambda x: f"{x:,.2f}"):
        print(sub[available].to_string(index=False))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--signals", required=True)
    p.add_argument("--charts", required=True, nargs="+",
                   help="CSV files or globs (e.g. data/XAUUSD_M1_*.csv).")
    p.add_argument("--output", default="sweep_results.csv")
    p.add_argument("--grid", default=None,
                   help="Path to JSON grid file. If unset, uses DEFAULT_GRID.")

    # Per-axis CLI overrides (comma-separated).
    p.add_argument("--risks", default=None,
                   help="e.g. '0.02,0.05,0.10'")
    p.add_argument("--entry-counts", default=None,
                   help="e.g. '3,5,7,10'")
    p.add_argument("--ladders", default=None,
                   help="e.g. 'range_uniform,range_to_sl'")
    p.add_argument("--sl-gaps", default=None,
                   help="e.g. '0.5,1.0,2.0,5.0' (range_to_sl only)")
    p.add_argument("--activation-delays", default=None,
                   help="e.g. '0,2,5' (minutes)")
    p.add_argument("--pending-expiries", default=None,
                   help="e.g. '60,120,240' (minutes)")
    p.add_argument("--max-holds", default=None,
                   help="e.g. '60,90,120' (minutes)")
    p.add_argument("--sl-multipliers", default=None,
                   help="e.g. '1.0,1.25,1.5'")
    p.add_argument("--final-targets", default=None,
                   help="e.g. 'TP1,TP2,TP3'")
    p.add_argument("--lock-after-tp1", default=None,
                   help="e.g. 'True,False'")

    p.add_argument("--split-date", default="2026-04-01",
                   help="ISO date splitting train (before) and test (on/after).")
    p.add_argument("--no-split", action="store_true",
                   help="Skip the IS/OOS split, only run full-period.")
    p.add_argument("--workers", type=int,
                   default=max(1, (os.cpu_count() or 4) - 1))
    p.add_argument("--top", type=int, default=20,
                   help="How many top configs to print per ranking.")
    p.add_argument("--max-configs", type=int, default=10000,
                   help="Abort if grid expands to more than this.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print combo count and exit.")
    args = p.parse_args()

    if args.grid:
        grid = json.loads(Path(args.grid).read_text())
    else:
        grid = {k: list(v) for k, v in DEFAULT_GRID.items()}

    overrides = {
        "risk_per_signal":          _parse_csv_list(args.risks, float),
        "entry_count":              _parse_csv_list(args.entry_counts, int),
        "entry_ladder":             _parse_csv_list(args.ladders, str),
        "entry_sl_gap":             _parse_csv_list(args.sl_gaps, float),
        "activation_delay_minutes": _parse_csv_list(args.activation_delays, int),
        "pending_expiry_minutes":   _parse_csv_list(args.pending_expiries, int),
        "max_hold_minutes":         _parse_csv_list(args.max_holds, int),
        "sl_multiplier":            _parse_csv_list(args.sl_multipliers, float),
        "final_target":             _parse_csv_list(args.final_targets, str),
        "lock_after_tp1":           _parse_csv_list(args.lock_after_tp1, bool),
    }
    for k, v in overrides.items():
        if v is not None:
            grid[k] = v

    combos = expand_grid(grid)

    if BASELINE not in combos:
        combos.insert(0, dict(BASELINE))

    print(f"Grid expands to {len(combos)} configurations "
          f"(including the v2 baseline anchor).")
    if len(combos) > args.max_configs:
        print(f"Exceeds --max-configs {args.max_configs}; aborting.")
        print("Reduce the grid via --risks/--entry-counts/... or raise --max-configs.")
        return 1
    if args.dry_run:
        return 0

    chart_paths = _expand_chart_paths(args.charts)
    split_iso = None if args.no_split else args.split_date
    print(f"Workers: {args.workers}")
    if split_iso:
        print(f"Train/test split: signals < {split_iso} = IS, >= = OOS "
              f"(each side starts fresh at $1,000)")
    else:
        print("No train/test split.")

    start = time.time()
    results: list[dict] = []
    with mp.Pool(
            processes=args.workers,
            initializer=_init_worker,
            initargs=(chart_paths, args.signals, split_iso),
    ) as pool:
        step = max(1, len(combos) // 50)
        for i, r in enumerate(pool.imap_unordered(_eval_config, combos, chunksize=1), start=1):
            results.append(r)
            if i % step == 0 or i == len(combos):
                elapsed = time.time() - start
                rate = i / elapsed if elapsed else 0
                eta = (len(combos) - i) / rate if rate else 0
                print(f"  [{i}/{len(combos)}]  "
                      f"{elapsed:.0f}s elapsed  ~{eta:.0f}s remaining")

    elapsed = time.time() - start
    print(f"\nCompleted {len(results)} configs in {elapsed:.0f}s "
          f"({elapsed / max(len(results),1):.2f}s per config).")

    df = pd.DataFrame(results)
    out_path = Path(args.output)
    df.to_csv(out_path, index=False)
    print(f"\nSaved full results: {out_path.resolve()}")

    if "oos_final_equity" in df.columns:
        _print_top(df, args.top,
                   f"TOP {args.top} BY OUT-OF-SAMPLE FINAL EQUITY  "
                   f"(fresh $1,000 from {args.split_date})  -- the OOS column "
                   f"is what matters for live trading",
                   "oos_final_equity")
    _print_top(df, args.top,
               f"TOP {args.top} BY FULL-PERIOD FINAL EQUITY  (compounded). "
               f"Compare with OOS table above; large IS/OOS gap = overfit.",
               "full_final_equity")

    sort_col = "oos_final_equity" if "oos_final_equity" in df.columns else "full_final_equity"
    best = df.sort_values(sort_col, ascending=False).iloc[0].to_dict()
    json_path = out_path.with_suffix(".best.json")
    json_path.write_text(json.dumps(best, indent=2, default=str))
    print(f"\nBest config (by {sort_col}) saved to: {json_path.resolve()}")

    print()
    print("Reminder: every config above ran through the same advance_one_bar")
    print("simulator the smoke test locks. No lookahead, no marketable fills,")
    print("no skipped cancellations. OOS is the column to trust when picking")
    print("a config to actually trade.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())