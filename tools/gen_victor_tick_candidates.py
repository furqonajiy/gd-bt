#!/usr/bin/env python3
"""Candidate list for the VICTOR tick tune (May+June R4), as a GitHub dynamic matrix.

Unlike the scalper24 sweep, the Victor feed is the PROVIDER's signals
(victor_signals.txt, a committed file) -- there are no generator/feed levers to
tune. So this samples only the STRATEGY/lifecycle space the deployed Victor uses
(sl_multiplier, max_hold, tp1_lock_fraction, entries, tp1_lock_delay,
lock_after_tp2, final_target), scored on the real executor over May+June ticks at
Victor's risk 2.5% sizing. The deployed Victor champion is always candidate c000.

Note: the Victor *signal R:R / ATR* policy (rewrite_tp_rr, sl_source=atr, ...) is
NOT swept here -- tick_backtest can't apply it, and the deployed Victor doesn't
use it. Those would need a feed-rewrite pre-step (a later phase).
"""
from __future__ import annotations

import argparse
import json
import random

GRID = {
    "slm": [1.7, 1.9, 2.1, 2.3, 2.5],
    "maxhold": [180, 240, 300],
    "frac": [0.25, 0.5, 0.75],
    "entries": [6, 7, 8],
    "delay": [0, 12, 24],
    "lock2": ["true", "false"],
    "final": ["TP2", "TP3"],
}
# deployed Victor champion (cli/champion_victor.txt) -> always c000
CHAMPION = {"slm": 2.1, "maxhold": 240, "frac": 0.5, "entries": 8,
            "delay": 24, "lock2": "true", "final": "TP3"}


def _cand(i: int, combo: dict) -> dict:
    return {"id": f"c{i:03d}", "slgap": 0.5, **combo}


def generate(n: int, seed: int = 20260625) -> list[dict]:
    rng = random.Random(seed)
    keys = list(GRID)
    seen: set[tuple] = set()
    out = [_cand(0, CHAMPION)]
    seen.add(tuple(sorted((k, str(CHAMPION[k])) for k in keys)))
    guard = 0
    while len(out) < n and guard < n * 200:
        guard += 1
        combo = {k: rng.choice(GRID[k]) for k in keys}
        sig = tuple(sorted((k, str(combo[k])) for k in keys))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(_cand(len(out), combo))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Emit the Victor tick-tune candidate matrix JSON.")
    p.add_argument("--n", type=int, default=150, help="Candidates incl. champion (<=256).")
    p.add_argument("--seed", type=int, default=20260625)
    args = p.parse_args(argv)
    n = max(1, min(args.n, 256))
    print(json.dumps({"include": generate(n, args.seed)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
