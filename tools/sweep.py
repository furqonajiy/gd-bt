#!/usr/bin/env python3
"""Full-sample execution sweep for the XAUUSD engine.

This sweep is designed for the current tuning workflow:

1. Run every strategy scenario on all available signals/chart data.
2. Rank by total result, drawdown, monthly/yearly stability, and worst period.
3. Keep optional IS/OOS split support only as an extra diagnostic, not the
   default decision method.

Every configuration runs through the same `advance_one_bar` simulator used by
backtest/live replay: strict-touch fills, spread-aware triggers, TP1/TP2 locks,
and same-bar worst-case stop priority.

By default, sizing is fixed at 0.5 lot per entry. This keeps the sweep focused
on execution rules rather than money-management effects.

Examples:

    # Main full-sample sweep
    python tools/sweep.py \
      --signals signals.txt \
      --charts data/DAT_MT_SHIFTED_XAUUSD_M1_*.csv data/XAUUSD_M1_*.csv \
      --output reports/sweep_results_full.csv

    # Smaller first pass
    python tools/sweep.py \
      --signals signals.txt \
      --charts data/DAT_MT_SHIFTED_XAUUSD_M1_*.csv data/XAUUSD_M1_*.csv \
      --output reports/sweep_results_small.csv \
      --entry-counts 2,3 \
      --activation-delays 0,2 \
      --pending-expiries 5,20 \
      --max-holds 30,90 \
      --sl-multipliers 1.5 \
      --final-targets TP2,TP3 \
      --lock-after-tp1 True \
      --lock-after-tp2 True,False

    # Optional split diagnostic, not the default decision method
    python tools/sweep.py ... --split-date 2026-01-01
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
from statistics import median
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
    "sizing_mode": ["fixed"],
    "lot_per_entry": [0.5],
    "risk_per_signal": [0.05],

    # Signal-provider native execution rules.
    "entry_count": [1, 2, 3],
    "entry_ladder": ["signal_range_3"],
    "entry_sl_gap": [2.0],

    # Timing: includes the current balanced candidate and highest-profit variant.
    "activation_delay_minutes": [0, 1, 2, 3, 5],
    "pending_expiry_minutes": [3, 5, 7, 10, 15, 20],
    "max_hold_minutes": [15, 30, 45, 60, 90],

    # Stop/target management.
    "sl_multiplier": [1.0, 1.25, 1.5, 1.75, 2.0],
    "final_target": ["TP1", "TP2", "TP3"],
    "lock_after_tp1": [True, False],
    "lock_after_tp2": [True, False],
}

# Always included as anchors for comparison, even if they are outside the grid.
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


# ---------------------------------------------------------------------------
# result summaries
# ---------------------------------------------------------------------------

def _empty_result(config: StrategyConfig) -> dict:
    return {
        "config": asdict(config),
        "chart_start": None,
        "chart_end": None,
        "signals_parsed": 0,
        "signals_included": 0,
        "signals_excluded": 0,
        "final_equity": float(config.initial_capital),
        "net_profit": 0.0,
        "realized_pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "no_fills": 0,
        "open": 0,
        "win_rate_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "rows": [],
        "entry_rows": [],
        "monthly": [],
        "daily": [],
    }


def _backtest_subset(signals: list[Signal], config: StrategyConfig) -> dict:
    if not signals:
        return _empty_result(config)
    assert _CHART is not None
    return run_backtest(signals, _CHART, config)


def _profit_factor_from_rows(rows: list[dict]) -> float:
    gross_win = sum(float(r["pnl"]) for r in rows if r.get("pnl") is not None and float(r["pnl"]) > 0)
    gross_loss = -sum(float(r["pnl"]) for r in rows if r.get("pnl") is not None and float(r["pnl"]) < 0)
    if gross_loss <= 0:
        return float("inf") if gross_win > 0 else 0.0
    return gross_win / gross_loss


def _safe_mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _safe_std(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    avg = _safe_mean(values)
    return (sum((v - avg) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def _aggregate_yearly(monthly: list[dict]) -> dict[str, float]:
    yearly: dict[str, float] = {}
    for m in monthly:
        year = str(m.get("month", ""))[:4]
        if not year:
            continue
        yearly[year] = yearly.get(year, 0.0) + float(m.get("pnl", 0.0))
    return dict(sorted(yearly.items()))


def _period_stability_metrics(result: dict) -> dict:
    monthly = result.get("monthly", []) or []
    daily = result.get("daily", []) or []

    month_pnls = [float(m.get("pnl", 0.0)) for m in monthly]
    month_labels = [str(m.get("month", "")) for m in monthly]
    yearly = _aggregate_yearly(monthly)
    year_pnls = list(yearly.values())

    if month_pnls:
        worst_month_idx = min(range(len(month_pnls)), key=lambda i: month_pnls[i])
        best_month_idx = max(range(len(month_pnls)), key=lambda i: month_pnls[i])
        worst_month = month_labels[worst_month_idx]
        best_month = month_labels[best_month_idx]
        worst_month_pnl = month_pnls[worst_month_idx]
        best_month_pnl = month_pnls[best_month_idx]
    else:
        worst_month = ""
        best_month = ""
        worst_month_pnl = 0.0
        best_month_pnl = 0.0

    if yearly:
        worst_year = min(yearly, key=lambda k: yearly[k])
        best_year = max(yearly, key=lambda k: yearly[k])
        worst_year_pnl = yearly[worst_year]
        best_year_pnl = yearly[best_year]
    else:
        worst_year = ""
        best_year = ""
        worst_year_pnl = 0.0
        best_year_pnl = 0.0

    profitable_months = sum(1 for p in month_pnls if p > 0)
    losing_months = sum(1 for p in month_pnls if p < 0)
    flat_months = sum(1 for p in month_pnls if p == 0)
    profitable_years = sum(1 for p in year_pnls if p > 0)
    losing_years = sum(1 for p in year_pnls if p < 0)

    day_pnls = [float(d.get("pnl", 0.0)) for d in daily]
    active_day_pnls = [float(d.get("pnl", 0.0)) for d in daily if int(d.get("signals", 0)) > 0]

    month_map = {m: round(p, 2) for m, p in zip(month_labels, month_pnls)}
    year_map = {y: round(p, 2) for y, p in yearly.items()}

    return {
        "months_tested": len(month_pnls),
        "profitable_months": profitable_months,
        "losing_months": losing_months,
        "flat_months": flat_months,
        "positive_month_rate_pct": profitable_months / len(month_pnls) * 100.0 if month_pnls else 0.0,
        "avg_month_pnl": _safe_mean(month_pnls),
        "median_month_pnl": float(median(month_pnls)) if month_pnls else 0.0,
        "monthly_pnl_std": _safe_std(month_pnls),
        "worst_month": worst_month,
        "worst_month_pnl": worst_month_pnl,
        "best_month": best_month,
        "best_month_pnl": best_month_pnl,
        "years_tested": len(year_pnls),
        "profitable_years": profitable_years,
        "losing_years": losing_years,
        "positive_year_rate_pct": profitable_years / len(year_pnls) * 100.0 if year_pnls else 0.0,
        "worst_year": worst_year,
        "worst_year_pnl": worst_year_pnl,
        "best_year": best_year,
        "best_year_pnl": best_year_pnl,
        "avg_day_pnl": _safe_mean(day_pnls),
        "avg_active_day_pnl": _safe_mean(active_day_pnls),
        "worst_day_pnl": min(active_day_pnls) if active_day_pnls else 0.0,
        "best_day_pnl": max(active_day_pnls) if active_day_pnls else 0.0,
        "monthly_pnl_json": json.dumps(month_map, sort_keys=True),
        "yearly_pnl_json": json.dumps(year_map, sort_keys=True),
    }


def _entry_status_metrics(result: dict) -> dict:
    entries = result.get("entry_rows", []) or []
    total = len(entries)
    counts: dict[str, int] = {}
    for e in entries:
        status = str(e.get("entry_status", ""))
        counts[status] = counts.get(status, 0) + 1

    def count(name: str) -> int:
        return counts.get(name, 0)

    def rate(n: int) -> float:
        return n / total * 100.0 if total else 0.0

    tp1 = count("TP1")
    tp2 = count("TP2")
    tp3 = count("TP3")
    sl = count("SL")
    lock_tp1 = count("LOCK_TP1")
    lock_tp2 = count("LOCK_TP2")
    time_exit = count("TIME_EXIT")
    no_fill = count("NO_FILL")
    open_count = count("OPEN")

    return {
        "entry_count_total": total,
        "entry_tp1": tp1,
        "entry_tp2": tp2,
        "entry_tp3": tp3,
        "entry_sl": sl,
        "entry_lock_tp1": lock_tp1,
        "entry_lock_tp2": lock_tp2,
        "entry_time_exit": time_exit,
        "entry_no_fill": no_fill,
        "entry_open": open_count,
        "entry_tp1_rate_pct": rate(tp1),
        "entry_tp2_rate_pct": rate(tp2),
        "entry_tp3_rate_pct": rate(tp3),
        "entry_sl_rate_pct": rate(sl),
        "entry_lock_tp1_rate_pct": rate(lock_tp1),
        "entry_lock_tp2_rate_pct": rate(lock_tp2),
        "entry_no_fill_rate_pct": rate(no_fill),
        "entry_status_json": json.dumps(dict(sorted(counts.items())), sort_keys=True),
    }


def _full_summary(result: dict, config: StrategyConfig) -> dict:
    rows = result.get("rows", []) or []
    pnl = float(result.get("net_profit", 0.0))
    signals = int(result.get("signals_included", 0))
    wins = int(result.get("wins", 0))
    losses = int(result.get("losses", 0))
    no_fills = int(result.get("no_fills", 0))
    open_count = int(result.get("open", 0))
    trade_count = wins + losses

    out = {
        "chart_start": result.get("chart_start"),
        "chart_end": result.get("chart_end"),
        "signals_parsed": result.get("signals_parsed", 0),
        "signals_included": signals,
        "signals_excluded": result.get("signals_excluded", 0),
        "final_equity": result.get("final_equity", config.initial_capital),
        "net_profit": pnl,
        "return_pct": pnl / config.initial_capital * 100.0 if config.initial_capital else 0.0,
        "wins": wins,
        "losses": losses,
        "no_fills": no_fills,
        "open": open_count,
        "trade_count": trade_count,
        "win_rate_pct": result.get("win_rate_pct", 0.0),
        "no_fill_rate_pct": no_fills / signals * 100.0 if signals else 0.0,
        "open_rate_pct": open_count / signals * 100.0 if signals else 0.0,
        "avg_pnl_per_signal": pnl / signals if signals else 0.0,
        "avg_pnl_per_trade": pnl / trade_count if trade_count else 0.0,
        "profit_factor": _profit_factor_from_rows(rows),
        "max_drawdown_pct": float(result.get("max_drawdown_pct", 0.0)),
    }
    out.update(_period_stability_metrics(result))
    out.update(_entry_status_metrics(result))
    return out


def _quality_score(summary: dict, *,
                   dd_weight: float,
                   worst_month_weight: float,
                   losing_month_weight: float,
                   losing_year_weight: float) -> dict:
    net_profit = float(summary.get("net_profit", 0.0))
    max_dd_pct = abs(float(summary.get("max_drawdown_pct", 0.0)))
    worst_month_pnl = float(summary.get("worst_month_pnl", 0.0))
    losing_months = int(summary.get("losing_months", 0))
    losing_years = int(summary.get("losing_years", 0))
    positive_month_rate = float(summary.get("positive_month_rate_pct", 0.0))
    positive_year_rate = float(summary.get("positive_year_rate_pct", 0.0))
    profit_factor = float(summary.get("profit_factor", 0.0))
    if profit_factor == float("inf"):
        profit_factor = 10.0

    drawdown_penalty = max_dd_pct * dd_weight
    worst_month_penalty = max(0.0, -worst_month_pnl) * worst_month_weight
    losing_month_penalty = losing_months * losing_month_weight
    losing_year_penalty = losing_years * losing_year_weight
    stability_bonus = positive_month_rate * 5.0 + positive_year_rate * 5.0
    profit_factor_bonus = min(profit_factor, 5.0) * 50.0

    score = (
        net_profit
        - drawdown_penalty
        - worst_month_penalty
        - losing_month_penalty
        - losing_year_penalty
        + stability_bonus
        + profit_factor_bonus
    )
    return {
        "quality_score": score,
        "drawdown_penalty": drawdown_penalty,
        "worst_month_penalty": worst_month_penalty,
        "losing_month_penalty": losing_month_penalty,
        "losing_year_penalty": losing_year_penalty,
        "stability_bonus": stability_bonus,
        "profit_factor_bonus": profit_factor_bonus,
    }


def _optional_split_summary(config: StrategyConfig) -> dict:
    if _SPLIT_TIME is None or _SIGNALS is None:
        return {}
    train = [s for s in _SIGNALS if s.signal_time_chart < _SPLIT_TIME]
    test = [s for s in _SIGNALS if s.signal_time_chart >= _SPLIT_TIME]
    is_result = _backtest_subset(train, config)
    oos_result = _backtest_subset(test, config)
    is_summary = _full_summary(is_result, config)
    oos_summary = _full_summary(oos_result, config)
    return {
        "is_net_profit": is_summary["net_profit"],
        "is_return_pct": is_summary["return_pct"],
        "is_max_drawdown_pct": is_summary["max_drawdown_pct"],
        "is_positive_month_rate_pct": is_summary["positive_month_rate_pct"],
        "is_signals": is_summary["signals_included"],
        "oos_net_profit": oos_summary["net_profit"],
        "oos_return_pct": oos_summary["return_pct"],
        "oos_max_drawdown_pct": oos_summary["max_drawdown_pct"],
        "oos_positive_month_rate_pct": oos_summary["positive_month_rate_pct"],
        "oos_signals": oos_summary["signals_included"],
        "positive_is_oos": bool(is_summary["net_profit"] > 0 and oos_summary["net_profit"] > 0),
    }


def _eval_config(combo: dict) -> dict:
    assert _SIGNALS is not None

    config = StrategyConfig(**{k: combo[k] for k in _STRATEGY_FIELDS})
    full = _backtest_subset(_SIGNALS, config)
    summary = _full_summary(full, config)

    out = dict(combo)
    out.update(summary)
    out.update(_quality_score(
        summary,
        dd_weight=_SCORING["dd_weight"],
        worst_month_weight=_SCORING["worst_month_weight"],
        losing_month_weight=_SCORING["losing_month_weight"],
        losing_year_weight=_SCORING["losing_year_weight"],
    ))
    out.update(_optional_split_summary(config))
    return out


# Scoring values are set by CLI in main() before multiprocessing starts.
_SCORING = {
    "dd_weight": 50.0,
    "worst_month_weight": 0.25,
    "losing_month_weight": 50.0,
    "losing_year_weight": 250.0,
}


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
    if df.empty:
        print("No results to print.")
        return
    sub = df.sort_values(sort_col, ascending=False).head(n)
    cols = [
        "preset_name", "sizing_mode", "lot_per_entry",
        "entry_count", "entry_ladder", "activation_delay_minutes",
        "pending_expiry_minutes", "max_hold_minutes", "sl_multiplier",
        "final_target", "lock_after_tp1", "lock_after_tp2",
        "net_profit", "quality_score", "max_drawdown_pct",
        "win_rate_pct", "profit_factor", "avg_pnl_per_signal",
        "positive_month_rate_pct", "profitable_months", "losing_months",
        "worst_month", "worst_month_pnl", "best_month", "best_month_pnl",
        "positive_year_rate_pct", "worst_year", "worst_year_pnl",
        "no_fill_rate_pct", "entry_tp3_rate_pct", "entry_sl_rate_pct",
        "entry_lock_tp1_rate_pct", "entry_lock_tp2_rate_pct",
    ]
    if "oos_net_profit" in df.columns:
        cols += ["is_net_profit", "oos_net_profit", "positive_is_oos"]
    available = [c for c in cols if c in df.columns]
    print()
    print("=" * 150)
    print(title)
    print("=" * 150)
    with pd.option_context(
        "display.max_rows", None,
        "display.max_columns", None,
        "display.width", 320,
        "display.float_format", lambda x: f"{x:,.2f}",
    ):
        print(sub[available].to_string(index=False))


def _write_best_files(df: pd.DataFrame, out_path: Path) -> None:
    best_quality = df.sort_values("quality_score", ascending=False).iloc[0].to_dict()
    best_profit = df.sort_values("net_profit", ascending=False).iloc[0].to_dict()
    best_monthly = df.sort_values(
        ["positive_month_rate_pct", "net_profit"], ascending=[False, False]
    ).iloc[0].to_dict()

    out_path.with_suffix(".best_quality.json").write_text(json.dumps(best_quality, indent=2, default=str))
    out_path.with_suffix(".best_profit.json").write_text(json.dumps(best_profit, indent=2, default=str))
    out_path.with_suffix(".best_monthly.json").write_text(json.dumps(best_monthly, indent=2, default=str))
    out_path.with_suffix(".presets.json").write_text(
        json.dumps({k: asdict(v) for k, v in PRESET_ANCHORS.items()}, indent=2)
    )


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
                   help="CSV files or globs, e.g. data/XAUUSD_M1_*.csv")
    p.add_argument("--output", default="sweep_results_full.csv")
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

    # Optional diagnostics. Full-sample is the default and the primary ranking.
    p.add_argument("--split-date", default=None,
                   help="Optional ISO split date. Adds IS/OOS diagnostics, but ranking remains full-sample-first.")

    # Score controls.
    p.add_argument("--dd-weight", type=float, default=50.0,
                   help="Quality-score penalty per 1 percentage point of max drawdown.")
    p.add_argument("--worst-month-weight", type=float, default=0.25,
                   help="Quality-score penalty per $1 of worst-month loss.")
    p.add_argument("--losing-month-weight", type=float, default=50.0,
                   help="Quality-score penalty per losing month.")
    p.add_argument("--losing-year-weight", type=float, default=250.0,
                   help="Quality-score penalty per losing year.")

    p.add_argument("--workers", type=int,
                   default=max(1, (os.cpu_count() or 4) - 1))
    p.add_argument("--top", type=int, default=20,
                   help="How many top configs to print per ranking.")
    p.add_argument("--max-configs", type=int, default=30000,
                   help="Abort if grid expands to more than this.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print combo count and sample config, then exit.")
    args = p.parse_args()

    _SCORING.update({
        "dd_weight": args.dd_weight,
        "worst_month_weight": args.worst_month_weight,
        "losing_month_weight": args.losing_month_weight,
        "losing_year_weight": args.losing_year_weight,
    })

    if args.grid:
        grid = json.loads(Path(args.grid).read_text())
    else:
        grid = {k: list(v) for k, v in DEFAULT_GRID.items()}

    overrides = {
        "sizing_mode": _parse_csv_list(args.sizing_modes, str),
        "lot_per_entry": _parse_csv_list(args.lots, float),
        "risk_per_signal": _parse_csv_list(args.risks, float),
        "entry_count": _parse_csv_list(args.entry_counts, int),
        "entry_ladder": _parse_csv_list(args.ladders, str),
        "entry_sl_gap": _parse_csv_list(args.sl_gaps, float),
        "activation_delay_minutes": _parse_csv_list(args.activation_delays, int),
        "pending_expiry_minutes": _parse_csv_list(args.pending_expiries, int),
        "max_hold_minutes": _parse_csv_list(args.max_holds, int),
        "sl_multiplier": _parse_csv_list(args.sl_multipliers, float),
        "final_target": _parse_csv_list(args.final_targets, str),
        "lock_after_tp1": _parse_csv_list(args.lock_after_tp1, bool),
        "lock_after_tp2": _parse_csv_list(args.lock_after_tp2, bool),
    }
    for k, v in overrides.items():
        if v is not None:
            grid[k] = v

    combos = expand_grid(grid)

    print(f"Grid expands to {len(combos)} configurations including preset anchors: "
          f"{', '.join(PRESET_ANCHORS)}.")
    print("Ranking mode: full-sample first, with monthly/yearly stability metrics.")
    if args.split_date:
        print(f"Optional split diagnostics enabled at {args.split_date}; full-sample ranking still applies.")
    if len(combos) > args.max_configs:
        print(f"Exceeds --max-configs {args.max_configs}; aborting.")
        print("Reduce the grid via --entry-counts/--pending-expiries/... or raise --max-configs.")
        return 1
    if args.dry_run:
        print(json.dumps({"sample_config": combos[0], "scoring": _SCORING}, indent=2, default=str))
        return 0

    chart_paths = _expand_chart_paths(args.charts)
    print(f"Workers: {args.workers}")
    print(f"Chart files: {len(chart_paths)}")

    start = time.time()
    results: list[dict] = []
    with mp.Pool(
        processes=args.workers,
        initializer=_init_worker,
        initargs=(chart_paths, args.signals, args.split_date),
    ) as pool:
        step = max(1, len(combos) // 50)
        for i, r in enumerate(pool.imap_unordered(_eval_config, combos, chunksize=1), start=1):
            results.append(r)
            if i % step == 0 or i == len(combos):
                elapsed = time.time() - start
                rate = i / elapsed if elapsed else 0.0
                eta = (len(combos) - i) / rate if rate else 0.0
                print(f"  [{i}/{len(combos)}]  {elapsed:.0f}s elapsed  ~{eta:.0f}s remaining")

    elapsed = time.time() - start
    print(f"\nCompleted {len(results)} configs in {elapsed:.0f}s "
          f"({elapsed / max(len(results), 1):.2f}s per config).")

    df = pd.DataFrame(results)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"\nSaved full results: {out_path.resolve()}")

    if not df.empty:
        _write_best_files(df, out_path)
        print(f"Saved best config JSON files next to: {out_path.resolve()}")

        _print_top(df, args.top, f"TOP {args.top} BY QUALITY SCORE", "quality_score")
        _print_top(df, args.top, f"TOP {args.top} BY FULL-SAMPLE NET PROFIT", "net_profit")
        _print_top(df, args.top, f"TOP {args.top} BY MONTHLY POSITIVE RATE", "positive_month_rate_pct")
        _print_top(df, args.top, f"TOP {args.top} BY LOWEST DRAWDOWN", "max_drawdown_pct")

    print()
    print("Selection reminder:")
    print("  1. Start with high net_profit and quality_score.")
    print("  2. Reject scenarios with bad worst_month_pnl or too many losing_months.")
    print("  3. Compare yearly_pnl_json and monthly_pnl_json before using live.")
    print("  4. Run tools/analyze_signal_paths.py on finalists to inspect TP/SL paths.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
