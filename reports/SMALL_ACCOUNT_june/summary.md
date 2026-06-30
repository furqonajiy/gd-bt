# Small-account ($2K) safe-deployment validation -- june

TSL18 / T818 feed + geometry, TICK-preferred. Only entries + the deployment gates change between cells. Reporting only -- promotes nothing.

| variant | cap | net | ret% | maxDD% | worstDay% | dailyWR% | sigWR% | entryWR% | payoff | PF | maxLoseStreak | peakConcSig | peakLots |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base_8entry_50k | $50,000 | $189,082 | 378.2 | -16.3 | -30.1 | 64.0 | 31.1 | 38.2 | 2.30 | 1.42 | 17 | 17 | 26.77 |
| base_8entry_2k | $2,000 | $14,513 | 725.6 | -37.3 | -98.0 | 64.0 | 31.1 | 38.2 | 2.04 | 1.26 | 17 | 17 | 1.52 |
| ts2k_e2_c1_d5_z6 | $2,000 | $396 | 19.8 | -11.5 | -6.4 | 54.2 | 32.5 | 39.5 | 1.89 | 1.23 | 6 | 1 | 0.02 |
| ts2k_e2_c1_d6_z6 | $2,000 | $404 | 20.2 | -11.5 | -6.0 | 52.0 | 32.7 | 39.9 | 1.86 | 1.24 | 6 | 1 | 0.02 |
| ts2k_e3_c1_d5_z6 | $2,000 | $508 | 25.4 | -14.6 | -8.2 | 54.2 | 33.5 | 39.4 | 1.85 | 1.20 | 6 | 1 | 0.03 |

## Exit mix & gate rejections

| variant | TP3 | TP2 | TP1 | SL | TIME | TRAIL | rejRiskBudget | rejDaily | rejConc |
|---|---|---|---|---|---|---|---|---|---|
| base_8entry_50k | 1113 | 0 | 0 | 5579 | 2898 | 598 | 0 | 0 | 0 |
| base_8entry_2k | 1113 | 0 | 0 | 5579 | 2898 | 598 | 0 | 0 | 0 |
| ts2k_e2_c1_d5_z6 | 28 | 0 | 0 | 155 | 95 | 18 | 0 | 14 | 1526 |
| ts2k_e2_c1_d6_z6 | 28 | 0 | 0 | 155 | 97 | 18 | 0 | 0 | 1539 |
| ts2k_e3_c1_d5_z6 | 41 | 0 | 0 | 230 | 143 | 25 | 0 | 55 | 1488 |

## Minimum account-size floor (from observed stop distances)

`D` = dollar risk of ONE 0.01-lot leg if stopped out (= stop distance in price, since 0.01 lot x 100 = $1/pt). Floors: faithful 1%/leg = 100xD; full 8-entry zone <=4% = 200xD; safe 2-entry zone <=6% = 33.3xD.

| stop pct | D ($/0.01 leg) | faithful 1%/leg floor | full-8-entry <=4% floor | safe-2-entry <=6% floor |
|---|---|---|---|---|
| p50 | $8.1 | $808 | $1,616 | $269 |
| p75 | $11.1 | $1,113 | $2,226 | $371 |
| p90 | $16.2 | $1,620 | $3,240 | $540 |
| p95 | $19.8 | $1,980 | $3,960 | $660 |
| max | $39.6 | $3,963 | $7,926 | $1,321 |

Observed 8-entry ZONE risk at 0.01 lot (whole ladder, $): p50 $64  p90 $122  p95 $146  max $231. On $2k, a p95 zone is 7% of the account.

## Verdict

- Full 8-entry TSL18 at $2k: worst day -98.0%, max DD -37.3%, max losing-signal streak 17.
- TS2K (e2/conc1/daily5/zone6) at $2k: worst day -6.4%, max DD -11.5%, net $396, return 19.8%, daily win rate 54%.
- Drawdown reduction: -37.3% -> -11.5%; worst day -98.0% -> -6.4%.
