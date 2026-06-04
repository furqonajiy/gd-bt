#!/usr/bin/env python3
"""Resumable parameter sweep using the wired concurrent lifecycle engine.

This tool intentionally does not mutate DEFAULT_CONFIG.  It constructs explicit
StrategyConfig objects, filters the raw provider signal feed once per preset, and
runs a chronological concurrent backtest where overlapping signals remain alive
at the same time.  New signals are sized before the current bar is processed, so
only P&L/bonus realised before that bar can affect their lot sizing.

Typical run:

    python tools/sweep.py \
      --signals signals.txt \
      --charts data/XAUUSD_M1_202602_*.csv data/XAUUSD_M1_202603_*.csv \
               data/XAUUSD_M1_202604_*.csv data/XAUUSD_M1_202605_*.csv \
               data/XAUUSD_M1_202606_*.csv \
      --output-dir reports/sweep_2026_feb_jun \
      --max-candidates 500 \
      --seed 42

Outputs:
- generated/live_provider_<preset>.txt files used for every run with that preset.
- results.jsonl checkpoint, safe to resume.
- leaderboard.csv and leaderboard.xlsx.
- top_configs/*.json with verbatim config dictionaries and suggested live command.
"""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import random
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading import (  # noqa: E402
    CONTRACT_SIZE_OZ,
    DEFAULT_CONFIG,
    CsvChartSource,
    StrategyConfig,
    advance_one_bar,
    open_position,
    parse_signals_file,
)
from xauusd_trading.core.chart import iter_bars  # noqa: E402
from xauusd_trading.core.trend_runner import prewarm_indicators_from_dataframe  # noqa: E402
from xauusd_trading.strategy.backtest import position_status  # noqa: E402
from tools.filter_provider_signals import keep_signal, parse_provider_signals, write_signals  # noqa: E402


FILTER_PRESETS = [
    "high_growth_hour_side",
    "best_hours",
    "no_bad_hours",
    "all",
    "research_month_hour_side",
]

LIVE_READY_FEATURES = "STRUCTURAL_PARITY_EXPECTED_RUN_TESTS"
LIVE_READY_NO_TREND = "LIVE_READY_STRUCTURAL"
RESEARCH_ONLY = "RESEARCH_ONLY_OR_OVERFIT_FILTER"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def _expand_chart_paths(patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            matches = sorted(glob.glob(pat))
            if not matches:
                raise SystemExit(f"No files match chart pattern: {pat}")
            out.extend(Path(m) for m in matches)
        else:
            p = Path(pat)
            if not p.exists():
                raise SystemExit(f"Chart file not found: {pat}")
            out.append(p)
    if not out:
        raise SystemExit("No chart files provided")
    return out


def _bool_text(value: bool) -> str:
    return "true" if bool(value) else "false"


def _json_hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:16]


def _now_label() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _entry_closed_lots(pos) -> float:
    return sum(
        float(e.lot or 0.0)
        for e in pos.entries
        if e.fill_time is not None and e.exit_time is not None
    )


def _bonus_for_position(pos, config: StrategyConfig) -> float:
    return _entry_closed_lots(pos) * float(getattr(config, "bonus_per_closed_lot", 0.0) or 0.0)


def _open_risk_abs(pos: Any, config: StrategyConfig) -> float:
    """Conservative open-risk proxy, not mark-to-market.

    For open entries, use the current effective stop.  For pending entries, use
    the entry's initial SL as if the pending order fills.  This estimates clustered
    exposure and is deliberately not used for realized equity compounding.
    """
    side = pos.signal.side
    risk = 0.0
    for e in pos.entries:
        if e.status == "OPEN":
            stop = pos.effective_stop_for(e, config)
            if side == "BUY":
                risk += max(0.0, float(e.entry_price) - float(stop)) * float(e.lot or 0.0) * CONTRACT_SIZE_OZ
            else:
                risk += max(0.0, float(stop) - float(e.entry_price)) * float(e.lot or 0.0) * CONTRACT_SIZE_OZ
        elif e.status == "PENDING":
            if side == "BUY":
                risk += max(0.0, float(e.entry_price) - float(e.initial_sl)) * float(e.lot or 0.0) * CONTRACT_SIZE_OZ
            else:
                risk += max(0.0, float(e.initial_sl) - float(e.entry_price)) * float(e.lot or 0.0) * CONTRACT_SIZE_OZ
    return risk


def _new_bucket(key: str, value: str, equity_start: float) -> dict[str, Any]:
    return {
        key: value,
        "signals": 0,
        "wins": 0,
        "losses": 0,
        "no_fills": 0,
        "open": 0,
        "pnl": 0.0,
        "trading_pnl": 0.0,
        "bonus": 0.0,
        "closed_lots": 0.0,
        "equity_start": equity_start,
        "equity_end": equity_start,
    }


def _finalize_bucket(bucket: dict[str, Any]) -> None:
    wl = bucket["wins"] + bucket["losses"]
    bucket["win_rate_pct"] = bucket["wins"] / wl * 100.0 if wl else 0.0
    start = float(bucket.get("equity_start") or 0.0)
    bucket["pnl_pct"] = bucket["pnl"] / start * 100.0 if start > 0 else 0.0


# ---------------------------------------------------------------------------
# Signal preparation
# ---------------------------------------------------------------------------
def prepare_filtered_signals(raw_signals_path: Path, output_dir: Path, preset: str) -> Path:
    output = output_dir / "generated" / f"live_provider_{preset}.txt"
    if output.exists():
        return output
    rows = parse_provider_signals(raw_signals_path)
    kept = [row for row in rows if keep_signal(row, preset)]
    write_signals(kept, output)
    return output


# ---------------------------------------------------------------------------
# Corrected concurrent backtest orchestration
# ---------------------------------------------------------------------------
def run_concurrent_backtest(
        signals: list,
        chart: CsvChartSource,
        config: StrategyConfig,
        *,
        exclude_structural_anomalies: bool = False,
        label: str = "full",
) -> dict[str, Any]:
    chart_df = chart.dataframe
    chart_start = chart.first_time()
    chart_end = chart.last_time()
    if chart_start is None or chart_end is None:
        raise ValueError("empty chart source")

    eligible = []
    excluded = []
    for sig in sorted(signals, key=lambda s: s.signal_time_chart):
        if sig.signal_time_chart < chart_start:
            excluded.append({"signal_key": sig.signal_key, "reason": "before chart start"})
            continue
        if sig.signal_time_chart > chart_end:
            excluded.append({"signal_key": sig.signal_key, "reason": "after chart end"})
            continue
        if exclude_structural_anomalies and sig.structural_anomaly:
            excluded.append({"signal_key": sig.signal_key, "reason": "structural anomaly"})
            continue
        activation = sig.signal_time_chart + timedelta(minutes=config.activation_delay_minutes)
        eligible.append((activation, sig))

    equity = float(config.initial_capital)
    peak_equity = equity
    max_dd_pct = 0.0
    max_concurrent_risk_dd_pct = 0.0
    max_open_positions = 0
    max_open_entries = 0
    max_pending_entries = 0
    max_open_risk_abs = 0.0

    active: list[Any] = []
    rows: list[dict[str, Any]] = []
    entry_rows: list[dict[str, Any]] = []
    pos_by_key: dict[str, Any] = {}
    next_signal = 0

    for bar in iter_bars(chart_df):
        # Add newly activated signals before processing this bar.  Therefore any
        # position closing on this same bar cannot inflate the new signal's lot.
        while next_signal < len(eligible) and eligible[next_signal][0] <= bar.time:
            _activation, sig = eligible[next_signal]
            pos = open_position(sig, equity, config)
            prewarm_indicators_from_dataframe(pos, chart_df, config, replay_start=pos.activation_time)
            active.append(pos)
            pos_by_key[sig.signal_key] = pos
            next_signal += 1

        if not active:
            continue

        still_active = []
        for pos in active:
            advance_one_bar(pos, bar, config)
            if pos.is_terminal():
                status, trading_pnl = position_status(pos)
                closed_lots = 0.0 if status == "OPEN" else _entry_closed_lots(pos)
                bonus = 0.0 if status == "OPEN" else _bonus_for_position(pos, config)
                total_pnl = trading_pnl + bonus if status != "OPEN" else None
                equity_before = equity
                if status != "OPEN":
                    equity += float(total_pnl or 0.0)
                rows.append(_position_row(pos, status, trading_pnl, bonus, closed_lots, total_pnl, equity_before, equity))
                entry_rows.extend(_entry_rows(pos, status, equity_before, equity, config))
            else:
                still_active.append(pos)
        active = still_active

        if equity > peak_equity:
            peak_equity = equity
        if peak_equity > 0:
            dd_pct = (equity - peak_equity) / peak_equity * 100.0
            max_dd_pct = min(max_dd_pct, dd_pct)

        open_risk = sum(_open_risk_abs(pos, config) for pos in active)
        max_open_risk_abs = max(max_open_risk_abs, open_risk)
        max_open_positions = max(max_open_positions, len(active))
        max_open_entries = max(max_open_entries, sum(len(pos.open_entries()) for pos in active))
        max_pending_entries = max(max_pending_entries, sum(sum(1 for e in pos.entries if e.status == "PENDING") for pos in active))
        if peak_equity > 0:
            concurrent_dd = (equity - open_risk - peak_equity) / peak_equity * 100.0
            max_concurrent_risk_dd_pct = min(max_concurrent_risk_dd_pct, concurrent_dd)

        if equity <= 0:
            break

    # Anything still non-terminal by chart end is reported as OPEN, matching the
    # existing report semantics.  Do not credit unrealised future P&L.
    for pos in active:
        status, trading_pnl = position_status(pos)
        rows.append(_position_row(pos, status, trading_pnl, 0.0, 0.0, None, equity, equity))
        entry_rows.extend(_entry_rows(pos, status, equity, equity, config))

    # Signals that never activated before chart end are excluded, not backfilled.
    for i in range(next_signal, len(eligible)):
        _activation, sig = eligible[i]
        excluded.append({"signal_key": sig.signal_key, "reason": "activation after chart end"})

    wins = sum(1 for r in rows if r["status"] == "WIN")
    losses = sum(1 for r in rows if r["status"] == "LOSS")
    no_fills = sum(1 for r in rows if r["status"] == "NO_FILL")
    open_count = sum(1 for r in rows if r["status"] == "OPEN")
    trading_pnl = sum(float(r.get("trading_pnl") or 0.0) for r in rows if r.get("pnl") is not None)
    bonus = sum(float(r.get("bonus") or 0.0) for r in rows if r.get("pnl") is not None)
    closed_lots = sum(float(r.get("closed_lots") or 0.0) for r in rows)

    monthly = _bucket_rows(rows, "month", lambda r: r["signal_time_chart"].strftime("%Y-%m"), config.initial_capital)
    daily = _bucket_rows(rows, "date", lambda r: r["signal_time_chart"].strftime("%Y-%m-%d"), config.initial_capital)

    return {
        "label": label,
        "config": asdict(config),
        "chart_start": chart_start.isoformat(sep=" "),
        "chart_end": chart_end.isoformat(sep=" "),
        "signals_parsed": len(signals),
        "signals_included": len(rows),
        "signals_excluded": len(excluded),
        "final_equity": equity,
        "net_profit": equity - config.initial_capital,
        "trading_pnl": trading_pnl,
        "bonus": bonus,
        "closed_lots": closed_lots,
        "wins": wins,
        "losses": losses,
        "no_fills": no_fills,
        "open": open_count,
        "win_rate_pct": wins / (wins + losses) * 100.0 if wins + losses else 0.0,
        "max_drawdown_pct": max_dd_pct,
        "max_concurrent_risk_dd_estimate_pct": max_concurrent_risk_dd_pct,
        "max_open_risk_abs": max_open_risk_abs,
        "max_open_positions": max_open_positions,
        "max_open_entries": max_open_entries,
        "max_pending_entries": max_pending_entries,
        "rows": rows,
        "entry_rows": entry_rows,
        "monthly": monthly,
        "daily": daily,
    }


def _position_row(pos, status, trading_pnl, bonus, closed_lots, total_pnl, equity_before, equity_after) -> dict[str, Any]:
    sig = pos.signal
    return {
        "global_id": sig.global_id,
        "signal_key": sig.signal_key,
        "signal_time_chart": sig.signal_time_chart,
        "side": sig.side,
        "status": status,
        "pnl": total_pnl,
        "trading_pnl": trading_pnl if status != "OPEN" else None,
        "bonus": bonus if status != "OPEN" else None,
        "closed_lots": closed_lots,
        "equity_before": equity_before,
        "equity_after": equity_after,
    }


def _entry_rows(pos, status, equity_before, equity_after, config: StrategyConfig) -> list[dict[str, Any]]:
    sig = pos.signal
    tz_label = f"GMT+{sig.source_tz_offset}" if sig.source_tz_offset >= 0 else f"GMT{sig.source_tz_offset}"
    out = []
    for e in pos.entries:
        entry_closed_lots = float(e.lot or 0.0) if e.fill_time is not None and e.exit_time is not None and status != "OPEN" else 0.0
        entry_bonus = entry_closed_lots * float(getattr(config, "bonus_per_closed_lot", 0.0) or 0.0)
        entry_total = (e.pnl + entry_bonus) if e.pnl is not None and status != "OPEN" else e.pnl
        out.append({
            "global_id": sig.global_id,
            "signal_key": sig.signal_key,
            "entry_key": f"{sig.signal_key}.{e.entry_index + 1}",
            "entry_number": e.entry_index + 1,
            "signal_date": sig.source_date,
            "signal_time_source": sig.source_time_text,
            "source_tz": tz_label,
            "signal_time_chart": sig.signal_time_chart,
            "side": sig.side,
            "range_low": sig.range_low,
            "range_high": sig.range_high,
            "original_SL": sig.sl,
            "TP1": sig.tp1,
            "TP2": sig.tp2,
            "TP3": sig.tp3,
            "final_target_label": config.final_target.upper(),
            "final_target_price": pos.target_level,
            "entry_index": e.entry_index,
            "entry_price": e.entry_price,
            "effective_SL": e.initial_sl,
            "SL_distance": pos.base_stop_distance,
            "lot": e.lot,
            "entry_status": e.status,
            "fill_time": e.fill_time,
            "exit_time": e.exit_time,
            "exit_price": e.exit_price,
            "stop_at_exit": e.stop_at_exit,
            "trading_pnl": e.pnl,
            "closed_lots": entry_closed_lots,
            "bonus": entry_bonus,
            "pnl": entry_total,
            "first_fill_time": pos.first_fill_time,
            "time_exit_deadline": pos.time_exit_deadline,
            "signal_status": status,
            "equity_before": equity_before,
            "equity_after": equity_after,
        })
    return out


def _bucket_rows(rows: list[dict[str, Any]], key_name: str, key_fn, initial_capital: float) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = key_fn(row)
        if key not in buckets:
            buckets[key] = _new_bucket(key_name, key, row["equity_before"])
        b = buckets[key]
        b["signals"] += 1
        if row["status"] == "WIN":
            b["wins"] += 1
        elif row["status"] == "LOSS":
            b["losses"] += 1
        elif row["status"] == "NO_FILL":
            b["no_fills"] += 1
        elif row["status"] == "OPEN":
            b["open"] += 1
        if row.get("pnl") is not None:
            b["pnl"] += float(row.get("pnl") or 0.0)
            b["trading_pnl"] += float(row.get("trading_pnl") or 0.0)
            b["bonus"] += float(row.get("bonus") or 0.0)
            b["closed_lots"] += float(row.get("closed_lots") or 0.0)
        b["equity_end"] = row["equity_after"]
    out = []
    for key in sorted(buckets):
        b = buckets[key]
        _finalize_bucket(b)
        out.append(b)
    return out


# ---------------------------------------------------------------------------
# Candidate generation and evaluation
# ---------------------------------------------------------------------------
def base_config_dict() -> dict[str, Any]:
    return asdict(DEFAULT_CONFIG)


def candidate_config(rng: random.Random, *, include_trend_runner: bool) -> dict[str, Any]:
    cfg = base_config_dict()
    cfg.update({
        "risk_per_signal": rng.choice([0.020, 0.0275, 0.035, 0.045, 0.05575, 0.065, 0.075]),
        "entry_count": rng.choice([1, 2, 3, 4]),
        "entry_ladder": rng.choice(["signal_range_3", "range_uniform", "range_to_sl"]),
        "entry_sl_gap": rng.choice([0.0, 1.0, 2.0, 3.0, 4.0]),
        "activation_delay_minutes": rng.choice([0, 1, 3, 5, 10]),
        "pending_expiry_minutes": rng.choice([180, 300, 420, 630, 900]),
        "max_hold_minutes": rng.choice([30, 45, 60, 90, 120, 180]),
        "sl_multiplier": rng.choice([1.15, 1.30, 1.45, 1.61, 1.75, 2.00, 2.30]),
        "final_target": rng.choice(["TP1", "TP2", "TP3"]),
        "lock_after_tp1": rng.choice([True, False]),
        "lock_after_tp2": rng.choice([True, False]),
        "tp1_lock_delay_minutes": rng.choice([0, 3, 5, 10, 15]),
        "tp2_lock_delay_minutes": rng.choice([0, 3, 5, 10, 15]),
        "profit_lock_mode": rng.choice(["tp_levels", "bep_plus_half_tp1"]),
        "bep_trigger_distance": rng.choice([1.0, 2.0, 3.0, 4.0, 6.0]),
        "tp1_lock_fraction": rng.choice([0.25, 0.5, 0.75, 1.0]),
        "tp2_lock_target": rng.choice(["TP1", "TP2"]),
        "trailing_open_distance": rng.choice([0.0, 1.0, 2.0, 3.0, 5.0]),
        "trailing_close_distance": rng.choice([0.0, 2.0, 3.0, 5.0, 8.0]),
    })

    if not cfg["lock_after_tp1"]:
        cfg["tp1_lock_delay_minutes"] = 0
    if not cfg["lock_after_tp2"]:
        cfg["tp2_lock_delay_minutes"] = 0

    if include_trend_runner and rng.random() < 0.25:
        cfg.update({
            "trend_runner_enabled": True,
            "final_target": "TP3",
            "trend_runner_ema_fast": rng.choice([8, 13, 21, 34]),
            "trend_runner_ema_slow": rng.choice([34, 55, 89]),
            "trend_runner_atr_period": rng.choice([7, 14, 21]),
            "trend_runner_atr_multiplier": rng.choice([1.5, 2.0, 2.5, 3.0, 4.0]),
            "trend_runner_override_max_hold": True,
        })
        if cfg["trend_runner_ema_fast"] >= cfg["trend_runner_ema_slow"]:
            cfg["trend_runner_ema_fast"], cfg["trend_runner_ema_slow"] = 21, 55
    else:
        cfg["trend_runner_enabled"] = False

    return cfg


def initial_candidates(args: argparse.Namespace) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    base = base_config_dict()
    out.append(base)

    # Handful of low-dimensional anchors around DD40 before random sampling.
    for risk in [0.035, 0.045, 0.05575, 0.065]:
        for target in ["TP2", "TP3"]:
            cfg = dict(base)
            cfg.update({"risk_per_signal": risk, "final_target": target})
            out.append(cfg)
    for to, tc in [(1.0, 0.0), (2.0, 0.0), (0.0, 3.0), (2.0, 3.0)]:
        cfg = dict(base)
        cfg.update({"trailing_open_distance": to, "trailing_close_distance": tc})
        out.append(cfg)

    rng = random.Random(args.seed)
    while len(out) < args.max_candidates:
        out.append(candidate_config(rng, include_trend_runner=args.include_trend_runner))

    dedup = []
    seen = set()
    for cfg in out:
        h = _json_hash(cfg)
        if h in seen:
            continue
        seen.add(h)
        dedup.append(cfg)
    return dedup[:args.max_candidates]


def config_from_dict(data: dict[str, Any], *, bonus: float | None = None) -> StrategyConfig:
    payload = dict(data)
    if bonus is not None:
        payload["bonus_per_closed_lot"] = bonus
    return StrategyConfig(**payload)


def parity_classification(config: dict[str, Any], filter_preset: str) -> str:
    parts = []
    if filter_preset == "research_month_hour_side":
        parts.append(RESEARCH_ONLY)
    if config.get("trend_runner_enabled"):
        parts.append(LIVE_READY_FEATURES)
    else:
        parts.append(LIVE_READY_NO_TREND)
    if config.get("trailing_open_distance", 0.0) > 0:
        parts.append("TRAILING_OPEN_STOP_PATH")
    if config.get("trailing_close_distance", 0.0) > 0:
        parts.append("EXECUTOR_OWNED_TRAILING_SL")
    return ";".join(parts)


def evaluate_candidate(
        candidate: dict[str, Any],
        *,
        filter_preset: str,
        signals: list,
        chart: CsvChartSource,
        train_signals: list,
        validate_signals: list,
        args: argparse.Namespace,
) -> dict[str, Any]:
    cfg = config_from_dict(candidate)
    cfg_no_bonus = config_from_dict(candidate, bonus=0.0)

    full = run_concurrent_backtest(signals, chart, cfg, exclude_structural_anomalies=args.exclude_structural_anomalies, label="full_bonus")
    full_no_bonus = run_concurrent_backtest(signals, chart, cfg_no_bonus, exclude_structural_anomalies=args.exclude_structural_anomalies, label="full_no_bonus")
    train = run_concurrent_backtest(train_signals, chart, cfg, exclude_structural_anomalies=args.exclude_structural_anomalies, label="train") if train_signals else None
    validate = run_concurrent_backtest(validate_signals, chart, cfg, exclude_structural_anomalies=args.exclude_structural_anomalies, label="validate") if validate_signals else None
    validate_no_bonus = run_concurrent_backtest(validate_signals, chart, cfg_no_bonus, exclude_structural_anomalies=args.exclude_structural_anomalies, label="validate_no_bonus") if validate_signals else None

    dd_abs = abs(float(full.get("max_drawdown_pct") or 0.0))
    concurrent_dd_abs = abs(float(full.get("max_concurrent_risk_dd_estimate_pct") or 0.0))
    stable_months = sum(1 for m in full.get("monthly", []) if float(m.get("trading_pnl") or 0.0) > 0)
    total_months = len(full.get("monthly", []))

    passes_dd = dd_abs <= args.max_sequential_dd_pct
    passes_no_bonus = float(full_no_bonus.get("net_profit") or 0.0) > args.min_no_bonus_profit
    validate_profit = float(validate.get("net_profit") or 0.0) if validate else 0.0
    validate_no_bonus_profit = float(validate_no_bonus.get("net_profit") or 0.0) if validate_no_bonus else 0.0
    passes_oos = (not validate_signals) or (validate_profit > 0 and validate_no_bonus_profit > 0)
    passes = bool(passes_dd and passes_no_bonus and passes_oos)

    score = (
            float(full_no_bonus.get("net_profit") or 0.0) * 0.55
            + float(full.get("net_profit") or 0.0) * 0.25
            + validate_no_bonus_profit * 0.20
            - max(0.0, dd_abs - 30.0) * 50.0
            - max(0.0, concurrent_dd_abs - 45.0) * 25.0
    )

    result = {
        "candidate_id": _json_hash({"preset": filter_preset, "config": candidate}),
        "filter_preset": filter_preset,
        "parity_classification": parity_classification(candidate, filter_preset),
        "passes_recommendation_gate": passes,
        "score": score,
        "net_profit_with_bonus": float(full.get("net_profit") or 0.0),
        "final_equity_with_bonus": float(full.get("final_equity") or 0.0),
        "net_profit_no_bonus": float(full_no_bonus.get("net_profit") or 0.0),
        "final_equity_no_bonus": float(full_no_bonus.get("final_equity") or 0.0),
        "sequential_max_drawdown_pct": float(full.get("max_drawdown_pct") or 0.0),
        "sequential_max_drawdown_abs_pct": dd_abs,
        "concurrent_risk_dd_estimate_pct": float(full.get("max_concurrent_risk_dd_estimate_pct") or 0.0),
        "concurrent_risk_dd_estimate_abs_pct": concurrent_dd_abs,
        "max_open_risk_abs": float(full.get("max_open_risk_abs") or 0.0),
        "max_open_positions": int(full.get("max_open_positions") or 0),
        "max_open_entries": int(full.get("max_open_entries") or 0),
        "max_pending_entries": int(full.get("max_pending_entries") or 0),
        "wins": int(full.get("wins") or 0),
        "losses": int(full.get("losses") or 0),
        "no_fills": int(full.get("no_fills") or 0),
        "open": int(full.get("open") or 0),
        "win_rate_pct": float(full.get("win_rate_pct") or 0.0),
        "signals_included": int(full.get("signals_included") or 0),
        "closed_lots": float(full.get("closed_lots") or 0.0),
        "bonus": float(full.get("bonus") or 0.0),
        "train_net_profit": float(train.get("net_profit") or 0.0) if train else None,
        "train_max_dd_pct": float(train.get("max_drawdown_pct") or 0.0) if train else None,
        "validate_net_profit": validate_profit if validate else None,
        "validate_no_bonus_profit": validate_no_bonus_profit if validate_no_bonus else None,
        "validate_max_dd_pct": float(validate.get("max_drawdown_pct") or 0.0) if validate else None,
        "positive_trading_months": stable_months,
        "months": total_months,
        "monthly_json": json.dumps(full.get("monthly", []), default=str),
        "config_json": json.dumps(candidate, sort_keys=True),
        "config": candidate,
    }
    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str, sort_keys=True) + "\n")


def read_existing(path: Path) -> dict[str, dict[str, Any]]:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out[row["candidate_id"]] = row
    return out


def flatten_for_csv(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k not in {"config", "monthly_json"}}
    cfg = row.get("config") or json.loads(row.get("config_json", "{}"))
    for k, v in cfg.items():
        out[f"cfg_{k}"] = v
    out["monthly_json"] = row.get("monthly_json")
    out["live_command"] = live_command(row)
    return out


def write_leaderboards(rows: list[dict[str, Any]], output_dir: Path, top_n: int) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    ranked = sorted(rows, key=lambda r: (bool(r.get("passes_recommendation_gate")), float(r.get("score") or -1e18)), reverse=True)
    flat = [flatten_for_csv(r) for r in ranked]
    csv_path = output_dir / "leaderboard.csv"
    xlsx_path = output_dir / "leaderboard.xlsx"

    if flat:
        fieldnames = list(flat[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(flat)
        try:
            import pandas as pd
            with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
                pd.DataFrame(flat).to_excel(writer, sheet_name="leaderboard", index=False)
                top = pd.DataFrame(flat[:top_n])
                top.to_excel(writer, sheet_name="top", index=False)
        except Exception as exc:
            xlsx_path.write_text(f"Excel write failed: {exc}\nCSV written to {csv_path}\n", encoding="utf-8")
    else:
        csv_path.write_text("no results\n", encoding="utf-8")
        xlsx_path.write_text("no results\n", encoding="utf-8")

    top_dir = output_dir / "top_configs"
    top_dir.mkdir(parents=True, exist_ok=True)
    for idx, row in enumerate(ranked[:top_n], start=1):
        payload = {
            "rank": idx,
            "candidate_id": row["candidate_id"],
            "filter_preset": row["filter_preset"],
            "metrics": {k: v for k, v in row.items() if k not in {"config", "config_json", "monthly_json"}},
            "monthly": json.loads(row.get("monthly_json", "[]")),
            "config": row["config"] if "config" in row else json.loads(row["config_json"]),
            "live_command": live_command(row),
        }
        (top_dir / f"rank_{idx:02d}_{row['candidate_id']}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return csv_path, xlsx_path


def live_command(row: dict[str, Any]) -> str:
    cfg = row.get("config") or json.loads(row.get("config_json", "{}"))
    preset = row.get("filter_preset", "high_growth_hour_side")
    signals = f"generated/live_provider_{preset}.txt"
    # auto_explicit.py reads no environment variables and requires the trailing
    # distances as explicit flags (0.0 disables). The old XAUUSD_* env prefix is
    # dead: config.py ignores it, so emitting it produced a command that either
    # fails argparse (flags now required) or silently runs trailing-OFF live.
    trailing_open = float(cfg.get("trailing_open_distance", 0.0) or 0.0)
    trailing_close = float(cfg.get("trailing_close_distance", 0.0) or 0.0)
    warning = ""
    if bool(cfg.get("trend_runner_enabled", False)):
        # auto_explicit.py exposes no trend-runner flags; the command below runs
        # WITHOUT the runner. Deploy trend-runner configs through `cli auto`.
        warning = (
            "# WARNING: trend-runner is enabled in this config, but tools/auto_explicit.py\n"
            "# has no trend-runner flags. The command below runs WITHOUT the runner.\n"
            "# Deploy via `python -m xauusd_trading.cli auto ... --trend-runner` instead\n"
            "# (with --trend-runner-ema-fast/-ema-slow/-atr-period/-atr-multiplier).\n"
        )
    return (
        f"{warning}python tools/auto_explicit.py \\\n"
        f"  --signals {signals} \\\n"
        f"  --positions-json positions.json \\\n"
        f"  --watch-interval 5 \\\n"
        f"  --mt5-symbol XAUUSD \\\n"
        f"  --mt5-server-offset 3 \\\n"
        f"  --mt5-history-bars 3000 \\\n"
        f"  --initial-capital {cfg['initial_capital']} \\\n"
        f"  --sizing-mode {cfg['sizing_mode']} \\\n"
        f"  --lot {cfg['lot_per_entry']} \\\n"
        f"  --risk {cfg['risk_per_signal']} \\\n"
        f"  --minimum-lot {cfg['minimum_lot']} \\\n"
        f"  --lot-step {cfg['lot_step']} \\\n"
        f"  --bonus-per-closed-lot {cfg['bonus_per_closed_lot']} \\\n"
        f"  --entries {cfg['entry_count']} \\\n"
        f"  --entry-ladder {cfg['entry_ladder']} \\\n"
        f"  --entry-sl-gap {cfg['entry_sl_gap']} \\\n"
        f"  --activation-delay {cfg['activation_delay_minutes']} \\\n"
        f"  --pending-expiry {cfg['pending_expiry_minutes']} \\\n"
        f"  --max-hold {cfg['max_hold_minutes']} \\\n"
        f"  --sl-multiplier {cfg['sl_multiplier']} \\\n"
        f"  --final-target {cfg['final_target']} \\\n"
        f"  --lock-after-tp1 {_bool_text(cfg['lock_after_tp1'])} \\\n"
        f"  --lock-after-tp2 {_bool_text(cfg['lock_after_tp2'])} \\\n"
        f"  --tp1-lock-delay-minutes {cfg['tp1_lock_delay_minutes']} \\\n"
        f"  --tp2-lock-delay-minutes {cfg['tp2_lock_delay_minutes']} \\\n"
        f"  --profit-lock-mode {cfg['profit_lock_mode']} \\\n"
        f"  --bep-trigger-distance {cfg['bep_trigger_distance']} \\\n"
        f"  --tp1-lock-fraction {cfg['tp1_lock_fraction']} \\\n"
        f"  --tp2-lock-target {cfg['tp2_lock_target']} \\\n"
        f"  --runner-after-tp3 {_bool_text(cfg.get('runner_after_tp3', False))} \\\n"
        f"  --tp3-lock-target {cfg.get('tp3_lock_target', 'TP2')} \\\n"
        f"  --trailing-open-distance {trailing_open} \\\n"
        f"  --trailing-close-distance {trailing_close}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Search XAUUSD strategy parameters with corrected concurrent backtest.")
    p.add_argument("--signals", default="signals.txt", help="Raw provider signal feed.")
    p.add_argument("--charts", nargs="+", default=["data/XAUUSD_M1_*.csv"], help="Chart CSV glob(s).")
    p.add_argument("--output-dir", default=None, help="Sweep output directory.")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-candidates", type=int, default=250)
    p.add_argument("--filter-presets", nargs="+", default=["high_growth_hour_side"], choices=FILTER_PRESETS)
    p.add_argument("--sweep-filter-presets", action="store_true", help="Evaluate all built-in filter presets as part of the search.")
    p.add_argument("--include-trend-runner", action="store_true", help="Include trend-runner candidates. Requires parity tests before live use.")
    p.add_argument("--exclude-structural-anomalies", action="store_true")
    p.add_argument("--max-sequential-dd-pct", type=float, default=35.0, help="Recommendation gate, intentionally below 40%%.")
    p.add_argument("--min-no-bonus-profit", type=float, default=0.0)
    p.add_argument("--validate-months", type=int, default=2, help="Last N signal months used as OOS validation.")
    p.add_argument("--top-n", type=int, default=20)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--progress-every", type=int, default=10)
    return p


def split_train_validate(signals: list, validate_months: int) -> tuple[list, list]:
    months = sorted({sig.signal_time_chart.strftime("%Y-%m") for sig in signals})
    if validate_months <= 0 or len(months) <= 1:
        return signals, []
    validate_set = set(months[-validate_months:])
    train = [s for s in signals if s.signal_time_chart.strftime("%Y-%m") not in validate_set]
    validate = [s for s in signals if s.signal_time_chart.strftime("%Y-%m") in validate_set]
    return train, validate


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw_signals = Path(args.signals)
    if not raw_signals.exists():
        raise SystemExit(f"signals file not found: {raw_signals}")
    output_dir = Path(args.output_dir) if args.output_dir else Path("reports") / f"sweep_{_now_label()}"
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = output_dir / "results.jsonl"

    print(f"[sweep] output_dir={output_dir}", flush=True)
    print(f"[sweep] loading charts...", flush=True)
    chart = CsvChartSource(_expand_chart_paths(args.charts))
    presets = FILTER_PRESETS if args.sweep_filter_presets else args.filter_presets

    existing = read_existing(checkpoint) if args.resume else {}
    all_rows = list(existing.values())
    candidates = initial_candidates(args)
    started = time.time()

    for preset in presets:
        filtered_path = prepare_filtered_signals(raw_signals, output_dir, preset)
        signals = parse_signals_file(filtered_path)
        train_signals, validate_signals = split_train_validate(signals, args.validate_months)
        print(
            f"[sweep] preset={preset} signals={len(signals)} train={len(train_signals)} validate={len(validate_signals)} file={filtered_path}",
            flush=True,
        )
        for idx, cfg_dict in enumerate(candidates, start=1):
            candidate_id = _json_hash({"preset": preset, "config": cfg_dict})
            if candidate_id in existing:
                continue
            try:
                row = evaluate_candidate(
                    cfg_dict,
                    filter_preset=preset,
                    signals=signals,
                    chart=chart,
                    train_signals=train_signals,
                    validate_signals=validate_signals,
                    args=args,
                )
            except Exception as exc:
                row = {
                    "candidate_id": candidate_id,
                    "filter_preset": preset,
                    "error": repr(exc),
                    "passes_recommendation_gate": False,
                    "score": -1e18,
                    "config": cfg_dict,
                    "config_json": json.dumps(cfg_dict, sort_keys=True),
                }
            write_jsonl(checkpoint, row)
            all_rows.append(row)
            if idx % max(1, args.progress_every) == 0:
                elapsed = time.time() - started
                best = max((r for r in all_rows if "error" not in r), key=lambda r: float(r.get("score") or -1e18), default=None)
                best_txt = f" best={best['candidate_id']} score={best['score']:.2f}" if best else ""
                print(f"[sweep] preset={preset} {idx}/{len(candidates)} elapsed={elapsed:.0f}s{best_txt}", flush=True)

    csv_path, xlsx_path = write_leaderboards(all_rows, output_dir, args.top_n)
    print(f"[sweep] checkpoint={checkpoint}")
    print(f"[sweep] leaderboard_csv={csv_path}")
    print(f"[sweep] leaderboard_xlsx={xlsx_path}")
    best_rows = [r for r in all_rows if "error" not in r]
    best_rows.sort(key=lambda r: (bool(r.get("passes_recommendation_gate")), float(r.get("score") or -1e18)), reverse=True)
    if best_rows:
        best = best_rows[0]
        print("[sweep] best_candidate")
        print(json.dumps({
            "candidate_id": best["candidate_id"],
            "passes_recommendation_gate": best.get("passes_recommendation_gate"),
            "filter_preset": best.get("filter_preset"),
            "score": best.get("score"),
            "net_profit_with_bonus": best.get("net_profit_with_bonus"),
            "net_profit_no_bonus": best.get("net_profit_no_bonus"),
            "sequential_max_drawdown_pct": best.get("sequential_max_drawdown_pct"),
            "concurrent_risk_dd_estimate_pct": best.get("concurrent_risk_dd_estimate_pct"),
            "parity_classification": best.get("parity_classification"),
            "config": best.get("config"),
        }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())