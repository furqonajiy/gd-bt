# SWEEP RUNBOOK — Finding the Best Strategy Parameters

**Purpose.** When the user says *"redo the sweep"* (after a strategy change, an
engine bug fix, new chart data, or a new objective), follow this document
end‑to‑end. It captures the methodology, exact commands, the decisions we've
already made, and the gotchas that have bitten us — so we never re‑derive them
or ask the user to re‑explain.

> If the user just says "sweep" / "redo the sweep" with no other detail, assume
> the **defaults in §2** and confirm only the items marked **(ASK)**.

---

## 0. Core concepts (read first — these have caused real confusion)

- **Baseline = one config, run once.** The "beat this" number is a *single*
  config backtested once — **not** the result of exhaustive search. The sweep's
  **incumbent baseline (every regime)** is **SC24** (`e6 slm2.1 max_hold240 d24
  tp2d2`, no trailing, 1% risk), defined once in `tools/sweep.py::sc24_config()`
  and seeded as a guaranteed candidate via `sc24_neighborhood_grid()`, *not* found
  by the random grid. The **deployable champion for R2bull and R3strong** is
  **SC24 + `entry_count 8` ("SC24T24E8")** — promoted on the **reliable forward-fit
  metrics (OOS AND fixed-lot edge)**, on which it is #1 in both. **R4parab has since
  moved on**: SC24T24E8 breached the ≤ 40% DD gate (58.3%) on 2026 data, so R4parab
  now runs **`rsi75_sqz6_rr40`** — the edge+OOS leader of the 34-variant RSI ×
  Bollinger × R:R sweep (edge $63,940 / OOS $11,633 / DD 38.4%), which superseded
  an interim e5 RSI champion. They live in `champions/CHAMPION_{R2bull,R3strong,
  R4parab}.json` / `cli/champion_R4_SQZ6_no_trailing` (tag SQZ6). SC24 (e6 d24) stays the
  baseline so the per-regime "did anything beat it?" comparison is stable across
  R4→R3→R2→R1.
- **Sweep = search for something that beats the baseline.** It samples many
  configs and ranks them.
- **We always SAMPLE, never exhaust.** The grid is millions of combinations;
  each backtest over multi‑year M1 is slow. Exhaustive is physically impossible
  (years of compute). Parallelism is linear (≤ #cores); it cannot beat
  exponential combinatorics.
- **Two‑stage selection — the automated *rank* vs the human *promote* decision:**
  - **Stage 1 — automated keyfn ranks by compounded net P&L + the $3/closed‑lot
    bonus** (`risk_net_profit_with_bonus`), among configs that pass **DD ≤ 40%**
    **and** a positive held‑out **OOS** gate. This produces the leaderboard. The
    compounded figure **does** reach billions/quadrillions on a dense feed — that's
    expected; treat it as a **model upper bound that RANKS configs, not a money
    forecast.**
  - **Stage 2 — PROMOTE on the reliable forward‑fit metrics, NOT the compounded
    headline.** Compounded net+bonus is hypersensitive to leverage/variance and
    in‑sample sequencing: a tighter SL or extra leverage inflates it *without*
    improving the real per‑trade edge, and it can rank a genuinely worse config
    first (see the "inflated" worked examples — a 1.5‑pt win‑rate gap → a 2× headline;
    a higher‑edge config can show a *lower* compounded number). So the champion is
    only promoted if it leads on **both** of:
    - **EDGE** (`fixed_no_bonus_profit`) — fixed‑lot, no‑compounding, no‑bonus profit.
      The grounded, bankable‑proxy $; what the account makes per unit of fixed size.
    - **OOS** (`oos_fixed_no_bonus_profit`) — fixed‑lot edge on the held‑out tail
      (`--validate-months 2` in the regime grid). The best proxy for forward/live
      performance, because live is always out‑of‑sample.
  - When net+bonus and edge/OOS **disagree, edge+OOS wins.** Real example:
    **SC24T24E8 (entry 8) was promoted for R2bull/R3strong** (and originally R4parab)
    even though the net+bonus #1 was a different config (slm1.9 in R2, SC24T15E6 in
    R4) — those led only on the inflated compounded headline while *losing* on edge
    AND OOS. The universal lever across trending regimes is **more entries**
    (e8 > e7 > e6 on OOS). (R4parab has since switched to `rsi75_sqz6_rr40` on the
    same edge+OOS rule after SC24T24E8 breached the DD gate on 2026 data.)
- **DD gate.** Candidates must keep concurrent drawdown ≤ the limit
  (`--max-concurrent-dd-pct`). Current target: **40%**, and risk is swept up
  to push each champion against that 40% DD gate.
- **Sweep one regime at a time, in volatility order R4 → R3 → R2 → R1.**
  Gold's dollar volatility scaled ~7× over 2021–2026, so no single fixed
  config spans it (see `sweep2021/REGIME_ANALYSIS.md`). The grid runs each
  regime window independently and ranks DD‑≤‑40% champions per regime.
- **Incumbent competes head‑to‑head.** The user's live "current CLI" — the
  **scalper24 config** (`entry_count 6`, `sl_multiplier 2.1`,
  `tp1_lock_delay 24`) at live **1% risk** on the dense scalper24 feed — is
  re‑run under the same DD ≤ 40% gate. Verdict is **HOLD** (keep the current
  CLI) when the incumbent is compliant and unbeaten on compounded net +
  bonus, else **SWITCH** to the new champion.
- **The sweep models live execution (NON‑NEGOTIABLE).** Every candidate AND the
  incumbent are scored with **locked‑exit slippage** baked in
  (`tools/sweep.base_config_dict` carries `lock_tp1_exit_slippage_points = 2.0`,
  `lock_tp2_exit_slippage_points = 1.0`). Live can't fill a profit‑lock exactly at
  TP1/TP2 (broker stops/freeze level → the executor clamps + ratchets the stop),
  so it gives back ~2 pt (TP1) / ~1 pt (TP2) on the retrace, measured in the
  2026‑06‑16 reconciliation. **Without this overlay the sweep DECIDES on idealized
  fills and picks an over‑optimistic champion that leans too hard on locked
  exits.** The slip touches only `LOCK_*` exits — raw SL and TP1/TP2/TP3 targets
  are untouched — and is **backtest‑only** (DEFAULT_CONFIG / live / parity stay at
  0; the broker, not the engine, adds the slip live). If you re‑seed or widen the
  grid, the slippage rides along automatically because it lives in
  `base_config_dict` — don't strip it to "get a bigger number."

---

## 1. Verify the data (ALWAYS — this burned us twice)

Uploaded "M1" data has twice turned out to be **daily/hourly bars mislabeled as
M1**. Before anything, confirm every month is real 1‑minute data.

```bash
# real M1 ≈ 28–31k rows/month, consecutive rows 1 minute apart.
# D1 ≈ 20–23 rows/month; H1 ≈ 450–530 rows/month -> REJECT those months.
for p in $(git ls-tree -r --name-only origin/main -- data/ | grep -E 'XAUUSD_M1_.*ELEV8\.csv' | sort); do
  rows=$(git show "origin/main:$p" | tail -n +2 | wc -l)
  [ "$rows" -lt 3000 ] && echo "BAD (D1/H1): $p rows=$rows"
done
```
Note the **valid date range** (the real‑M1 span). "From 2021" in practice meant
**2021‑11**, because 2021‑01…10 were hourly. Don't sweep mislabeled months —
the engine treats each row as one minute, so a daily bar corrupts the backtest.

---

## 2. Defaults / decisions already made (don't re‑ask unless changed)

| Item | Default | Notes |
|---|---|---|
| Objective | **max compounded net P&L + $3/closed‑lot bonus** at DD ≤ 40% | OOS > 0 sanity gate; edge as cross‑check |
| DD gate | **40%** | `--max-concurrent-dd-pct 40`; push risk up to this gate |
| Regime order | **R4 → R3 → R2 → R1** (volatility order) | sweep one regime at a time |
| Trailing | **OFF** (pinned 0) | proven to add no deployable edge (16‑feed sweep) |
| Risk levels | **1–5%** | swept and pushed to the 40% DD gate |
| Period | per‑regime window (see `sweep2021/REGIME_ANALYSIS.md`) | candidates **and** incumbent over the *same* window |
| OOS | held‑out tail | `--validate-months 6` for the long full‑history sweep; **`2` for the regime grid** (short regime windows — e.g. R4 is only 2026, so the OOS tail is its last 2 months). Used as the OOS > 0 gate **and** the promote‑decision metric. |
| Feeds | scalper24, scalperwide24, risk02allhours (3 HIGH‑FREQ self‑scalpers) + adaptive / breakout / meanrev | high‑frequency, 24h, loose filters → more closed lots |
| Candidates/feed | 300 (Phase 1) | **(ASK)** bump to 600 for denser coverage |
| Run model | in‑container (free) OR GitHub‑paid | **(ASK)** — see §6 |

---

## 3. Generate the self‑signal feeds ("more signals is better")

Use 24‑hour, all‑session generators with `--start <data‑start>`. The basket now
leads with **three HIGH‑FREQUENCY self‑scalper feeds** — `scalper24`,
`scalperwide24`, `risk02allhours` — alongside the adaptive / breakout / meanrev
feeds; high frequency is the point now (more trades → more closed lots → more
bonus + faster compounding inside the DD cap). Exact commands (`scalper24` is
the incumbent's feed — always include it):

```bash
python tools/generate_scalper_signals.py --charts data/XAUUSD_M1_*_ELEV8.csv \
  --output signals/self_scalper24.txt --start <START> --session-start 0 --session-end 0 --signal-tz 7

python tools/generate_scalper_signals.py --charts data/XAUUSD_M1_*_ELEV8.csv \
  --output signals/self_scalper_widerr24.txt --start <START> --session-start 0 --session-end 0 --signal-tz 7 \
  --rr1 1.5 --rr2 2.5 --rr3 4.0

python tools/generate_aggressive_limit_risk02.py --charts data/XAUUSD_M1_*_ELEV8.csv \
  --output signals/self_risk02_allhours.txt --start-date <START> \
  --execution-hours "0,1,2,...,23"

python tools/generate_better_self_signals.py --m15-charts data/XAUUSD_M15_*_ELEV8.csv \
  --m1-charts data/XAUUSD_M1_*_ELEV8.csv --output signals/self_better.txt --start-date <START>
```

---

## 4. Configure the grid + seeds (THE critical step — most common mistake)

**The random grid must INCLUDE the champion's values, AND the champion must be
re‑seeded as a guaranteed candidate.** Otherwise the sweep cannot reach, let
alone beat, the baseline. The champion's `d24` (tp1‑lock‑delay = 24), `e6/e8`,
`slm2.1`, `max_hold 240` were **never** in the default random grid
(`{0,3,5,10,15}`, `{1,2,3,4}`, …) — they were seeds.

1. **Widen** `tools/sweep.py::candidate_config` so each value set brackets the
   champion (done): entries → up to **8**; tp1‑lock‑delay → include **20, 24,
   30**; tp2‑lock‑delay → include **2**; max_hold → include **240, 300**;
   sl_multiplier → include **2.1**; entry_sl_gap → include **0.5**;
   activation_delay → include **2**; risk → include **0.01** (down to live 1%).
2. **Re‑seed** the baseline + neighbors as guaranteed candidates. The incumbent
   baseline **SC24** is defined once in `tools/sweep.py::sc24_config()`
   (DEFAULT + `e6 slm2.1 max_hold240 d24 tp2d2 gap0.5 act2 no‑trailing 1% risk`),
   and `sc24_neighborhood_grid()` is the staged coordinate sweep around it (one
   axis at a time: tp1‑lock‑delay `{15,20,24,27,30}`, sl_mult `{1.9..2.3}`,
   max_hold `{120,180,240,300}`, entries `{5,6,7,8}`, …). `sweep_self_limit.make_limit_candidates`
   seeds that grid on shard 0 so SC24 + neighbors are always evaluated. The SAME
   `sc24_config()` is the sweep's **incumbent** (`incumbent_baseline.incumbent_config`),
   so "did a challenger beat the baseline?" is exactly apples‑to‑apples. (The
   `entry_count 8` neighbor — "SC24T24E8" — is what won R2/R3 (and initially R4) on
   OOS+edge and got promoted; the `tp1_lock_delay 15` neighbor "SC24T15E6" led R4
   only on the compounded net+bonus mirage and was superseded. R4parab later moved
   to `rsi75_sqz6_rr40` once SC24T24E8 breached the DD gate on 2026 data.)
3. Keep **trailing pinned 0** (no‑trailing sweep).

> The previous "best" only beat ~6 seeds + a few hundred random draws — it is
> **not proven optimal**. A widened + re‑seeded sweep can genuinely beat it.

---

## 5. Run the per‑feed sweep

```bash
python tools/sweep_self_limit.py \
  --signals signals/self_<feed>.txt \
  --charts data/XAUUSD_M1_*_ELEV8.csv \      # = the full real-M1 span
  --output-dir <out>/sweep_<feed> \
  --max-candidates 300 \                     # sample size per feed
  --max-concurrent-dd-pct 40 \               # DD gate
  --validate-months 6 \                      # last 6 mo held out as OOS
  --top-n 20 --progress-every 20 --resume
```
- `_sweep_self.py` = the wrapper that injects seeds (use it, or replicate the
  seed injection if calling `sweep_self_limit.py` directly).

### Feed‑filter combination sweeps (R4)

The scalper24 generator (`tools/generate_scalper_signals.py`) can pre‑filter the
feed on five orthogonal dimensions; the workflows cross them and keep a variant
only if it beats the unfiltered **`base`** feed on **BOTH edge AND OOS at DD ≤ 40%**
(shared aggregator `.github/scripts/agg_entry_feature.sh`):

- **R:R** — `--rr1/--rr2/--rr3` (TP geometry).
- **Bollinger** — `--bb-bandwidth-min` (squeeze) / `--bb-buy-pctb-max` /
  `--bb-sell-pctb-min` (%B overextension).
- **RSI** — `--rsi-buy-max` / `--rsi-sell-min`.
- **Support/Resistance** — `--sr-proximity-atr` (+ `--sr-round-step`): enter only
  within X·ATR of prior‑day H/L or a round‑number level.
- **Supply/Demand** — `--sd-mode rbr_dbd`: Rally‑Base‑Rally / Drop‑Base‑Drop. A
  tight `--sd-base-bars` consolidation (range ≤ `--sd-base-max-atr`·ATR) broken by
  an impulse ≥ `--sd-impulse-min-atr`·ATR (confirmed `--sd-impulse-bars` later, so
  **no lookahead**) marks a demand (up) / supply (down) zone; BUY only on a return
  within `--sd-proximity-atr`·ATR of a demand band, SELL into a supply band. Zones
  expire after `--sd-max-age-bars`.

`self-scalper-rsi-bb-rr-sweep.yml` crossed the first three (→ champion
`rsi75_sqz6_rr40`). `self-scalper-rr-bb-rsi-sr-sd-sweep.yml` is the **full 2⁵
cross‑product** of all five (R4 only, prefix `selfsdr`) — its `rsi_sqz6_rr40` cell
reproduces the current champion, and the `…_sr` / `…_sd` cells test whether adding
S/R or S&D beats it.

**Per‑regime realistic slippage.** Locked‑exit slippage scales with *absolute*
ATR (it does not self‑normalize the way the edge does — see
`docs/REGIME_ASSESSMENT.md`), so scoring every regime at the flat R4‑measured
2.0/1.0 over‑penalizes locked exits 2–5× in calmer regimes.
`tools/sweep_self_limit.py` takes `--lock-tp1-slippage` / `--lock-tp2-slippage`
(default −1 = keep the baked‑in 2.0/1.0) to score each regime at its
volatility‑scaled value: **R3 0.9/0.45, R2 0.5/0.25, R1 0.4/0.2** (median abs ATR /
the R4 anchor × 2.0/1.0). `self-scalper-5way-sweep-r3r2r1.yml` runs the full
**144‑variant** 5‑way cross (RSI{off,70,75} × BB{off,%B80,sqz6} × R:R{base,rr08,
rr25,rr40} × S/R × S&D) for R3→R2→R1 chained (prefix `self5r`), each at its
realistic slippage. Backtest‑realism only — never a live order.
- Incumbent: run the live "current CLI" (scalper24 `e6 slm2.1 d24`, 1% risk)
  once via `tools/backtest_explicit.py` over the same charts/regime window →
  record compounded net + bonus, OOS, and DD as the "beat this" target.
- The unattended per‑regime grid runs from the
  `.github/workflows/regime-grid-sweep.yml` workflow (renamed from
  `self-regime-grid.yml`): it sweeps one regime at a time (R4 → R3 → R2 → R1),
  risk 1–5%, and ranks DD‑≤‑40% champions on compounded net + bonus.

---

## 6. Execution model — how to run it unattended **(ASK the user)**

| | In‑container (Claude) | GitHub Actions (paid) |
|---|---|---|
| Cost | $0 GitHub; **costs Claude tokens** to keep session warm | ~$0.008/min (~$25–75/sweep) |
| Uptime | **only while session active**; pauses when idle | true 24/7, independent of chat |
| Babysitting | user pings ~every 10–15 min to keep warm | none |
| Speed | slow (days–weeks, idle pauses) | fast (hours) |

- **Public repo = free unlimited Actions minutes BUT exposes the whole strategy
  — do NOT make the repo public.**
- **Resilience (both models):** commit checkpoints every 15 min + `--resume` +
  per‑feed `DONE` markers. In‑container also needs a **SessionStart auto‑resume
  hook** (`~/.claude/settings.json` → a `resume.sh` that checks out the branch
  and relaunches the orchestrator). Put the sweep on its own **`research/…`
  branch**; never the completed branches or `main`.
- **Single writer only.** Never run two orchestrators (local + CI) on the same
  branch — concurrent pushes diverge and silently lose work (non‑fast‑forward).

---

## 7. Search strategy — be smart, don't brute‑force

- **Phase 1 — broad map:** ~300 random configs/feed over the widened grid +
  seeds. Finds promising regions and the first "does anything beat baseline?"
  read.
- **Phase 2 — focused refinement (the real win):** take the top configs + the
  baseline and **hill‑climb** (perturb each parameter to neighboring grid
  values, keep improvements, repeat). ~10–100× more sample‑efficient than
  random; this is where you actually beat the baseline.
- **Parallelism:** the container has 4 cores; the sweeper uses 1. Run feeds
  concurrently (~4× throughput). Each worker loads the charts (~3–4 GB) → cap at
  3–4 parallel on 16 GB.

---

## 8. Monitor (token‑frugal)

```bash
# one-line progress (use on each user ping)
up=$(pgrep -f '[o]rchestrate.py' >/dev/null && echo up || echo DOWN)
n=$(wc -l < <out>/sweep_<feed>/results.jsonl 2>/dev/null || echo 0)
echo "$(date -u +%H:%M)Z $up <feed>=$n/300"
```
- Reply minimally to routine pings; break brevity only for a **milestone**
  (feed done + best config, first DD‑≤40% winners, crash/stall).
- **Outputs:** `<out>/BEST_*.txt` (live leaderboard: top by OOS + by edge),
  and `self_cli_best.txt` (the current best config rendered as a runnable
  `backtest_explicit.py` command — this is the deliverable).

---

## 9. Pick the winner & call it

- Among **DD ≤ gate** gate‑passers with **OOS > 0**, take the **highest
  compounded net P&L + $3/closed‑lot bonus**; cross‑check edge.
- "**Beats incumbent**" = higher compounded‑net‑plus‑bonus than the
  incumbent (scalper24 `e6 slm2.1 d24` at 1% risk) at DD ≤ gate with OOS > 0.
  The DD ≤ 40% cap + OOS > 0 gate are what keep this honest — they reject the
  over‑levered "trillions" configs before they can rank.
- **Verdict:** **HOLD** the current CLI when the incumbent is compliant and
  unbeaten; **SWITCH** otherwise, writing the winner to `self_cli_best.txt`
  (or `self_cli_best_<regime>.txt`) as the new champion CLI command.

---

## Gotchas / lessons learned (the short list)

1. **Verify M1 data first** — daily/hourly bars get mislabeled as M1.
2. **Baseline is a seeded config, not exhaustive** — the grid must include its
   values AND it must be re‑seeded, or the sweep can't reach it.
3. **Rank on compounded net + $3/closed‑lot bonus** to build the leaderboard
   (`risk_net_profit_with_bonus`), but **PROMOTE the champion on fixed‑lot edge +
   OOS** (the reliable forward‑fit metrics) when they disagree with the compounded
   headline — compounded net+bonus is inflated by leverage/variance and ranks but
   does not decide (see "Two‑stage selection" above; SC24T24E8 was promoted across
   R2/R3 — and initially R4 — over the net+bonus #1 on exactly this rule; R4parab
   now runs `rsi75_sqz6_rr40` on the same rule).
   The compounded figure **does** reach billions/quadrillions on a dense feed
   (1% of a growing balance over thousands of signals) — that is expected and it
   is a **model upper bound that RANKS configs, not a money forecast**. The
   **DD ≤ 40% cap + OOS > 0 gate** keep the ranking honest: DD bounds the risk it
   can chase and OOS rejects in‑sample‑only blow‑ups; edge/OOS are the deciding
   metrics, and risk is swept 1–5% (down to 1%) up to the DD gate. A **DD 40–50%
   "stretch" tier** is also published when a config beats the DD≤40% champion's net+bonus
   by ≥25% (`champions_report.stretch_challenger`). Sweep one regime at a time,
   R4 → R3 → R2 → R1.
4. **In‑container pauses when idle** and costs Claude tokens to keep warm;
   GitHub‑paid is the only true 24/7. State this tradeoff every time.
5. **Single writer per branch** — local + CI both pushing = lost work.
6. **Match the comparison period** — recompute the baseline over the *same*
   period as the candidates (don't compare 2021–26 candidates to a 2025‑only
   baseline).
