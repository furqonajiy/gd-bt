# Self-scalper trailing TICK sweep -- REAL compounding DD

18 cells on committed June ELEV8 ticks, scored by backtest_hybrid (run_hybrid_backtest COMPOUNDING equity curve -- the real DD, not the parse_tick_run proxy). Ranked by net P&L among cells with **max_drawdown_pct <= 40%**.

**Best at DD<=40%:** `to05_tc05_af1` -- net $115,568, DD 23.51%, win 41.1%.

(0,0,*) = plain C160 (no trailing). Tick fills are broker-driven; ELEV8 != demo, so demo/forward-validate before deploying.
