# Small-account ($2K) safe-deployment validation -- jan_jun

TSL18 / T818 feed + geometry, TICK-preferred. Only entries + the deployment gates change between cells. Reporting only -- promotes nothing.

| variant | cap | net | ret% | maxDD% | worstDay% | dailyWR% | sigWR% | entryWR% | payoff | PF | maxLoseStreak | peakConcSig | peakLots |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ts2k_e2_c1_d5_z6 | $2,000 | $60,557 | 3027.8 | -8.3 | -27.2 | 68.5 | 49.9 | 45.3 | 2.22 | 1.86 | 10 | 33 | 0.82 |
| ts2k_e3_c1_d5_z6 | $2,000 | $75,097 | 3754.8 | -10.9 | -42.1 | 66.4 | 48.9 | 45.2 | 2.18 | 1.83 | 9 | 35 | 1.02 |

## Exit mix & gate rejections

| variant | TP3 | TP2 | TP1 | SL | TIME | TRAIL | rejRiskBudget | rejDaily | rejConc |
|---|---|---|---|---|---|---|---|---|---|
| ts2k_e2_c1_d5_z6 | 193 | 0 | 0 | 1150 | 557 | 471 | 0 | 25 | 8797 |
| ts2k_e3_c1_d5_z6 | 289 | 0 | 0 | 1708 | 833 | 693 | 0 | 77 | 8759 |

## Minimum account-size floor (from observed stop distances)

`D` = dollar risk of ONE 0.01-lot leg if stopped out (= stop distance in price, since 0.01 lot x 100 = $1/pt). Floors: faithful 1%/leg = 100xD; full 8-entry zone <=4% = 200xD; safe 2-entry zone <=6% = 33.3xD.

| stop pct | D ($/0.01 leg) | faithful 1%/leg floor | full-8-entry <=4% floor | safe-2-entry <=6% floor |
|---|---|---|---|---|
| p50 | $9.9 | $990 | $1,980 | $330 |
| p75 | $15.3 | $1,530 | $3,060 | $510 |
| p90 | $21.6 | $2,160 | $4,320 | $720 |
| p95 | $21.6 | $2,160 | $4,320 | $720 |
| max | $27.5 | $2,752 | $5,504 | $917 |

Observed 8-entry ZONE risk at 0.01 lot (whole ladder, $): p50 $20  p90 $43  p95 $43  max $43. On $2k, a p95 zone is 2% of the account.

## Verdict

