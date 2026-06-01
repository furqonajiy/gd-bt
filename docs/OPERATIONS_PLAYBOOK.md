# Operations Playbook — daily live trading

Quick reference for running the v2 engine against a live MT5 account.
For full setup, see `MT5_SETUP.md`. For repo overview, see `../README.md`.

## Two ways to run live

The engine supports two live-trading modes. Pick one per session — don't
race them against each other on the same `positions.json`.

| Mode                      | Best for                                              | How                                              |
|---------------------------|-------------------------------------------------------|--------------------------------------------------|
| **One-shot `decide`**     | Manual signal-by-signal control                       | Run `decide --execute` per signal as it arrives  |
| **Continuous `auto`**     | Hands-off live trading with the Telegram listener     | Run listener + `auto` side-by-side, leave both up |

**Recommended live setup:** two PowerShell windows — window 1 runs the
listener (ingests Victor signals into `signals.txt`), window 2 runs `auto`
(reads `signals.txt`, places orders, manages positions). Both leave
persistent output; both exit on Ctrl+C.

## Before each session

```powershell
conda activate xauusd
```

Confirm MT5 is connected and your account looks right:

```powershell
python -m xauusd_trading.cli mt5-info --mt5-symbol XAUUSD
```

What to check in the output:

- **Latest bar time** matches what you see in MT5's chart window. If
  off by hours, your `--mt5-server-offset` is wrong (default 3, common
  alternatives 2 or 0).
- **Account equity** matches MT5's terminal equity to the cent. If not,
  MT5 is connected to a different account than you think.
- **Open positions/orders** match what's in `positions.json`. If MT5
  shows orders not in JSON, you have orphans — investigate before trading.

## Live trailing behavior — important

When `trailing_open_distance = 0`, live placement uses normal broker LIMIT
orders, subject to the stale/marketable-entry guards.

When `trailing_open_distance > 0`, live placement does **not** use LIMIT orders.
The executor uses broker STOP orders to model the virtual trailing entry:

- BUY trailing-open: after Ask moves at least the distance below the planned
  entry, the executor places/trails a `BUY_STOP` at `Ask + distance`.
- SELL trailing-open: after Bid moves at least the distance above the planned
  entry, the executor places/trails a `SELL_STOP` at `Bid - distance`.

Protective trailing stop is also owned by the executor, not MT5's terminal
native trailing feature. Each manage/Auto cycle recomputes the engine stop and
moves MT5 SL with `TRADE_ACTION_SLTP` when needed. Leave MT5's right-click
**Trailing Stop** option **OFF** for these positions. If native trailing is on,
the terminal and executor can fight over the SL, and the backtest models only the
executor trail.

Expected live gap: executor SL can lag the backtest by up to one closed M1 bar +
the watch interval, plus broker slippage/stop-level clamping.

If the engine sees an executor-owned SL changed externally on a cycle where it
did not issue a modify, it logs a warning:

```text
external SL change detected — is MT5 native trailing enabled?
```

The warning is passive. The executor does not fight the external change in that
cycle.

## Mode A — manual `decide` flow

### Step 1 — preview the order plan (no orders placed)

Copy the signal exactly as it appears, including the leading number:

```powershell
python -m xauusd_trading.cli decide `
    --signal "1. BUY XAUUSD 4717 - 4715 SL 4710 TP1 4725 TP2 4735 TP3 4750 6:24 PM" `
    --signal-date 2026-05-07 `
    --signal-tz 7 `
    --mt5 --equity-from-mt5 `
    --positions-json positions.json
```

Set `--signal-date` to today's date in the signal's source timezone. Set
`--signal-tz` to whatever GMT offset the signal time was given in (Victor
uses GMT+7). The engine converts to GMT+3 internally.

What you'll see in the report:

- **NEW SIGNAL** section with `Action:` — `FOLLOW`, `FOLLOW (partial)`,
  `SKIP_EXPIRED`, or `SKIP_INVALIDATED`. For `FOLLOW`, the planned entries
  show computed entry prices, SL, lot, dollar risk per entry. With
  `trailing_open_distance=0`, live sends LIMITs; with `trailing_open_distance>0`,
  live sends trailing STOP orders after the required beyond-entry move.
- **OPEN POSITIONS** section: status of each tracked signal — pending
  with deadline, filled with floating P&L, locked at TP1, etc. If any
  signal has a recorded `executed_at` and was placed meaningfully late,
  you'll see both the ideal-execution and actual-execution replays.
- **SUMMARY** section: equity + total realized + total floating.

If the plan looks wrong (lot size off, expected entries don't match),
stop and figure out why before executing. Common causes: wrong date,
wrong timezone, stale `positions.json`.

### Step 2 — execute on MT5

If the preview looks right, run the same command with `--execute` added
(and drop `--mt5 --equity-from-mt5`, since `--execute` implies them):

```powershell
python -m xauusd_trading.cli decide `
    --signal "1. BUY XAUUSD 4717 - 4715 SL 4710 TP1 4725 TP2 4735 TP3 4750 6:24 PM" `
    --signal-date 2026-05-07 `
    --signal-tz 7 `
    --execute `
    --positions-json positions.json
```

This:

1. Sanity-checks the account (equity > 0, market open, equity within 50%
   of expected — protects against running on the wrong account)
2. Reconciles each tracked signal with MT5 (patches PENDING entries the
   bar-replay missed)
3. Manages every existing tracked signal: cancels expired pendings, runs
   Late TP1 catch-up where needed, locks SL to TP1 on stage-1 positions,
   time-closes positions past the 90-min deadline, and applies executor-owned
   trailing-close/trend-runner SL moves when enabled
4. Places orders for the new signal — LIMITs only when trailing-open is off;
   STOP orders when trailing-open is enabled
5. Stamps `executed_at` on the registry entry and prunes any signals
   whose MT5 footprint is gone

Real money moves. There is **no confirmation prompt** between you running
the command and orders being placed.

### Step 3 — keep the SL-lock fresh

After placing a signal, the 90-min hold and the SL-to-TP1 lock are the
time-critical pieces. Either run `manage --watch` in another window,
or schedule `manage --execute` via Task Scheduler every minute:

```powershell
# manage.ps1 -- run every minute via Task Scheduler
$ErrorActionPreference = 'Continue'
Set-Location 'C:\path\to\your\repo'
& conda run -n xauusd python -m xauusd_trading.cli manage `
    --execute --positions-json positions.json `
    *>> manage.log
```

If you forget and a `LOCK_TP1` is missed, the Late TP1 catch-up closes
the position at the next cycle's market price. Better than nothing, but
worse than a clean broker-side trigger at TP1.

## Mode B — continuous `auto` flow

### Step 1 — start the listener

In window 1:

```powershell
python listener\telegram_listener.py
```

Leave it running. It auto-creates daily section headers in `signals.txt`
and appends each new Victor signal. Parse failures get quarantined to
`telegram_quarantine.txt` and posted as a ⚠️ to your Telegram Saved
Messages with a pre-filled correction template — edit + send back, and
the listener injects the correction.

### Step 2 — start auto

In window 2:

```powershell
python -m xauusd_trading.cli auto `
    --signals signals.txt `
    --positions-json positions.json
```

Each iteration: reconcile → render dashboard → manage tracked positions →
re-read `signals.txt` → for each new signal not already tracked, run
`decide` and act on the result. Exits only on Ctrl+C.

Default `--watch-interval` is 5 seconds. Under 2s emits a warning; under
1s is rejected. 5s gives a 12× safety margin against the worst-case 60s
TP1-touch-to-reversal window on M1.

## When something is wrong

### Sanity checks failed → orders NOT placed

The execute command prints `SANITY CHECKS FAILED` and a list of reasons.
Common ones:

- **No live tick** → market is closed (weekend, holiday, or your broker's
  off-hours). Wait for the market to open.
- **Equity differs by 50%+** → you're running on the wrong account, or
  your account has been credited/debited a lot. Verify the MT5 terminal
  is logged into the expected account before trying again.
- **Trading disabled** → MT5 has algorithmic trading off. Tools → Options
  → Expert Advisors → "Allow algorithmic trading".

The executor never half-places. Either everything got placed or nothing did.

### MT5 orders don't match `positions.json`

The execute command prints a `WARNINGS:` section listing each unknown
order/position. It still proceeds. To fix:

- If MT5 orders are real and you want to track them, add the original
  signal text to `positions.json` manually.
- If MT5 orders are stale and should be cancelled, do it manually in MT5
  or wait for the engine's expiry handling to catch them on the next
  `--execute` / `auto` cycle.

### External SL change warning

If you see `external SL change detected`, MT5's SL differs from the executor's
expected SL and the executor did not issue a modify in that cycle. Check whether
MT5 native trailing stop is enabled, another EA is managing the same symbol, or a
manual SL edit happened.

### A signal closed but `positions.json` still has it

`--execute` and `auto` auto-prune entries whose MT5 magic has zero
orders+positions. If you've only been doing previews, the JSON may be
stale — run any `--execute` (or any `auto` cycle) and the cleanup happens.

To force a clean-up without a new signal, the simplest path is to delete
`positions.json` — the next session rebuilds it from new signals.

### Listener says "matched existing entry by content"

Layer 2 (content-based) dedup fired because Layer 1 (state-based) had no
record. Normal after a `telegram_state.json` deletion. Harmless: no
duplicate ends up in `signals.txt`. If it's noisy across many messages,
let `catch_up` finish settling.

### Listener parse failure on a Victor message

Look at `telegram_quarantine.txt` for the raw text, and check Saved
Messages for the ⚠️ post with the correction template. Edit the template
into a canonical line, send it back, and the listener injects it as the
next free index in today's section. Engine never sees malformed lines.

## Rules I stick to

1. **Always preview before execute** in Mode A. A 5-second preview run
   prevents real-money mistakes.
2. **One mode at a time.** Don't run `auto` and manual `decide --execute`
   against the same `positions.json` in parallel. Don't run two `auto`
   loops at the same time.
3. **Do not enable MT5 native Trailing Stop** on engine-managed positions.
   The executor owns SL moves; native trailing creates SL fights and the
   backtest does not model it.
4. **Don't edit `core/config.py` to chase a bigger number.** If you want
   to change strategy parameters, run `tools/sweep.py` first, lock the
   new `tests/test_smoke.py`, then deploy. Otherwise the engine drifts.
5. **Don't delete entries from `positions.json` while orders are still
   live on MT5.** That orphans the orders — they'll execute without engine
   management (no SL→TP1 lock, no time-exit). If you want out, close on
   MT5 first, then the next `--execute` / `auto` cycle auto-prunes.
6. **Drawdown tolerance is 50%.** If realized DD goes deeper than that,
   stop trading and re-evaluate.
7. **Don't delete `telegram_state.json` while the listener is running.**
   It works, but you'll get a flurry of "matched by content" lines as
   `catch_up` rebuilds state.

## Known limits

- **Backtest equity ≠ live equity.** Different broker tick data produces
  different fills. Treat the headline figures as upper bounds, not predictions.
- **MT5 only retains ~103 days of M1 history.** Run `fetch` daily (or
  let `decide --mt5` / `manage` / `auto` do it) to accumulate the archive.
- **Late TP1 catch-up exits at current market, not at TP1.** Backtest
  models the lock SL triggering exactly at TP1; the catch-up closes at
  whatever the bid/ask is right now.
- **Executor trailing stop is interval-based.** SL can lag the backtest by up
  to one closed M1 bar plus the Auto/manage watch interval, plus slippage.
