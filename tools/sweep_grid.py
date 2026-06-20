#!/usr/bin/env python3
"""Exhaustive GRID sweep over chosen knobs, everything else frozen at DEFAULT_CONFIG.

Unlike tools/sweep.py (random sampling from hardcoded value lists), this tries the
full cartesian product of the explicit values you pass via --grid, so you can
sweep e.g. bep_trigger_distance over any range and cover every combination. It
reuses sweep.py's concurrent backtest, recommendation gates, and leaderboard
output verbatim — only candidate generation differs, so results stay comparable
and parity-safe.

Distances (bep_trigger_distance, trailing_*_distance) are in PRICE DOLLARS, not
pips. XAUUSD pip ~= $0.10. The pre-TP1 BEP move only exists in
profit_lock_mode=bep_plus_half_tp1.

Example — sweep the BEP trigger (your "50 vs 100 pips to BEP"):

    python tools/sweep_grid.py \
      --signals signals.txt \
      --charts data/XAUUSD_M1_2025*_ELEV8.csv data/XAUUSD_M1_2026*_ELEV8.csv \
      --filter-presets high_growth_hour_side \
      --output-dir reports/grid_bep \
      --grid profit_lock_mode=bep_plus_half_tp1 \
      --grid bep_trigger_distance=2,3,4,5,6,8,10 \
      --grid tp1_lock_fraction=0.25,0.5,0.75,1.0 \
      --validate-months 4 --max-sequential-dd-pct 35
"""
from __future__ import annotations

import argparse
import importlib
import itertools
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse the validated sweep machinery (concurrent backtest, gates, leaderboard).
sweep = importlib.import_module("tools.sweep")

from trading.engine import CsvChartSource, parse_signals_file  # noqa: E402


def _cast(value: str, default):
    """Cast a grid string to the type of the corresponding DEFAULT_CONFIG field."""
    if isinstance(default, bool):  # bool check must precede int (bool is an int)
        low = value.lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
        raise SystemExit(f"expected true/false for boolean field, got: {value}")
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value


def parse_grid(grid_specs: list[str], base: dict) -> dict[str, list]:
    """Parse repeated NAME=v1,v2,... specs into {field: [typed values]}."""
    out: dict[str, list] = {}
    for spec in grid_specs:
        if "=" not in spec:
            raise SystemExit(f"--grid must be NAME=v1,v2,... (got: {spec})")
        name, raw = spec.split("=", 1)
        name = name.strip()
        if name not in base:
            raise SystemExit(f"unknown config field: {name}")
        values = [_cast(v.strip(), base[name]) for v in raw.split(",") if v.strip() != ""]
        if not values:
            raise SystemExit(f"no values given for {name}")
        out[name] = values
    return out


def build_grid_candidates(grid: dict[str, list], base: dict) -> list[dict]:
    """Full cartesian product of the gridded fields on top of the base config."""
    names = list(grid.keys())
    seen: set[str] = set()
    out: list[dict] = []
    for combo in itertools.product(*(grid[n] for n in names)):
        cfg = dict(base)
        for n, val in zip(names, combo):
            cfg[n] = val
        h = sweep._json_hash(cfg)
        if h in seen:
            continue
        seen.add(h)
        out.append(cfg)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Exhaustive grid sweep over chosen knobs; everything else stays at DEFAULT_CONFIG.",
    )
    p.add_argument("--signals", default="signals.txt")
    p.add_argument("--charts", nargs="+", default=["data/XAUUSD_M1_*.csv"])
    p.add_argument("--output-dir", required=True)
    p.add_argument("--filter-presets", nargs="+", default=["high_growth_hour_side"],
                   choices=sweep.FILTER_PRESETS)
    p.add_argument("--grid", action="append", default=[], metavar="NAME=v1,v2,...",
                   help="Repeatable. Values to try for a config field; ungridded fields stay at DEFAULT_CONFIG.")
    p.add_argument("--max-combos", type=int, default=500,
                   help="Refuse to run if the grid expands beyond this (guards against blow-ups).")
    p.add_argument("--validate-months", type=int, default=4)
    p.add_argument("--max-sequential-dd-pct", type=float, default=35.0)
    p.add_argument("--min-no-bonus-profit", type=float, default=0.0)
    p.add_argument("--exclude-structural-anomalies", action="store_true")
    p.add_argument("--top-n", type=int, default=20)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--progress-every", type=int, default=10)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.grid:
        raise SystemExit("provide at least one --grid NAME=v1,v2,...")

    base = sweep.base_config_dict()
    grid = parse_grid(args.grid, base)
    candidates = build_grid_candidates(grid, base)
    if len(candidates) > args.max_combos:
        raise SystemExit(
            f"grid expands to {len(candidates)} combos (> --max-combos {args.max_combos}). "
            f"Narrow the value lists or raise --max-combos."
        )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = output_dir / "results.jsonl"

    print(f"[grid] fields={list(grid)} combos={len(candidates)} presets={args.filter_presets}", flush=True)
    chart = CsvChartSource(sweep._expand_chart_paths(args.charts))
    eval_args = SimpleNamespace(
        exclude_structural_anomalies=args.exclude_structural_anomalies,
        max_sequential_dd_pct=args.max_sequential_dd_pct,
        min_no_bonus_profit=args.min_no_bonus_profit,
    )

    existing = sweep.read_existing(checkpoint) if args.resume else {}
    all_rows = list(existing.values())

    for preset in args.filter_presets:
        filtered = sweep.prepare_filtered_signals(Path(args.signals), output_dir, preset)
        signals = parse_signals_file(filtered)
        train, validate = sweep.split_train_validate(signals, args.validate_months)
        print(f"[grid] preset={preset} signals={len(signals)} train={len(train)} validate={len(validate)}", flush=True)

        for idx, cfg in enumerate(candidates, start=1):
            candidate_id = sweep._json_hash({"preset": preset, "config": cfg})
            if candidate_id in existing:
                continue
            try:
                row = sweep.evaluate_candidate(
                    cfg, filter_preset=preset, signals=signals, chart=chart,
                    train_signals=train, validate_signals=validate, args=eval_args,
                )
            except Exception as exc:  # one bad combo must not kill the grid
                row = {
                    "candidate_id": candidate_id, "filter_preset": preset,
                    "error": repr(exc), "passes_recommendation_gate": False,
                    "score": -1e18, "config": cfg,
                }
            sweep.write_jsonl(checkpoint, row)
            all_rows.append(row)
            if idx % max(1, args.progress_every) == 0:
                print(f"[grid] preset={preset} {idx}/{len(candidates)}", flush=True)

    csv_path, xlsx_path = sweep.write_leaderboards(all_rows, output_dir, args.top_n)
    print(f"[grid] checkpoint={checkpoint}")
    print(f"[grid] leaderboard_csv={csv_path}")
    print(f"[grid] leaderboard_xlsx={xlsx_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())