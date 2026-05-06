# xauusd_trading

Validated XAUUSD strategy as a reusable engine. Same code path runs both
historical backtests and live signal-by-signal decisions, so behavior is
guaranteed identical.

## Validated baseline (locked, do not drift)

Configuration that produced the result:
- Initial capital: `$1,000`
- Risk per signal: `5%` of current equity
- Direction: follow original signal
- Entries: `3` range entries
- Activation delay: `0` minutes
- Pending expiry: `20` minutes after activation
- Max hold: `90` minutes after first fill
- Initial SL: `1.25 ×` raw first-entry-to-SL distance
- Final target: `TP2`
- Stop lock: after TP1 touched, remaining stops move to TP1
- Fill mode: strict touch-only (no marketable / no stale)
- Spread-aware MT5 bid/ask trigger logic

Result on the supplied dataset (Jan 22 – May 5, 2026):

| Metric | Value |
|---|---|
| Final equity | $8,748.88 |
| Net profit | +$7,748.88 (≈ 8.7×) |
| Win rate | 61.22% |
| Wins / Losses / No-fills | 180 / 114 / 550 |
| Signals included | 844 of 993 |

The smoke test in `tests/test_smoke.py` asserts these numbers exactly.

## Layout

```
xauusd_trading/
├── config.py        StrategyConfig + chart constants. The single source of truth.
├── signal.py        Signal dataclass + parser for the human signal file.
├── chart.py         Bar dataclass + MT5 M1 CSV loader.
├── triggers.py      Spread-aware fill / SL / TP predicates.
├── positions.py     Entry, Position, advance_one_bar — the simulator core.
├── adapters.py      ChartSource and PositionSource (manual now, MT5 later).
├── engine.py        decide() + render_report() — the live decision API.
├── backtest.py      Historical replay using the same core modules.
└── cli.py           `xauusd backtest …` and `xauusd decide …` subcommands.
```

The simulator (`advance_one_bar` in `positions.py`) is the only place that
owns state transitions. Both backtest and live decide go through it, so the
two cannot disagree.

## Install

```bash
pip install -r requirements.txt
```

## Run a backtest

```bash
python -m xauusd_trading.cli backtest --signals xauusd_signals_corrected_all.txt --charts XAUUSD_M1_202601221044_202604302359.csv XAUUSD_M1_202605010100_202605052359.csv --output-dir backtest_output
```

## Run live decide on one signal

```bash
python -m xauusd_trading.cli decide \
  --signal "1. BUY XAUUSD 4543 - 4541 SL 4536 TP1 4551 TP2 4561 TP3 4576 2:02 PM" \
  --signal-date 2026-05-05 \
  --signal-tz 7 \
  --charts XAUUSD_M1_202605010100_202605052359.csv \
  --equity 8748.88
```

With currently-open positions:

```bash
python -m xauusd_trading.cli decide \
  --signal "..." --signal-date ... --signal-tz 7 \
  --charts ... \
  --equity 8050 \
  --positions-json sample_positions.json
```

Where `sample_positions.json` is a list of prior signals, e.g.:

```json
[
  {
    "signal": "1. BUY XAUUSD 4542 - 4540 SL 4535 TP1 4550 TP2 4560 TP3 4575 11:21 AM",
    "date": "2026-04-30",
    "tz": 7,
    "equity_at_open": 8000.0
  }
]
```

The engine replays each prior signal against the chart up to "now" to
reconstruct its current state (filled / pending / closed, stage, P&L).

For demos or simulation you can override "now":

```bash
python -m xauusd_trading.cli decide ... --now "2026-04-30 08:00"
```

## Sample output

```
======================================================================
XAUUSD TRADING DECISION
Generated:  2026-04-30 08:00 GMT+3 (chart time)   Equity: $8,050.00
======================================================================

NEW SIGNAL
----------------------------------------------------------------------
  SELL XAUUSD 4565 - 4567   SL 4572   TP1 4557  TP2 4547  TP3 4532
  Issued 12:33 PM GMT+7 = 2026-04-30 08:33 GMT+3

  Action: FOLLOW
  Reason: Strategy follows every signal. 3 limits @ 4565, 4566, 4567, ...

  Orders to place:
    #0 SELL LIMIT 4565   SL 4573.75   lot 0.1533   risk -$134.17
    #1 SELL LIMIT 4566   SL 4574.75   lot 0.1533   risk -$134.17
    #2 SELL LIMIT 4567   SL 4575.75   lot 0.1533   risk -$134.17
  Pending expires:  2026-04-30 08:53 GMT+3 (20 min after activation)
  Final target:     TP2 @ 4547 (lock to TP1 after TP1 touch)
  Max hold:         90 min from first fill
  Total initial risk if all fill: -$402.50 (5.0% of equity)

OPEN POSITIONS  (1)
----------------------------------------------------------------------
  Signal 2026-04-30#01  BUY 4542-4540  issued 2026-04-30 07:21
    Stage:  Stage 1 (initial SL active)    First fill: 2026-04-30 07:33
            Time exit: 2026-04-30 09:03  (63 min left)
      #0 (4542)  OPEN   stop @ 4533.25   floating +$33.37
      #1 (4541)  NoFill
      #2 (4540)  NoFill
    Realized: +$0.00   Floating: +$33.37   Action: HOLD

SUMMARY
----------------------------------------------------------------------
  New signal:             FOLLOW  (3 orders, max risk -$402.50)
  Existing positions:     1  realized +$0.00  floating +$33.37
  Equity:                 $8,050.00
======================================================================
```

## Importable API

```python
from xauusd_trading import (
    parse_one_signal, CsvChartSource, ManualPositionSource,
    decide, render_report, DEFAULT_CONFIG,
)

chart = CsvChartSource(["chart.csv"])
signal = parse_one_signal("1. BUY XAUUSD 4543 - 4541 SL 4536 TP1 4551 TP2 4561 TP3 4576 2:02 PM",
                          source_date="2026-05-05", source_offset=7)
positions = ManualPositionSource(equity=8750.0, positions=[])
recommendation = decide(signal, chart, positions, DEFAULT_CONFIG)
print(render_report(recommendation))
```

## Future: MT5 integration

When you're ready to pull state directly from MT5, write two adapters
satisfying the abstract interfaces in `adapters.py`:

- `Mt5ChartSource(ChartSource)` — `latest`, `bars_between`, `first_time`, `last_time`
- `Mt5PositionSource(PositionSource)` — `open_positions`, `equity`

Drop them in. No engine code or backtest code changes.

## What this engine does NOT do

By design, the engine adds **no** cross-signal overlay logic — no skip,
switch direction, hedge, or take-profit-early-because-new-signal-arrived.
Those decisions are not part of the strategy that produced the validated
result. Adding them now without re-backtesting would risk degrading the
61% / ~8.7× outcome.

If you want to add overlay rules later, they belong in a separate layer
that wraps `decide()`, not inside the engine itself, and they should be
re-validated end-to-end against the historical dataset before going live.

## Run the smoke test

```bash
python -m pytest tests/
```

Will fail loudly if any change in this repo causes the backtest to drift
from the validated baseline.
