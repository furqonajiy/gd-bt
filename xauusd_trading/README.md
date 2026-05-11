# xauusd_trading

Validated XAUUSD strategy as a reusable engine. Same code path runs both
historical backtests and live signal-by-signal decisions, so behavior is
guaranteed identical.

## Validated baseline (v2 — locked, do not drift)

Configuration tuned via 3-stage parameter sweep over ~7,900 configs against
full broker M1 data (Jan–May 2026):

- Initial capital: `$1,000`
- Risk per signal: `5%` of current equity
- Direction: follow original signal
- Entries: `3` per signal
- Entry ladder: `range_to_sl` with `$2` gap to signal SL
  (deepest entry sits $2 above SL on a BUY, $2 below on a SELL)
- Activation delay: `0` minutes
- Pending expiry: `240` minutes after activation (4-hour fill window)
- Max hold: `90` minutes after first fill
- Initial SL: `1.0 ×` raw first-entry-to-SL distance
- Final target: `TP2`
- Stop lock: after TP1 touched, remaining stops move to TP1
- Fill mode: strict touch-only (no marketable / no stale)
- Spread-aware MT5 bid/ask trigger logic

Backtest result on broker data:

| Metric | Value |
|---|---|
| Full period (Jan 22 – May 7, 2026) | ~$1.13M |
| In-sample (Jan–Mar) | ~$13,854 |
| Out-of-sample (Apr–May) | ~$66,797 |
| Max drawdown | -42.8% |
| Win rate | 55.9% |
| April-only smoke baseline | $50,493.01 (locked in `tests/test_smoke.py`) |

**Forward expectation:** the headline figures reflect the favorable Apr–May
2026 regime. Realistic forward expectation is closer to **2–10× per month**;
the IS column is a better median predictor than the OOS column.

### Previous baseline (v1 — superseded)

The v1 strategy (range_uniform, 20-min expiry, 1.25× SL multiplier)
produced $1,000 → $8,748.88 on the original validation CSVs. v2 supersedes
v1 across the board; the smoke test is locked to v2.

## Layout

```
xauusd_trading/
├── config.py        StrategyConfig + chart constants. Single source of truth.
├── signal.py        Signal dataclass + parser + compute_entries(signal, config).
├── chart.py         Bar dataclass + MT5 M1 CSV loader.
├── triggers.py      Spread-aware fill / SL / TP predicates.
├── positions.py     Entry, Position, advance_one_bar — the simulator core.
├── adapters.py      ChartSource / PositionSource abstract bases + CSV impls.
├── mt5_adapter.py   Live MT5 chart, equity, archive (Windows-only).
├── mt5_executor.py  Order placement and management on MT5.
├── engine.py        decide() + render_report() — live decision API.
├── backtest.py      Historical replay (uses same simulator core).
├── excel_report.py  Excel report writer (soft dep on openpyxl).
└── cli.py           backtest / decide / fetch / mt5-info subcommands.
```

`advance_one_bar` in `positions.py` is the only place that owns state
transitions. Entry prices are computed by `compute_entries(signal, config)`
in `signal.py`. Both backtest and live decide use both, so they cannot disagree.

## Install

```bash
pip install -r requirements.txt
```

## Run a backtest

```bash
python -m xauusd_trading.cli backtest \
  --signals signals.txt \
  --charts data/XAUUSD_M1_*.csv \
  --output-dir backtest_output
```

## Run live decide on one signal (CSV chart source)

```bash
python -m xauusd_trading.cli decide \
  --signal "6. BUY XAUUSD 4717 - 4715 SL 4710 TP1 4725 TP2 4735 TP3 4750 6:24 PM" \
  --signal-date 2026-05-05 \
  --signal-tz 7 \
  --charts data/XAUUSD_M1_*.csv \
  --equity 50493
```

With currently-open positions:

```bash
python -m xauusd_trading.cli decide \
  --signal "..." --signal-date ... --signal-tz 7 \
  --charts data/XAUUSD_M1_*.csv \
  --equity 50000 \
  --positions-json positions.json
```

`positions.json` (or `sample_positions.json`) is a list of prior signals:

```json
[
  {
    "signal": "1. BUY XAUUSD 4542 - 4540 SL 4535 TP1 4550 TP2 4560 TP3 4575 11:21 AM",
    "date": "2026-04-30",
    "tz": 7,
    "equity_at_open": 45000.0
  }
]
```

The engine replays each prior signal against the chart up to "now" to
reconstruct its current state (filled / pending / closed, stage, P&L).

For demos or simulation you can override "now":

```bash
python -m xauusd_trading.cli decide ... --now "2026-04-30 08:00"
```

## MT5 live integration

See **`MT5_SETUP.md`** for the step-by-step setup. Quick version:

```powershell
pip install MetaTrader5

python -m xauusd_trading.cli mt5-info --mt5-symbol XAUUSD

python -m xauusd_trading.cli decide ^
  --signal "1. BUY XAUUSD ..." --signal-date 2026-05-07 --signal-tz 7 ^
  --mt5 --equity-from-mt5 ^
  --positions-json positions.json
```

## Importable API

```python
from xauusd_trading import (
    parse_one_signal, CsvChartSource, ManualPositionSource,
    decide, render_report, DEFAULT_CONFIG,
)

chart = CsvChartSource(["data/XAUUSD_M1_202605.csv"])
signal = parse_one_signal(
    "1. BUY XAUUSD 4543 - 4541 SL 4536 TP1 4551 TP2 4561 TP3 4576 2:02 PM",
    source_date="2026-05-05", source_offset=7,
)
positions = ManualPositionSource(equity=50000.0, positions=[])
recommendation = decide(signal, chart, positions, DEFAULT_CONFIG)
print(render_report(recommendation))
```

## What this engine does NOT do

By design, the engine adds **no** cross-signal overlay logic — no skip,
switch direction, hedge, or take-profit-early-because-new-signal-arrived.
Those decisions are not part of the strategy that produced the validated
result. Adding them now without re-validating against the historical
dataset would risk degrading the outcome.

If you want to add overlay rules later, they belong in a separate layer
that wraps `decide()`, not inside the engine itself, and they should be
re-validated end-to-end against the historical dataset before going live.

## Run the smoke test

```bash
python -m pytest tests/
```

Will fail loudly if any change in this repo causes the backtest to drift
from the v2 baseline locked in `tests/test_smoke.py`.

## Re-tuning the strategy

Use `sweep.py` (at repo root) to explore configurations:

```powershell
python sweep.py --signals signals.txt --charts data\XAUUSD_M1_*.csv `
    --output sweep_results.csv
```

The sweep runs every config through the same `advance_one_bar` simulator
the smoke test locks, so all results are honest. See `sweep.py --help`
for the full set of axes you can vary.
