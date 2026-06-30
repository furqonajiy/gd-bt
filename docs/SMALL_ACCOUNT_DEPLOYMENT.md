# Small-account ($2K) safe deployment of TSL18 / T818

This is the contract for running the existing **profitable-but-volatile**
self-scalper (T818, live tag TSL18) on a **small $2,000 account** without one bad
zone blowing the account. It does **not** invent a new edge — it adds a
deployment-risk wrapper (candidate **TS2K**) and the tooling to validate it.

**Be honest up front:** TS2K reduces tail risk and cluster exposure and **will
cap upside**. Judge it by max drawdown, worst daily loss, and *survival* first —
not net profit. It is **research / demo** until the live executor enforces the
gates (see "Live enforcement gap" below) and a demo A/B confirms it.

## Why full 8-entry TSL18 is unsafe at $2K — the 0.01-lot floor

TSL18 ladders up to **8 entries into one price zone**. When that zone fails, all
8 legs can hit SL together. On a large account that is sized to ~1% per leg and
the cluster is a controlled loss. On a **$2,000** account it is not, because the
broker minimum order size is **0.01 lot** and you cannot go below it:

- XAUUSD 0.01 lot ≈ 1 oz, so a $1 gold move ≈ $1 P&L. One 0.01-lot leg stopped
  out at distance `D` (price points) loses ≈ `$D`.
- 1% of $2,000 is only **$20**. On a **wide-stop** signal, a single 0.01-lot leg
  already risks **more** than 1% — and you cannot size down. The account is
  *forced to over-risk*.
- 8 legs × 0.01 lot = **0.08 lot** stacked in one zone.

### Risk math (8-entry zone, 0.01 lot, $2K)

| per-leg stop `D` | 8-entry zone loss (≈8·D) | % of $2,000 |
|---|---|---|
| $20 | $160 | **8%** |
| $30 | $240 | **12%** |
| $50 | $400 | **20%** |
| $75 | $600 | **30%** |

A single failed wide-stop zone can exceed any sane daily-loss limit. That is the
problem TS2K exists to solve.

## The TS2K deployment wrapper

Same feed, same geometry. Only the deployment constraints change:

| lever | TSL18 | TS2K | why |
|---|---|---|---|
| entries per signal | 8 | **2** | fewer legs in one zone → a failed zone costs 2·D, not 8·D |
| max concurrent open signals | unlimited | **1** | stops several zones stacking in one session |
| daily-loss circuit breaker | none | **5–6%** | stop *new* signals after the day is down 5–6% of start-of-day equity |
| risk-budget gate | off | **on** | refuse a signal whose worst-case min-lot zone risk is too big for equity |
| └ max single-entry risk | — | **4%** of equity | one min-lot leg can't risk > ~$80 at $2K |
| └ max zone risk | — | **6%** of equity | the allowed 2-leg zone can't risk > ~$120 at $2K |
| capital | $50K base | **$2,000** | |
| lot | 0.01 floor | 0.01 floor | cannot go lower |

These are **default-OFF** engine features (`StrategyConfig.risk_budget_gate`,
`max_single_entry_risk_pct`, `max_zone_risk_pct`, `daily_loss_limit_pct`,
`max_open_signals`), enforced by a single shared `DeploymentGate`
(`trading.engine.strategy.deployment_gate`) used identically by `run_backtest`
and the hybrid tick backtest. With every flag off the backtest is **byte-identical**
to before (parity-pinned by `tests/test_deployment_gate.py`).

### Gate semantics (precise)

- **Risk-budget gate** (per signal, pre-trade): `single = max` over the planned
  ladder legs of `|entry − effective_SL| × min_lot × contract`; `zone = sum`
  over legs. Reject if `single > equity × max_single_entry_risk_pct` or
  `zone > equity × max_zone_risk_pct`. Uses the **planned** ladder (independent
  of fills), so it is the true pre-trade budget. Reject-only (no dynamic entry
  reduction yet).
- **Daily-loss breaker**: tracks realized P&L per **feed-zone (source) day** (the
  same day key the report's Daily breakdown uses). Once the day's realized P&L
  reaches `−daily_loss_limit_pct × start-of-day equity`, **new** signals are
  rejected for the rest of that day. Already-open positions are **not**
  force-closed (the engine keeps managing them). Resets next day.
- **Max concurrent open signals**: a signal occupies the one slot from
  **placement** (arrival) until it is fully closed or its pending orders expire
  (`pending_expiry_minutes`). A multi-entry signal counts as **one** group. A new
  signal arriving while ≥ `max_open_signals` groups are open is rejected.

## Minimum account-size floor

For each observed per-min-lot-leg dollar stop risk `D` ( = stop distance in price,
since 0.01 lot × 100 = $1/pt), the account size needed to run a given posture at a
given per-trade budget is:

```
faithful 1% per min-lot leg     account floor = D / 0.01        = 100 · D
full 8-entry zone ≤ 4%          account floor = (8 · D) / 0.04  = 200 · D
safe  2-entry zone ≤ 6%         account floor = (2 · D) / 0.06  ≈ 33.3 · D
```

Example — if the p95 per-leg stop is **$50**: faithful-1% floor = **$5,000**;
full-8-entry-≤4% floor = **$10,000**; safe-2-entry-≤6% floor ≈ **$1,667**. So
$2K can carry the *2-entry* posture but is far under the *8-entry* one.

The validation report computes `D` at p50/p75/p90/p95/max from the **actual
backtest signals** (not assumed) and prints all three floors per percentile —
see `reports/SMALL_ACCOUNT_<window>/summary.md`.

## Measured result — June 2026 (TICK, `reports/SMALL_ACCOUNT_june/`)

The validation confirms the thesis decisively:

| variant | cap | maxDD | worst day | maxLoseStreak | peak concurrent | net (ret%) |
|---|---|---|---|---|---|---|
| base_8entry_50k | $50k | −16.3% | −30.1% | 17 | **17** | $189k (378%) |
| **base_8entry_2k** | $2k | **−37.3%** | **−98.0%** | 17 | **17** | $14.5k |
| **ts2k_e2_c1_d5_z6** | $2k | **−11.5%** | **−6.4%** | 6 | **1** | $396 (19.8%) |
| ts2k_e2_c1_d6_z6 | $2k | −11.5% | −6.0% | 6 | 1 | $404 (20.2%) |
| ts2k_e3_c1_d5_z6 | $2k | −14.6% | −8.2% | 6 | 1 | $508 (25.4%) |

- **Full 8-entry TSL18 at $2K is NOT deployable**: one day lost **98%** of the
  account (max DD −37%), because up to **17** signal groups stacked at once and a
  failed wide-stop zone has no min-lot escape.
- **TS2K makes it survivable**: worst day **−98% → −6.4%**, max DD **−37% → −11.5%**,
  max losing-signal streak **17 → 6**, peak concurrency **17 → 1**. It does this by
  rejecting **~1,526** of 1,744 signals on the concurrency cap (one-at-a-time) and
  **14** on the daily breaker — i.e. it trades far less, which is the point.
- **Cost**: net drops to ~+20% for the month (still positive in a strong month);
  upside is capped, exactly as designed. Entries 3 (`ts2k_e3`) earns a bit more
  (+25%) at a slightly deeper DD (−14.6%).
- **Account-size floor (p95 stop D=$19.8)**: full-8-entry ≤4% needs **~$4,000**
  (≈$8k at the max stop); the safe 2-entry ≤6% posture needs only **~$660** — so
  $2K comfortably carries 2 entries but is far under the 8-entry posture.

Jan–Jun confirmation run: `python tools/sweep_small_account_deploy.py --window jan_jun`.

## How to validate (always TICK)

```bash
python tools/sweep_small_account_deploy.py --window june
python tools/sweep_small_account_deploy.py --window jan_jun
```

Both windows end on **2026-07-01** internally (`--end-date` is **exclusive**, so
June 30 is kept). Each cell runs through `run_hybrid_backtest` against the
committed ELEV8 ticks. The report compares: `base_8entry_50k` (the reference
profile), `base_8entry_2k` (the danger), `ts2k_e2_c1_d5_z6` / `…_d6_z6` /
`ts2k_e3_c1_d5_z6`. Metrics include net / return% / max-DD% / worst-day% / daily
win rate / signal & entry win rate / payoff / profit factor /
max-consecutive-losing-signals / peak concurrent signals / peak open lots / the
exit mix (TP1/2/3, SL, time, trailing) / gate-rejection counts / the stop-distance
distribution / the account-size floor.

A per-signal profile of any single workbook is available via
`python tools/strategy_profile.py reports/TS2K_202606`.

## Growth expectation — honest

This is a ~40%-win-rate / ~1:2.3-payoff strategy (it loses small often, wins big
occasionally, is green on most days but throws off deep down-days). At $2K with
the 0.01-lot floor it runs *hotter* than the $50K backtest shows.

- **5–15% / month** is a realistic target for the gated wrapper. Some months will
  be negative.
- **10× ($2K → $20K)** in **2–4 years** is the realistic, lower-risk path
  (steady compounding inside the daily-loss guardrail).
- A **1-year 10×** requires ≈ **21% / month compounded** — achievable only at
  high risk-of-ruin. Do not size for it.
- High win rate **and** high payoff together (e.g. "70% at 1:2") do **not** exist
  for this (or any) strategy on gold — they are a tradeoff. TS2K keeps the
  existing low-WR/high-payoff edge and makes it survivable; it does not convert it
  into a high-WR grinder.

## Capital ladder — full vs limited entry (V817, measured on TICK)

`tools/sweep_small_account_deploy.py --feed victor` across capital, **TICK-only
(2026-05-11 .. 06-29), risk 5%**, full 8-entry vs gated 2-entry. Drawdown is shown
in **% and $** (the % is roughly capital-independent; the $ scales with the base):

| capital | mode | return | max DD % | max DD $ | PF | peak concurrent |
|---|---|---|---|---|---|---|
| $2K | full 8-entry | +205% | −30.9% | $1,338 | 1.61 | 4 |
| $2K | **limited e2** | +108% | **−18.2%** | **$572** | **2.08** | 1 |
| $5K | full 8-entry | +187% | −27.5% | $2,676 | 1.61 | 4 |
| $5K | limited e2 | +114% | −18.2% | $1,430 | 2.08 | 1 |
| $10K | full 8-entry | +233% | −30.8% | $6,690 | 1.63 | 4 |
| $10K | limited e2 | +95% | −20.9% | $3,004 | 1.94 | 1 |
| $20K | full 8-entry | +240% | −30.9% | $13,380 | 1.64 | 4 |
| $20K | limited e2 | +96% | −20.9% | $6,008 | 1.93 | 1 |

Read: full 8-entry rides a **permanent ~−30% drawdown at every capital** (it does
NOT shrink with more money — only the $ figure grows); the gated 2-entry holds
**~−18 to −21%** with a higher profit factor. At **$2K a single worst-case zone
(~$1,469) is ~73% of the account**, so full-entry is ruin-prone there.

**Decision: at $2K run LIMITED 2-entry (TS2K / VS2K). Switch to full 8-entry
around ~$10K**, where the worst zone is ~15% of the account and full ≈ doubles the
return for the same ~−30% DD on a comfortable base.

**This is MANUAL, not automatic.** Nothing auto-scales the entry count with
equity. What IS automatic: per-entry **lot sizing** (risk% × current equity) and
the **risk-budget gate** (equity-relative — it rejects a signal whose min-lot zone
risk exceeds your % of *current* equity). The gate is **reject-only**; it never
trims a ladder from 8→2 on its own. So the posture is a staged ladder you drive by
hand: launch with `--entries 2` from $2K, and when equity clears ~$10K, **stop the
executor, change `--entries` to 8, and restart**. (A future capital-tiered entry
resolver could automate this, but it is not built — change the parameter, re-run.)

**Do not pool both books on one small account.** A combined TSL18+V817 $2K run
made things *worse* (combined DD −41.6%, and TSL18's high signal rate starved
V817 via the shared concurrency cap, dragging it to a loss). Run each book on its
own account / slot; V817 alone is the stronger small-account vehicle.

## Live enforcement (wired) — backtest ↔ live ↔ tick-sim parity

The three gates are enforced in **all three** paths via the SAME
`DeploymentGate`: `run_backtest`, the hybrid **tick** backtest, and the **live**
executor (`auto`, through `DeploymentGate.live_check`). The live decision is
proven identical to the backtest/tick decision by
`tests/test_deployment_gate.py::test_live_check_matches_backtest_gate_decisions`
(same signal slice → identical accept/reject reasons), so a **TS2K tick backtest
predicts live placement**.

How the live executor sources the gate state each cycle (in
`trading/engine/cli_impl.py`'s `auto` candidate loop):

- **risk-budget** — the planned ladder from `rec.new_signal.orders`
  (`entry_price` / `initial_sl` per leg) → `worst_case_risk` vs live equity.
  Identical math to the backtest.
- **max-open-signals** — currently-open tracked signal groups
  (`len(tracked)`) plus any placed earlier this cycle.
- **daily-loss breaker** — today's realized P&L from MT5 deal history
  (`Mt5Executor.realized_pnl_since`, OUT deals since server-midnight),
  **account-level** (suits a dedicated small account); start-of-day equity ≈
  current equity − today's realized (floating ignored — a coarse circuit-breaker
  basis). Best-effort: if MT5 history is unavailable the breaker simply does not
  fire (conservative).

Two documented divergences (immaterial to a coarse safety gate): live uses the
**server day** for the breaker boundary vs the backtest's feed-zone (source) day;
and the live start-of-day-equity is an approximation. The gate **only ever
rejects** a placement — it can never send an extra order, so it cannot increase
exposure. Still **DEMO-validate** first (ELEV8 ticks ≠ your broker; the live
daily-P&L read depends on MT5 history).

## What must be true before promoting TS2K to a real $2K account

1. The report shows TS2K **materially lower** max drawdown and worst-day loss than
   `base_8entry_2k`, on **both** windows.
2. The account-size floor confirms the **2-entry ≤6%** posture fits $2K (it should)
   while the **8-entry** posture does not.
3. The gates are wired into the live executor (done) and a **demo A/B** confirms
   the live guarded feed matches the backtest.
4. You accept the capped upside and the 2–4-year (not 1-year) growth horizon.
