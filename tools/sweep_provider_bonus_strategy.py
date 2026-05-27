#!/usr/bin/env python3
"""Brute-force provider signal strategy with closed-lot bonus.

Objective:
    maximize total net profit = trading P&L + bonus_per_closed_lot * closed_lots

Constraint:
    abs(max_drawdown_pct) <= max_drawdown_limit_pct

The script filters provider signals into GMT+3 signal files, then backtests each
strategy combination with the same engine used by live auto/decide/manage.

Example:

    python tools/sweep_provider_bonus_strategy.py \
      --signals signals.txt \
      --charts data/XAUUSD_M1_*.csv \
      --output-csv reports/provider_bonus_sweep.csv \
      --best-signals generated/provider_bonus_best.txt \
      --bonus-per-closed-lot 3 \
      --max-drawdown-limit-pct 40
"""
from __future__ import annotations

import argparse
import csv
import glob
import itertools
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
for p in (ROOT, TOOLS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from xauusd_trading import CsvChartSource, DEFAULT_CONFIG, StrategyConfig, parse_signals_file, run_backtest  # noqa: E402
from filter_provider_signals import parse_provider_signals, keep_signal, write_signals  # noqa: E402


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


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "calculating"
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}h {m:02d}m {s:02d}s"
    if m:
        return f"{m:d}m {s:02d}s"
    return f"{s:d}s"


class Heartbeat:
    def __init__(self, total: int, interval_minutes: float):
        self.total = total
        self.interval_seconds = max(0.0, float(interval_minutes)) * 60.0
        self.started = time.time()
        self.current = 0
        self.completed = 0
        self.stage = "initializing"
        self.best: dict | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.interval_seconds <= 0:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.interval_seconds <= 0:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def update(self, *, current=None, completed=None, stage=None, best=None) -> None:
        with self._lock:
            if current is not None:
                self.current = current
            if completed is not None:
                self.completed = completed
            if stage is not None:
                self.stage = stage
            if best is not None:
                self.best = dict(best)

    def print_now(self, prefix="provider-sweep") -> None:
        with self._lock:
            current = self.current
            completed = self.completed
            stage = self.stage
            best = dict(self.best) if self.best else None
        elapsed = time.time() - self.started
        pct = completed / self.total * 100.0 if self.total else 0.0
        eta = None
        if completed > 0:
            rate = completed / elapsed
            eta = (self.total - completed) / rate if rate > 0 else None
        best_txt = "best=none" if best is None else (
            f"best cand={best['candidate']} net={best['net_profit']:.2f} "
            f"dd={best['max_drawdown_pct']:.2f}% preset={best['preset']} risk={best['risk']}"
        )
        print(
            f"[{prefix}] {current:,}/{self.total:,} completed={completed:,} ({pct:5.1f}%) "
            f"stage={stage} elapsed={_fmt_duration(elapsed)} ETA={_fmt_duration(eta)} | {best_txt}",
            file=sys.stderr,
            flush=True,
        )

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.print_now(prefix="provider-sweep-heartbeat")


def _make_config(args: argparse.Namespace, *, risk: float, entries: int, activation_delay: int,
                 pending_expiry: int, max_hold: int, sl_multiplier: float,
                 final_target: str, lock_tp1: bool, lock_tp2: bool) -> StrategyConfig:
    return replace(
        DEFAULT_CONFIG,
        initial_capital=args.initial_capital,
        sizing_mode=args.sizing_mode,
        lot_per_entry=args.lot,
        risk_per_signal=risk,
        minimum_lot=args.minimum_lot,
        lot_step=args.lot_step,
        bonus_per_closed_lot=args.bonus_per_closed_lot,
        entry_count=entries,
        entry_ladder=args.entry_ladder,
        entry_sl_gap=args.entry_sl_gap,
        activation_delay_minutes=activation_delay,
        pending_expiry_minutes=pending_expiry,
        max_hold_minutes=max_hold,
        sl_multiplier=sl_multiplier,
        final_target=final_target,
        lock_after_tp1=lock_tp1,
        lock_after_tp2=lock_tp2,
    )


def _metrics(result: dict) -> dict:
    return {
        "net_profit": float(result.get("net_profit") or 0.0),
        "trading_pnl": float(result.get("trading_pnl") or 0.0),
        "bonus": float(result.get("bonus") or 0.0),
        "closed_lots": float(result.get("closed_lots") or 0.0),
        "final_equity": float(result.get("final_equity") or 0.0),
        "max_drawdown_pct": float(result.get("max_drawdown_pct") or 0.0),
        "signals_included": int(result.get("signals_included") or 0),
        "wins": int(result.get("wins") or 0),
        "losses": int(result.get("losses") or 0),
        "no_fills": int(result.get("no_fills") or 0),
        "open": int(result.get("open") or 0),
        "win_rate_pct": float(result.get("win_rate_pct") or 0.0),
    }


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Sweep provider strategies with closed-lot bonus objective.")
    p.add_argument("--signals", required=True)
    p.add_argument("--charts", required=True, nargs="+")
    p.add_argument("--output-csv", default="reports/provider_bonus_sweep.csv")
    p.add_argument("--work-dir", default="generated/_provider_bonus_sweep")
    p.add_argument("--best-signals", default="generated/provider_bonus_best.txt")
    p.add_argument("--progress-every", type=int, default=10)
    p.add_argument("--progress-interval-minutes", type=float, default=5.0)
    p.add_argument("--max-combos", type=int, default=0)

    p.add_argument("--initial-capital", type=float, default=10_000.0)
    p.add_argument("--sizing-mode", choices=["risk", "fixed"], default="risk")
    p.add_argument("--lot", type=float, default=0.5)
    p.add_argument("--minimum-lot", type=float, default=0.01)
    p.add_argument("--lot-step", type=float, default=0.01)
    p.add_argument("--bonus-per-closed-lot", type=float, default=3.0)
    p.add_argument("--max-drawdown-limit-pct", type=float, default=40.0)
    p.add_argument("--entry-ladder", choices=["signal_range_3", "range_uniform", "range_to_sl"], default="signal_range_3")
    p.add_argument("--entry-sl-gap", type=float, default=2.0)

    p.add_argument("--presets", default="high_growth_hour_side,no_bad_hours,best_hours,all")
    p.add_argument("--risks", default="0.01,0.02,0.03,0.04,0.05,0.06,0.08,0.10,0.12")
    p.add_argument("--entries-list", default="1,2,3")
    p.add_argument("--activation-delays", default="0,1,2")
    p.add_argument("--pending-expiries", default="5,10,20")
    p.add_argument("--max-holds", default="30,60,90")
    p.add_argument("--sl-multipliers", default="1.0,1.25,1.5")
    p.add_argument("--final-targets", default="TP1,TP2,TP3")
    p.add_argument("--lock-modes", default="both,tp1,none", help="both,tp1,none")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.time()
    raw_rows = parse_provider_signals(Path(args.signals))
    if not raw_rows:
        raise SystemExit(f"No provider signals parsed from {args.signals}")
    chart = CsvChartSource(_expand_chart_paths(args.charts))
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    preset_values = [x.strip() for x in args.presets.split(",") if x.strip()]
    risk_values = _parse_list(args.risks, float)
    entries_values = _parse_list(args.entries_list, int)
    activation_values = _parse_list(args.activation_delays, int)
    expiry_values = _parse_list(args.pending_expiries, int)
    hold_values = _parse_list(args.max_holds, int)
    sl_values = _parse_list(args.sl_multipliers, float)
    target_values = [x.strip().upper() for x in args.final_targets.split(",") if x.strip()]
    lock_values = [x.strip().lower() for x in args.lock_modes.split(",") if x.strip()]

    grid = list(itertools.product(
        preset_values, risk_values, entries_values, activation_values, expiry_values,
        hold_values, sl_values, target_values, lock_values,
    ))
    if args.max_combos > 0:
        grid = grid[:args.max_combos]
    total = len(grid)
    print(
        f"Parsed provider signals={len(raw_rows):,}; chart rows={len(chart.dataframe):,}; combos={total:,}",
        file=sys.stderr,
        flush=True,
    )

    filtered_cache: dict[str, list] = {}
    parsed_cache: dict[str, list] = {}
    rows: list[dict] = []
    best: dict | None = None
    best_file: Path | None = None
    heartbeat = Heartbeat(total, args.progress_interval_minutes)
    heartbeat.start()

    try:
        for idx, combo in enumerate(grid, start=1):
            preset, risk, entries, activation_delay, pending_expiry, max_hold, sl_multiplier, final_target, lock_mode = combo
            lock_tp1 = lock_mode in {"both", "tp1"}
            lock_tp2 = lock_mode == "both"
            heartbeat.update(current=idx, stage="filtering/backtesting")

            if preset not in filtered_cache:
                kept = [row for row in raw_rows if keep_signal(row, preset)]
                signal_file = work_dir / f"provider_{preset}.txt"
                write_signals(kept, signal_file)
                filtered_cache[preset] = kept
                parsed_cache[preset] = parse_signals_file(signal_file)
            else:
                signal_file = work_dir / f"provider_{preset}.txt"

            signals = parsed_cache[preset]
            cfg = _make_config(
                args,
                risk=risk,
                entries=entries,
                activation_delay=activation_delay,
                pending_expiry=pending_expiry,
                max_hold=max_hold,
                sl_multiplier=sl_multiplier,
                final_target=final_target,
                lock_tp1=lock_tp1,
                lock_tp2=lock_tp2,
            )
            result = run_backtest(signals, chart, cfg)
            m = _metrics(result)
            dd_abs = abs(min(0.0, m["max_drawdown_pct"]))
            passes_dd = dd_abs <= args.max_drawdown_limit_pct
            score = m["net_profit"] if passes_dd else m["net_profit"] - (dd_abs - args.max_drawdown_limit_pct) * 1_000_000
            row = {
                "score": score,
                "passes_dd": passes_dd,
                "candidate": idx,
                **m,
                "preset": preset,
                "risk": risk,
                "entries": entries,
                "activation_delay": activation_delay,
                "pending_expiry": pending_expiry,
                "max_hold": max_hold,
                "sl_multiplier": sl_multiplier,
                "final_target": final_target,
                "lock_mode": lock_mode,
                "signals_file": str(signal_file),
            }
            rows.append(row)
            if passes_dd and (best is None or row["net_profit"] > best["net_profit"]):
                best = row
                best_file = signal_file
                heartbeat.update(best=best)
            heartbeat.update(completed=idx, best=best)
            if idx == 1 or idx % args.progress_every == 0 or idx == total:
                heartbeat.print_now()
    finally:
        heartbeat.stop()

    rows.sort(key=lambda r: r["score"], reverse=True)
    _write_rows(Path(args.output_csv), rows)
    if best and best_file is not None:
        Path(args.best_signals).parent.mkdir(parents=True, exist_ok=True)
        Path(args.best_signals).write_text(best_file.read_text(encoding="utf-8"), encoding="utf-8")

    print(f"Wrote sweep results: {Path(args.output_csv).resolve()}")
    if best:
        print("Best candidate within DD limit:")
        print(
            f"  candidate={best['candidate']} net={best['net_profit']:.2f} trading={best['trading_pnl']:.2f} "
            f"bonus={best['bonus']:.2f} closed_lots={best['closed_lots']:.2f} dd={best['max_drawdown_pct']:.2f}%"
        )
        print(
            f"  preset={best['preset']} risk={best['risk']} entries={best['entries']} "
            f"activation={best['activation_delay']} expiry={best['pending_expiry']} hold={best['max_hold']} "
            f"slx={best['sl_multiplier']} target={best['final_target']} locks={best['lock_mode']}"
        )
        print(f"  best signals copied to: {Path(args.best_signals).resolve()}")
    else:
        print("No candidate passed the drawdown limit.")
    print(f"Elapsed: {_fmt_duration(time.time() - started)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
