# Regime Calibration

`tools/regime_calibration_report.py` checks whether the current R1/R2/R3/R4
contract still matches the chart data.

It does not change live routing. It produces a review artifact before changing
thresholds or promoting champions.

## Run

```bash
python tools/regime_calibration_report.py \
  --charts data/XAUUSD_M1_*_ELEV8.csv \
  --out reports/regime_calibration.md \
  --csv-out reports/regime_calibration.csv \
  --json-out reports/regime_calibration.json \
  --xlsx-out reports/regime_calibration.xlsx
```

## What It Measures

- Monthly mean, median, and p90 M15 ATR.
- Monthly close-to-close return, range, and trend measured in ATR multiples.
- Four learned volatility clusters from monthly mean M15 ATR.
- Current live-router regime label for each month.
- Existing sweep calendar label for each month.
- Months near learned ATR boundaries.

## How To Use It

Use the report before changing regime thresholds, sweep windows, or live
champion routing.

- If learned clusters, live-router labels, and calendar labels mostly agree,
  the current R1/R2/R3/R4 split is defensible.
- If a month is near a learned boundary, treat it as unstable and avoid using it
  as the only proof for a champion.
- If the live router disagrees with the learned map across many recent months,
  review `xauusd_trading/strategy/regime.py` before deploying a regime-specific
  champion.
- If the calendar sweep label disagrees with the learned map, consider splitting
  that calendar window or re-running the sweep with a more data-driven slice.

The report is a calibration layer. Champion selection still uses DD, Edge, OOS,
and bonus-aware profitability gates.
