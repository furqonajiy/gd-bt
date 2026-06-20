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

The Victor executor is `cli/champion_victor.txt` (tag **VIC**, feed
`signals/victor_live.txt`, positions `positions_victor.json`). Plug the winning
config's flags into its BACKTEST + AUTO commands (entries, sl_multiplier, lock
delays, max-hold, final-target, …). If the winner uses the signal policy
(`signal_min_rr`, `rewrite_tp*_rr`, `sl_source=atr`, …), note these are
**backtest/sweep dimensions** — to trade them live the executor must apply the
same policy (it reads the same StrategyConfig fields; confirm `auto_explicit`
forwards them before relying on a policy winner live). A pure-param winner
(`[Victor TP/SL as-is]`) deploys with no policy flags.

Pick `--risk` to your cap (Victor's deployed champion runs **2.5%** — the DD≤40%
compliant max on 2026: ~36.8% equity-curve DD, $50k → ~$417k. 5% was ~57% and over
the gate; 3%+ breaches it; 1% is compliant but smaller — your sizing choice,
separate from the strategy the sweep picks).

---

## 3b. ENTRY-FEATURE sweep (RSI / Bollinger / ADX / VWAP / HTF / S-R)

Victor signals are *received*, not generated, so the scalper generator's
entry-feature filters can't be applied at generation time. Instead
`tools/filter_provider_signals_by_indicator.py` computes the **same** indicators
(reusing `generate_scalper_signals._add_indicators` / `_entry_filters_ok`) on the
chart and keeps a Victor signal only if its bar passes the filter — output is the
provider feed format (drop-in for backtest/sweep/live). Run
`.github/workflows/victor-entry-feature-sweep.yml` (dispatch): for each variant
(base/rsi/adx12/18/25/pctb/bbsqueeze/vwap/htf15/60/sr/adx12rsi/adx12pctb/combo) it
filters `victor_signals.txt` for the 2026/R4 window, sweeps the full strategy
grid over the filtered feed (geometry-only, no `--signal-policy` — isolates the
entry-feature effect against the deployed "[Victor TP/SL as-is]" champion), and
the aggregate keeps a variant **only if it beats the unfiltered `base` feed on
BOTH edge AND OOS at DD≤40%** (same bar as the R4 scalper entry-feature sweep).
A follow-up full `victor-sweep --signal-policy` can then refine the winning feed.

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
| `tools/reconcile_report_html.py` | reconcile an MT5 ReportHistory **HTML** vs a backtest workbook (the slippage-calibration measurement; §8) |
| `tools/dump_mt5_spec.py` | capture the live broker spec (stops level, swap, real spread) for §7 realism |

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

---

## 7. Backtest realism vs Elev8 live (verified 2026-06-16)

Captured with `tools/dump_mt5_spec.py` against the live XAUUSD symbol. The backtest
matches live on every input that matters — **don't re-litigate this**:

| Live spec (Elev8) | Value | Backtest status |
|---|---|---|
| **Stops level** (min SL distance) | **0.40** price units (40 pts) | tiny — our tightest configs (sl_mult 0.6 → ~1–2 price-unit stops) sit well above it, so **no tight-stop config is unplaceable**. Floor is effectively non-binding (base stop = `(entry−SL)×sl_mult` is never that small). |
| **Spread** | median **0.28** (p99 0.34) | **already modeled** — the M1 CSV carries the broker's own per-bar `<SPREAD>` (median 0.25), used as `spread_price`. ≈ live to ~3 cents. ✅ |
| **Commission** | 0.00 in deal history | commission-free / in-spread → nothing to model. ✅ |
| Locked-exit slippage | ~2.0 / ~1.0 (TP1/TP2) | modeled via `lock_tp1/tp2_exit_slippage_points` (from the live reconciliation). ✅ |
| **Swap** long/short | −5.83 / −2.65 per lot/night (~−$0.06 per 0.01 lot) | **NOT modeled.** Second-order; only bites configs whose `max_hold` crosses the 22:00 rollover. Flag long-hold winners; model only if a winner depends on overnight holds. |
| digits 2 · point 0.01 · contract 100 · tick 0.01=$1/lot · lev 1:1000 · stop-out 15% | — | confirms P&L math; leverage non-binding at 0.01–0.02 lots. |

**Verdict:** entries, TP3, SL, spread, and commission match live; locked-exit
slippage is modeled. The only unmodeled cost is **swap on overnight holds**
(~−$0.06/0.01 lot/night) — negligible except for long-`max_hold` configs. The
sweep's numbers are trustworthy on the execution-realism axis.

---

## 8. The slippage calibration loop (iterate, don't re-derive)

Slippage (`SWEEP_LOCK_TP1/TP2_SLIPPAGE` in `tools/sweep.base_config_dict`, today
**2.0 / 1.0**) is the one input measured from live, and so far only from the
clean **2026-06-16** day. Refine it by *trading the winner and remeasuring* -- a
loop, not a one-off:

1. **Sweep** with the current estimate -> pick the best config per regime (DD<=40%,
   OOS>0, edge+bonus; see section 2). While we still have only ~1 clean day,
   treat the estimate as uncertain and prefer winners **robust to slippage**
   (re-score the top configs at 1.0 / 2.0 / 3.0 and keep the ones that win across
   the range; drop configs that only win at low slippage as fragile).
2. **Deploy stably** -- clean config, #130 churn cooldown live, no manual closes.
   Instability (churn / mid-day config swaps / hand-closes) makes the history
   unreconcilable, so a *stable* window is the prerequisite for clean data.
3. **After ~2-4 weeks**, export a clean multi-week `ReportHistory` (MT5 History
   tab -> Report -> HTML) -> reconcile with
   ```
   python tools/reconcile_report_html.py --report <history.html> \
       --backtest <reports/BEST_VIC_*.xlsx> --tag VIC
   ```
   It matches each live leg to its backtest entry by the `[TAG-]MMDD#DD.N`
   comment and prints per-exit-type live-vs-backtest P&L + **avg LOCK_* slip in
   points** (= the give-back to calibrate; SL/TP3 should be ~0), plus churn /
   manual-close flags. Use a STABLE window -- on the churny 2026-06-15 day the
   tool shows LOCK slip ~3-4 pt (reopen noise), vs the clean 2026-06-16 ~2.0/1.0.
4. **Update** `SWEEP_LOCK_TP1/TP2_SLIPPAGE` (+ the champion CLI snapshots' explicit
   `--lock-tp1/tp2-exit-slippage`) to the remeasured values.
5. **Re-sweep** with the calibrated slippage and redeploy. Repeat -- each pass the
   slippage (and the chosen champion) gets more trustworthy.
