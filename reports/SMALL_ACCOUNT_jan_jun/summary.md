# Small-account ($2K) safe-deployment validation -- jan_jun

TSL18 / T818 feed + geometry, TICK-preferred. Only entries + the deployment gates change between cells. Reporting only -- promotes nothing.

| variant | cap | net | ret% | maxDD% | worstDay% | dailyWR% | sigWR% | entryWR% | payoff | PF | maxLoseStreak | peakConcSig | peakLots |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ts2k_e2_c1_d5_z6 | $2,000 | $15 | 0.7 | -0.7 | 0.7 | 100.0 | 50.0 | 33.3 | 2.02 | 2.02 | 1 | 2 | 0.02 |

## Exit mix & gate rejections

| variant | TP3 | TP2 | TP1 | SL | TIME | TRAIL | rejRiskBudget | rejDaily | rejConc |
|---|---|---|---|---|---|---|---|---|---|
| ts2k_e2_c1_d5_z6 | 0 | 0 | 0 | 2 | 2 | 0 | 0 | 0 | 10231 |

## Minimum account-size floor (from observed stop distances)

`D` = dollar risk of ONE 0.01-lot leg if stopped out (= stop distance in price, since 0.01 lot x 100 = $1/pt). Floors: faithful 1%/leg = 100xD; full 8-entry zone <=4% = 200xD; safe 2-entry zone <=6% = 33.3xD.

| stop pct | D ($/0.01 leg) | faithful 1%/leg floor | full-8-entry <=4% floor | safe-2-entry <=6% floor |
|---|---|---|---|---|
| p50 | $9.0 | $900 | $1,800 | $300 |
| p75 | $13.0 | $1,305 | $2,610 | $435 |
| p90 | $14.4 | $1,440 | $2,880 | $480 |
| p95 | $14.4 | $1,440 | $2,880 | $480 |
| max | $14.4 | $1,440 | $2,880 | $480 |

Observed 8-entry ZONE risk at 0.01 lot (whole ladder, $): p50 $18  p90 $27  p95 $28  max $29. On $2k, a p95 zone is 1% of the account.

## Verdict

