# TSL18 quality-entry + collision sweep — validate_top (2026-01-01..2026-08-01)

Score objective: **edge_plus_rebate_guarded**. Base TSL18 feed vs quality-entry AND collision-policy variants, same TSL18 geometry, TICK where covered. **Rebate-aware**: a candidate green only on the $3/closed-lot rebate (bad pure P&L) is guarded out, never promoted. **Collision policies are real** here — the opposite/same-side metrics come from backtest_hybrid's collision layer (zero for a baseline candidate, populated for a non-baseline policy).

| label | profile | extreme | opp policy | same policy | pure $ | net $ | score | guards | DD% | coll $ | opp tot/rej/bank | same tot/rej/dsz | ranked |
|---|---|---|---|---|--:|--:|--:|:--|--:|--:|:--:|:--:|:--:|

Collision columns: **opp tot/rej/bank** = opposite collisions total / rejected / profit-bank-rearmed; **same tot/rej/dsz** = same-side clusters total / rejected / downsized; **coll $** = collision_policy_pnl (banked old-side delta).

## Ranked survivors (gates passed)

_No survivors (skeleton run, or all candidates failed the gates)._

Gates: **rebate guards** (pure-P&L floor + max rebate share), **partial-tick-lifecycle exclusion** (mixed TICK/M1 windows when `--require-full-tick-lifecycle`), **open/pending-left** (`--exclude-open-or-pending`), and **collision-metrics-present** (a non-baseline policy whose run emitted no collision block is excluded).
