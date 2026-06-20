# Regime determination — maximum assessment (research)

**Branch:** `research/regime-granularity-assessment` · **Reproduce:**
`python tools/regime_granularity_assessment.py` (full ELEV8 M1 archive,
2021‑11 → 2026‑06, 56 months).

**Question asked:** is the live 4‑regime scheme right, or should it be finer /
defined differently ("maybe more than R4/R3/R2")?

**Answer in one line:** the current scheme keys on **absolute** M15 ATR, which is
biased by a 2.6× price rise — the *labels* are inconsistent across price eras.
**BUT a validation backtest shows the deployed champion is volatility‑scale‑
invariant, so the mislabelling does NOT warrant tuning the regime/champion
mapping.** The metric flaw matters for exactly one thing — **slippage** (and the
report's regime column) — which is handled by a separate continuous abs‑ATR
scaler, not by re‑drawing regimes.

> **Recheck verdict (this update): do NOT tune the regime strategy.** The metric
> is imperfect but the strategy already self‑normalizes to volatility through its
> ATR‑derived geometry + risk sizing, so finer/normalized regimes would not change
> which champion wins. See Finding 5.

---

## How regime is determined today

`strategy/regime.py` → `detect_regime(smoothed_m15_atr, trend)`:

| condition | regime |
|---|---|
| M15 ATR ≥ $9.5 | R4parab |
| $4.0 ≤ ATR < $9.5 | R3strong |
| ATR < $4.0, trend ≥ +1.5% | R2bull |
| ATR < $4.0, else | R1quiet |

3 absolute volatility tiers; the low tier is direction‑split into R1/R2.

## Finding 1 — the absolute threshold is price‑biased

Gold ran **$1,800 → $5,000** across the archive. A fixed dollar threshold means a
different *amount of volatility* in each price era:

| threshold | % of price @ $1,800 (2021) | % of price @ $4,700 (2026) |
|---|---|---|
| $4.0 (R3 cutoff) | 0.22% | 0.09% |
| $9.5 (R4 cutoff) | 0.53% | 0.20% |

So "R4parab" in 2026 is *partly just high price*. Concretely, ranked by
price‑normalized volatility (M15 ATR / price):

- **2025‑04** (labelled **R3strong**, abs $7.73) was **0.239%** — *more* volatile
  in % terms than three months labelled **R4parab** (2026‑05 0.210%, 2026‑06
  0.233%, 2026‑04 0.234%).
- The label tracks the price era, not just the market.

## Finding 2 — on price‑normalized vol, the data supports ~4 tiers

1‑D k‑means on %‑ATR (variance explained):

| k | var. explained | centers (%‑ATR) |
|---|---|---|
| 2 | 73.8% | 0.120 / 0.248 |
| 3 | 87.9% | 0.118 / 0.210 / 0.326 |
| 4 | 92.3% | 0.094 / 0.126 / 0.210 / 0.326 |
| 5 | 94.3% | … |
| 6 | 97.1% | … |

Clean elbow at **k = 3–4** (88–92%); beyond that is over‑fitting 56 months.
Proposed bands (with the two calmest direction‑split):

| tier | %‑ATR band | n | median %‑ATR | median **abs** ATR | price span |
|---|---|---|---|---|---|
| V0 dead | < 0.10% | 7 | 0.091% | $1.69 | $1.8k–2.0k |
| V1 normal | 0.10–0.15% | 35 | 0.122% | $2.48 | $1.7k–3.7k |
| V2 elevated | 0.15–0.20% | 6 | 0.182% | $5.75 | $1.9k–4.3k |
| V3 high | 0.20–0.26% | 5 | 0.234% | $10.08 | $3.2k–4.7k |
| V4 extreme | ≥ 0.26% | 3 | 0.344% | $17.26 | $4.6k–5.0k |

## Finding 3 — today's R4 is two regimes wedged into one

Within‑regime absolute‑ATR spread (max/min — lower = more homogeneous):

| current | spread | | proposed | spread |
|---|---|---|---|---|
| R4parab | **1.82×** | → | V3 high | 1.43× |
| R3strong | 1.63× | | V4 extreme | 1.31× |

Splitting R4 (currently abs $9.6–$17.4) into **high** and **extreme** nearly halves
its internal spread. 2026‑02/03 (0.34%, abs ~$17) are a different market from
2026‑05/06 (0.21%, abs ~$10) — one champion + one slippage value for both is too
coarse. The cross‑tab confirms 4 of 7 "R4parab" months are really V3‑high (price‑
inflated), only 3 are genuinely extreme.

## Finding 4 — slippage is a *separate* axis (absolute ATR), not the regime label

Locked‑exit slippage scales with **absolute** ATR (dollars travelled through the
fill window — the live‑measured driver from the R4 reconciliation). Per‑tier
abs‑ATR scaler vs the R4 anchor, applied to the measured R4 lock slip 2.0/1.0:

| tier | median abs ATR | scaler | → TP1 / TP2 slip |
|---|---|---|---|
| V0 dead | $1.69 | 0.15× | 0.31 / 0.15 |
| V1 normal | $2.48 | 0.22× | 0.45 / 0.22 |
| V2 elevated | $5.75 | 0.52× | 1.04 / 0.52 |
| V3 high | $10.08 | 0.91× | 1.82 / 0.91 |
| V4 extreme | $17.26 | 1.56× | 3.12 / 1.56 |

Slippage spans ~**10×** across the archive. The flat 2.0/1.0 is only right near
mid‑R4; it is far too high for calm regimes (where the sweep over‑penalizes locked
exits) and slightly low for the extreme. **Within** a %‑normalized tier the
absolute ATR still varies by price era (e.g. V3 holds 2025‑04 abs $7.73 and
2026‑04 abs $11.05), so slippage should scale on the *live absolute ATR reading*,
**not** be bucketed by the regime label.

## Finding 5 — validation: does the relabelling change strategy behaviour? **NO**

The assessment above proves the *metric* is biased, but not that fixing it changes
*outcomes*. To test that, run the deployed R4 champion (SQZ6 = `rsi75_sqz6_rr40`)
separately on the two halves of 2026 — which today share one label (R4parab) but
are very different markets:

| 2026 half | abs ATR | %‑ATR | trend | proposed tier |
|---|---|---|---|---|
| Jan–Mar | $16.13 | 0.325% | **+7.8%** | V4 extreme |
| Apr–Jun | $10.23 | 0.223% | **−11.1%** | V3 high |

1.6× volatility apart and **opposite trend**. Champion backtest, fixed 0.01 lot
(clean per‑trade edge), slippage 2.0/1.0:

| half | signals | edge $ | **$/signal** | DD | win rate |
|---|---|---|---|---|---|
| V4 extreme (Jan–Mar) | 4,995 | $31,729 | **$6.4** | 7.3% | 55% |
| V3 high (Apr–Jun) | 4,768 | $30,624 | **$6.4** | 8.8% | 57% |

**The per‑trade edge is identical** ($6.4/sig) and win rate within 2 pts, across a
1.6× volatility gap and an inverted trend. The per‑*month* spread is much larger
and **uncorrelated with volatility** — the single most extreme month (2026‑02,
0.344%) had the *best* edge ($11.7/sig); the next most extreme (2026‑01) the
*worst* ($2.5/sig). So the variation is idiosyncratic (path/news), not a
volatility‑tier signal.

**Why:** the strategy is volatility‑scale‑invariant *by construction*. Entries,
SL and TP are all ATR‑derived, and risk sizing scales the lot to the stop
distance — so a higher‑vol month has proportionally bigger stops/targets/moves and
the *relative* (R‑multiple) edge is preserved. Splitting R4 into High/Extreme
would therefore not produce different champions; it would fit month‑level noise
(which is larger than the tier signal). The strategy is also two‑sided, so the
opposite trends didn't matter either. *(Reproduce:
`python tools/regime_split_validation.py`.)*

---

## Recommended determination

Given Finding 5 (the champion is scale‑invariant), the recommendation is **scoped
to where the metric flaw actually bites — NOT a re‑draw of the live regimes**:

1. **Do NOT split R4 / add champion tiers.** The validation shows it wouldn't
   change champion selection — V3‑high and V4‑extreme deliver the same per‑trade
   edge under SQZ6. Re‑sweeping per finer tier would fit noise. Keep the live
   `R1/R2/R3/R4 → champion` mapping as‑is.
2. **Fix slippage (the one thing that does NOT self‑normalize).** A locked‑exit
   give‑back is a fixed *dollar* cost, so it scales with **absolute** ATR while the
   strategy's edge does not. Make the sweep/backtest slippage a **continuous**
   function of the live absolute ATR (`scaler = abs_ATR / R4_anchor`, anchored on
   the measured R4 lock slip 2.0/1.0) instead of the flat global 2.0/1.0. This is
   backtest‑realism only (never a live order) and is the actionable item from the
   original R3‑vs‑R4 slippage thread.
3. **Optional — price‑normalize the report's regime *label*.** The Monthly
   Breakdown's regime column is biased by price era (2025‑04 'R3' is more volatile
   in %‑terms than three 2026 'R4' months). Switching the *display* metric to
   %‑ATR makes the labels price‑stable. Cosmetic — it does not affect any trade.

## Caveats / what this assessment is NOT

- 56 months; the **Extreme** tier is only 3 months (all 2026). The scale‑
  invariance (Finding 5) is the robust result; the exact %‑tier thresholds are
  indicative.
- Finding 5 tests the *deployed* champion's stability across tiers; it does not
  exhaustively prove no per‑tier champion could ever edge it out — but the
  identical per‑trade edge plus the noise dominating the tier signal make a
  per‑tier re‑sweep low‑value.
- Live `strategy/regime.py` and the champion mapping are **unchanged** — the
  verdict is that they should stay that way. Only the **slippage model** (backtest
  realism) is worth changing, and that is independent of the regime determination.
