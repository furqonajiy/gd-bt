#!/usr/bin/env python3
"""Aggressive random sweep with LIMIT entries only (trailing-open forced OFF).

Same broad random search as tools/sweep.py, but every candidate is pinned to
trailing_open_distance=0.0 -> entries are plain LIMIT orders. trailing_close is
still varied (the executor-owned trailing SL on the exit). Everything else
(risk, entries, ladder, gap, activation, expiry, max_hold, sl_mult, target,
locks, profit_lock_mode, bep, tp1_lock_fraction, tp2_lock_target,
trailing_close) is drawn by sweep.candidate_config and evaluated through
sweep's validated concurrent engine, gates, and leaderboard. Only candidate
generation differs, so results stay comparable and parity-safe.

Filtering sweep.py's own output is wasteful: it draws trailing_open=0 only ~1/5
of the time, so ~80% of a plain sweep lands outside this region. Forcing it here
puts 100% of the budget on LIMIT entries.

CAVEATS:
- LIMIT-entry winners ride the LIMIT order path, which still has the latent
  ORDER_FILLING_RETURN fill bug -- fix that before any live use.
- trailing_close is M1-vs-live-tick parity-fragile, so this is not a
  parity-safe alternative; it moves the trailing risk from entry to exit.
- sweep.py draws trailing_close from {0,2,3,5,8} (no 0.5); probe finer values
  around any winner with tools/sweep_grid.py.
"""
from __future__ import annotations

import argparse
import importlib
import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse the validated sweep machinery (concurrent backtest, gates, leaderboard).
sweep = importlib.import_module("tools.sweep")

from trading.xauusd import CsvChartSource, parse_signals_file  # noqa: E402


def make_limit_candidates(seed: int, max_candidates: int) -> list[dict]:
    """Random candidates from sweep.candidate_config with trailing-open pinned to 0."""
    seen: set[str] = set()
    out: list[dict] = []

    def _add(cfg: dict) -> None:
        cfg = dict(cfg)
        cfg["trailing_open_distance"] = 0.0  # force LIMIT entry; leave trailing_close as drawn
        h = sweep._json_hash(cfg)
        if h not in seen:
            seen.add(h)
            out.append(cfg)

    _add(sweep.base_config_dict())  # DD40 base is already a LIMIT config
    rng = random.Random(seed)
    attempts = 0
    # Pinning trailing_open collapses configs that differed only in that field,
    # so draw with headroom to still fill the cap.
    while len(out) < max_candidates and attempts < max_candidates * 30:
        _add(sweep.candidate_config(rng, include_trend_runner=False))
        attempts += 1
    return out[:max_candidates]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Aggressive random sweep, LIMIT entries only (trailing-open off; trailing-close still varied)."
    )
    p.add_argument("--signals", default="signals.txt")
    p.add_argument("--charts", nargs="+", default=["data/XAUUSD_M1_*.csv"])
    p.add_argument("--output-dir", required=True)
    p.add_argument("--filter-presets", nargs="+", default=["high_growth_hour_side"],
                   choices=sweep.FILTER_PRESETS)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-candidates", type=int, default=400)
    p.add_argument("--validate-months", type=int, default=4)
    p.add_argument("--max-sequential-dd-pct", type=float, default=40.0)
    p.add_argument("--min-no-bonus-profit", type=float, default=0.0)
    p.add_argument("--exclude-structural-anomalies", action="store_true")
    p.add_argument("--top-n", type=int, default=20)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--progress-every", type=int, default=10)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw_signals = Path(args.signals)
    if not raw_signals.exists():
        raise SystemExit(f"signals file not found: {raw_signals}")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = output_dir / "results.jsonl"

    candidates = make_limit_candidates(args.seed, args.max_candidates)
    print(f"[limit-sweep] candidates={len(candidates)} (trailing_open pinned to 0) "
          f"presets={args.filter_presets}", flush=True)

    chart = CsvChartSource(sweep._expand_chart_paths(args.charts))
    eval_args = SimpleNamespace(
        exclude_structural_anomalies=args.exclude_structural_anomalies,
        max_sequential_dd_pct=args.max_sequential_dd_pct,
        min_no_bonus_profit=args.min_no_bonus_profit,
    )

    existing = sweep.read_existing(checkpoint) if args.resume else {}
    all_rows = list(existing.values())

    for preset in args.filter_presets:
        filtered = sweep.prepare_filtered_signals(raw_signals, output_dir, preset)
        signals = parse_signals_file(filtered)
        train, validate = sweep.split_train_validate(signals, args.validate_months)
        print(f"[limit-sweep] preset={preset} signals={len(signals)} "
              f"train={len(train)} validate={len(validate)}", flush=True)

        for idx, cfg in enumerate(candidates, start=1):
            candidate_id = sweep._json_hash({"preset": preset, "config": cfg})
            if candidate_id in existing:
                continue
            try:
                row = sweep.evaluate_candidate(
                    cfg, filter_preset=preset, signals=signals, chart=chart,
                    train_signals=train, validate_signals=validate, args=eval_args,
                )
            except Exception as exc:  # one bad candidate must not kill the sweep
                row = {
                    "candidate_id": candidate_id, "filter_preset": preset,
                    "error": repr(exc), "passes_recommendation_gate": False,
                    "score": -1e18, "config": cfg,
                    "config_json": json.dumps(cfg, sort_keys=True),
                }
            sweep.write_jsonl(checkpoint, row)
            all_rows.append(row)
            if idx % max(1, args.progress_every) == 0:
                print(f"[limit-sweep] preset={preset} {idx}/{len(candidates)}", flush=True)

    csv_path, xlsx_path = sweep.write_leaderboards(all_rows, output_dir, args.top_n)
    print(f"[limit-sweep] checkpoint={checkpoint}")
    print(f"[limit-sweep] leaderboard_csv={csv_path}")
    print(f"[limit-sweep] leaderboard_xlsx={xlsx_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())