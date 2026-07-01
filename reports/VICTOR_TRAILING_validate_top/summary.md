# Victor/V017 trailing-geometry sweep — validate_top (2026-01-01..2026-07-01)

Score objective: **edge_plus_rebate_guarded**. Base V017 geometry vs single-knob trailing-geometry perturbations on the Victor provider feed, TICK where covered. **Rebate-aware**: a candidate green only on the $3/closed-lot rebate (bad pure P&L) is guarded out, never promoted. Research/backtest only — never live.

| label | open | close | stage | slm | hold | expiry | gap | final | pure $ | net $ | DD% | win% | sigs | ranked | guard |
|---|--:|--:|--:|--:|--:|--:|--:|:--|--:|--:|--:|--:|--:|:--:|:--|

## Ranked survivors (gates passed)

_No survivors (skeleton run, or all candidates failed the gates)._

Gates: **rebate guards** (pure-P&L floor + max rebate share), **partial-tick-lifecycle exclusion** (mixed TICK/M1 windows when `--require-full-tick-lifecycle`), and **open/pending-left** (`--exclude-open-or-pending`). Prefer a survivor that beats `base_v017` on pure P&L at similar/lower DD, consistently across the validation window — not one that wins on a single lucky signal.
