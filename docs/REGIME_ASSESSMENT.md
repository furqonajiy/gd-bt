# Regime determination — maximum assessment (research)

**Branch:** `research/regime-granularity-assessment` · **Reproduce:**
`python tools/regime_granularity_assessment.py` (full ELEV8 M1 archive,
2021‑11 → 2026‑06, 56 months).

**Question asked:** is the live 4‑regime scheme right, or should it be finer /
defined differently ("maybe more than R4/R3/R2")?

**Answer in one line:** the current scheme has the right *idea* (volatility tiers)
but the wrong *metric* — it keys on **absolute** M15 ATR, which is biased by a
2.6× price rise. Fixing the metric (price‑normalize) and splitting the overloaded
top band gives a cleaner, more granular, price‑stable determination — and shows
slippage should be a separate continuous axis, not bucketed by regime.

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

---

## Recommended determination

**Two decoupled axes** instead of one absolute‑ATR metric:

1. **Volatility tier — from price‑normalized M15 ATR (% of price), 4 tiers:**
   Normal (< 0.15%), Elevated (0.15–0.20%), High (0.20–0.26%), Extreme (≥ 0.26%).
   *(A 5th "Dead" < 0.10% exists but is behaviourally close to Normal — keep it as
   a sub‑label, not a separate champion.)* This is price‑stable: it won't
   re‑label the market just because gold rose.
2. **Direction split** (quiet vs bull) on the **Normal** tier via the trend score,
   exactly as today (R1/R2 differ by direction, not volatility).
3. **Slippage** as a **continuous** function of the live **absolute** ATR
   (`scaler = abs_ATR / R4_anchor`, R4 lock slip 2.0/1.0), independent of the tier
   label — so it is correct in every price era.

Populated regimes over the archive become: Normal‑quiet, Normal‑bull, Elevated,
High, Extreme — **more granular than today (the current single R4 splits into
High + Extreme) and price‑stable**, which is exactly the "maybe more than
R4/R3/R2" the question anticipated.

## Caveats / what this assessment is NOT

- 56 months; the **Extreme** tier is only 3 months (all 2026) and **Elevated**
  6 — thresholds there are indicative, re‑validate as data accrues.
- This re‑defines the **metric**; it does **not** re‑pick champions. Implementing
  it means re‑labelling history and **re‑running the per‑regime sweeps** under the
  new tiers (especially splitting the current R4 sweep into High vs Extreme) to
  see whether each tier wants a different champion. That is the next step, not
  done here.
- Live `strategy/regime.py` is **unchanged** by this assessment (it is the live
  contract; changing it is a separate, sign‑off‑gated step with a full re‑sweep).
