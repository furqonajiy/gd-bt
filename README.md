# MT5 integration — setup guide

You can replace `--charts CSV` with live data from a running MetaTrader
5 terminal. Same engine, same numbers — only the data source changes.

For the daily live-trading procedures (Modes A and B), see
`OPERATIONS_PLAYBOOK.md`. For repo overview, see `../README.md`.

## 1. Install the package

On Windows, with your MT5 terminal already installed:

```powershell
pip install MetaTrader5
```

The `MetaTrader5` package is **Windows-only** and requires the MT5
terminal to be installed. The terminal does not have to be running with
a chart open, but it must be installed and capable of being launched.

## 2. Allow Python access in MT5

In MT5: **Tools → Options → Expert Advisors**

- ✅ Allow algorithmic trading
- ✅ Allow DLL imports

(These are the typical defaults; double-check.)

## 3. Find your symbol name

Symbols vary by broker:

| Broker style                | Likely symbol                 |
|-----------------------------|-------------------------------|
| Standard                    | `XAUUSD`                      |
| Pepperstone / cTrader-style | `XAUUSD.r`                    |
| FXCM / IC Markets           | `GOLD` or `XAUUSD`            |
| ECN suffix                  | `XAUUSDm`, `XAUUSD-ECN`, etc. |

Check **Market Watch** in MT5 — right-click → Show All — and use the
exact string you see there.

## 4. Confirm your broker server timezone

Most XAUUSD brokers run their server clock at **GMT+3** (year-round, no
DST, or with the same DST rule as Europe/Athens). This matches the
project's chart timezone, so no shift is needed and
`--mt5-server-offset 3` (the default) works.

If your broker is on a different offset (e.g. some are GMT+2, GMT+0),
pass the right value:

```powershell
--mt5-server-offset 2
```

You can verify with `mt5-info` (next step) — the printed "Latest bar"
time should match the time you see in MT5's chart window.

## 5. Test the connection

```powershell
python -m xauusd_trading.cli mt5-info --mt5-symbol XAUUSD
```

Expected output (your equity will differ):

```
Symbol:           XAUUSD
Server offset:    GMT+3
Latest bar:       2026-05-07 14:32:00  close=4555.18  spread=27 pts
Account equity:   $1,000.00

Open MT5 positions / pending orders for the symbol:
  (none)
```

If the bar time is wrong, adjust `--mt5-server-offset`. If the symbol
is not found, check the spelling against Market Watch.

## 6. Decide on a new signal using live MT5 data

```powershell
python -m xauusd_trading.cli decide ^
  --signal "1. BUY XAUUSD 4543 - 4541 SL 4536 TP1 4551 TP2 4561 TP3 4576 2:02 PM" ^
  --signal-date 2026-05-07 ^
  --signal-tz 7 ^
  --mt5 ^
  --equity-from-mt5
```

That command:
- Pulls the most recent M1 bars directly from MT5
- Reads your account equity from MT5
- Outputs the same decision report as the CSV path

## 7. M1 archive: build a long history one fetch at a time

MT5 only exposes the last ~103 days of M1 history (varies by broker).
Every `decide --mt5` call automatically fetches the available range and
saves it to **per-month CSV files** in `data/`. Over time you
accumulate a growing archive that goes beyond what MT5 itself keeps —
exactly the data you need for re-running backtests on your own broker's
quotes.

```
data/
├── XAUUSD_M1_202602.csv     ← built up over many fetches
├── XAUUSD_M1_202603.csv
├── XAUUSD_M1_202604.csv
└── XAUUSD_M1_202605.csv     ← appends new bars on each call
```

Each save **merges** with the existing file: bars with new timestamps
are added, bars with matching timestamps are replaced with MT5's latest
values, and bars only present in the existing file (e.g. early-month
bars that have since fallen out of MT5's window) are **preserved**.
This is the safe default.

### One-off bulk fetch

If you just want to pull data without running a decision:

```powershell
python -m xauusd_trading.cli fetch --mt5-symbol XAUUSD
```

Useful for filling the archive on a schedule (e.g. a daily Task
Scheduler job that captures the latest day before older data falls off
MT5's window).

### Using the archive for backtests

```powershell
python -m xauusd_trading.cli backtest ^
  --signals my_signals.txt ^
  --charts data\XAUUSD_M1_*.csv
```

The CSV format is identical to the original repo files, so they're
directly interchangeable.

## 8. Including currently-open signals

The engine uses `positions.json` to know which **signals** you are
currently in. MT5 alone does not preserve enough context (TP1, TP2,
TP3, range, signal time) to reconstruct the engine's position state, so
you maintain a small JSON file with the prior signals you have orders
or fills on. Entries auto-prune as positions close under `--execute` or
`auto`.

`positions.json`:

```json
[
  {
    "signal_key": "2026-05-07#01",
    "signal": "10. SELL XAUUSD 4555 - 4557 SL 4562 TP1 4547 TP2 4537 TP3 4522 1:14 PM",
    "date": "2026-05-07",
    "tz": 7,
    "equity_at_open": 50000.0,
    "executed_at": "2026-05-07T13:14:22"
  }
]
```

Then:

```powershell
python -m xauusd_trading.cli decide ^
  --signal "..." --signal-date 2026-05-07 --signal-tz 7 ^
  --mt5 --equity-from-mt5 ^
  --positions-json positions.json
```

The engine replays each prior signal against the live MT5 chart up to
"now" to reconstruct stage / floating P&L / time-exit countdown, then
prints the combined report.

## 9. Auto-execution (`--execute`)

`decide --execute` places the planned orders directly on MT5 and
manages existing tracked signals (cancels expired pendings, locks SL to
TP1 on TP1 touch, time-closes on max-hold deadline). Read-only
diagnostic output without `--execute`; with `--execute`, real orders
are sent.

```powershell
python -m xauusd_trading.cli decide ^
  --signal "..." --signal-date 2026-05-07 --signal-tz 7 ^
  --mt5 --equity-from-mt5 ^
  --positions-json positions.json ^
  --execute
```

The executor cross-checks account equity against the engine's recent
expectation; if they diverge by more than 50%, it aborts before placing
orders (safety against running on the wrong account).

## Sanity-check: does MT5 agree with positions.json?

The `mt5-info` output lists what MT5 thinks is currently open for the
symbol. After placing orders for a signal, run it to confirm everything
is in. After closing orders, run it again to confirm they are gone —
and update `positions.json` to match (or let `--execute` / `auto` auto-
prune it for you).

## Optional: connecting to a specific terminal / login

By default `mt5.initialize()` connects to whatever terminal session is
currently active. To pin to a specific install or login non-
interactively:

```powershell
--mt5-path "C:\Program Files\MetaTrader 5\terminal64.exe" ^
--mt5-login 12345678 ^
--mt5-password "..." ^
--mt5-server "MyBroker-Live"
```

Use this only if you have multiple terminals or want a scripted login.
For a single terminal that's already running, none of these are needed.

## What is NOT implemented (and why)

- **Auto-detect open positions from MT5.** MT5 stores ticket / open
  price / SL / TP / comment / magic, but not the original signal's
  TP1/TP2/TP3 range and issue time. To fully reconstruct an engine
  `Position`, you need the signal text. `positions.json` is the
  canonical source of truth for active signals; `mt5-info` is a sanity
  check, not a substitute.