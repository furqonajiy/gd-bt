# XAUUSD Optimized Strategy Backtester

This package contains the final Python code for the optimized strategy:

- Initial capital: `$1,000`
- Risk: `5%` of current equity per signal
- Direction: follow the original signal direction
- Entries: `3` range entries
- Activation delay: `0 minutes`
- Pending expiry: `20 minutes`
- Max hold: `90 minutes` after first fill
- Fill mode: strict touch-only, no stale/marketable auto-fill
- Initial SL: `1.25 ×` original first-entry-to-signal-SL distance
- Final target: `TP2`
- Stop lock: after TP1 touch, move remaining open entries to TP1
- Spread-aware MT5 bid/ask trigger logic

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python xauusd_optimized_backtest.py --signals xauusd_signals_corrected_all.txt --charts XAUUSD_M1_202601221044_202604302359.csv XAUUSD_M1_202605010100_202605052359.csv --output-dir backtest_output
```

## Optional risk example

```bash
python xauusd_optimized_backtest.py \
  --signals xauusd_signals_corrected_all.txt \
  --charts XAUUSD_M1_202601221044_202604302359.csv XAUUSD_M1_202605010100_202605052359.csv \
  --risk 0.03 \
  --output-dir backtest_output_risk_3pct
```

## Broker lot constraints example

If your broker requires minimum lot `0.01` and lot step `0.01`, run:

```bash
python xauusd_optimized_backtest.py \
  --signals xauusd_signals_corrected_all.txt \
  --charts XAUUSD_M1_202601221044_202604302359.csv XAUUSD_M1_202605010100_202605052359.csv \
  --minimum-lot 0.01 \
  --lot-step 0.01 \
  --output-dir backtest_output_lot_step
```

## Outputs

The script writes these files to the output directory:

- `summary.json`
- `signal_results.csv`
- `entry_results.csv`
- `monthly_results.csv`
- `weekly_results.csv`
- `anomalies.csv`
- `excluded_signals.csv`

## Notes

The default run keeps signal anomalies but flags them, matching the optimized analysis behavior. To exclude structural anomalies, add:

```bash
--exclude-structural-anomalies
```

This backtester does not include broker margin checks, commission, slippage, rejection, minimum stop distance, or execution latency.
