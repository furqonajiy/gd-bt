# TSL18 quality-entry + collision sweep — smoke (2026-06-27..2026-07-01)

Score objective: **edge_plus_rebate_guarded**. Base TSL18 feed vs quality-entry AND collision-policy variants, same TSL18 geometry, TICK where covered. **Rebate-aware**: a candidate green only on the $3/closed-lot rebate (bad pure P&L) is guarded out, never promoted. **Collision policies are real** here — the opposite/same-side metrics come from backtest_hybrid's collision layer (zero for a baseline candidate, populated for a non-baseline policy).

| label | profile | extreme | opp policy | same policy | pure $ | net $ | score | guards | DD% | coll $ | opp tot/rej/bank | same tot/rej/dsz | ranked |
|---|---|---|---|---|--:|--:|--:|:--|--:|--:|:--:|:--:|:--:|
| base | off | off | allow_hedge | allow_all | -2591.63 | -2547.95 | -2591.63 | pure_pnl_below_min | 6.27 | 0 | 0/0/0 | 0/0/0 | False |
| hybrid_quality | hybrid_quality | off | allow_hedge | allow_all | -120.73 | -116.05 | -120.73 | pure_pnl_below_min | 1.85 | 0 | 0/0/0 | 0/0/0 | False |
| profit_bank_rearm | off | off | profit_bank_rearm | allow_all | -2591.63 | -2547.95 | -2591.63 | pure_pnl_below_min | 6.27 | 0.0 | 95/0/0 | 0/0/0 | False |
| hybrid_quality_profit_bank_scale_better | hybrid_quality | off | profit_bank_rearm | scale_in_better_entry_only | -92.83 | -90.22 | -92.83 | pure_pnl_below_min | 0.87 | 0.0 | 5/0/0 | 7/7/0 | False |

Collision columns: **opp tot/rej/bank** = opposite collisions total / rejected / profit-bank-rearmed; **same tot/rej/dsz** = same-side clusters total / rejected / downsized; **coll $** = collision_policy_pnl (banked old-side delta).

## Ranked survivors (gates passed)

_No survivors (skeleton run, or all candidates failed the gates)._

Gates: **rebate guards** (pure-P&L floor + max rebate share), **partial-tick-lifecycle exclusion** (mixed TICK/M1 windows when `--require-full-tick-lifecycle`), **open/pending-left** (`--exclude-open-or-pending`), and **collision-metrics-present** (a non-baseline policy whose run emitted no collision block is excluded).
