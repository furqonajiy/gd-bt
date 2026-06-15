# MT5 integration — setup guide

You can replace `--charts CSV` with live data from a running MetaTrader 5
terminal. Same engine, same numbers — only the data source changes.

## 1. Install the package

On Windows, with your MT5 terminal already installed:

```powershell
pip install MetaTrader5
```

The `MetaTrader5` package is **Windows-only** and requires the MT5 terminal
to be installed. The terminal does not have to be running with a chart open,
but it must be installed and capable of being launched.

## 2. Allow Python access in MT5

In MT5: **Tools → Options → Expert Advisors**

- ✅ Allow algorithmic trading
- ✅ Allow DLL imports

(These are the typical defaults; double-check.)

Important: leave MT5's per-position right-click **Trailing Stop** feature **OFF**
for engine-managed XAUUSD positions. The Python executor owns protective trailing
SL moves via `TRADE_ACTION_SLTP`; native terminal trailing will fight the executor
and is not modeled by the backtest.

## 3. Find your symbol name

Symbols vary by broker:

| Broker style | Likely symbol |
|---|---|
| Standard | `XAUUSD` |
| Pepperstone / cTrader-style | `XAUUSD.r` |
| FXCM / IC Markets | `GOLD` or `XAUUSD` |
| ECN suffix | `XAUUSDm`, `XAUUSD-ECN`, etc. |

Check **Market Watch** in MT5 — right-click → Show All — and use the exact
string you see there.

## 4. Confirm your broker server timezone

XAUUSD brokers typically run their server clock on **Eastern European Time
(EET/EEST)** — UTC+2 in winter, UTC+3 in summer, switching on the EU rule (last
Sunday of March / October). The project's chart timezone is exactly this
(`core/chart_tz.py`), and `fetch`/`decide` store the broker clock **verbatim**,
so **keep `--mt5-server-offset 3` year-round** (it applies a 0-hour shift =
store-as-is). Do **not** switch it to 2 in winter — that would add an hour and
corrupt the timestamps.

Only change it if your broker uses a genuinely different *fixed* offset and you
want it normalised; e.g.:

```powershell
--mt5-server-offset 2
```

You can verify with `mt5-info` (next step) — the printed "Latest bar" time
should match the time you see in MT5's chart window.

## 5. Test the connection

```powershell
python -m xauusd_trading.cli mt5-info --mt5-symbol XAUUSD
```

Expected output (your equity will differ):

```
Symbol:           XAUUSD
Server offset:    GMT+3
Latest bar:       2026-05-07 14:32:00  close=4555.18  spread=27 pts
Account equity:   $5,000.00

Open MT5 positions / pending orders for the symbol:
  (none)
```

If the bar time is wrong, adjust `--mt5-server-offset`. If the symbol is not
found, check the spelling against Market Watch.

## 6. Live order types: LIMIT vs trailing-open STOP

When `trailing_open_distance = 0`, the live executor places normal broker LIMIT
orders, with guards so stale/marketable LIMITs are skipped instead of converted
into market orders.

When `trailing_open_distance > 0`, the live executor does **not** place LIMITs.
It places broker STOP orders to implement the virtual trailing-open rule:

- BUY: after Ask moves at least `distance` below the planned entry, place/trail a
  `BUY_STOP` at `Ask + distance`.
- SELL: after Bid moves at least `distance` above the planned entry, place/trail a
  `SELL_STOP` at `Bid - distance`.

This is intentional. A normal BUY LIMIT at 4750 would fill immediately when Ask
falls through 4750 on the way to 4740, which is exactly what trailing-open is
trying to avoid.

## 7. Protective trailing SL ownership

The executor owns protective trailing stops. Each `manage`/`auto` cycle
recomputes the engine's expected stop level and modifies MT5 SL with
`TRADE_ACTION_SLTP` when the stop should improve.

Do **not** enable MT5 terminal-native Trailing Stop on these positions. If native
trailing is enabled, MT5 and the executor can both modify the same SL. That
creates SL fights and live/backtest divergence because the backtest models only
the executor trail.

Expected live gap: executor SL can lag the backtest by up to one closed M1 bar +
the watch interval, plus broker slippage and broker stop/freeze-level clamping.

If a tracked position's SL differs from the executor's expected SL on a cycle
where the executor did not issue a modify, the engine logs:

```text
external SL change detected — is MT5 native trailing enabled?
```

This warning is passive. The executor does not fight the external change in that
cycle; inspect MT5 native trailing, other EAs, or manual SL edits.

## 8. Decide on a new signal using live MT5 data

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

## 9. M1 archive: build a long history one fetch at a time

MT5 only exposes the last ~103 days of M1 history (varies by broker). Every
`decide --mt5` call automatically fetches the available range and saves it
to **per-month CSV files** in `data/`. Over time you accumulate a growing
archive that goes beyond what MT5 itself keeps — exactly the data you need
for re-running backtests on your own broker's quotes.

```
data/
├── XAUUSD_M1_202602.csv     ← built up over many fetches
├── XAUUSD_M1_202603.csv
├── XAUUSD_M1_202604.csv
└── XAUUSD_M1_202605.csv     ← appends new bars on each call
```

Each save **merges** with the existing file: bars with new timestamps are
added, bars with matching timestamps are replaced with MT5's latest values,
and bars only present in the existing file (e.g. early-month bars that have
since fallen out of MT5's window) are **preserved**. This is the safe default.

### One-off bulk fetch

If you just want to pull data without running a decision:

```powershell
python -m xauusd_trading.cli fetch --mt5-symbol XAUUSD
# --months N limits how far back to refresh (default 2); live feed loops
# use --months 1 since rolled-over months are immutable.
```

Useful for filling the archive on a schedule (e.g. a daily Task Scheduler
job that captures the latest day before older data falls off MT5's window).

### Syncing the chart with the correct timezone (read this)

The chart timeline is the **broker server clock = Eastern European Time
(EET/EEST)**: UTC+2 in winter, UTC+3 in summer, switching on the EU rule (last
Sunday of March / October). `fetch` stores that clock **verbatim** — it does
**not** normalise to a fixed offset — so the rule is simple:

- **Keep `--mt5-server-offset 3` (the default) all year.** With offset 3 the
  store-time shift is `3 − 3 = 0`, i.e. the broker's own EET/EEST timestamps are
  written as-is, which is exactly what the engine expects.
- **Never drop it to 2 in winter.** That applies a `+1h` shift and corrupts every
  stored bar. The offset is *not* "the broker's current UTC offset" — it is the
  knob that keeps the stored clock equal to the broker clock, and `3` does that
  in both seasons.
- **The engine is DST-aware** (`xauusd_trading/core/chart_tz.py`). Because the
  CSV is EET/EEST and the engine knows the EU DST schedule, a provider signal in
  GMT+7 (Victor, fixed Jakarta time) is matched to the correct chart bar
  automatically — shifted by −4h in summer and **−5h in winter** — so your
  backtest aligns with what MT5 actually showed, with no manual adjustment.

To **rebuild the whole M1 archive back to 2020**, use the standalone
`cli_resync_m1_from_2020.txt` at the repo root (`fetch --months 80`); it
documents the broker-history limit and the connection flags.

**Verify** a sync is timezone-correct two ways: (1) `mt5-info`'s "Latest bar"
time must equal the time you see in MT5's own chart window; (2) the archive's
weekly close sits at `23:59` server-time most of the year but at `22:59` during
the US-vs-EU DST-mismatch windows (mid/late March, late Oct / early Nov) — the
fingerprint of a correct EET/EEST feed.

### Using the archive for backtests

```powershell
python -m xauusd_trading.cli backtest ^
  --signals my_signals.txt ^
  --charts data\XAUUSD_M1_*.csv
```

The CSV format is identical to the original repo files, so they're
directly interchangeable.

## 10. Including currently-open signals

The engine still uses `positions.json` to know which **signals** you are
currently in. MT5 alone does not preserve enough context (TP1, TP2, TP3,
range, signal time) to reconstruct the engine's position state, so you
maintain a small JSON file with the prior signals you have orders or fills
on, and remove them as they close.

`positions.json`:

```json
[
  {
    "signal_key": "2026-05-07#10",
    "signal": "10. SELL XAUUSD 4555 - 4557 SL 4562 TP1 4547 TP2 4537 TP3 4522 1:14 PM",
    "date": "2026-05-07",
    "tz": 7,
    "equity_at_open": 50000.0
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

The engine replays each prior signal against the live MT5 chart up to "now"
to reconstruct stage / floating P&L / time-exit countdown, then prints the
combined report.

## 11. Auto-execution (`--execute`)

`decide --execute` places the planned orders directly on MT5 and manages
existing tracked signals (cancels expired pendings, locks SL to TP1 on
TP1 touch, applies executor-owned trailing SL moves, and time-closes on
max-hold deadline). Read-only diagnostic output without `--execute`; with
`--execute`, real orders are sent.

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
symbol. After placing orders for a signal, run it to confirm everything is
in. After closing orders, run it again to confirm they are gone — and update
`positions.json` to match (or let `--execute` auto-prune it for you).

## Optional: connecting to a specific terminal / login

By default `mt5.initialize()` connects to whatever terminal session is
currently active. To pin to a specific install or login non-interactively:

```powershell
--mt5-path "C:\Program Files\MetaTrader 5\terminal64.exe" ^
--mt5-login 12345678 ^
--mt5-password "..." ^
--mt5-server "MyBroker-Live"
```

Use this only if you have multiple terminals or want a scripted login. For
a single terminal that's already running, none of these are needed.

## Parity check: overlay your live MT5 fills on the backtest

`tools/backtest_explicit.py --mt5-history <FILE>` overlays your real MT5
execution onto the backtest so you can compare them entry-by-entry.

1. In MT5, open the **History** tab, set it to **Positions**, make sure the
   **Comment** column is visible, then right-click → **Report** (or **Save as
   Report**) and save as `.xlsx` (or `.csv`/`.html`).
2. Run the backtest with that file:

   ```powershell
   python tools/backtest_explicit.py `
     --signals victor_signals.txt --charts data\XAUUSD_M1_*.csv `
     --output-dir reports\parity --mt5-history history.xlsx `
     ... (the rest of your strategy flags)
   ```

Each live position is matched to a backtest entry by the order **Comment**,
which the executor writes as the entry key (`2026-06-08#02.1`). The Per-Entry
Detail sheet then gains a **LIVE (MT5 execution)** column group — Live Entry /
SL / Exit / P&L / R — next to the **EXECUTED (backtest result)** group, and any
live value that differs from the plan is highlighted, so a parity gap (e.g. a
TP1-lock exit that filled at market live vs at the level in the backtest) is
easy to spot. The runner prints how many comments matched / were unmatched.

## What is NOT implemented (and why)

- **Auto-detect open positions from MT5.** MT5 stores ticket / open price /
  SL / TP / comment / magic, but not the original signal's TP1/TP2/TP3
  range and issue time. To fully reconstruct an engine `Position`, you need
  the signal text. `positions.json` is the canonical source of truth for
  active signals; `mt5-info` is a sanity check, not a substitute.
