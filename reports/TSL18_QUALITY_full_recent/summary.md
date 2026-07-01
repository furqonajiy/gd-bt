# TSL18 quality-entry + collision sweep — full_recent (2026-06-01..2026-08-01)

Score objective: **edge_plus_rebate_guarded**. Base TSL18 feed vs quality-entry AND collision-policy variants, same TSL18 geometry, TICK where covered. **Rebate-aware**: a candidate green only on the $3/closed-lot rebate (bad pure P&L) is guarded out, never promoted. **Collision policies are real** here — the opposite/same-side metrics come from backtest_hybrid's collision layer (zero for a baseline candidate, populated for a non-baseline policy).

| label | profile | extreme | opp policy | same policy | pure $ | net $ | score | guards | DD% | coll $ | opp tot/rej/bank | same tot/rej/dsz | ranked |
|---|---|---|---|---|--:|--:|--:|:--|--:|--:|:--:|:--:|:--:|
| base | off | off | allow_hedge | allow_all | 170265.73 | 171798.85 | 171798.85 | ok | 14.28 | 0 | 0/0/0 | 0/0/0 | False |
| trend_only | trend_only | off | allow_hedge | allow_all | 11236.89 | 11332.98 | 11332.98 | ok | 7.79 | 0 | 0/0/0 | 0/0/0 | False |
| reversal_extreme | reversal_extreme | off | allow_hedge | allow_all | 980.48 | 1018.01 | 1018.01 | ok | 7.67 | 0 | 0/0/0 | 0/0/0 | False |
| hybrid_quality | hybrid_quality | off | allow_hedge | allow_all | 12195.76 | 12334.9 | 12334.9 | ok | 7.27 | 0 | 0/0/0 | 0/0/0 | False |
| high_frequency_quality | high_frequency_quality | off | allow_hedge | allow_all | 12195.76 | 12334.9 | 12334.9 | ok | 7.27 | 0 | 0/0/0 | 0/0/0 | False |
| extreme_support_demand | off | support_demand | allow_hedge | allow_all | -1884.63 | -1836.27 | -1884.63 | pure_pnl_below_min | 8.33 | 0 | 0/0/0 | 0/0/0 | False |
| extreme_supply_resistance | off | supply_resistance | allow_hedge | allow_all | 2841.58 | 2847.85 | 2847.85 | ok | 0.45 | 0 | 0/0/0 | 0/0/0 | False |
| extreme_both | off | both | allow_hedge | allow_all | 1254.21 | 1309.32 | 1309.32 | ok | 7.97 | 0 | 0/0/0 | 0/0/0 | False |
| reject_opposite | off | off | reject_opposite | allow_all | 45920.2 | 46612.42 | 46612.42 | ok | 15.48 | 0.0 | 683/683/0 | 0/0/0 | False |
| profit_bank_rearm | off | off | profit_bank_rearm | allow_all | 164996.18 | 166507.79 | 166507.79 | ok | 14.38 | -3869.25 | 1008/0/25 | 0/0/0 | False |
| same_reject_overlap | off | off | allow_hedge | reject_overlap | 36404.29 | 36807.01 | 36807.01 | ok | 3.83 | 0.0 | 0/0/0 | 1135/1135/0 | False |
| same_scale_better_only | off | off | allow_hedge | scale_in_better_entry_only | 36404.29 | 36807.01 | 36807.01 | ok | 3.83 | 0.0 | 0/0/0 | 1135/1135/0 | False |
| same_scale_fixed_risk | off | off | allow_hedge | scale_in_fixed_risk | 36404.29 | 36807.01 | 36807.01 | ok | 3.83 | 0.0 | 0/0/0 | 1135/1135/0 | False |
| hybrid_quality_profit_bank | hybrid_quality | off | profit_bank_rearm | allow_all | 11922.76 | 12061.66 | 12061.66 | ok | 7.21 | -337.8 | 16/0/1 | 0/0/0 | False |
| hybrid_quality_scale_better | hybrid_quality | off | allow_hedge | scale_in_better_entry_only | 4597.84 | 4659.1 | 4659.1 | ok | 4.35 | 0.0 | 0/0/0 | 89/89/0 | False |
| hybrid_quality_profit_bank_scale_better | hybrid_quality | off | profit_bank_rearm | scale_in_better_entry_only | 4293.82 | 4355.08 | 4355.08 | ok | 4.35 | -304.02 | 16/0/1 | 89/89/0 | False |
| hybrid_quality_extreme_both_profit_bank_scale_better | hybrid_quality | both | profit_bank_rearm | scale_in_better_entry_only | 1327.58 | 1351.94 | 1351.94 | ok | 3.17 | 0.0 | 2/0/0 | 31/31/0 | False |

Collision columns: **opp tot/rej/bank** = opposite collisions total / rejected / profit-bank-rearmed; **same tot/rej/dsz** = same-side clusters total / rejected / downsized; **coll $** = collision_policy_pnl (banked old-side delta).

## Ranked survivors (gates passed)

_No survivors (skeleton run, or all candidates failed the gates)._

Gates: **rebate guards** (pure-P&L floor + max rebate share), **partial-tick-lifecycle exclusion** (mixed TICK/M1 windows when `--require-full-tick-lifecycle`), **open/pending-left** (`--exclude-open-or-pending`), and **collision-metrics-present** (a non-baseline policy whose run emitted no collision block is excluded).
