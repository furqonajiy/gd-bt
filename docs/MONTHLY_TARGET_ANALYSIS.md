# Monthly Return Target Analysis

## Goal

The requested target is at least 20% monthly return.

This document records the current finding: with the tested breakout-retest edge,
20% monthly return is not achieved by signal logic alone. It requires increasing
risk per signal, and that materially increases drawdown.

## Current best signal family

The best current family is breakout-retest:

- BUY after a break above previous-day high or completed Asian-session high, then enter on retest.
- SELL after a break below previous-day low or completed Asian-session low, then enter on retest.

This is preferred over:

- EMA pullback: too lagging, high drawdown.
- Support/resistance fade: negative over full sample.
- Proactive bounce: negative over full sample.

## Balanced preset

Generator:

```powershell
python tools/generate_breakout_retest_balanced.py `
  --charts data/XAUUSD_M1_*.csv `
  --output generated/breakout_retest_balanced_full2024.txt `
  --diagnostics generated/breakout_retest_balanced_full2024.csv
```

Backtest at 1% risk:

```powershell
python tools/backtest_configurable.py `
  --signals generated/breakout_retest_balanced_full2024.txt `
  --charts data/XAUUSD_M1_*.csv `
  --output-dir reports/breakout_retest_balanced_full2024_risk1 `
  --initial-capital 10000 `
  --sizing-mode risk `
  --risk 0.01 `
  --max-drawdown-limit-pct 40
```

Observed snapshot from the full uploaded Jan-2024 -> May-2026 data:

```text
Net profit: about +$6.8k on $10k
Max drawdown: about -11.5%
```

## Aggressive preset

Generator:

```powershell
python tools/generate_breakout_retest_aggressive.py `
  --charts data/XAUUSD_M1_*.csv `
  --output generated/breakout_retest_aggressive_full2024.txt `
  --diagnostics generated/breakout_retest_aggressive_full2024.csv
```

Backtest at 1% risk:

```powershell
python tools/backtest_configurable.py `
  --signals generated/breakout_retest_aggressive_full2024.txt `
  --charts data/XAUUSD_M1_*.csv `
  --output-dir reports/breakout_retest_aggressive_full2024_risk1 `
  --initial-capital 10000 `
  --sizing-mode risk `
  --risk 0.01 `
  --max-drawdown-limit-pct 40
```

Observed snapshot from the full uploaded Jan-2024 -> May-2026 data:

```text
Net profit: about +$7.0k on $10k
Max drawdown: about -20.9%
```

## Risk-scaling implication

The current edge does not naturally produce stable 20% monthly return at low risk.
Increasing risk can create some months above 20%, but also increases max drawdown.

Approximate risk-scaling behavior observed from local validation:

```text
Balanced preset:
- 1% risk: max DD around low double digits; no month reaches 20%.
- 2% risk: some 20%+ months; max DD around mid-20%.
- 3% risk: more 20%+ months; max DD approaches the 40% guardrail.
- 4%+ risk: drawdown likely exceeds the 40% guardrail.

Aggressive preset:
- 1% risk: higher P&L than balanced but higher drawdown.
- 2% risk: can approach high monthly returns, but drawdown is already around/above the 40% guardrail in local validation.
```

## Recommendation

Do not optimize only for 20% per month. Use a two-stage target:

1. Improve signal quality while keeping risk at 1% to 2%.
2. Only increase risk after forward testing confirms the live fill behavior and drawdown remain acceptable.

Recommended live-test starting point:

```text
Balanced preset, risk 1%
```

Recommended high-growth research point:

```text
Balanced preset, risk 2% to 3%
```

Avoid using the aggressive preset above 1% risk without additional out-of-sample or live-forward validation.
