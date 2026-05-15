# xauusd_trading

Validated XAUUSD M1 strategy as a reusable engine. The same code path runs
historical backtests and live signal-by-signal decisions, so behavior is
guaranteed identical between simulation and live trading.

## Validated baseline (v2 — locked, do not drift)

These values produced the v2 backtest result. Touching them changes
behavior and invalidates `tests/test_smoke.py`. Re-run the parameter sweep
and re-lock the smoke test before deploying any change.

| Parameter          | Value                                                           |
|--------------------|-----------------------------------------------------------------|
| Initial capital    | $1,000                                                          |
| Risk per signal    | 5% of current equity                                            |
| Entries per signal | 3 limit orders                                                  |
| Entry ladder       | `range_to_sl` with $2 gap to signal SL                          |
| Activation delay   | 0 minutes                                                       |
| Pending expiry     | 240 minutes (4-hour fill window)                                |
| Max hold           | 90 minutes after first fill                                     |
| Initial SL         | 1.0× raw first-entry-to-SL distance                             |
| Final target       | TP2                                                             |
| Stop lock          | After TP1 touched, remaining stops move to TP1                  |
| Fill mode          | Strict touch-only with arming                                   |
| Sizing             | All entries same lot, total initial-SL price-risk = 5% × equity |

Backtest performance on broker M1 data (Jan 22 – May 7, 2026):

| Metric                    | Value                                        |
|---------------------------|----------------------------------------------|
| Full period               | ~$1.13M                                      |
| In-sample (Jan–Mar)       | ~$13,854                                     |
| Out-of-sample (Apr–May)   | ~$66,797                                     |
| Max drawdown              | -42.8%                                       |
| Win rate                  | 55.9%                                        |
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
pip install telethon
```

For every PowerShell session afterwards, just activate the env:

```powershell
conda activate xauusd
```

**PowerShell line continuation note:** all multi-line commands below use
backtick `` ` `` at the end of each line. That's the PowerShell continuation
character. Copy the whole block including the backticks and it works.
Don't substitute `\` (Bash) or `^` (cmd).

## Project layout

```
xauusd_trading/
├── __init__.py            Public re-exports (stable import surface)
├── cli.py                 backtest / decide / manage / auto / fetch / mt5-info
├── core/
│   ├── config.py          StrategyConfig + chart constants. Single source of truth.
│   ├── signal.py          Signal dataclass + parser + compute_entries(signal, config).
│   ├── chart.py           Bar dataclass + MT5 M1 CSV loader.
│   ├── triggers.py        Spread-aware fill / SL / TP predicates.
│   └── positions.py       Entry, Position (with executed_at), advance_one_bar.
├── strategy/
│   ├── engine.py          decide() + render_report() — live decision API.
│   └── backtest.py        Historical replay + monthly/daily aggregation.
├── io/
│   ├── adapters.py        ChartSource / PositionSource bases + CSV impls.
│   └── mt5_adapter.py     Live MT5 chart, equity, archive (Windows-only).
├── execution/
│   └── mt5_executor.py    Placement + management + reconcile + late-TP1 catch-up.
└── reporting/
    └── excel_report.py    Excel report writer (soft dep on openpyxl).

(repo root)
├── listener/
│   └── telegram_listener.py    Telethon-based Victor channel ingestion.
├── tools/
│   └── sweep.py                Parameter sweep utility.
├── tests/
│   ├── test_smoke.py           Locked v2 baseline (April-2026).
│   ├── test_archive.py         M1 archive merge logic.
│   └── test_reconcile.py       reconcile_with_mt5 behaviour.
├── docs/
│   ├── MT5_SETUP.md            First-time MT5 setup guide.
│   └── OPERATIONS_PLAYBOOK.md  Daily live-trading runbook.
├── data/                       MT5 M1 archive (per-month CSVs).
├── signals.txt                 Signal feed (listener writes, engine reads).
├── positions.json              Tracked-signal registry.
└── README.md
```

`advance_one_bar` in `core/positions.py` is the only place that owns state
transitions. Entry prices are computed by `compute_entries(signal, config)`
in `core/signal.py`. Both backtest and live `decide` use both, so they
cannot disagree.

## Quick start — most common commands

### 1. Run a historical backtest

```powershell
python -m xauusd_trading.cli backtest `
    --signals signals.txt `
    --charts data\XAUUSD_M1_*.csv `
    --output-dir backtest_output
```

Writes the following to `backtest_output\`:

- `summary.json` — config + overall + monthly + daily aggregates
- `signal_results.csv` — one row per signal
- `entry_results.csv` — one row per entry slot (3 per signal)
- `daily_results.csv` — one row per calendar day in the chart range
- `backtest_results.xlsx` — three-sheet styled report

The CLI expands the `*` glob itself, so the same command works on cmd, Bash,
or PowerShell. If MT5 is running, the backtest auto-fetches the latest 2
months into `data\` first. If MT5 isn't running, it skips the fetch and
uses whatever CSVs are already in `data\`.

### 2. Decide on a signal (preview, no orders placed)

```powershell
python -m xauusd_trading.cli decide `
    --signal "1. BUY XAUUSD 4717 - 4715 SL 4710 TP1 4725 TP2 4735 TP3 4750 6:24 PM" `
    --signal-date 2026-05-07 `
    --signal-tz 7 `
    --mt5 --equity-from-mt5 `
    --positions-json positions.json
```

The `--signal-tz 7` says the time `6:24 PM` is in GMT+7 (Victor's tz); the
engine converts it to GMT+3 (chart time) internally. The `--positions-json`
flag lets the engine replay any signals already open so the report shows
their current stage / floating P&L alongside the new plan.

The preview report shows `Action:` as one of:

- **`FOLLOW`** — standard, all 3 orders to be placed
- **`FOLLOW` (partial)** — only a subset will be placed; some entries already
  played out in the backtest replay. Per-entry breakdown is appended
- **`SKIP_EXPIRED`** — 4-hour pending window already closed; no orders
- **`SKIP_INVALIDATED`** — every entry already played out in replay; no orders

### 3. Decide and execute on MT5

Same command + `--execute`. Real orders, no confirmation prompt:

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
   live tick available, equity within 50% of the engine's expectation —
   guards against running on the wrong account)
2. Reconciles each tracked signal with MT5 (patches PENDING entries the
   bar-replay missed; see "Reconciliation" below)
3. Manages every tracked signal: cancels expired pendings, performs Late
   TP1 catch-up closes where needed, locks SL to TP1 on positions in stage 1,
   time-closes positions past the 90-minute deadline
4. Places LIMIT orders for the new signal — only entries whose backtest-
   replay status is PENDING or OPEN; terminal entries are filtered out
5. Stamps `executed_at` (wall-clock placement time, chart-tz) on the
   registry entry and prunes any signals whose MT5 footprint is gone

If sanity checks fail, the executor aborts before placing anything.

### 4. Manage existing tracked signals (no new placement)

```powershell
python -m xauusd_trading.cli manage `
    --execute `
    --positions-json positions.json
```

Reads `positions.json`, replays each tracked signal against the live MT5
chart up to "now", reconciles with MT5, and (with `--execute`) applies
engine-driven changes **without placing anything new**:

- Cancels pending orders past the 4-hour expiry
- Late TP1 catch-up closes (see below)
- Locks SL to TP1 on filled positions where TP1 was touched
- Time-closes positions past the 90-minute max-hold deadline
- Auto-prunes `positions.json` of closed signals

Without `--execute`, prints status only.

`manage` supports `--watch` to loop the sweep every N seconds (default 5,
min 1) on a single persistent MT5 connection. It exits when the registry
has no live MT5 footprint or on Ctrl+C:

```powershell
python -m xauusd_trading.cli manage --execute --watch
```

The watch interval is M1-aware: 5s gives a 12× safety margin against the
worst-case 60s TP1-touch-to-reversal window. Under 2s emits a soft warning;
under 1s is rejected outright. Use `--no-clear` to disable the ANSI
clear-screen between iterations if your terminal doesn't render it.

### 5. Auto mode — continuous live trading

```powershell
python -m xauusd_trading.cli auto `
    --signals signals.txt `
    --positions-json positions.json
```

Loops forever. Each iteration: reconcile → render dashboard → manage
tracked positions → re-read `signals.txt` → for each new signal not already
tracked, run `decide` and act on the result (skip / partial FOLLOW / full
FOLLOW). Exits on Ctrl+C. Same `--watch-interval` and `--no-clear` flags
as `manage`.

**Recommended live setup**: two PowerShell windows — window 1 runs the
Telegram listener (below), window 2 runs `auto`. Both leave persistent
output; both exit on Ctrl+C.

### 6. Telegram listener — auto-ingest Victor signals

```powershell
python listener\telegram_listener.py
```

Watches the VICTOR Telegram channel and appends new signals to
`signals.txt` as they're posted. First-run setup (creating an API app at
my.telegram.org, finding your channel id, etc.) is in `docs/MT5_SETUP.md`'s
sibling section — see also the inline comments in
`listener/telegram_listener.py` for the corrections workflow via
Saved Messages.

The listener and the engine never share in-memory state — they communicate
only through `signals.txt` (atomic writes from the listener, polling reads
from `auto`). Either side can be stopped or replaced without coordinated
change to the other.

### 7. Quick MT5 diagnostic

```powershell
python -m xauusd_trading.cli mt5-info --mt5-symbol XAUUSD
```

Prints the latest M1 bar, your account equity, and any open MT5
positions/pending orders for the symbol. Use this to verify MT5 is
connected, your symbol name is right, and the broker timezone offset is
correct (the bar time should match what you see in MT5's chart window).

### 8. Bulk fetch M1 history

```powershell
python -m xauusd_trading.cli fetch --mt5-symbol XAUUSD
```

Pulls the last 2 months of M1 from MT5 into per-month CSVs at
`data\XAUUSD_M1_YYYYMM.csv`. Useful as a daily Task Scheduler job — MT5
only retains ~103 days of M1 history per broker, so accumulating your own
archive over time gives you backtest data that goes further back than MT5
itself remembers. Bars merge with existing files; nothing is overwritten.

## Engine gates: SKIP_EXPIRED, SKIP_INVALIDATED, partial FOLLOW

`decide()` runs two pre-flight gates per signal before reaching the "place
all orders" path:

- **`SKIP_EXPIRED`** — `now >= pending_expires_at`. The signal's 4-hour
  pending window has already passed. No orders placed.
- **`SKIP_INVALIDATED`** — backtest replay from `activation_time` to `now`
  shows **every** entry has reached a terminal status (SL, LOCK_TP1,
  TP1/2/3, TIME_EXIT, NO_FILL). Nothing left to place; orders would diverge
  from the backtest path. The execution log includes a per-entry breakdown.
- **`FOLLOW` (full)** — every entry is still PENDING or OPEN; all planned
  orders are placed.
- **`FOLLOW` (partial)** — mix of terminal and still-PENDING/OPEN entries.
  Only the placeable entries are sent to MT5; terminal entries are filtered
  out. The execution log surfaces the per-entry replay breakdown.

**The rule, stated bluntly:** place an entry if and only if its backtest
replay status is `PENDING` or `OPEN`. The replay is the authoritative source
for which entries should reach MT5.

None of these gates is a cross-signal overlay. They're per-signal
divergence-correction mechanisms — they don't change which signals the
backtest would fill, only how live placement aligns with the backtest path
when `decide` runs late.

## Re-entry protection (2 layers)

`decide --execute` and `auto` are idempotent by design. Two guards prevent
the same signal from being placed twice within a session:

1. **`positions.json` membership** (in `cli.py`) — if the signal is
   already tracked, it's managed (not re-placed).
2. **Live MT5 footprint** (`find_orders` / `find_positions` in
   `Mt5Executor.place_signal`) — if MT5 still has open orders or positions
   tagged with this signal's magic, skip.

The engine's per-entry replay filter (above) supersedes any history-based
guard: entries whose replay status is terminal never reach `place_signal`
in the first place. The two guards above check current live state, not
history, so they're orthogonal to the replay decision.

If the backtest replay is wrong (rare — same-minute fills or positive
slippage that the bar replay missed) and MT5 has deals the replay thinks
didn't happen, `reconcile_with_mt5` patches the engine's state from MT5
reality on each cycle. See below.

## Reconciliation (`reconcile_with_mt5`)

Runs on every `decide`, `manage`, and `auto` cycle that has MT5 access.
When the bar-by-bar replay misses an MT5 fill (typically a same-minute
fill at placement, or positive slippage), MT5 has positions the engine
still thinks are PENDING. Reconciliation queries MT5 directly, patches
those entries to OPEN using MT5's actual fill price, lot, and time, then
re-advances the position through chart bars from the earliest patched fill
so stage transitions catch up.

The engine's `initial_sl` is left unchanged so the broker-side SL still
matches the planned stop distance — positive slippage on entry becomes
better R:R, not a wider stop.

## Late TP1 catch-up

If the engine's replay shows any entry in status `LOCK_TP1` but MT5 has
positions whose SL is NOT yet at TP1 (because an earlier `manage` cycle
didn't fire in the window between TP1 touch and the next return-to-TP1),
those positions are closed at market on the next cycle via
`TRADE_ACTION_DEAL`. Positions whose SL is already at TP1 are left alone —
the broker triggers them naturally when price returns to TP1, matching
backtest's LOCK_TP1 exit precisely.

The execution log records:

```
Late TP1 catch-up closed #{ticket} @ {price} ({signal_key};
backtest LOCK_TP1 would have realized ${expected_pnl} --
actual close at current market)
```

## Position management — `positions.json`

The engine uses a JSON file to remember which signals are currently in
flight:

```json
[
  {
    "signal_key": "2026-05-07#01",
    "signal": "1. BUY XAUUSD 4717 - 4715 SL 4710 TP1 4725 TP2 4735 TP3 4750 6:24 PM",
    "date": "2026-05-07",
    "tz": 7,
    "equity_at_open": 50000.0,
    "executed_at": "2026-05-07T15:24:18"
  }
]
```

Under `--execute` or `auto`, the engine adds new signals on successful
placement and auto-prunes entries whose MT5 footprint is gone (closed by
TP, SL, time-exit, Late TP1 catch-up, or manually). Under preview mode,
it just reads.

`executed_at` is the wall-clock placement time (chart tz, GMT+3). When
present, `manage` and `auto` show a dual-view replay: an *ideal* execution
replay (from `activation_time`, matching backtest semantics) and an
*actual* execution replay (from `executed_at`, matching MT5 reality), with
a "lateness cost so far" summary line. MT5 actions are driven by the
*actual* replay; the ideal view is diagnostic only.

## Strategy overrides

Both `backtest` and `decide` accept these flags to test alternative
configurations without editing `core/config.py`. Defaults match v2.

```powershell
--initial-capital 1000.0       # starting equity for backtest sizing
--risk 0.05                    # fraction of equity to risk per signal
--entries 3                    # number of entry slots (>= 1, no hard cap)
--entry-ladder range_to_sl     # range_to_sl | range_uniform
--entry-sl-gap 2.0             # $ between deepest entry and signal SL
```

For exhaustive parameter exploration use `tools/sweep.py`, not overrides
on the CLI — it runs many configs in parallel and produces a ranked CSV.
Don't change `core/config.py` defaults without re-running the sweep and
re-locking the smoke test.

## CSV-only mode (no MT5)

If MT5 isn't available (Linux, Mac, no terminal installed, paper-trading
on your laptop), use `--charts` instead of `--mt5` for `decide`:

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

For first-time MT5 setup, see **`docs/MT5_SETUP.md`**.

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

# recommendation.new_signal.action is "FOLLOW" / "SKIP_EXPIRED" / "SKIP_INVALIDATED"
```

## What this engine does NOT do

By design, the engine adds **no** cross-signal overlay logic — no skip,
switch direction, hedge, or take-profit-early-because-new-signal-arrived.
Those decisions are not part of the strategy that produced the validated
result. Adding them now without re-validating against the historical
dataset would risk degrading the outcome.

`SKIP_EXPIRED`, `SKIP_INVALIDATED`, partial FOLLOW, the 2-layer re-entry
guards, the Late TP1 catch-up, and `reconcile_with_mt5` are **not**
overlays — they're per-signal idempotency / divergence-correction
mechanisms that change nothing about which signals the strategy would
fill in backtest.

If you want to add real overlay rules later, they belong in a separate
layer that wraps `decide()`, not inside the engine itself, and they should
be re-validated end-to-end against the historical dataset before going
live.

The engine also does **not** auto-detect open positions from MT5 alone.
MT5 stores ticket / open price / SL / TP / comment / magic, but not the
original signal's TP1/TP2/TP3 range and issue time — which the engine
needs. `positions.json` is the canonical source of truth for active
signals; `mt5-info` is a sanity check, not a substitute.

## Smoke test

Run after any change in `xauusd_trading\` to verify the engine still
matches the locked v2 baseline:

```powershell
python -m pytest tests\
```

The smoke test asserts exact equity, win/loss/no-fill counts, and win
rate on April 2026 only. If it fails, the engine has drifted — either
revert the change or re-validate by running the full sweep and locking
new numbers.

## Re-tuning the strategy

Use `tools/sweep.py` to explore configurations:

```powershell
python tools\sweep.py --signals signals.txt --charts data\XAUUSD_M1_*.csv `
    --output sweep_results.csv
```

Every config runs through the same `advance_one_bar` simulator the smoke
test locks, so all results are honest (strict-touch arming, same-bar
worst-case stop wins, spread-aware triggers — no lookahead). See
`python tools\sweep.py --help` for the full set of axes you can vary.

## Common gotchas

- **`--execute` and `auto` write real orders to MT5.** There is no
  confirmation prompt. Test with the dry-run version first.
- **Backtest equity ≠ live equity.** The same engine on the original
  validation CSVs gives different numbers from your broker's CSVs because
  tick prices differ. Both are correct outputs; broker data is what
  predicts live performance.
- **Backtest doesn't model overlapping positions.** Signals run
  sequentially in historical replay; live, 3–5 concurrent is normal.
  Risk per signal × N concurrent = effective combined risk; the backtest's
  -42.8% max DD doesn't fully model this.
- **PowerShell does not expand glob wildcards.** The CLI expands them
  itself, so `data\XAUUSD_M1_*.csv` works.
- **MT5 only retains ~103 days of M1 per broker.** Run `fetch` daily (or
  let `decide --mt5` / `manage` / `auto` do it) to accumulate an archive
  past that window.
- **`MetaTrader5` package is Windows-only.** On macOS or Linux, only the
  CSV-mode flows (`backtest`, `decide --charts`) work; `--mt5`, `fetch`,
  `mt5-info`, `manage`, `auto`, and `--execute` raise an import error.
- **Late TP1 catch-up doesn't exactly tie to backtest's LOCK_TP1.**
  Backtest models the lock SL triggering at exactly TP1; the catch-up
  closes at current market price (the realistic divergence cost).
- **Engine view of "Realized" can mismatch MT5 right before a catch-up
  fires.** When actual replay declares entries `LOCK_TP1` (terminal) but
  MT5 still has them open, the engine shows backtest figures, not what
  MT5 has actually realized yet.
- **Watch mode + Task Scheduler racing.** Pick one cadence mechanism per
  session for `manage --execute` / `auto`.
- **ANSI clear-screen requires a VT-aware terminal.** Watch mode's
  default clear-screen uses `\x1b[H\x1b[J`. Pass `--no-clear` if literal
  characters show up.
- **Listener state file is precious.** `telegram_state.json` makes Layer 1
  dedup work. Don't delete it casually. If you do, Layer 2 (content
  dedup) still catches duplicates, but `catch_up` will log a "matched
  existing entry by content" line for every previously-seen signal.
- **`xauusd_listener.session` and `listener_config.json` are
  credentials.** `.gitignore` them; don't sync via cloud drives.

## License

Private project. Not for redistribution.