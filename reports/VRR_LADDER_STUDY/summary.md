# Victor corrected-R:R ladder study (VRR) — three-era A/B vs V072

**Question:** Victor's posted TP1 R:R collapsed (2024→Jan-2026 ≈ 0.5–0.67, July-2026 back to 0.67
vs his good Feb–Jun-2026 era ≈ 1.1). Does baking a consistent asymmetric TP ladder into the feed
beat the as-posted feed on the incumbent V072 geometry?

**Method:** derived feeds via `tools/generate_victor_rr_feed.py` (TPk = entry_edge ± rrk·|edge−SL|,
nominal risk — the exact `apply_signal_rr_policy` math; 18 provider SL-typo lines kept verbatim;
4325 signals rewritten per ladder; skeleton byte-identical). Backtests: V072 §6 command verbatim
(e8/gap0.7/slm1.6/mh180/TP3/to0.25/tc0.5-after-TP1, risk 5%, $50K) with only --signals/--output-dir
swapped; era-matched locked-exit slippage (2026: 2.0/1.0 · 2025: 0.9/0.45 · 2024: 0.5/0.25);
tick-preferred (2026 runs replay 328 signals on the real 32.7M-row tick archive).

## Results

### 2026-01 → today (R4; TICK from May; the LIVE-relevant era)
| feed | net P&L | max DD | win |
|---|---|---|---|
| base (as-posted) | $9,416,458 | −22.53% | 58.7% |
| rr10x20x40 | $9,198,623 | −12.24% | 59.7% |
| rr12x25x50 | $9,493,045 | −15.29% | 58.5% |
| rr15x30x50 | $9,449,024 | **−10.99%** | 57.5% |
| rr20x30x50 | $9,644,740 | −21.76% | 55.7% |
| rr20x40x60 | **$9,727,496** | −22.85% | 54.4% |

### 2025 (R3 strong; M1)
| feed | net P&L | max DD | win |
|---|---|---|---|
| base | $7,703,235 | −34.15% | 76.3% |
| rr10x20x40 | $8,588,248 | −32.71% | 71.7% |
| rr12x25x50 | $9,155,590 | −33.17% | 72.1% |
| rr15x30x50 | **$9,590,712 (+24.5%)** | −34.16% | 71.5% |
| rr20x30x50 | $8,864,443 | −33.00% | 66.3% |
| rr20x40x60 | $9,526,173 | **−31.64%** | 68.8% |

### 2024 (Apr–Dec, R2 bull; M1) — **INVERTED**
| feed | net P&L | max DD | win |
|---|---|---|---|
| base | **$80,550** | **−34.69%** | 65.5% |
| rr10x20x40 | $50,290 | −43.56% | 60.6% |
| rr12x25x50 | $25,862 | −47.43% | 58.6% |
| rr15x30x50 | $12,061 | −49.46% | 56.3% |
| rr20x30x50 | $6,971 | −50.26% | 55.0% |
| rr20x40x60 | $3,378 | −50.17% | 55.0% |

## Reading
1. **R4-2026 + R3-2025 (the regimes that matter for live): ladders win.** rr15x30x50 wins both
   (2026: +0.3% net at HALF the DD; 2025: +24.5% net at flat DD). rr20x40x60 is the raw-net
   runner-up. Every ladder ≥1.2 beat baseline net in both windows.
2. **R2-2024 inverts monotonically** — in the bull-grind, price routinely pays 0.6R then reverses,
   so a 1.5R TP1 rarely fills, the TP1 profit-lock never arms, and full-SL losses stack: every
   ladder underperforms as-posted AND breaches the 40% DD gate (43.6–50.3% vs 34.7%).
3. **Regime-dependence, not a universal constant** — consistent with the repo's per-regime champion
   architecture and with VICTOR_SWEEP_RUNBOOK's "Victor's 2026 style ≠ 2025, sweep per regime".
   Note Victor REWROTE his generator in 2026; the 2024-era signal engine no longer exists, so 2024
   is a robustness check on a retired regime+generator combination, not a deployment scenario.
4. Compounded net at 5% risk RANKS but does not DECIDE (repo rule); the DD structure
   (2026: 22.5%→11.0%) is the robust part of the 2026 finding.

## Status / next
- Stage-4 pending: parameter re-sweep under the chosen ladder (victor-sweep machinery, Actions),
  then — only after operator approval — the live auto-correction layer in the provider filter.
- Candidate ladders for stage-4: **rr15x30x50** (risk-adjusted leader) or **rr20x40x60** (max-net).
