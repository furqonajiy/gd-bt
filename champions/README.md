# Per-regime champions for `auto --adaptive` / `backtest --adaptive`

`CHAMPION_<regime>.json` holds the deployable config for each volatility regime
(R1quiet / R2bull / R3strong / R4parab). The live `auto --adaptive` loop and the
`backtest_explicit.py --adaptive` path read these via `--champions-dir champions`.

**Current state:** two regimes carry promoted sweep winners, two are still SC24
placeholders:

- **R4parab — `SC24T15`** (SC24 + `tp1_lock_delay 24→15`, entry 6): the grid's #1
  by net+bonus, and it beats SC24 on OOS *and* drawdown.
- **R3strong — `SC24T24E8`** (SC24 + `entry_count 6→8`, `tp1_lock_delay 24`): tops
  the R3 grid on net+bonus AND OOS (OOS 110,907 vs SC24 e6's 84,980, +31%) at
  DD ≤ 40%. In R3 (strong trend) the lever is *more entries*, not the tp1-delay.
- **R1quiet / R2bull** — still seeded with **SC24** until the sweep advances to
  them.

As the regime grid sweep (`research/regime-adaptive`) publishes a validated
winner for a regime that beats the incumbent, replace that one file here — the
adaptive executor/backtest picks it up immediately. The `config` block is the
authoritative StrategyConfig; the rest is metadata.
