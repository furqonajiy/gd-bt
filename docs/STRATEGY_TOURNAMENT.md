# Strategy Tournament Report

Use this after Victor and self-generated sweeps have produced artifacts. It
normalizes completed sweep outputs into one scoreboard so the best Victor-feed
strategy and the best self-generated-feed strategy can be compared under the
same DD/OOS/edge/bonus lens.

Example:

```bash
python tools/strategy_tournament_report.py \
  --source Victor=victor_results \
  --source Self=sweep_regime_out_grid \
  --out reports/STRATEGY_TOURNAMENT.md \
  --json-out reports/STRATEGY_TOURNAMENT.json \
  --dd-gate 40
```

Accepted source paths:

- a directory containing `leaderboard.csv`, `results.jsonl`, `CHAMPION_*.json`,
  `INCUMBENT_*.json`, or `WINNER_*.json`
- a single `leaderboard.csv`
- a single `results.jsonl`
- a single champion/incumbent/winner JSON record

Ranking:

1. fixed-lot edge + bonus
2. OOS
3. fixed-lot edge
4. walk-forward positive-fold rate, when present
5. compounded net+bonus as the last tiebreak/context field

The report only admits rows that pass the DD gate and OOS > 0. If a row contains
`passes_walk_forward=false`, it is rejected; older result files without that
field remain readable.
