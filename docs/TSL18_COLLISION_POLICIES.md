# TSL18 collision policies

A **testable, OFF-by-default research/backtest layer** that resolves the two
ways TSL18 can place a signal that *collides* with a position it already holds.
This document is the contract for what it is, why it exists, how it is modeled,
and how to validate it.

## The problem

TSL18 is a trend-pullback scalper, so in a fast move it keeps firing entries.
Two collision shapes show up in the live/backtest record:

1. **Opposite-side collision.** A BUY at 4750 while an earlier SELL is still
   open (or a SELL at 4950 while a BUY is open). The account carries *both*
   directions at once — a hedge it never intended.
2. **Same-side overlap.** BUY 4700, then BUY 4699, then BUY 4698 within a few
   minutes — a *cluster* of near-identical entries that stacks risk into one
   spot instead of laddering a single zone.

Neither is fixed by another RSI/SL/TP sweep — they are *interaction* effects
between signals. This layer makes the interaction an explicit, swept choice.

## What it is (and is NOT)

- It is a **stateful, deterministic decision layer** (`strategy/collision_policy.py`,
  `CollisionPolicy`), built **only** when a non-baseline policy is set
  (`CollisionPolicy.maybe` → `None` otherwise). Driven in feed order, it sees
  the signals still ACTIVE at each new signal's arrival and decides what to do.
- It only ever **REJECTS, DOWNSIZES, or BANKS/REDUCES an existing side**. It
  never invents a trade, and never moves a stop or target.
- **Default OFF → byte-identical parity.** The baseline policies
  (`--opposite-signal-policy allow_hedge` + `--same-side-overlap-policy
  allow_all`) reproduce current behavior exactly; `run_backtest` does zero extra
  work and emits no collision columns/summary
  (`tests/test_collision_policy.py::test_run_backtest_collision_off_is_byte_identical`).
- It is a **research/backtest layer.** The same shared object is wired into
  `run_backtest`, the hybrid tick backtest (via `backtest_explicit`'s parser /
  config), and is plumbed into the live `auto` config. **Live applies only the
  safe acceptance outcomes (reject / downsize); it never auto-closes or flips an
  existing live position** — that irreversible action needs separate,
  demo-validated wiring. The old-side banked P&L is a backtest model. Treat any
  promising configuration as a **fresh research result: forward-validate on
  demo before trusting it live.**

## The policies

### Opposite-side (`--opposite-signal-policy`)

| Policy | Behavior |
| --- | --- |
| `allow_hedge` | **Baseline.** Keep both sides (current behavior). |
| `reject_opposite` | Reject the new opposite signal while an opposite signal is active. |
| `profit_bank_rearm` | If the active opposite side is profitable by `--opposite-profit-threshold-r` R, **bank** it (close the profitable side), allow the new signal, and keep the banked side **rearmable only at its original planned entry or better**. Not profitable enough → fall back to `allow_hedge` (a loss is never force-banked). |
| `close_then_flip` | Close the old side and open the new side. |
| `reduce_then_hedge` | Keep both but cut the old side to `--hedge-lot-fraction` of its exposure and size the new hedge at that fraction. |

### Same-side overlap (`--same-side-overlap-policy`)

Same-side signals within `--same-side-cluster-window-minutes` (default 30) form
one **cluster**; the earliest member is its **anchor**.

| Policy | Behavior |
| --- | --- |
| `allow_all` | **Baseline.** Take every overlap. |
| `reject_overlap` | Reject an overlapping same-side signal. |
| `scale_in_better_entry_only` | Allow only a **strictly better** entry — a BUY at least `--same-side-cluster-entry-gap` LOWER than the cluster's best, a SELL that much HIGHER — and only while the cluster's total risk stays within `--max-cluster-risk-multiple` × the anchor's risk. |
| `scale_in_fixed_risk` | Allow the scale-in but **DOWNSIZE** it so cluster risk ≤ anchor risk × `--max-cluster-risk-multiple`; reject if the downsized lot would fall below the broker min lot. |

### The knobs

| Flag | Default | Meaning |
| --- | --- | --- |
| `--opposite-signal-policy` | `allow_hedge` | opposite-side policy (above) |
| `--same-side-overlap-policy` | `allow_all` | same-side policy (above) |
| `--same-side-cluster-window-minutes` | `30` | same-side cluster window |
| `--same-side-cluster-entry-gap` | `5.0` | min price improvement to scale in |
| `--same-side-cluster-sl-gap` | `10.0` | reserved: min SL separation in a cluster |
| `--max-cluster-risk-multiple` | `1.0` | cluster risk ≤ anchor risk × this |
| `--opposite-profit-threshold-r` | `0.5` | bank old side only at ≥ this many R |
| `--hedge-lot-fraction` | `0.5` | kept fraction of the old side under `reduce_then_hedge` |

All live on `tools/backtest_explicit.py` (inherited by `tools/backtest_hybrid.py`)
and `tools/auto_explicit.py`.

## How the P&L is modeled (backtest)

The layer resolves collisions at the signal-acceptance level, like the
`DeploymentGate`:

- **Reject** (`reject_opposite`, `reject_overlap`, a failed scale-in) → the new
  signal is excluded; it simply never trades. Subset-of-baseline, never invents
  (`test_run_backtest_reject_opposite_only_removes_the_opposite`).
- **Downsize** (`scale_in_fixed_risk`, the new hedge under `reduce_then_hedge`) →
  the new signal's lots/P&L are scaled. P&L scales linearly with lot, so the
  downsize is exact without re-replaying.
- **Old-side close** (`close_then_flip`, `profit_bank_rearm`, the cut side of
  `reduce_then_hedge`) → the still-open legs of the old side are marked to the
  chart price at the collision instant (no lookahead); the **banked vs natural**
  delta is folded into the colliding signal's realized P&L and reported as
  `collision_policy_pnl`. The equity curve stays continuous (equity_after −
  equity_before == the row's realized total).

The **re-arm contract** (`can_rearm`) is the live no-chase rule made explicit: a
banked BUY may re-enter only at its **original planned entry or lower**, a SELL
only at its original entry or higher, and **never** once the signal hit a system
terminal (SL/TP, a locked exit, an engine close) or its **original SL was
touched**. This mirrors `test_manual_close_rearm_nochase.py` /
`test_live_entry_guard.py` so a banked side can never resurrect into a chase or
a stopped-out trade.

## Reporting

When a policy is active, every accepted signal row + entry row carries
`collision_type`, `collision_policy`, `collision_policy_action`, `cluster_id`,
`cluster_risk_before/after`, `opposite_exposure_before/after`, and the workbook
gains a **COLLISION POLICY** column group on the Per-Entry sheet plus a **TSL18
Collision Policies** block on the Summary (totals:
`opposite_collisions_total/allowed/rejected/flipped/profit_bank_rearmed`,
`same_side_clusters_total/accepted/rejected/downsized`,
`max_same_side_cluster_risk`, `max_opposite_exposure`, `collision_policy_pnl`).
These appear **only** when a policy is active, so pure runs are byte-identical.

## Validate it

```bash
# unit + integration tests for the layer
pytest tests/test_collision_policy.py

# a collision-aware tick backtest (same flags + feed as the TSL18/T818 snapshot)
python tools/backtest_hybrid.py --signals signals/t818.txt \
  --charts "data/XAUUSD_M1_*_ELEV8.csv" --ticks "data/ticks/XAUUSD_TICK_*_ELEV8.csv" \
  --output-dir reports/T818_collision_reject_opposite \
  --opposite-signal-policy reject_opposite \
  ...same geometry flags as cli/candidate_T818_trailing_tick.txt...
```

Compare the collision summary + `collision_policy_pnl` and the
`max_consecutive_losing_signals` / max-daily-loss against the baseline run
(both policies at their defaults). **Promote bar:** a policy must reduce the
sequential-loss / hedge-churn it targets **without** giving back edge or OOS at
DD ≤ the deployed gate — and then survive a demo forward-test, since the live
old-side actions are not yet wired. **Do not run the full aggressive sweep to
land this layer**; it ships OFF and is swept separately under
`docs/SWEEP_RUNBOOK.md` once a hypothesis is set.
