#!/usr/bin/env python3
"""Parameter sweep for the XAUUSD engine.

Every configuration runs through the same `advance_one_bar` simulator used by
backtest/live replay: strict-touch fills, spread-aware triggers, and same-bar
worst-case stop priority.

The sweep is focused on execution logic, not money management. By default it
uses fixed 0.5 lot per entry so P&L comparisons are not distorted by changing
risk or compounding.

Usage:
    python tools/sweep.py --signals signals.txt \
                          --charts data/XAUUSD_M1_*.csv \
                          --output sweep_results.csv

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
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

# Make `xauusd_trading` importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xauusd_trading import (  # noqa: E402
    BALANCED_LIVE_CONFIG,
    HIGHEST_PROFIT_CONFIG,
    LOWER_EXPOSURE_CONFIG,
    CsvChartSource,
    Signal,
    StrategyConfig,
    parse_signals_file,
    run_backtest,
)


# ---------------------------------------------------------------------------
# default grid — override via CLI flags
# ---------------------------------------------------------------------------

DEFAULT_GRID: dict[str, list[Any]] = {
    # Keep sizing fixed while tuning execution.
    "sizing_mode":               ["fixed"],
    "lot_per_entry":             [0.5],
    "risk_per_signal":           [0.05],

    # Signal-provider native execution rules.
    "entry_count":               [1, 2, 3],
    "entry_ladder":              ["signal_range_3"],
    "entry_sl_gap":              [2.0],

    # Timing: include the known variants and local neighborhood around them.
    "activation_delay_minutes":  [0, 1, 2, 3, 5],
    "pending_expiry_minutes":    [3, 5, 7, 10, 15, 20],
    "max_hold_minutes":          [15, 30, 45, 60, 90],

    # Trade management.
    "sl_multiplier":             [1.0, 1.25, 1.5, 1.75, 2.0],
    "final_target":              ["TP1", "TP2", "TP3"],
    "lock_after_tp1":            [True, False],
    "lock_after_tp2":            [True, False],
}

# Always included as anchors for comparison.
PRESET_ANCHORS = {
    "balanced_live_candidate": BALANCED_LIVE_CONFIG,
    "highest_profit_variant": HIGHEST_PROFIT_CONFIG,
    "lower_exposure_variant": LOWER_EXPOSURE_CONFIG,
}

_STRATEGY_FIELDS = {
    "sizing_mode", "lot_per_entry", "risk_per_signal",
    "entry_count", "entry_ladder", "entry_sl_gap",
    "activation_delay_minutes", "pending_expiry_minutes", "max_hold_minutes",
    "sl_multiplier", "final_target", "lock_after_tp1", "lock_after_tp2",
}


# ---------------------------------------------------------------------------
# worker pool plumbing
# ---------------------------------------------------------------------------

_CHART: CsvChartSource | None = None
_SIGNALS: list[Signal] | None = None
_SPLIT_TIME: pd.Timestamp | None = None


def _init_worker(chart_paths: list[str], signals_path: str, split_iso: str | None):
    global _CHART, _SIGNALS, _SPLIT_TIME
    _CHART = CsvChartSource([Path(p) for p in chart_paths])
    _SIGNALS = parse_signals_file(Path(signals_path))
    _SPLIT_TIME = pd.Timestamp(split_iso) if split_iso else None


def _empty_result(config: StrategyConfig) -> dict:
    return {
        "final_equity": float(config.initial_capital),
        "net_profit": 0.0,
        "realized_pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "no_fills": 0,
        "open": 0,
        "win_rate_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "signals_included": 0,
        "rows": [],
    }


def _backtest_subset(signals: list[Signal], config: StrategyConfig) -> dict:
    """run_backtest may early-exit on equity <= 0; defend against empty input."""
    if not signals:
        return _empty_result(config)
    assert _CHART is not None
    return run_backtest(signals, _CHART, config)


def _profit_factor(rows: list[dict]) -> float:
    gross_win = sum(r["pnl"] for r in rows if r.get("pnl") is not None and r["pnl"] > 0)
    gross_loss = -sum(r["pnl"] for r in rows if r.get("pnl") is not None and r["pnl"] < 0)
    if gross_loss <= 0:
        return float("inf") if gross_win > 0 else 0.0
    return gross_win / gross_loss


def _summarize_result(prefix: str, result: dict, config: StrategyConfig) -> dict:
    rows = result.get("rows", [])
    pnl = result.get("net_profit", 0.0)
    signals = result.get("signals_included", 0)
    avg = pnl / signals if signals else 0.0
    ret = pnl / config.initial_capital * 100.0 if config.initial_capital else 0.0
    return {
        f"{prefix}_final_equity": result["final_equity"],
        f"{prefix}_net_profit": pnl,
        f"{prefix}_return_pct": ret,
        f"{prefix}_wins": result["wins"],
        f"{prefix}_losses": result["losses"],
        f"{prefix}_no_fills": result["no_fills"],
        f"{prefix}_open": result["open"],
        f"{prefix}_win_rate_pct": result["win_rate_pct"],
        f"{prefix}_max_dd_pct": result["max_drawdown_pct"],
        f"{prefix}_signals": signals,
        f"{prefix}_avg_pnl_per_signal": avg,
        f"{prefix}_profit_factor": _profit_factor(rows),
    }


def _eval_config(combo: dict) -> dict:
    """Evaluate one configuration: full-period + optional IS/OOS."""
    assert _SIGNALS is not None

    config = StrategyConfig(**{k: combo[k] for k in _STRATEGY_FIELDS})
    full = _backtest_subset(_SIGNALS, config)

    out = dict(combo)
    out.update(_summarize_result("full", full, config))

    if _SPLIT_TIME is not None:
        train = [s for s in _SIGNALS if s.signal_time_chart < _SPLIT_TIME]
        test = [s for s in _SIGNALS if s.signal_time_chart >= _SPLIT_TIME]
        is_ = _backtest_subset(train, config)
        oos = _backtest_subset(test, config)
        out.update(_summarize_result("is", is_, config))
        out.update(_summarize_result("oos", oos, config))

        out["positive_is_oos"] = bool(out["is_net_profit"] > 0 and out["oos_net_profit"] > 0)
        out["overfit_flag"] = bool(out["is_return_pct"] > 50.0 and out["oos_return_pct"] < 5.0)
    else:
        out["positive_is_oos"] = False
        out["overfit_flag"] = False

    # Robustness score: prefer OOS when available, penalize drawdown and overfit.
    primary_profit = out.get("oos_net_profit", out["full_net_profit"])
    primary_dd = abs(out.get("oos_max_dd_pct", out["full_max_dd_pct"]))
    out["robust_score"] = (
        primary_profit
        - primary_dd * 25.0
        - (500.0 if out.get("overfit_flag") else 0.0)
        + (250.0 if out.get("positive_is_oos") else 0.0)
    )
    return out


# ---------------------------------------------------------------------------
# grid expansion
# ---------------------------------------------------------------------------

def _combo_key(d: dict) -> tuple:
    return tuple(d[k] for k in sorted(_STRATEGY_FIELDS))


def _config_to_combo(config: StrategyConfig, preset_name: str) -> dict:
    d = {k: getattr(config, k) for k in _STRATEGY_FIELDS}
    d["preset_name"] = preset_name
    return d


def expand_grid(grid: dict[str, list]) -> list[dict]:
    """Cartesian product of grid values, with redundant combos removed."""
    keys = list(grid.keys())
    seen: set[tuple] = set()
    out: list[dict] = []

    for values in itertools.product(*[grid[k] for k in keys]):
        d = dict(zip(keys, values))
        if d.get("entry_ladder") != "range_to_sl":
            d["entry_sl_gap"] = grid.get("entry_sl_gap", [2.0])[0]
        d["preset_name"] = "grid"
        key = _combo_key(d)
        if key in seen:
            continue
        seen.add(key)
        out.append(d)

    for name, config in PRESET_ANCHORS.items():
        d = _config_to_combo(config, name)
        key = _combo_key(d)
        if key in seen:
            continue
        seen.add(key)
        out.insert(0, d)

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
        "preset_name", "sizing_mode", "lot_per_entry",
        "entry_count", "entry_ladder", "entry_sl_gap",
        "activation_delay_minutes", "pending_expiry_minutes",
        "max_hold_minutes", "sl_multiplier", "final_target",
        "lock_after_tp1", "lock_after_tp2",
        "full_net_profit", "full_win_rate_pct", "full_max_dd_pct",
        "full_profit_factor", "full_avg_pnl_per_signal",
    ]
    if "oos_net_profit" in df.columns:
        cols += [
            "is_net_profit", "oos_net_profit",
            "is_win_rate_pct", "oos_win_rate_pct",
            "oos_max_dd_pct", "positive_is_oos", "overfit_flag",
        ]
    cols += ["robust_score"]
    available = [c for c in cols if c in df.columns]
    print()
    print("=" * 120)
    print(title)
    print("=" * 120)
    with pd.option_context("display.max_rows", None,
                           "display.max_columns", None,
                           "display.width", 260,
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
    p.add_argument("--sizing-modes", default=None, help="e.g. 'fixed,risk'")
    p.add_argument("--lots", default=None, help="e.g. '0.25,0.5'")
    p.add_argument("--risks", default=None, help="e.g. '0.02,0.05,0.10'")
    p.add_argument("--entry-counts", default=None, help="e.g. '1,2,3'")
    p.add_argument("--ladders", default=None, help="e.g. 'signal_range_3,range_uniform,range_to_sl'")
    p.add_argument("--sl-gaps", default=None, help="e.g. '1.0,2.0,5.0' (range_to_sl only)")
    p.add_argument("--activation-delays", default=None, help="e.g. '0,1,2,3,5' minutes")
    p.add_argument("--pending-expiries", default=None, help="e.g. '3,5,7,10,20' minutes")
    p.add_argument("--max-holds", default=None, help="e.g. '15,30,45,60,90' minutes")
    p.add_argument("--sl-multipliers", default=None, help="e.g. '1.0,1.25,1.5,2.0'")
    p.add_argument("--final-targets", default=None, help="e.g. 'TP1,TP2,TP3'")
    p.add_argument("--lock-after-tp1", default=None, help="e.g. 'True,False'")
    p.add_argument("--lock-after-tp2", default=None, help="e.g. 'True,False'")

    p.add_argument("--split-date", default="2025-01-01",
                   help="ISO date splitting train (before) and test (on/after).")
    p.add_argument("--no-split", action="store_true",
                   help="Skip the IS/OOS split, only run full-period.")
    p.add_argument("--workers", type=int,
                   default=max(1, (os.cpu_count() or 4) - 1))
    p.add_argument("--top", type=int, default=20,
                   help="How many top configs to print per ranking.")
    p.add_argument("--max-configs", type=int, default=20000,
                   help="Abort if grid expands to more than this.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print combo count and exit.")
    args = p.parse_args()

    if args.grid:
        grid = json.loads(Path(args.grid).read_text())
    else:
        grid = {k: list(v) for k, v in DEFAULT_GRID.items()}

    overrides = {
        "sizing_mode":               _parse_csv_list(args.sizing_modes, str),
        "lot_per_entry":             _parse_csv_list(args.lots, float),
        "risk_per_signal":           _parse_csv_list(args.risks, float),
        "entry_count":               _parse_csv_list(args.entry_counts, int),
        "entry_ladder":              _parse_csv_list(args.ladders, str),
        "entry_sl_gap":              _parse_csv_list(args.sl_gaps, float),
        "activation_delay_minutes":  _parse_csv_list(args.activation_delays, int),
        "pending_expiry_minutes":    _parse_csv_list(args.pending_expiries, int),
        "max_hold_minutes":          _parse_csv_list(args.max_holds, int),
        "sl_multiplier":             _parse_csv_list(args.sl_multipliers, float),
        "final_target":              _parse_csv_list(args.final_targets, str),
        "lock_after_tp1":            _parse_csv_list(args.lock_after_tp1, bool),
        "lock_after_tp2":            _parse_csv_list(args.lock_after_tp2, bool),
    }
    for k, v in overrides.items():
        if v is not None:
            grid[k] = v

    combos = expand_grid(grid)

    print(f"Grid expands to {len(combos)} configurations including preset anchors: "
          f"{', '.join(PRESET_ANCHORS)}.")
    if len(combos) > args.max_configs:
        print(f"Exceeds --max-configs {args.max_configs}; aborting.")
        print("Reduce the grid via --entry-counts/--pending-expiries/... or raise --max-configs.")
        return 1
    if args.dry_run:
        print(json.dumps({"sample_config": combos[0]}, indent=2, default=str))
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

    _print_top(df, args.top, f"TOP {args.top} BY ROBUST SCORE", "robust_score")
    if "oos_net_profit" in df.columns:
        _print_top(df, args.top, f"TOP {args.top} BY OUT-OF-SAMPLE NET PROFIT", "oos_net_profit")
    _print_top(df, args.top, f"TOP {args.top} BY FULL-PERIOD NET PROFIT", "full_net_profit")

    sort_col = "robust_score"
    best = df.sort_values(sort_col, ascending=False).iloc[0].to_dict()
    json_path = out_path.with_suffix(".best.json")
    json_path.write_text(json.dumps(best, indent=2, default=str))
    print(f"\nBest config (by {sort_col}) saved to: {json_path.resolve()}")

    preset_path = out_path.with_suffix(".presets.json")
    preset_path.write_text(json.dumps({k: asdict(v) for k, v in PRESET_ANCHORS.items()}, indent=2))
    print(f"Preset anchors saved to: {preset_path.resolve()}")

    print()
    print("Reminder: choose configs that are positive in both IS and OOS, not just")
    print("the highest full-sample P&L. Then inspect path-analysis metrics before")
    print("using the scenario live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
