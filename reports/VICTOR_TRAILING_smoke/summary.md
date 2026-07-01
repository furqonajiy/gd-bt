# Victor/V017 trailing-geometry sweep — smoke (2026-06-25..2026-07-01)

Score objective: **edge_plus_rebate_guarded**. Base V017 geometry vs single-knob trailing-geometry perturbations on the Victor provider feed, TICK where covered. **Rebate-aware**: a candidate green only on the $3/closed-lot rebate (bad pure P&L) is guarded out, never promoted. Research/backtest only — never live.

| label | open | close | stage | slm | hold | expiry | gap | final | pure $ | net $ | DD% | win% | sigs | ranked | guard |
|---|--:|--:|--:|--:|--:|--:|--:|:--|--:|--:|--:|--:|--:|:--:|:--|
| base_v017 | 0.5 | 0.5 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | -261.72 | -240.0 | 3.88 | 50.0 | 34 | False | pure_pnl_below_min |
| open_0_75 | 0.75 | 0.5 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | 15.12 | 36.12 | 3.91 | 50.0 | 34 | False | rebate_share_too_high |
| close_0_75_stage1 | 0.5 | 0.75 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | -261.72 | -240.0 | 3.88 | 50.0 | 34 | False | pure_pnl_below_min |
| slm_1_80 | 0.5 | 0.5 | 1 | 1.8 | 150 | 180 | 0.5 | TP2 | -519.89 | -499.46 | 4.04 | 50.0 | 34 | False | pure_pnl_below_min |

## Ranked survivors (gates passed)

_No survivors (skeleton run, or all candidates failed the gates)._

Gates: **rebate guards** (pure-P&L floor + max rebate share), **partial-tick-lifecycle exclusion** (mixed TICK/M1 windows when `--require-full-tick-lifecycle`), and **open/pending-left** (`--exclude-open-or-pending`). Prefer a survivor that beats `base_v017` on pure P&L at similar/lower DD, consistently across the validation window — not one that wins on a single lucky signal.
