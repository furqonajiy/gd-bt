#!/usr/bin/env python3
"""Sweep proactive S/R generator parameters and backtest each combination.

This is the next step after manual generators: search combinations, rank them,
and reject unstable results.  It uses generated signal files internally so every
candidate still goes through the same parser and backtest engine used elsewhere.

Example:

    python tools/sweep_proactive_sr.py \
      --charts data/XAUUSD_M1_*.csv \
      --output-csv reports/proactive_sr_sweep.csv \
      --work-dir generated/_sweep_proactive_sr \
      --initial-capital 10000 \
      --sizing-mode risk \
      --risk 0.01 \
      --train-end 2025-12-31 \
      --max-drawdown-limit-pct 40
"""
from __future__ import annotations

import argparse
import csv
import glob
import itertools
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from xauusd_trading import CsvChartSource, DEFAULT_CONFIG, StrategyConfig, parse_signals_file, run_backtest  # noqa: E402
from generate_proactive_sr_signals import generate_signals, _write_signal_file  # noqa: E402


def _expand_chart_paths(patterns: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            matches = sorted(glob.glob(pat))
            if not matches:
                raise SystemExit(f"No files match pattern: {pat}")
            out.extend(Path(m) for m in matches)
        else:
            path = Path(pat)
            if not path.exists():
                raise SystemExit(f"Chart file not found: {pat}")
            out.append(path)
    if not out:
        raise SystemExit("No chart files provided")
    return out


def _parse_list(raw: str, cast):
    return [cast(x.strip()) for x in raw.split(",") if x.strip()]


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}h {m:02d}m {s:02d}s"
    if m:
        return f"{m:d}m {s:02d}s"
    return f"{s:d}s"


def _base_generator_args(args: argparse.Namespace) -> SimpleNamespace:
    # Defaults mirror generate_proactive_sr_signals.py but keep this tool
    # independent from that script's required CLI arguments.
    return SimpleNamespace(
        charts=args.charts,
        output="",
        diagnostics=None,
        start=args.start,
        end=args.end,
        progress_every_rows=0,
        progress_interval_seconds=0,
        direction="both",
        cooldown_minutes=10.0,
        level_cooldown_minutes=45.0,
        max_signals_per_day=0,
        session_start=args.session_start,
        session_end=args.session_end,
        weekdays_only=True,
        max_spread_points=60,
        asian_start=0,
        asian_end=7,
        atr_period=14,
        min_atr=0.25,
        max_atr=8.0,
        min_distance=1.0,
        max_distance=5.0,
        min_distance_atr=0.4,
        max_distance_atr=1.8,
        max_body_atr=1.5,
        require_approach_candle=True,
        price_step=0.5,
        range_width=2.0,
        entry_buffer=0.5,
        stop_distance=6.0,
        stop_atr=2.0,
        min_risk=4.0,
        max_risk=12.0,
        min_room_rr=1.0,
        rr1=1.0,
        rr2=1.5,
        rr3=2.0,
    )


def _strategy_config(args: argparse.Namespace) -> StrategyConfig:
    return replace(
        DEFAULT_CONFIG,
        initial_capital=args.initial_capital,
        sizing_mode=args.sizing_mode,
        lot_per_entry=args.lot,
        risk_per_signal=args.risk,
        entry_count=args.entries,
        entry_ladder=args.entry_ladder,
        activation_delay_minutes=args.activation_delay,
        pending_expiry_minutes=args.pending_expiry,
        max_hold_minutes=args.max_hold,
        sl_multiplier=args.sl_multiplier,
        final_target=args.final_target,
        lock_after_tp1=not args.no_lock_after_tp1,
        lock_after_tp2=not args.no_lock_after_tp2,
    )


def _split_signals(signals, train_end: str | None, test_start: str | None):
    if train_end:
        train_end_dt = datetime.fromisoformat(train_end)
    else:
        train_end_dt = None
    if test_start:
        test_start_dt = datetime.fromisoformat(test_start)
    elif train_end_dt is not None:
        test_start_dt = train_end_dt
    else:
        test_start_dt = None

    if train_end_dt is None:
        train = signals
    else:
        train = [s for s in signals if s.signal_time_chart < train_end_dt]
    if test_start_dt is None:
        test = []
    else:
        test = [s for s in signals if s.signal_time_chart >= test_start_dt]
    return train, test


def _metrics(result: dict) -> dict:
    return {
        "net_profit": float(result.get("net_profit") or 0.0),
        "max_dd": float(result.get("max_drawdown_pct") or 0.0),
        "signals": int(result.get("signals_included") or 0),
        "wins": int(result.get("wins") or 0),
        "losses": int(result.get("losses") or 0),
        "no_fills": int(result.get("no_fills") or 0),
        "win_rate": float(result.get("win_rate_pct") or 0.0),
        "final_equity": float(result.get("final_equity") or 0.0),
    }


def _score(full: dict, train: dict, test: dict | None, max_dd_limit: float) -> float:
    full_dd = abs(min(0.0, full["max_dd"]))
    train_dd = abs(min(0.0, train["max_dd"]))
    test_dd = abs(min(0.0, test["max_dd"])) if test else 0.0
    dd_penalty = max(0.0, full_dd - max_dd_limit) * 500.0
    score = full["net_profit"] + 0.50 * train["net_profit"] - 20.0 * full_dd - dd_penalty
    if test:
        score += 1.50 * test["net_profit"] - 20.0 * test_dd
        if train["net_profit"] <= 0 or test["net_profit"] <= 0:
            score -= 10_000.0
    if full["signals"] < 100:
        score -= 5_000.0
    return score


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Sweep proactive support/resistance generator parameters.")
    p.add_argument("--charts", required=True, nargs="+")
    p.add_argument("--output-csv", default="reports/proactive_sr_sweep.csv")
    p.add_argument("--work-dir", default="generated/_sweep_proactive_sr")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--train-end", default="2026-01-01", help="Train split end, exclusive. Default: 2026-01-01.")
    p.add_argument("--test-start", default=None, help="Test split start. Default: same as train-end.")
    p.add_argument("--max-combos", type=int, default=0, help="0 = all combinations.")
    p.add_argument("--keep-signal-files", action="store_true")
    p.add_argument("--progress-every", type=int, default=5)

    # Backtest strategy controls.
    p.add_argument("--initial-capital", type=float, default=10_000.0)
    p.add_argument("--sizing-mode", default="risk", choices=["fixed", "risk"])
    p.add_argument("--lot", type=float, default=0.5)
    p.add_argument("--risk", type=float, default=0.01, help="Risk fraction per signal for risk sizing. Default 1%.")
    p.add_argument("--entries", type=int, default=3)
    p.add_argument("--entry-ladder", default="signal_range_3", choices=["signal_range_3", "range_uniform", "range_to_sl"])
    p.add_argument("--activation-delay", type=int, default=2)
    p.add_argument("--pending-expiry", type=int, default=5)
    p.add_argument("--max-hold", type=int, default=90)
    p.add_argument("--sl-multiplier", type=float, default=1.5)
    p.add_argument("--final-target", default="TP3", choices=["TP1", "TP2", "TP3"])
    p.add_argument("--no-lock-after-tp1", action="store_true")
    p.add_argument("--no-lock-after-tp2", action="store_true")
    p.add_argument("--max-drawdown-limit-pct", type=float, default=40.0)

    # Generator sweep grid: comma-separated values.
    p.add_argument("--cooldowns", default="5,10,15")
    p.add_argument("--level-cooldowns", default="20,45,90")
    p.add_argument("--max-spreads", default="40,60,80")
    p.add_argument("--min-distances", default="0.5,1.0")
    p.add_argument("--max-distances", default="4.0,6.0,8.0")
    p.add_argument("--stop-distances", default="4.0,6.0,8.0")
    p.add_argument("--min-room-rrs", default="0,0.5,1.0")
    p.add_argument("--rr3s", default="1.5,2.0")
    p.add_argument("--session-start", type=int, default=7)
    p.add_argument("--session-end", type=int, default=23)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.time()
    chart_paths = _expand_chart_paths(args.charts)
    print(f"Loading chart files: {len(chart_paths):,}", file=sys.stderr, flush=True)
    chart = CsvChartSource(chart_paths)
    print(
        f"Loaded chart rows: {len(chart.dataframe):,} | range: {chart.first_time()} -> {chart.last_time()}",
        file=sys.stderr,
        flush=True,
    )

    cfg = _strategy_config(args)
    base_gen = _base_generator_args(args)
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    grid = list(itertools.product(
        _parse_list(args.cooldowns, float),
        _parse_list(args.level_cooldowns, float),
        _parse_list(args.max_spreads, int),
        _parse_list(args.min_distances, float),
        _parse_list(args.max_distances, float),
        _parse_list(args.stop_distances, float),
        _parse_list(args.min_room_rrs, float),
        _parse_list(args.rr3s, float),
    ))
    if args.max_combos and args.max_combos > 0:
        grid = grid[:args.max_combos]
    total = len(grid)
    print(f"Sweeping {total:,} combinations...", file=sys.stderr, flush=True)

    rows: list[dict] = []
    best_row: dict | None = None
    for idx, combo in enumerate(grid, start=1):
        cooldown, level_cooldown, max_spread, min_distance, max_distance, stop_distance, min_room_rr, rr3 = combo
        gen_args = SimpleNamespace(**vars(base_gen))
        gen_args.cooldown_minutes = cooldown
        gen_args.level_cooldown_minutes = level_cooldown
        gen_args.max_spread_points = max_spread
        gen_args.min_distance = min_distance
        gen_args.max_distance = max_distance
        gen_args.stop_distance = stop_distance
        gen_args.min_room_rr = min_room_rr
        gen_args.rr3 = rr3
        gen_args.rr2 = min(1.5, (1.0 + rr3) / 2.0)

        signals_raw = generate_signals(chart.dataframe, gen_args)
        signal_file = work_dir / f"candidate_{idx:04d}.txt"
        _write_signal_file(signals_raw, signal_file)
        parsed = parse_signals_file(signal_file)
        train_sigs, test_sigs = _split_signals(parsed, args.train_end, args.test_start)

        full_result = run_backtest(parsed, chart, cfg)
        train_result = run_backtest(train_sigs, chart, cfg) if train_sigs else None
        test_result = run_backtest(test_sigs, chart, cfg) if test_sigs else None
        full_m = _metrics(full_result)
        train_m = _metrics(train_result) if train_result else _metrics({})
        test_m = _metrics(test_result) if test_result else None
        score = _score(full_m, train_m, test_m, args.max_drawdown_limit_pct)
        passes_dd = abs(min(0.0, full_m["max_dd"])) <= args.max_drawdown_limit_pct
        stable = bool(test_m is None or (train_m["net_profit"] > 0 and test_m["net_profit"] > 0))

        row = {
            "rank_score": score,
            "passes_dd": passes_dd,
            "stable_train_test": stable,
            "candidate": idx,
            "signals_generated": len(signals_raw),
            "full_net_profit": full_m["net_profit"],
            "full_max_dd_pct": full_m["max_dd"],
            "full_signals": full_m["signals"],
            "full_wins": full_m["wins"],
            "full_losses": full_m["losses"],
            "full_no_fills": full_m["no_fills"],
            "full_win_rate_pct": full_m["win_rate"],
            "train_net_profit": train_m["net_profit"],
            "train_max_dd_pct": train_m["max_dd"],
            "test_net_profit": test_m["net_profit"] if test_m else None,
            "test_max_dd_pct": test_m["max_dd"] if test_m else None,
            "cooldown_minutes": cooldown,
            "level_cooldown_minutes": level_cooldown,
            "max_spread_points": max_spread,
            "min_distance": min_distance,
            "max_distance": max_distance,
            "stop_distance": stop_distance,
            "min_room_rr": min_room_rr,
            "rr1": gen_args.rr1,
            "rr2": gen_args.rr2,
            "rr3": gen_args.rr3,
            "signal_file": str(signal_file),
        }
        rows.append(row)
        if best_row is None or row["rank_score"] > best_row["rank_score"]:
            best_row = row
        if not args.keep_signal_files:
            try:
                signal_file.unlink()
            except FileNotFoundError:
                pass

        if idx == 1 or idx % args.progress_every == 0 or idx == total:
            elapsed = time.time() - started
            best_txt = "none" if best_row is None else (
                f"cand={best_row['candidate']} score={best_row['rank_score']:.1f} "
                f"pnl={best_row['full_net_profit']:.2f} dd={best_row['full_max_dd_pct']:.2f}%"
            )
            print(
                f"[sweep] {idx:,}/{total:,} combos | elapsed={_fmt_duration(elapsed)} | best {best_txt}",
                file=sys.stderr,
                flush=True,
            )

    rows.sort(key=lambda r: r["rank_score"], reverse=True)
    _write_rows(Path(args.output_csv), rows)
    print(f"Wrote sweep results: {Path(args.output_csv).resolve()}")
    if rows:
        top = rows[0]
        print("Top candidate:")
        print(
            f"  candidate={top['candidate']} score={top['rank_score']:.1f} "
            f"pnl={top['full_net_profit']:.2f} dd={top['full_max_dd_pct']:.2f}% "
            f"train={top['train_net_profit']:.2f} test={top['test_net_profit']} "
            f"signals={top['full_signals']}"
        )
        print(
            "  params: "
            f"cooldown={top['cooldown_minutes']}, level_cooldown={top['level_cooldown_minutes']}, "
            f"spread={top['max_spread_points']}, distance={top['min_distance']}-{top['max_distance']}, "
            f"stop={top['stop_distance']}, room_rr={top['min_room_rr']}, rr3={top['rr3']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
