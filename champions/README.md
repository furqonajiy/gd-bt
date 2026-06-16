# Per-regime champions for `auto --adaptive` / `backtest --adaptive`

`CHAMPION_<regime>.json` holds the deployable config for each volatility regime
(R1quiet / R2bull / R3strong / R4parab). The live `auto --adaptive` loop and the
`backtest_explicit.py --adaptive` path read these via `--champions-dir champions`.

**Current state:** the three completed regimes all promote the SAME config,
**`SC24T24E8`** (SC24 + `entry_count 6→8`, `tp1_lock_delay` stays 24) — because it
is **#1 on the reliable forward-fit metrics (OOS *and* fixed-lot edge)** in every
one of them, not the compounded net+bonus mirage:

- **R2bull — `SC24T24E8`**: OOS 52,831 (vs SC24 e6 ~39,457), edge $124,014, DD 34.8%.
- **R3strong — `SC24T24E8`**: OOS 110,907 (vs SC24 e6 84,980, +31%), edge $260,388, DD 39.3%.
- **R4parab — `SC24T24E8`**: OOS 15,032 (vs SC24T15E6 11,624) and edge $218,244
  (vs 165,497). **Supersedes the earlier SC24T15E6**, which only led on the
  compounded net+bonus (a leverage/luck mirage) and a ~1.7 pt lower drawdown.
- **R1quiet** — still seeded with **SC24** until the sweep advances to it.

Across the trending regimes the universal lever is **more entries** (e8 > e7 > e6
on OOS everywhere), *not* the SL multiplier or the tp1-delay. As the sweep
publishes R1quiet, replace that one file here — the adaptive executor/backtest
picks it up immediately. The `config` block is the authoritative StrategyConfig;
the rest is metadata.
