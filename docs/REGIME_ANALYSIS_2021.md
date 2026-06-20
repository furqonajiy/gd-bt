# XAUUSD Regime Analysis & Phase-2 Adaptive Sweep Plan

_2026-06-14 — derived from data/XAUUSD_M1_*_ELEV8.csv (2021-11 → 2026-06)._

## Why one strategy can't span 2021-2026
Gold's **dollar volatility scaled ~7×**. A fixed-dollar SL/TP that fits the quiet
years is noise-width in the parabola (instant stop-outs); one that fits the
parabola never triggers in the quiet years.

| Regime | Period | Price | Daily range | Per-minute range | Realized vol | Character |
|---|---|---|---|---|---|---|
| **R1 Quiet range** | 2021-11 → 2023-09 | ~$1,650–2,050 | $21–26 (1.0–1.6%) | **$0.44–0.56/min** | 11–14% | Rangebound, choppy |
| **R2 Bull ignites** | 2023-10 → 2024-12 | $1,900 → $2,650 | $25–40 | $0.50–0.80/min | 12–15% | Trend emerging |
| **R3 Strong bull** | 2025 | $2,700 → $4,300 (+43%) | ~$60 (1.7%) | $1.37/min | 18% | Persistent uptrend |
| **R4 Parabolic** | 2026-01 → 2026-06 | $4,300–5,020 | **$141–184 (3–3.9%)** | **$3.26–4.31/min** | 32–42% | Whipsaw (Feb +13%, Mar −12%) |

**Killer metric:** a fixed $7 stop = ~16 min of normal movement in 2021 vs ~**2 min**
in 2026. Average daily range went **$21 → $141 (~7×)**.

## Diagnosis — two compounding faults
- **(A) Signals don't scale with vol.** `self`/`better` generators size SL/TP in
  **fixed dollars**; `scalper` only half-adapts (entry+SL use ATR, TPs don't).
- **(B) Scoring is blended over 2021-26**, where the parabolic 2026 dominates
  fixed-lot drawdown — so even sane configs look like they blow the 40% gate.
  (Evidence: `scalper24`, partly ATR-aware, still produced 0 of 618 gate-passers.)

## Plan (decided: 4 regimes · best-per-regime + live switcher)
1. **Adaptive generator** `generate_adaptive_self_signals.py`: every distance an
   **ATR multiple** — `entry_offset_atr, range_atr, sl_gap_atr, tp1_atr, tp2_atr,
   tp3_atr` — so one config self-scales from ~$3 stops (2021) to ~$25 (2026).
2. **Regime windows** = the 4 rows above (chart-month subsets + signal date filter).
3. **Sweep params:** ATR multipliers · ATR entry-filter band · lifecycle
   (`entry_count, tp1_lock_delay, max_hold, bep`) · `risk_pct` 1–5%.
4. **Score per regime, not blended:** each candidate backtested inside each regime
   window → `(edge, OOS, DD%)` per regime; gate DD≤40% **within** the regime.
5. **Deliverable:** best DD≤40% config **per regime** → `self_cli_best_<regime>.txt`,
   plus a **regime detector** (ATR percentile / price-vs-MA) that switches the
   active config live.
6. **Infra:** the same 16-core Actions matrix, matrix over `(regime × shard)`.
7. **Validate:** OOS split within each regime, smoke-test, lock.

_(The accompanying `regime_chart.png` — price / daily-$ / per-minute-$ over time —
was a throwaway artifact generated in the since-removed `sweep2021/` working dir;
recover it from git history if needed.)_
