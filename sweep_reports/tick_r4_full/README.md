# R4 non-trailing FULL sweep -- TICK vs M1 (May+June 2026, full R4 tick window)

2 candidates, scored on the SAME tick-covered May+June window three ways:
- **TICK** net (risk 1%) -- real Mt5Executor on committed May+June ticks (the rank key)
- **M1** net (risk 1%, slippage 2.0/1.0) -- compounded net + bonus, concurrent-risk DD
- **edge(fixed)** -- M1 at fixed 0.01 lot (capital-independent)
- **disc** = M1 net - TICK net (how much the backtest over-states)

**Winner c015**: tick $-6737.38 | M1 $46406.80666667987 (disc 788.8%) | M1 DD 36.391754590632445% | edge(fixed) $6688.050000001756 | slm 2.1 e7 mh240 f0.25 d24 lock2=true TP3 | rr 1/2.5/4 bb0.0006 rsi70/30

## Top 20 (by TICK net)

| # | id | TICK $ | M1 $ | disc% | M1 DD% | edge(fix) $ | slm | e | mh | f | d | lock2 | tgt | rr | bb | rsi |
|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|
| 1 | c015 | -6737.38 | 46406.80666667987 | 788.8 | 36.391754590632445 | 6688.050000001756 | 2.1 | 7 | 240 | 0.25 | 24 | true | TP3 | 1/2.5/4 | 0.0006 | 70/30 |
| 2 | c008 | -18377.49 | 1125.5166666645528 | 106.1 | 44.23150071402148 | 1615.1466666646375 | 1.5 | 7 | 180 | 0.25 | 12 | true | TP3 | 1/2.5/4 | 0.0008 | 70/30 |

TICK fills are broker-driven -- the winner is RESEARCH until forward/demo-validated.
