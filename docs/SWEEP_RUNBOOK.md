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
  champion config backtested once — **not** the result of exhaustive search.
  The current champion (`scalper24`, `e6 slm2.1 d24`) came from **hand‑seeded
  configs** in `_sweep_self.py::_seed_configs()`, *not* from the random grid.
- **Sweep = search for something that beats the baseline.** It samples many
  configs and ranks them.
- **We always SAMPLE, never exhaust.** The grid is millions of combinations;
  each backtest over multi‑year M1 is slow. Exhaustive is physically impossible
  (years of compute). Parallelism is linear (≤ #cores); it cannot beat
  exponential combinatorics.
- **Two metrics that matter (and one mirage):**
  - **EDGE** — fixed‑lot, sizing‑neutral profit. The primary *quality* signal.
  - **OOS** — profit on the held‑out last 6 months (`--validate-months 6`).
    The primary *generalization* signal. **Rank by OOS, cross‑check edge.**
  - **Compounded net $ (at risk%)** — a *mirage*. At 1% risk compounding over
    several years on a dense feed it explodes to *trillions*, and its drawdown
    usually blows past the DD gate. Use it only as a loose ranking aid, never
    as the deployable truth or the comparison basis.
- **DD gate.** Candidates must keep concurrent drawdown ≤ the limit
  (`--max-concurrent-dd-pct`). Current target: **40%**.

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
| Objective | **max profit** at DD ≤ 40% | rank by OOS, cross‑check edge |
| DD gate | **40%** | `--max-concurrent-dd-pct 40` |
| Trailing | **OFF** (pinned 0) | proven to add no deployable edge (16‑feed sweep) |
| Risk levels | **1/2/3/4/5%** | set in `tools/sweep.py::candidate_config` |
| Period | full real‑M1 span | candidates **and** baseline over the *same* period |
| OOS | last **6 months** | `--validate-months 6` |
| Feeds | scalper24, scalper_widerr24, risk02_allhours, better | "more signals is better" → 24h, loose filters |
| Candidates/feed | 300 (Phase 1) | **(ASK)** bump to 600 for denser coverage |
| Run model | in‑container (free) OR GitHub‑paid | **(ASK)** — see §6 |

---

## 3. Generate the self‑signal feeds ("more signals is better")

Use 24‑hour, all‑session generators with `--start <data‑start>`. Exact commands
(`scalper24` is the baseline's feed — always include it):

```bash
python tools/generate_scalper_signals.py --charts data/XAUUSD_M1_*_ELEV8.csv \
  --output generated/self_scalper24.txt --start <START> --session-start 0 --session-end 0 --signal-tz 7

python tools/generate_scalper_signals.py --charts data/XAUUSD_M1_*_ELEV8.csv \
  --output generated/self_scalper_widerr24.txt --start <START> --session-start 0 --session-end 0 --signal-tz 7 \
  --rr1 1.5 --rr2 2.5 --rr3 4.0

python tools/generate_aggressive_limit_risk02.py --charts data/XAUUSD_M1_*_ELEV8.csv \
  --output generated/self_risk02_allhours.txt --start-date <START> \
  --execution-hours "0,1,2,...,23"

python tools/generate_better_self_signals.py --m15-charts data/XAUUSD_M15_*_ELEV8.csv \
  --m1-charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_better.txt --start-date <START>
```

---

## 4. Configure the grid + seeds (THE critical step — most common mistake)

**The random grid must INCLUDE the champion's values, AND the champion must be
re‑seeded as a guaranteed candidate.** Otherwise the sweep cannot reach, let
alone beat, the baseline. The champion's `d24` (tp1‑lock‑delay = 24), `e6/e8`,
`slm2.1`, `max_hold 240` were **never** in the default random grid
(`{0,3,5,10,15}`, `{1,2,3,4}`, …) — they were seeds.

1. **Widen** `tools/sweep.py::candidate_config` so each value set brackets the
   champion: entries → up to **6/8**; tp1/tp2‑lock‑delay → include **2, 24**;
   max_hold → include **240**; sl_multiplier → include **2.1**; entry_sl_gap →
   include **0.5**; activation_delay → include **2**.
2. **Re‑seed** the champion + neighbors as guaranteed candidates (mirror
   `_sweep_self.py::_seed_configs`): base `e8 slm2.1 d24 max_hold240 TP3
   gap0.5 act2 tp2d2`, plus mutations `{e6}`, `{e4 slm1.61}`, `{slm2.2}`,
   `{d12}`. The current champion is the `{e6}` mutation.
3. Keep **trailing pinned 0** (no‑trailing sweep).

> The previous "best" only beat ~6 seeds + a few hundred random draws — it is
> **not proven optimal**. A widened + re‑seeded sweep can genuinely beat it.

---

## 5. Run the per‑feed sweep

```bash
python tools/sweep_self_limit.py \
  --signals generated/self_<feed>.txt \
  --charts data/XAUUSD_M1_*_ELEV8.csv \      # = the full real-M1 span
  --output-dir <out>/sweep_<feed> \
  --max-candidates 300 \                     # sample size per feed
  --max-concurrent-dd-pct 40 \               # DD gate
  --validate-months 6 \                      # last 6 mo held out as OOS
  --top-n 20 --progress-every 20 --resume
```
- `_sweep_self.py` = the wrapper that injects seeds (use it, or replicate the
  seed injection if calling `sweep_self_limit.py` directly).
- Baseline: run the champion config once via `tools/backtest_explicit.py` over
  the same charts/period → record edge / OOS / DD as the "beat this" target.

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

- Among **DD ≤ gate** gate‑passers, take the **highest OOS**; cross‑check edge.
- "**Beats baseline**" = higher **OOS** *and* **edge** than the baseline at
  DD ≤ gate. (Not raw compounded net.)
- Write the winner to `self_cli_best.txt` as the new champion CLI command.

---

## Gotchas / lessons learned (the short list)

1. **Verify M1 data first** — daily/hourly bars get mislabeled as M1.
2. **Baseline is a seeded config, not exhaustive** — the grid must include its
   values AND it must be re‑seeded, or the sweep can't reach it.
3. **Compounded net is a mirage** — compare on edge/OOS + DD‑gated deployable.
4. **In‑container pauses when idle** and costs Claude tokens to keep warm;
   GitHub‑paid is the only true 24/7. State this tradeoff every time.
5. **Single writer per branch** — local + CI both pushing = lost work.
6. **Match the comparison period** — recompute the baseline over the *same*
   period as the candidates (don't compare 2021–26 candidates to a 2025‑only
   baseline).
