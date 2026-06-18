# Per-regime champions for `auto --adaptive` / `backtest --adaptive`

`CHAMPION_<regime>.json` holds the deployable config for each volatility regime
(R1quiet / R2bull / R3strong / R4parab). The live `auto --adaptive` loop and the
`backtest_explicit.py --adaptive` path read these via `--champions-dir champions`.

**Current state:** R2bull and R3strong promote the SAME config, **`SC24T24E8`**
(SC24 + `entry_count 6→8`, `tp1_lock_delay` stays 24) — **#1 on the reliable
forward-fit metrics (OOS *and* fixed-lot edge)** in both, not the compounded
net+bonus mirage. **R4parab has moved on to `rsi75_sqz6_rr40`** after SC24T24E8
breached the ≤ 40% DD gate (58.3%) on 2026 data:

- **R2bull — `SC24T24E8`**: OOS 52,831 (vs SC24 e6 ~39,457), edge $124,014, DD 34.8%.
- **R3strong — `SC24T24E8`**: OOS 110,907 (vs SC24 e6 84,980, +31%), edge $260,388, DD 39.3%.
- **R4parab — `rsi75_sqz6_rr40`**: edge **$63,940** (edge+bonus $65,948) / OOS
  **$11,633** / DD **38.4%**, 6/6 stable months — #1 on **both** edge and OOS of the
  34-variant RSI × Bollinger × R:R R4 sweep. The lever is the **feed** (Bollinger
  bandwidth squeeze `--bb-bandwidth-min 0.0006` + R:R 1.0/2.0/4.0 + RSI 75/25) on
  the **e8** strategy (e8 / range_to_sl / slm2.1 / max_hold 240 / tp1_lock_delay 24 /
  lock_after_tp2 on / shared_sl off). **Supersedes** the interim e5 RSI champion
  (edge $39,508 / OOS $7,199 / DD 33.4%) by ~+62% on both, and the over-DD SC24T24E8.
  **Fresh sweep winner — forward-validate before scaling live.**
- **R1quiet** — still seeded with **SC24** until the sweep advances to it.

Across R2bull/R3strong the universal lever is **more entries** (e8 > e7 > e6 on OOS),
*not* the SL multiplier or the tp1-delay; R4parab's edge instead comes from the
filtered feed (squeeze + wide R:R) on that same e8 geometry. As the sweep publishes
R1quiet, replace that one file here — the adaptive executor/backtest picks it up
immediately. The `config` block is the authoritative StrategyConfig; the rest is
metadata.
