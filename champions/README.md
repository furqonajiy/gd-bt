# Per-regime champions for `auto --adaptive` / `backtest --adaptive`

`CHAMPION_<regime>.json` holds the deployable config for each volatility regime
(R1quiet / R2bull / R3strong / R4parab). The live `auto --adaptive` loop and the
`backtest_explicit.py --adaptive` path read these via `--champions-dir champions`.

**Current state:** every regime is seeded with **SC24** (the live incumbent) as a
placeholder. As the regime grid sweep (`research/regime-adaptive`) publishes a
validated winner for a regime that beats SC24, replace that one file here — the
adaptive executor/backtest picks it up immediately. The `config` block is the
authoritative StrategyConfig; the rest is metadata.
