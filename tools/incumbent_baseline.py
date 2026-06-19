#!/usr/bin/env python3
"""Evaluate the INCUMBENT (live) config on a fixed feed for one regime.

The champion/challenger deploy workflow (`.github/workflows/self-regime-grid.yml`)
compares the user's INCUMBENT live config -- the scalper24 baseline,
``entry_count=6, sl_multiplier=2.1, tp1_lock_delay_minutes=24`` on top of the
blessed ``DEFAULT_CONFIG`` -- against the per-regime sweep CHALLENGER winners.
For that comparison to be apples-to-apples the incumbent MUST be scored with the
*exact same metric definitions* the sweep uses (fixed-lot no-bonus edge, held-out
fixed-lot OOS, concurrent risk-sized DD). So rather than re-deriving those
metrics, this script imports ``tools.sweep_self_limit.evaluate_self_limit`` -- the
very function each sweep cell runs per candidate -- and calls it once on the
incumbent config.

Unlike the adaptive feeds the grid sweeps, the incumbent is evaluated on the
FIXED self archive (``generated/self_better.txt`` by default): that is what the
user actually trades live, so it is the honest baseline to beat.

Emits ``INCUMBENT_<regime>.json`` with ``edge`` (fixed_no_bonus_profit),
``oos`` (oos_fixed_no_bonus_profit) and ``dd`` (concurrent_risk_max_dd_pct),
plus the config + feed it was scored on, so the aggregate can render the
deploy file without recomputing anything.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sweep = importlib.import_module("tools.sweep")
ssl = importlib.import_module("tools.sweep_self_limit")

from xauusd_trading import CsvChartSource, parse_signals_file  # noqa: E402


def incumbent_config() -> dict:
    """The sweep's incumbent baseline = **SC24** (the R4 live champion SQZ6, in
    cli_champion_R4_SQZ6_no_trailing, builds on this scalper24 base):
    the blessed DEFAULT_CONFIG + the SC24 overrides, defined once in
    ``sweep.sc24_config()`` and shared with the sweep's seeded staged grid so the
    "did a challenger beat the live champion?" verdict is exactly apples-to-apples
    with what the user actually trades (entries 6, slm 2.1, max_hold 240, locks
    with tp1-delay 24 / tp2-delay 2, no trailing, 1% risk)."""
    return sweep.sc24_config()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Score the INCUMBENT live config on a fixed feed for one "
                    "regime, reusing the sweep's own metric definitions.")
    p.add_argument("--regime", required=True,
                   help="Regime label, used only for the output filename.")
    p.add_argument("--signals", default="generated/self_better.txt",
                   help="FIXED incumbent feed (what the user trades live).")
    p.add_argument("--charts", nargs="+", required=True,
                   help="This regime's chart month-glob (same as the shard job).")
    p.add_argument("--output-dir", default="sweep_regime_out_grid",
                   help="Where INCUMBENT_<regime>.json is written.")
    p.add_argument("--validate-months", type=int, default=2,
                   help="Held-out tail months for fixed-lot OOS (match the sweep).")
    p.add_argument("--max-concurrent-dd-pct", type=float, default=40.0,
                   help="DD gate (used only for the recorded pass flag; match the sweep).")
    p.add_argument("--fixed-lot", type=float, default=0.01)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    signals_path = Path(args.signals)
    if not signals_path.exists():
        raise SystemExit(f"incumbent feed not found: {signals_path}")

    chart = CsvChartSource(sweep._expand_chart_paths(args.charts))
    signals = parse_signals_file(signals_path)
    _, validate = sweep.split_train_validate(signals, args.validate_months)

    # Exactly the eval_args SimpleNamespace shape sweep_self_limit.main builds,
    # with the gate thresholds wide open except DD/oos -- we only read the three
    # metrics, the pass flags are informational.
    eval_args = SimpleNamespace(
        exclude_structural_anomalies=False,
        max_concurrent_dd_pct=args.max_concurrent_dd_pct,
        min_fixed_no_bonus_profit=0.0,
        min_stable_month_fraction=0.0,
        worst_month_floor=-1e18,
        walk_forward_months=2,
        min_walk_forward_folds=1,
        min_walk_forward_positive_fraction=0.50,
        walk_forward_worst_fold_floor=-1e18,
        fixed_lot=args.fixed_lot,
    )

    cfg = incumbent_config()
    print(f"[incumbent] regime={args.regime} feed={signals_path} "
          f"signals={len(signals)} validate={len(validate)} "
          f"charts={args.charts}", flush=True)

    row = ssl.evaluate_self_limit(
        cfg, signals=signals, chart=chart,
        validate_signals=validate, args=eval_args)

    out = {
        "regime": args.regime,
        "feed": str(signals_path),
        "kind": "incumbent",
        "edge": row.get("fixed_no_bonus_profit"),
        "oos": row.get("oos_fixed_no_bonus_profit"),
        "dd": row.get("concurrent_risk_max_dd_pct"),
        "net_bonus": row.get("risk_net_profit_with_bonus"),
        "passes_walk_forward": row.get("passes_walk_forward"),
        "walk_forward_folds": row.get("walk_forward_folds"),
        "walk_forward_positive_fraction": row.get("walk_forward_positive_fraction"),
        "walk_forward_worst_pnl": row.get("walk_forward_worst_pnl"),
        "config": cfg,
        "config_json": json.dumps(cfg, sort_keys=True),
    }

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"INCUMBENT_{args.regime}.json"
    out_path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(f"[incumbent] regime={args.regime} net_bonus={out['net_bonus']} "
          f"edge={out['edge']} oos={out['oos']} dd={out['dd']}%", flush=True)
    print(f"[incumbent] wrote {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
