# xauusd_trading

Validated XAUUSD M1 strategy as a reusable engine. The same code path runs
historical backtests and live signal-by-signal decisions, so behavior is
guaranteed identical between simulation and live trading.

## Validated baseline (v2 — locked, do not drift)

These values produced the v2 backtest result. Touching them changes
behavior and invalidates `tests/test_smoke.py`. Re-run the parameter sweep
and re-lock the smoke test before deploying any change.

| Parameter | Value |
|---|---|
| Initial capital | $1,000 |
| Risk per signal | 5% of current equity |
| Entries per signal | 3 limit orders |
| Entry ladder | `range_to_sl` with $2 gap to signal SL |
| Activation delay | 0 minutes |
| Pending expiry | 240 minutes (4-hour fill window) |
| Max hold | 90 minutes after first fill |
| Initial SL | 1.0× raw first-entry-to-SL distance |
| Final target | TP2 |
| Stop lock | After TP1 touched, remaining stops move to TP1 |
| Fill mode | Strict touch-only with arming |
| Sizing | All entries same lot, total initial-SL price-risk = 5% × equity |

Backtest performance on broker M1 data (Jan 22 – May 7, 2026):

| Metric | Value |
|---|---|
| Full period | ~$1.13M |
| In-sample (Jan–Mar) | ~$13,854 |
| Out-of-sample (Apr–May) | ~$66,797 |
| Max drawdown | -42.8% |
| Win rate | 55.9% |
| April-only smoke baseline | $50,493.01 (locked in `tests/test_smoke.py`) |

**Forward expectation:** the headline figures reflect the favorable Apr–May
2026 regime. Realistic forward expectation is **2–10× per month**; the IS
column is a better median predictor than the OOS column.

## Install

One-time setup on Windows:

```powershell
conda create -n xauusd python=3.12 -y
conda activate xauusd
conda install -c conda-forge pandas pytest openpyxl -y
pip install MetaTrader5
```

For every PowerShell session afterwards, just activate the env:

```powershell
conda activate xauusd
```

**PowerShell line continuation note:** all multi-line commands below use
backtick `` ` `` at the end of each line. That's the PowerShell continuation
character. Copy the whole block including the backticks and it works.
Don't substitute `\` (Bash) or `^` (cmd).

## Quick start — most common commands

### 1. Run a historical backtest

```powershell
python -m xauusd_trading.cli backtest `
    --signals signals.txt `
    --charts data\XAUUSD_M1_*.csv `
    --output-dir backtest_output
```

Writes `summary.json`, `signal_results.csv`, `entry_results.csv`, and
`backtest_results.xlsx` (the styled Excel report) to `backtest_output\`.
The CLI expands the `*` glob itself, so the same command works on cmd, Bash,
or PowerShell.

If MT5 is running, the backtest auto-fetches the latest 2 months into
`data\` first. If MT5 isn't running, it skips the fetch and uses whatever
CSVs are already in `data\`.

### 2. Decide on a BUY signal (preview, no order placed)

A new BUY signal arrives in your Telegram, e.g.:

> 1. BUY XAUUSD 4717 - 4715 SL 4710 TP1 4725 TP2 4735 TP3 4750 6:24 PM

Send it to the engine to see the order plan:

```powershell
python -m xauusd_trading.cli decide `
    --signal "1. BUY XAUUSD 4717 - 4715 SL 4710 TP1 4725 TP2 4735 TP3 4750 6:24 PM" `
    --signal-date 2026-05-07 `
    --signal-tz 7 `
    --mt5 --equity-from-mt5 `
    --positions-json positions.json
```

The `--signal-tz 7` says the time `6:24 PM` is in GMT+7 (your local time);
the engine converts it to GMT+3 (chart time) internally. The
`--positions-json` flag lets the engine replay any signals already open so
the report shows their current stage / floating P&L alongside the new plan.

### 3. Decide on a SELL signal

Same as BUY, just paste the SELL line in:

```powershell
python -m xauusd_trading.cli decide `
    --signal "2. SELL XAUUSD 4738 - 4740 SL 4745 TP1 4730 TP2 4720 TP3 4700 2:06 PM" `
    --signal-date 2026-05-07 `
    --signal-tz 7 `
    --mt5 --equity-from-mt5 `
    --positions-json positions.json
```

### 4. Execute a signal directly on MT5

Add `--execute` to actually place the orders. This implies `--mt5` and
auto-fetches your equity from MT5, so you can drop those flags:

```powershell
python -m xauusd_trading.cli decide `
    --signal "1. BUY XAUUSD 4717 - 4715 SL 4710 TP1 4725 TP2 4735 TP3 4750 6:24 PM" `
    --signal-date 2026-05-07 `
    --signal-tz 7 `
    --execute `
    --positions-json positions.json
```

What `--execute` does, in order:

1. Sanity-checks the MT5 account (equity > 0, trading enabled, market open,
   live tick available, your account equity within 50% of the engine's
   expectation — guards against running on the wrong account)
2. Manages every signal in `positions.json`: cancels expired pendings,
   moves SL to TP1 on positions that already touched TP1, time-closes
   positions past the 90-minute deadline
3. Places 3 BUY/SELL LIMIT orders for the new signal, with SL and TP2
   attached to each
4. Adds the new signal to `positions.json` and prunes any signals whose
   MT5 footprint is already gone

If sanity checks fail, the executor aborts before placing anything and
prints what's wrong.

### 5. Quick MT5 diagnostic

```powershell
python -m xauusd_trading.cli mt5-info --mt5-symbol XAUUSD
```

Prints the latest M1 bar, your account equity, and any open MT5
positions/pending orders for the symbol. Use this to verify MT5 is
connected, your symbol name is right, and the broker timezone offset is
correct (the bar time should match what you see in MT5's chart window).

### 6. Bulk fetch M1 history without running a decision

```powershell
python -m xauusd_trading.cli fetch --mt5-symbol XAUUSD
```

Pulls the last 2 months of M1 from MT5 into per-month CSVs at
`data\XAUUSD_M1_YYYYMM.csv`. Useful as a daily Task Scheduler job to keep
the archive fresh — MT5 only retains ~103 days of M1 history per broker,
so accumulating your own archive over time gives you backtest data that
goes further back than MT5 itself remembers. Bars merge with existing
files; nothing is overwritten.

## Position management — `positions.json`

The engine uses a small JSON file to remember which signals are currently
in flight. Each entry has the original signal text, its date, its source
timezone, and the equity at the time you placed it:

```json
[
  {
    "signal": "1. BUY XAUUSD 4717 - 4715 SL 4710 TP1 4725 TP2 4735 TP3 4750 6:24 PM",
    "date": "2026-05-07",
    "tz": 7,
    "equity_at_open": 50000.0
  }
]
```

When you run `decide --execute`, the engine:

- Replays each entry against the live chart up to "now" to reconstruct its
  current stage (pending / filled / TP1 locked / closed)
- Manages each one (cancel/lock/timeout) on MT5 as needed
- After successfully placing the new signal, appends it to `positions.json`
- Auto-prunes entries whose MT5 orders+positions are all gone (TP hit, SL
  hit, time-closed, or manually closed)

In dry-run mode (no `--execute`), the engine still replays them and prints
the report, but doesn't touch MT5 or the JSON file.

You can edit `positions.json` by hand if needed — it's just a list. If MT5
shows orders/positions that aren't in the JSON, `--execute` will print a
WARNING line about each unknown one but proceed anyway.

## Strategy overrides

Both `backtest` and `decide` accept these flags to test alternative
configurations without editing `config.py`. Defaults match the v2 baseline.

```powershell
--initial-capital 1000.0       # starting equity for backtest sizing
--risk 0.05                    # fraction of equity to risk per signal
--entries 3                    # number of entry slots (>= 1, no hard cap)
--entry-ladder range_to_sl     # range_to_sl | range_uniform
--entry-sl-gap 2.0             # $ between deepest entry and signal SL
```

Example — see what happens with 5 entries and a $5 SL gap:

```powershell
python -m xauusd_trading.cli backtest `
    --signals signals.txt `
    --charts data\XAUUSD_M1_*.csv `
    --output-dir backtest_5entries `
    --entries 5 --entry-sl-gap 5.0
```

For exhaustive parameter exploration use `sweep.py` at the repo root, not
overrides on the CLI — it runs many configs in parallel and produces a
ranked CSV. Don't change `config.py` defaults without re-running the sweep
and re-locking the smoke test.

## CSV-only mode (no MT5)

If MT5 isn't available (Linux, Mac, no terminal installed, paper-trading on
your laptop), use `--charts` instead of `--mt5` for `decide`:

```powershell
python -m xauusd_trading.cli decide `
    --signal "1. BUY XAUUSD 4717 - 4715 SL 4710 TP1 4725 TP2 4735 TP3 4750 6:24 PM" `
    --signal-date 2026-05-07 `
    --signal-tz 7 `
    --charts data\XAUUSD_M1_*.csv `
    --equity 50000
```

Backtest runs identically in CSV-only mode — if MT5 isn't reachable the
auto-archive step is skipped silently and the backtest just uses the CSVs
you point it at.

## MT5 broker-specific tweaks

If your broker uses a different symbol name or server timezone, override
on every command:

```powershell
--mt5-symbol XAUUSD.r          # Pepperstone / cTrader-style
--mt5-symbol GOLD              # FXCM-style
--mt5-symbol XAUUSDm           # ECN-suffixed
--mt5-server-offset 2          # broker on GMT+2 instead of GMT+3
```

Verify with `mt5-info` first — the printed "Latest bar" time should match
the time you see in MT5's chart window. If it doesn't, your offset is
wrong.

For first-time MT5 setup (allowing Python access in MT5 Tools, finding
your symbol name, optional non-interactive login), see **`MT5_SETUP.md`**.

## Smoke test

Run after any change in `xauusd_trading\` to verify the engine still
matches the locked v2 baseline:

```powershell
python -m pytest tests\
```

The smoke test asserts exact equity, win/loss/no-fill counts, and win rate
on April 2026 only. If it fails, the engine has drifted — either revert the
change or re-validate by running the full sweep and locking new numbers.

## Re-tuning the strategy

Use `sweep.py` at the repo root to explore configurations:

```powershell
python sweep.py --signals signals.txt --charts data\XAUUSD_M1_*.csv `
    --output sweep_results.csv
```

Every config runs through the same `advance_one_bar` simulator the smoke
test locks, so all results are honest (strict-touch arming, same-bar
worst-case stop wins, spread-aware triggers — no lookahead). See
`python sweep.py --help` for the full set of axes you can vary.

## Project layout

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
in `signal.py`. Both backtest and live decide use both, so they cannot
disagree.

## Importable API

```python
from xauusd_trading import (
    parse_one_signal, CsvChartSource, ManualPositionSource,
    decide, render_report, DEFAULT_CONFIG,
)

chart = CsvChartSource(["data/XAUUSD_M1_202605.csv"])
signal = parse_one_signal(
    "1. BUY XAUUSD 4717 - 4715 SL 4710 TP1 4725 TP2 4735 TP3 4750 6:24 PM",
    source_date="2026-05-07", source_offset=7,
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

The engine also does **not** auto-detect open positions from MT5 alone. MT5
stores ticket / open price / SL / TP / comment / magic, but not the
original signal's TP1/TP2/TP3 range and issue time — which the engine
needs. `positions.json` is the canonical source of truth for active
signals; `mt5-info` is a sanity check, not a substitute.

## Common gotchas

- **`--execute` writes real orders to MT5.** There is no confirmation
  prompt. Test with the dry-run version first (drop `--execute`).
- **Backtest equity ≠ live equity.** The same engine on the original
  validation CSVs gives different numbers from your broker's CSVs because
  tick prices differ. Both are correct outputs; broker data is what
  predicts live performance.
- **PowerShell does not expand glob wildcards.** The CLI expands them
  itself, so `data\XAUUSD_M1_*.csv` works. If you write your own scripts,
  pass paths through `(Get-ChildItem ...).FullName` or list files
  explicitly.
- **MT5 only retains ~103 days of M1 per broker.** Run `fetch` daily (or
  every `decide --mt5` will do it for you) to accumulate an archive past
  that window.
- **`MetaTrader5` package is Windows-only.** On macOS or Linux, only the
  CSV-mode flows (`backtest`, `decide --charts`) work; `--mt5`, `fetch`,
  `mt5-info`, and `--execute` raise an import error.
