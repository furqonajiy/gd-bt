# July staged Victor/V017 trailing-geometry sweep — status

- Run: `28510754898`  objective: `edge_plus_rebate_guarded`
- Recent window: May+June (tick-covered); validation: Jan-Jun (tick where covered).
- Bounded curated single-knob grid from base_v017, NOT a cartesian product.
- Research/backtest only; never live.

## smoke
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

## full_recent
# Victor/V017 trailing-geometry sweep — full_recent (2026-05-01..2026-07-01)

Score objective: **edge_plus_rebate_guarded**. Base V017 geometry vs single-knob trailing-geometry perturbations on the Victor provider feed, TICK where covered. **Rebate-aware**: a candidate green only on the $3/closed-lot rebate (bad pure P&L) is guarded out, never promoted. Research/backtest only — never live.

| label | open | close | stage | slm | hold | expiry | gap | final | pure $ | net $ | DD% | win% | sigs | ranked | guard |
|---|--:|--:|--:|--:|--:|--:|--:|:--|--:|--:|--:|--:|--:|:--:|:--|
| base_v017 | 0.5 | 0.5 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | 111704.18 | 112465.28 | 14.05 | 51.0 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| open_0_25 | 0.25 | 0.5 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | 140274.39 | 141088.32 | 13.62 | 51.8 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| open_0_50 | 0.5 | 0.5 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | 111704.18 | 112465.28 | 14.05 | 51.0 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| open_0_75 | 0.75 | 0.5 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | 109857.7 | 110626.33 | 14.17 | 52.2 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| open_1_00 | 1.0 | 0.5 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | 97993.76 | 98721.17 | 13.66 | 51.7 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| close_0_25_stage1 | 0.5 | 0.25 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | 113793.28 | 114560.8 | 14.03 | 51.0 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| close_0_50_stage1 | 0.5 | 0.5 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | 111704.18 | 112465.28 | 14.05 | 51.0 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| close_0_75_stage1 | 0.5 | 0.75 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | 109103.19 | 109858.59 | 14.08 | 51.0 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| close_0_50_stage2 | 0.5 | 0.5 | 2 | 1.7 | 150 | 180 | 0.5 | TP2 | 88437.67 | 89120.38 | 14.15 | 50.6 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| slm_1_60 | 0.5 | 0.5 | 1 | 1.6 | 150 | 180 | 0.5 | TP2 | 125685.37 | 126524.08 | 14.47 | 49.8 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| slm_1_70 | 0.5 | 0.5 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | 111704.18 | 112465.28 | 14.05 | 51.0 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| slm_1_80 | 0.5 | 0.5 | 1 | 1.8 | 150 | 180 | 0.5 | TP2 | 107374.74 | 108064.17 | 14.25 | 51.8 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| hold_120 | 0.5 | 0.5 | 1 | 1.7 | 120 | 180 | 0.5 | TP2 | 94760.23 | 95467.57 | 14.73 | 51.2 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| hold_150 | 0.5 | 0.5 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | 111704.18 | 112465.28 | 14.05 | 51.0 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| hold_180 | 0.5 | 0.5 | 1 | 1.7 | 180 | 180 | 0.5 | TP2 | 114278.25 | 115040.97 | 13.87 | 50.0 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| expiry_120 | 0.5 | 0.5 | 1 | 1.7 | 150 | 120 | 0.5 | TP2 | 83119.02 | 83790.84 | 12.29 | 50.0 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| expiry_180 | 0.5 | 0.5 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | 111704.18 | 112465.28 | 14.05 | 51.0 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| entry_gap_0_50 | 0.5 | 0.5 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | 111704.18 | 112465.28 | 14.05 | 51.0 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| entry_gap_0_70 | 0.5 | 0.5 | 1 | 1.7 | 150 | 180 | 0.7 | TP2 | 116635.51 | 117411.49 | 14.05 | 51.0 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| tp2_final | 0.5 | 0.5 | 1 | 1.7 | 150 | 180 | 0.5 | TP2 | 111704.18 | 112465.28 | 14.05 | 51.0 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |
| tp3_final | 0.5 | 0.5 | 1 | 1.7 | 150 | 180 | 0.5 | TP3 | 126879.65 | 127665.5 | 14.04 | 49.4 | 365 | False | excluded: mixed tick/M1 (partial tick lifecycle) |

## Ranked survivors (gates passed)

_No survivors (skeleton run, or all candidates failed the gates)._

Gates: **rebate guards** (pure-P&L floor + max rebate share), **partial-tick-lifecycle exclusion** (mixed TICK/M1 windows when `--require-full-tick-lifecycle`), and **open/pending-left** (`--exclude-open-or-pending`). Prefer a survivor that beats `base_v017` on pure P&L at similar/lower DD, consistently across the validation window — not one that wins on a single lucky signal.

## validate_top
# Victor/V017 trailing-geometry sweep — validate_top (2026-01-01..2026-07-01)

Score objective: **edge_plus_rebate_guarded**. Base V017 geometry vs single-knob trailing-geometry perturbations on the Victor provider feed, TICK where covered. **Rebate-aware**: a candidate green only on the $3/closed-lot rebate (bad pure P&L) is guarded out, never promoted. Research/backtest only — never live.

| label | open | close | stage | slm | hold | expiry | gap | final | pure $ | net $ | DD% | win% | sigs | ranked | guard |
|---|--:|--:|--:|--:|--:|--:|--:|:--|--:|--:|--:|--:|--:|:--:|:--|

## Ranked survivors (gates passed)

_No survivors (skeleton run, or all candidates failed the gates)._

Gates: **rebate guards** (pure-P&L floor + max rebate share), **partial-tick-lifecycle exclusion** (mixed TICK/M1 windows when `--require-full-tick-lifecycle`), and **open/pending-left** (`--exclude-open-or-pending`). Prefer a survivor that beats `base_v017` on pure P&L at similar/lower DD, consistently across the validation window — not one that wins on a single lucky signal.

