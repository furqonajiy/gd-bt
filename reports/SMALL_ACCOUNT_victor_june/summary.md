# Small-account ($2K) safe-deployment validation -- V817 (Victor) -- june

V817 (Victor) feed + geometry, TICK-preferred. Only entries + the deployment gates change between cells. Reporting only -- promotes nothing.

| variant | cap | net | ret% | maxDD% | worstDay% | dailyWR% | sigWR% | entryWR% | payoff | PF | maxLoseStreak | peakConcSig | peakLots |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base_8entry_50k | $50,000 | $63,153 | 126.3 | -11.3 | -4.7 | 47.6 | 27.6 | 53.0 | 1.48 | 1.67 | 2 | 4 | 9.66 |
| base_8entry_2k | $2,000 | $2,250 | 112.5 | -11.8 | -4.8 | 47.6 | 27.6 | 53.0 | 1.45 | 1.63 | 2 | 4 | 0.32 |
| vs2k_e2_c1_d5_z6 | $2,000 | $1,305 | 65.2 | -6.7 | -6.2 | 47.6 | 28.6 | 57.4 | 1.84 | 2.48 | 2 | 1 | 0.1 |
| vs2k_e2_c1_d6_z6 | $2,000 | $1,305 | 65.2 | -6.7 | -6.2 | 47.6 | 28.6 | 57.4 | 1.84 | 2.48 | 2 | 1 | 0.1 |
| vs2k_e3_c1_d5_z6 | $2,000 | $1,008 | 50.4 | -7.3 | -5.1 | 47.6 | 27.0 | 56.8 | 1.76 | 2.31 | 2 | 1 | 0.09 |

## Exit mix & gate rejections

| variant | TP3 | TP2 | TP1 | SL | TIME | TRAIL | rejRiskBudget | rejDaily | rejConc |
|---|---|---|---|---|---|---|---|---|---|
| base_8entry_50k | 0 | 157 | 0 | 285 | 149 | 50 | 0 | 0 | 0 |
| base_8entry_2k | 0 | 157 | 0 | 285 | 149 | 50 | 0 | 0 | 0 |
| vs2k_e2_c1_d5_z6 | 0 | 16 | 0 | 19 | 13 | 6 | 0 | 2 | 109 |
| vs2k_e2_c1_d6_z6 | 0 | 16 | 0 | 19 | 13 | 6 | 0 | 2 | 109 |
| vs2k_e3_c1_d5_z6 | 0 | 23 | 0 | 29 | 20 | 9 | 0 | 2 | 109 |

## Minimum account-size floor (from observed stop distances)

`D` = dollar risk of ONE 0.01-lot leg if stopped out (= stop distance in price, since 0.01 lot x 100 = $1/pt). Floors: faithful 1%/leg = 100xD; full 8-entry zone <=4% = 200xD; safe 2-entry zone <=6% = 33.3xD.

| stop pct | D ($/0.01 leg) | faithful 1%/leg floor | full-8-entry <=4% floor | safe-2-entry <=6% floor |
|---|---|---|---|---|
| p50 | $11.9 | $1,190 | $2,380 | $397 |
| p75 | $12.8 | $1,275 | $2,550 | $425 |
| p90 | $13.6 | $1,360 | $2,720 | $453 |
| p95 | $15.1 | $1,507 | $3,014 | $502 |
| max | $183.6 | $18,360 | $36,720 | $6,120 |

Observed 8-entry ZONE risk at 0.01 lot (whole ladder, $): p50 $95  p90 $104  p95 $111  max $1469. On $2k, a p95 zone is 6% of the account.

## Verdict

- Full 8-entry V817 at $2k: worst day -4.8%, max DD -11.8%, max losing-signal streak 2.
- VS2K (e2/conc1/daily5/zone6) at $2k: worst day -6.2%, max DD -6.7%, net $1,305, return 65.2%, daily win rate 48%.
- Drawdown reduction: -11.8% -> -6.7%; worst day -4.8% -> -6.2%.
