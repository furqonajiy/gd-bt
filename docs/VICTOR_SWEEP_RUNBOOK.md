# Victor sweep runbook

**Goal:** find the most-profitable strategy *parameters* for trading Victor's
Telegram feed, **decided on real (slippage-aware) fills**, **per regime**, and
**reproducibly** — so next time you run ONE workflow instead of rebuilding this.

Victor posts discrete signals with his own entry/SL/TP. We do **not** sweep his
R:R directly; we sweep **our** strategy parameters over his feed, and (gated)
optionally **filter / rewrite / ATR-source** his SL+TP. This is the sibling of
`docs/SWEEP_RUNBOOK.md` (the scalper grid); read that for the shared concepts
(edge/OOS/compounded, DD gate, champion/challenger).

---

## 0. Why it's built the way it is (don't relitigate)

- **Per regime, always.** Victor **rewrote his generator in 2026**. Measured on
  `victor_signals.txt`:

  | year | sig/day | TP1 R:R (median) | TP3 R:R | % signals RR1<1 |
  |---|---:|---:|---:|---:|
  | 2024 | 3.6 | 0.60 | 2.60 | 97% |
  | 2025 (R3) | 9.1 | **0.50** | 2.50 | **100%** |
  | 2026 (R4) | 10.6 | **1.00** | **4.38** | 26% |

  2025 and 2026 are *different strategies*. Sweep **R3 (2025)** and **R4 (2026 =
  his current style)** separately; R4 is the deployment-relevant one. (Regenerate
  this table with `tools` + `parse_signals_file` if the feed grows — see §6.)

- **Slippage-aware, always.** Every candidate carries the locked-exit slippage
  overlay (TP1 2.0 / TP2 1.0 via `sweep.base_config_dict`). Deciding on the
  idealized exact-level fill picks an over-optimistic champion (see the SC24
  finding: ~34% of R4 edge was a perfect-lock mirage). DEFAULT_CONFIG / live /
  parity stay at 0 — the broker, not the engine, adds the slip.

- **Decide on edge + the $3/lot bonus, guarded by OOS** — NOT compounded net.
  Objective = `fixed_with_bonus_profit` (fixed-lot edge + $3/closed-lot bonus, so
  **more closed signals scores higher** — Victor is high-frequency), with hard
  gates **DD ≤ 40%** (concurrent risk) **and OOS > 0** (held-out last 2 months of
  that regime). Compounded `risk_net_profit_with_bonus` is reported, never
  decides (hypersensitive; it crashes under slippage and over-reacts to
  sequencing). The aggregate also surfaces the best config under EACH objective
  (net+bonus / edge+bonus / edge / OOS) so a "net profit's not bad → execute"
  call is one glance.

- **"Every combination" is impossible.** The discrete grid is **~1.4×10¹⁷**
  combos/regime; exhaustive ≈ 4 billion years. We **random-search ~16k/regime**
  over a wide grid + the seeded SC24 neighborhood (guarantees the known-good
  region is scored). That is the method; more samples refine, they don't unlock a
  hidden point.

---

## 1. Run it (one command)

```bash
# GitHub Actions -> "victor-sweep" -> Run workflow (or via API):
#   gh workflow run victor-sweep.yml -f max_candidates=250
```

What it does (`.github/workflows/victor-sweep.yml`):

- Matrix **regime {R3strong, R4parab} × batch {0..7}**, each job runs **4
  `tools/sweep_self_limit.py` shards** on the runner's 4 cores → **64 shards**,
  `--signal-policy` on, `--validate-months 2`, `--max-concurrent-dd-pct 40`,
  fixed-lot 0.01. `max_candidates` per shard (default 120) × 64 ≈ that many ×8
  evaluations/regime. Resumable (`--resume`), partial-safe.
- Slices the feed per regime first (`tools/slice_signals.py`) so OOS = the last 2
  months **of that regime** (split_train_validate slices whatever feed it gets).
- One **aggregate** job merges all shards per regime and writes the results.

Charts/windows are wired in the workflow: R3strong = `data/XAUUSD_M1_2025*`,
R4parab = `data/XAUUSD_M1_2026*`. Add a regime by extending the matrix + the
`case` in the "Resolve regime window" step.

---

## 2. Read the result

Download the **`victor-sweep-results`** artifact from the run. Per regime:

- **`WINNER_<regime>.md`** — top of file is the **best-config-per-objective**
  table (max net+bonus / edge+bonus / edge / OOS, all gated DD≤40% & OOS>0),
  then the full edge+bonus leaderboard. The config brief shows the chosen policy:
  `[Victor TP/SL as-is]`, or `[ATRsl1.5p14 minRR1.0(nom) rwRR1.5/2.5/3.5]`.
- **`BEST_<regime>.json`** — the edge+bonus winner (deployable config dict).
- **`BEST_<regime>_<objective>.json`** — the winner under each objective.
- **`leaderboard.xlsx` / `.csv`** — every scored candidate.

**Decide:** take the edge+bonus winner unless another objective's config is
clearly better for your risk appetite (e.g. far higher net+bonus at the same DD).
OOS>0 and DD≤40% are non-negotiable.

---

## 3. Deploy a winner

The Victor executor is `cli_champion_victor.txt` (tag **VIC**, feed
`generated/victor_live.txt`, positions `positions_victor.json`). Plug the winning
config's flags into its BACKTEST + AUTO commands (entries, sl_multiplier, lock
delays, max-hold, final-target, …). If the winner uses the signal policy
(`signal_min_rr`, `rewrite_tp*_rr`, `sl_source=atr`, …), note these are
**backtest/sweep dimensions** — to trade them live the executor must apply the
same policy (it reads the same StrategyConfig fields; confirm `auto_explicit`
forwards them before relying on a policy winner live). A pure-param winner
(`[Victor TP/SL as-is]`) deploys with no policy flags.

Pick `--risk` to your cap (Victor's incumbent runs 5%, ~57% concurrent DD = over
the 40% gate; 1% is compliant but smaller — your sizing choice, separate from the
strategy the sweep picks).

---

## 4. The grid (what's swept vs fixed)

**Swept** (random, per shard): risk, entry_count, entry_ladder, entry_sl_gap,
activation/pending, max_hold (15–900), **sl_multiplier 0.6–3.0**, final_target,
lock_after_tp1/2 + delays, profit_lock_mode, bep_trigger, tp1_lock_fraction,
tp2_lock_target, trailing_close, shared_sl. **Signal policy** (gated): sl_source
(posted vs ATR; period 5–28, mult 0.5–3.5), R:R filter (min_rr 0.3–2.5), TP
rewrite (14 rr sets), nominal/effective reference. Exact value lists live in
`tools/sweep.candidate_config` (the `signal_policy` block).

**Held FIXED / excluded** (intentional — extend `candidate_config` if you want
them swept): `$3/lot` bonus, the slippage overlay, **trailing-open = 0** (LIMIT
only), **trend-runner off**, scale-out at TP1/TP2, BEP-after-TP1,
runner-after-TP3, per-entry targets.

---

## 5. Pieces (so you can change one without re-reading everything)

| File | Role |
|---|---|
| `.github/workflows/victor-sweep.yml` | the parallel runner + aggregate |
| `tools/slice_signals.py` | slice the feed to a regime date range (correct OOS) |
| `tools/sweep_self_limit.py` | the shard driver (`--signal-policy` adds the dims); computes edge/OOS/compounded |
| `tools/sweep.py::candidate_config(signal_policy=True)` | the grid (gated so the scalper sweep is unchanged) |
| `tools/victor_sweep_aggregate.py` | merge shards, gate, rank on edge+bonus, per-objective table |
| `strategy/backtest.apply_signal_rr_policy` | applies filter/rewrite/ATR per signal (all default OFF → parity) |

---

## 6. Re-run checklist

1. **Feed fresh?** `victor_signals.txt` is the Telegram export/listener output.
   If Victor's behaviour shifts again, re-check §0's table and add a regime if a
   new style appears.
2. **Data fresh?** R3 needs `data/XAUUSD_M1_2025*`, R4 needs `2026*` (real M1 —
   verify per `docs/SWEEP_RUNBOOK.md` §1).
3. `gh workflow run victor-sweep.yml -f max_candidates=<N>`; widen
   `candidate_config` first if you want denser/broader coverage.
4. Read §2, deploy §3. Done — no rebuild.
