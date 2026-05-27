# Balanced Breakout-Retest Candidate

This is the currently recommended generated-signal candidate on `feature/improve`.

## Validation data

Uploaded MT5 XAUUSD M1 chart data:

- 2024-01 through 2025-12: `*_INTERNET.csv`
- 2026-01 through 2026-05: `*_ELEV8.csv` where available
- Chart timezone: GMT+3
- Bid prices, spread in points, 1 point = $0.01

Validation period observed:

```text
2024-01-02 01:00 -> 2026-05-27 18:02
```

## Generator logic

Use a breakout-retest model, not EMA pullback and not support/resistance fading:

- If price breaks previous-day high or completed Asian-session high, generate BUY retest around the broken level.
- If price breaks previous-day low or completed Asian-session low, generate SELL retest around the broken level.
- The backtest then applies the standard optimized execution engine: activation delay, strict-touch fills, spread-aware triggers, TP1/TP2 locks, and TP3 target.

## Balanced generator parameters

```text
cooldown-minutes: 3
level-cooldown-minutes: 45
max-spread-points: 40
breakout-buffer: 1.0
entry-buffer: 0.0
stop-distance: 3.0
rr3: 2.0
session-start: 7
session-end: 23
require-body: true
min-body-atr: 0.1
```

## Backtest parameters

```text
initial-capital: 10000
sizing-mode: risk
risk: 0.01
entry-ladder: signal_range_3
activation-delay: 2
pending-expiry: 5
max-hold: 90
sl-multiplier: 1.5
final-target: TP3
lock-after-tp1: true
lock-after-tp2: true
max-drawdown-limit-pct: 40
```

## Observed full-range result

```text
Net profit: +$6,826.06
Final equity: $16,826.06
Max drawdown: -11.53%
Signals: 3,351
Wins: 903
Losses: 623
No fills: 1,814
Open: 11
Win rate: 59.17%
```

Train/test check:

```text
Train before 2026: +$3,181.84, max DD -11.53%
Test from 2026: +$2,601.79, max DD -4.50%
```

## Local commands

Generate signals:

```powershell
python tools/generate_breakout_retest_balanced.py `
  --charts data/XAUUSD_M1_*.csv `
  --output generated/breakout_retest_balanced_full2024.txt `
  --diagnostics generated/breakout_retest_balanced_full2024.csv
```

Backtest:

```powershell
python tools/backtest_configurable.py `
  --signals generated/breakout_retest_balanced_full2024.txt `
  --charts data/XAUUSD_M1_*.csv `
  --output-dir reports/breakout_retest_balanced_full2024 `
  --initial-capital 10000 `
  --sizing-mode risk `
  --risk 0.01 `
  --max-drawdown-limit-pct 40 `
  --progress-interval-seconds 15
```

## Notes

This candidate is preferred over the higher-profit aggressive variant because profit is similar while drawdown is much lower. Continue treating this as a backtest candidate, not a guarantee of live performance. Live execution can differ because fills, slippage, and TP1/TP2 stop-lock timing are broker/tick dependent.
