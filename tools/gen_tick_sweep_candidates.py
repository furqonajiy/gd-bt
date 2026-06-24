#!/usr/bin/env python3
"""Generate the candidate list for the FULL R4 non-trailing TICK sweep.

Emits a GitHub Actions dynamic-matrix JSON ({"include": [..]}) of up to --n
candidates, each a full non-trailing config sampled over BOTH the feed levers
(the scalper24 generator flags -- the champion's stated edge lever) and the
strategy/geometry levers. The workflow scores each candidate with
tools/tick_backtest.py on the committed June ticks and ranks on tick P&L, so the
new champion is decided on real-executor fills, not the optimistic M1 engine.

Deterministic: a fixed seed makes the candidate set reproducible across re-runs
(so a resumed/retried workflow scores the same grid). The current champion is
always candidate c000 so the leaderboard shows the incumbent in-line.
"""
from __future__ import annotations

import argparse
import json
import random

# --- the full non-trailing search space -----------------------------------
# Feed levers (generator flags) -- the champion's edge lever is the feed.
FEED = {
    "rr": [(1.0, 2.0, 4.0), (0.8, 1.5, 3.0), (1.0, 2.0, 3.0), (1.2, 2.5, 5.0), (1.0, 2.5, 4.0)],
    "bb": ["0.0004", "0.0006", "0.0008"],
    "rsi": [(75, 25), (70, 30), (80, 20), (100, 0)],  # (buy_max, sell_min); (100,0) = RSI off
}
# Strategy / geometry levers.
STRAT = {
    "slm": [1.5, 1.7, 1.9, 2.1, 2.3, 2.5],
    "entries": [6, 7, 8],
    "slgap": [0.3, 0.5, 0.7],
    "maxhold": [180, 240, 300],
    "delay": [0, 12, 24],
    "frac": [0.25, 0.5, 0.75],
    "lock2": ["true", "false"],
    "final": ["TP2", "TP3"],
}

# The deployed R4 champion (rsi75_sqz6_rr40) -- always scored as c000.
CHAMPION = {
    "rr": (1.0, 2.0, 4.0), "bb": "0.0006", "rsi": (75, 25),
    "slm": 2.1, "entries": 8, "slgap": 0.5, "maxhold": 240,
    "delay": 24, "frac": 0.5, "lock2": "true", "final": "TP3",
}


def _candidate(i: int, combo: dict) -> dict:
    rr1, rr2, rr3 = combo["rr"]
    rsi_buy, rsi_sell = combo["rsi"]
    return {
        "id": f"c{i:03d}",
        "rr1": rr1, "rr2": rr2, "rr3": rr3,
        "bb": combo["bb"], "rsi_buy": rsi_buy, "rsi_sell": rsi_sell,
        "slm": combo["slm"], "entries": combo["entries"], "slgap": combo["slgap"],
        "maxhold": combo["maxhold"], "delay": combo["delay"], "frac": combo["frac"],
        "lock2": combo["lock2"], "final": combo["final"],
    }


def generate(n: int, seed: int = 20260625) -> list[dict]:
    rng = random.Random(seed)
    keys = list(FEED) + list(STRAT)
    space = {**FEED, **STRAT}
    seen: set[tuple] = set()
    out = [_candidate(0, CHAMPION)]
    seen.add(tuple(sorted((k, str(CHAMPION[k])) for k in keys)))
    # Random sample distinct combos until we hit n (capped well under the GitHub
    # 256-matrix-entry limit by the caller).
    guard = 0
    while len(out) < n and guard < n * 200:
        guard += 1
        combo = {k: rng.choice(space[k]) for k in keys}
        sig = tuple(sorted((k, str(combo[k])) for k in keys))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(_candidate(len(out), combo))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Emit the tick-sweep candidate matrix JSON.")
    p.add_argument("--n", type=int, default=200, help="Candidates incl. champion (<=256).")
    p.add_argument("--seed", type=int, default=20260625)
    args = p.parse_args(argv)
    n = max(1, min(args.n, 256))
    print(json.dumps({"include": generate(n, args.seed)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
