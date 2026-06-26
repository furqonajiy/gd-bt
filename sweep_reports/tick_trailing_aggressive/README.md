# Aggressive trailing TICK sweep -- beat TOC5 on net AND DD

32 cells on committed June ELEV8 ticks, scored by backtest_hybrid (REAL compounding-equity DD). Incumbent **TOC5** = open0.5/close0.5/trail-after-TP1: net $115,568, DD 23.51%.

**Best net at DD<=40%:** `trail_tp2_tc05_dly24_slm18` (mode trail_tp2) -- net $166,820, DD 23.65%, win 35.3%.

### No cell beat TOC5 on BOTH axes. TOC5 stays the trailing champion.

Modes: toc5 (lock TP1+trail), bep_half (early BE +3 then half-TP1 + trail), scaleout (close worst leg at TP1 + BE rest + trail), trail_tp2 (trail only after TP2). ELEV8 != demo -- forward-validate before deploying.
