# Small-account ($2K) safe-deployment validation -- V817 (Victor) -- tick_only

V817 (Victor) feed + geometry, TICK-preferred. Only entries + the deployment gates change between cells. Reporting only -- promotes nothing.

| variant | cap | net | ret% | maxDD% | worstDay% | dailyWR% | sigWR% | entryWR% | payoff | PF | maxLoseStreak | peakConcSig | peakLots |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base_8entry_2k | $2,000 | $4,103 | 205.1 | -30.9 | -11.3 | 55.6 | 25.4 | 49.0 | 1.67 | 1.61 | 5 | 4 | 0.46 |
| vs2k_e2_c1_d5_z6 | $2,000 | $2,161 | 108.1 | -18.2 | -6.4 | 55.6 | 27.3 | 52.9 | 1.85 | 2.08 | 3 | 1 | 0.12 |

## Exit mix & gate rejections

| variant | TP3 | TP2 | TP1 | SL | TIME | TRAIL | rejRiskBudget | rejDaily | rejConc |
|---|---|---|---|---|---|---|---|---|---|
| base_8entry_2k | 0 | 270 | 0 | 536 | 258 | 78 | 0 | 0 | 0 |
| vs2k_e2_c1_d5_z6 | 0 | 28 | 0 | 40 | 28 | 8 | 0 | 4 | 189 |

## Minimum account-size floor (from observed stop distances)

`D` = dollar risk of ONE 0.01-lot leg if stopped out (= stop distance in price, since 0.01 lot x 100 = $1/pt). Floors: faithful 1%/leg = 100xD; full 8-entry zone <=4% = 200xD; safe 2-entry zone <=6% = 33.3xD.

| stop pct | D ($/0.01 leg) | faithful 1%/leg floor | full-8-entry <=4% floor | safe-2-entry <=6% floor |
|---|---|---|---|---|
| p50 | $11.9 | $1,190 | $2,380 | $397 |
| p75 | $12.8 | $1,275 | $2,550 | $425 |
| p90 | $13.6 | $1,360 | $2,720 | $453 |
| p95 | $15.3 | $1,532 | $3,064 | $511 |
| max | $183.6 | $18,360 | $36,720 | $6,120 |

Observed 8-entry ZONE risk at 0.01 lot (whole ladder, $): p50 $95  p90 $102  p95 $112  max $1469. On $2k, a p95 zone is 6% of the account.

## Verdict

- Full 8-entry V817 at $2k: worst day -11.3%, max DD -30.9%, max losing-signal streak 5.
- VS2K (e2/conc1/daily5/zone6) at $2k: worst day -6.4%, max DD -18.2%, net $2,161, return 108.1%, daily win rate 56%.
- Drawdown reduction: -30.9% -> -18.2%; worst day -11.3% -> -6.4%.

## $2K equity journey (monthly, compounded)

### base_8entry_2k

| month | signals | win% | month P&L | equity end |
|---|---|---|---|---|
| (start) | - | - | - | $2,000 |
| 2026-05 | 129 | 45 | $1,181 | $3,181 |
| 2026-06 | 174 | 57 | $2,922 | $6,103 |
| **final** | - | - | **$4,103 net** | **$6,103** |

### vs2k_e2_c1_d5_z6

| month | signals | win% | month P&L | equity end |
|---|---|---|---|---|
| (start) | - | - | - | $2,000 |
| 2026-05 | 47 | 48 | $674 | $2,674 |
| 2026-06 | 63 | 64 | $1,487 | $4,161 |
| **final** | - | - | **$2,161 net** | **$4,161** |

> Dollar levels compound from the $2K base at the 0.01-lot floor and are a MODEL UPPER BOUND, not a forecast. Read the ratios (max DD, worst day, win rate) as the real signal; realistic live is ~5-15%/month with losing months.
