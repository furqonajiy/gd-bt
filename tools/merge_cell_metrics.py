#!/usr/bin/env python3
"""Merge one tick-sweep cell's TICK + M1 backtests into a single comparison record.

A cell is scored on the SAME June window three ways so TICK vs M1 is apples-to-
apples:
  --tick       parse_tick_run.py JSON  (tools/tick_backtest.py, real executor)
  --m1-risk    backtest_explicit JSON  (M1 engine, risk 1%, slippage 2.0/1.0)
  --m1-fixed   backtest_explicit JSON  (M1 engine, fixed 0.01 lot -> edge proxy)

Output is one flat JSON row: the config, the TICK result, the M1 result
(compounded net incl. bonus, net ex-bonus, concurrent-risk DD%, closed lots, win
rate), the fixed-lot edge, and the TICK<->M1 discrepancy ($ and %). The sweep
ranks on TICK net (the real-executor truth); the M1 columns expose how much the
backtest over-states.
"""
from __future__ import annotations

import argparse
import json
import sys


def _load(path: str | None) -> dict:
    if not path:
        return {}
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception as e:  # missing/failed backtest -> empty, row still emitted
        print(f"[merge] could not load {path}: {e}", file=sys.stderr)
        return {}


def merge(config: dict, tick: dict, m1_risk: dict, m1_fixed: dict) -> dict:
    tick_net = tick.get("tick_pnl")
    m1_net = m1_risk.get("net_profit")
    disc = (m1_net - tick_net) if (m1_net is not None and tick_net is not None) else None
    disc_pct = (disc / abs(tick_net) * 100) if (disc is not None and tick_net) else None
    row = {
        **config,
        # TICK (real executor, risk 1%, June)
        "tick_net": tick_net,
        "tick_dd_pct": tick.get("max_drawdown_pct"),
        "tick_nofill": tick.get("n_nofill"),
        "tick_reasons": tick.get("reasons"),
        # M1 (risk 1%, slippage 2.0/1.0, June) -- "compounding + net" + concurrent DD
        "m1_net": m1_net,                                    # incl. $3/lot bonus (compounded)
        "m1_net_nobonus": m1_risk.get("trading_pnl"),
        "m1_bonus": m1_risk.get("bonus"),
        "m1_dd_pct": abs(m1_risk.get("max_drawdown_pct")) if m1_risk.get("max_drawdown_pct") is not None else None,
        "m1_closed_lots": m1_risk.get("closed_lots"),
        "m1_winrate": m1_risk.get("win_rate_pct"),
        # M1 fixed-lot edge (capital-independent)
        "m1_edge_fixed": m1_fixed.get("net_profit"),
        # discrepancy: how much M1 over-states the real tick result
        "disc_m1_minus_tick": round(disc, 2) if disc is not None else None,
        "disc_pct": round(disc_pct, 1) if disc_pct is not None else None,
    }
    return row


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Merge a tick-sweep cell's TICK + M1 results.")
    p.add_argument("--config", required=True, help="Config JSON (the matrix entry).")
    p.add_argument("--tick", help="parse_tick_run.py output JSON.")
    p.add_argument("--m1-risk", help="backtest_explicit risk-1% summary JSON.")
    p.add_argument("--m1-fixed", help="backtest_explicit fixed-lot summary JSON.")
    args = p.parse_args(argv)
    cfg = json.loads(args.config)
    row = merge(cfg, _load(args.tick), _load(args.m1_risk), _load(args.m1_fixed))
    print(json.dumps(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
