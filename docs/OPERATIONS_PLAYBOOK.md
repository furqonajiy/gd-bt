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
  off by hours, your `--mt5-server-offset` is wrong. Keep it at the
  default **3** year-round: with `--mt5-server-offset 3` the broker's
  EET/EEST server clock is stored verbatim (shift 0). Do **not** drop it to
  2 in winter — that adds an hour and corrupts timestamps. (To rebuild the
  M1 archive from 2020, use the standalone `cli_resync_m1_from_2020.txt`
  at the repo root: `fetch --months 80`.)
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

**Trailing-open stop with `--shared-sl`.** The SL on each trailing-open leg is
anchored at that leg's planned distance to the shared level, measured from the
**actual trigger/fill** — not the frozen shared price. A deep dive can fill a
BUY leg below the shared level, where the shared price would sit *above* the
fill (an illegal stop MT5 rejects, and a phantom instant-exit in the backtest);
anchoring from the fill keeps the stop correctly below it and makes the
backtest match what the executor places. The pending-STOP trail uses the same
rule, so trailing a stop past the shared level no longer floods
`Invalid stops` rejections.

**STOP-reject market fallback.** Between the cycle tick and `order_send` the
market can cross the trigger, making the STOP invalid (a BUY STOP must sit
above Ask) — the broker rejects it. The virtual trailing entry has already
fired at that point (the backtest fills it at the trigger), so the executor
re-reads the tick and, **only when it confirms the trigger was passed**
(BUY: Ask ≥ trigger / SELL: Bid ≤ trigger), opens the leg **at market** with
the planned stop distance anchored on the actual fill. Any other rejection
keeps the all-or-nothing failure path — the executor never market-fills below
the trigger, because that would open a trade the model never had.

Protective trailing stop is also owned by the executor, not MT5's terminal
native trailing feature. Each manage/Auto cycle recomputes the engine stop and
moves MT5 SL with `TRADE_ACTION_SLTP` when needed. Leave MT5's right-click
**Trailing Stop** option **OFF** for these positions. If native trailing is on,
the terminal and executor can fight over the SL, and the backtest models only the
executor trail. (Native trailing is not an option anyway: it is a
terminal-client feature the MetaTrader5 Python API cannot set, and it dies the
moment the terminal disconnects.) To cut broker traffic on dense trailing
configs, `--trailing-close-min-step N` sends the SLTP modify only once the
recomputed stop improves on the broker's current SL by at least `N` price
units (the first protective set always goes out; 0 = send every improvement).
The engine still trails continuously, so the live SL can lag the modeled stop
by up to `N`.

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
uses GMT+7, fixed Jakarta, no DST). The engine converts the signal time to
chart time (EET/EEST: +2 winter / +3 summer, EU DST rule) internally and
DST-aware via `core/chart_tz.py` — so a GMT+7 signal shifts by −4h in summer
but −5h in winter.

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
   the Late TP1/TP2 lock catch-up where needed (protective stop at the lock
   level, market close only as last resort), locks SL to TP1 on stage-1
   positions, time-closes positions past the 90-min deadline, and applies
   executor-owned trailing-close/trend-runner SL moves when enabled
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

If you forget and a `LOCK_TP1` is missed, the Late TP1 catch-up now
**protects the leg instead of flattening it**: the SL is moved to TP1 when
price is still beyond it (the broker then exits at the modeled level, or the
leg keeps running and beats the model); if price has already come back
through TP1, the stop locks 0.5 below/above the live price and later cycles
ratchet it toward TP1 as price recovers. The leg is only closed at current
market as a last resort, when no broker-legal stop exists or the modify is
rejected. (Before 2026-06: the catch-up always closed at market — the
2026-06-12 reconciliation measured −$468 vs model from that on one session.)

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

Every new and edited message is first run through the logic-only typo
fixer (`apply_signal_corrections`): a stop on the wrong side, an
out-of-order TP, an extra-zero / wrong-hundreds price, **and a
directionally-valid but implausibly-far SL** (a wrong-hundreds mistype,
e.g. BUY `4319-4321 SL 4214` → `4314`) are repaired before the line is
written. It never tightens a stop to improve risk:reward; a far stop it
can't cleanly repair is left as posted (and you're notified).

The feed follows the channel's latest state: when VICTOR edits a signal
the line is amended in place (same `N.`, same signal_key/magic) and an
`amend` record is appended to `signal_overrides.jsonl`; when he deletes
one the line is removed and a `revoke` record is appended. When the Victor
provider-filter is run (`tools/live_provider_signal_filter.py --watch`, as
in `cli_champion_victor.txt`), it regenerates the filtered live feed
(`generated/victor_live.txt`) from the raw feed on every change. Startup
catch-up applies the same reconciliation to the
last 24 h, so edits/deletions made while the listener was down are not
lost (see "Listener was down" below for longer gaps).

To have the live executor *act* on those edits/deletes, run `auto` with
**`--apply-signal-edits`** (see Step 2). Each cycle it consumes the
journal and, matched by the tagged magic: on an **edit** flattens the
signal (cancels its pending orders and closes any open position) and
re-places it at the corrected levels — **close-and-reopen**; on a
**delete** flattens and untracks it. A byte-offset sidecar makes it
exactly-once and anchors at end-of-file on first run, so the pre-existing
backlog (already reflected in the feed) is never replayed onto live
orders.

### Step 2 — start auto

In window 2:

```powershell
python -m xauusd_trading.cli auto `
    --signals signals.txt `
    --positions-json positions.json `
    --apply-signal-edits
```

Each iteration: reconcile → render dashboard → manage tracked positions →
**apply any provider edits/deletes** (with `--apply-signal-edits`) →
re-read `signals.txt` → for each new signal not already tracked, run
`decide` and act on the result. Exits only on Ctrl+C. Add
`--apply-signal-edits` only on the executor whose feed the Telegram
listener writes (it reads `signal_overrides.jsonl`); leave it off for a
self-feed scalper that has no listener.

Default `--watch-interval` is 5 seconds. Under 2s emits a warning; under
1s is rejected. 5s gives a 12× safety margin against the worst-case 60s
TP1-touch-to-reversal window on M1.

### Running two executors on one MT5 account

You can run two `auto` processes against the same account — e.g. the Victor
feed and a self-feed scalper — without them stepping on each other. Give each a
distinct **`--strategy-tag`** (e.g. `VIC` and `SC24`) and its own
**`--positions-json`**:

```powershell
# window A — Victor
python tools/auto_explicit.py --signals generated/live_provider_all.txt `
    --positions-json positions_victor.json --strategy-tag VIC  ... (Victor strategy flags)
# window B — scalper
python tools/auto_explicit.py --signals generated/self_scalper24_live.txt `
    --positions-json positions_scalper.json --strategy-tag SC24 ... (scalper strategy flags)
```

How isolation works: the tag is stamped onto every `signal_key`, and the
**magic number** — `signal_to_magic(signal_key)`, the order's true identity — is
hashed from that key, so the two executors get **disjoint magics**. Each one only
ever queries/manages its own magics (`find_orders(magic)`), so it physically
can't touch the other's orders. That is also how either executor knows which
BUY/SELL LIMIT belongs to which signal — by magic, not by reading the comment.

In the MT5 terminal you tell them apart by the order **comment**, which reads
`[TAG-]MMDD#DD.N` — tag, month-day, signal-of-day, entry — e.g. `VIC-0615#05.2`
vs `SC24-0615#05.2`. Only the year is dropped (it's in the magic + open time) so
the comment survives the broker's truncation (Elev8 cuts near 16 chars). The
tag is **capped at 4 chars**; a longer one keeps its first 4. The tag is
live-only — backtests run untagged, so parity is unaffected.

### Regime auto-switch (`--adaptive`)

Pass `--adaptive` (and `--champions-dir <dir>` if your `CHAMPION_<regime>.json`
files live somewhere other than `sweep_regime_out_grid/`) to let `auto` pick the
strategy by **volatility regime**. Each cycle it classifies the current market
(`xauusd_trading.strategy.regime` — smoothed M15 ATR + trend → R1quiet / R2bull /
R3strong / R4parab) from a trailing window of M1 (`--adaptive-window-days`,
default 20) and runs that regime's **published champion** config; when no
champion exists for the detected regime it **falls back to the CLI/incumbent
config** you passed, so you're never left without a config. A switch is logged
once per change (`[adaptive] regime=… → champion …`). The detected regime governs
the **whole cycle** — both new placements and management of currently-tracked
positions — so when the regime flips, open positions are managed under the new
regime's champion. Default off; without the flag `auto` runs the explicit config
exactly as before. Detection never raises: if the chart can't be read it keeps
the incumbent and the cycle continues. (`python tools/regime_auto.py` is the
one-shot advisory version — print the detected regime + its champion CLI without
running the loop.)

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

### Accidentally cancelled a signal's pending limit orders

If a signal has filled at least one entry and you delete its remaining pending
LIMITs by hand, the engine will **not** re-place them by default: once the
signal's magic has any MT5 footprint, placement is skipped (it manages instead).

Run `auto` with `--replace-missing-entries true` (or `--replace-missing-entries`
on `python -m xauusd_trading.cli auto`) to self-heal: each cycle it re-places any
entry still **PENDING** in the replay (price hasn't reached it, window still
open) whose per-entry comment is missing from MT5. It only acts when the signal
still has ≥1 footprint on MT5, never re-places an entry price has already passed
(no chasing), and is LIMIT-only (skips when trailing-open is enabled).

### Manually closed a position the strategy still holds

With `--reopen-missing-positions true`, MT5 mirrors the replay: any entry the
backtest engine still considers **OPEN** that has no live position (e.g. you
closed it by hand to thin out exposure) is restored on the next cycle — same
per-entry comment, the replay's lot, its current effective stop (clamped to a
broker-legal level), and the leg's target. Restoration is **price-aware, per
leg**: at market when the current price is at-or-better than that leg's entry
(BUY: ask ≤ entry; SELL: bid ≥ entry) or when its stop is already locked
at/beyond the entry (in-profit protection mode); otherwise a LIMIT at the
leg's original entry inside the pending window — it never chases a price
that ran away. Re-opening stops on
its own once the replay exits the leg. While this flag is on, a signal whose
replay still holds OPEN legs also survives the registry prune even with zero
MT5 footprint, so a hand-closed signal can't vanish before it is restored.

**Churn guard.** A leg whose live position closed in the **last ~3 minutes**
(its SL / TP1-lock / TP fired intrabar) is **not** re-opened. The replay only
advances on *closed* M1 bars, so for up to a bar after an intrabar live close it
still shows the leg OPEN — without this guard `auto` would resurrect a just-locked
or just-stopped leg, which then immediately closes again (the re-open → close →
re-open churn). The cooldown lets the replay register the close and stop asking.
A genuine *early* hand-close that the replay still holds OPEN after the cooldown
is restored normally — so the only cost is a few minutes' delay before an
intentionally-closed leg comes back.
If you truly want out of a position early, close it AND remove the feed line
(or stop the runner) — otherwise the engine will put it back.

**Reopen mode also handles partially played-out signals.** If you start the
executor mid-life and a signal's replay has already closed some legs, `auto`
no longer skips the whole signal: it places the still-PENDING legs as fresh
LIMITs now and tracks the signal on its replay-OPEN legs, so those are re-opened
by the rule above on the next cycle. (Without `--reopen-missing-positions` the
legacy behaviour stands — a partial signal is skipped wholesale, because the
registry is signal-level and there is no per-entry healing to mirror it.)

Separately from this flag, `auto` never re-places a signal whose magic
already has **deal history** in MT5: a finished signal can't be accidentally
traded twice (the 2026-06-12 `#10` double-trade), even if its registry entry
was pruned.

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

### Listener was down — backfill days from a Telegram export

Short outages need no manual work: on startup the listener's catch-up
not only ingests messages that arrived while it was down, it also
reconciles the lookback window (24 h) against the channel's current
state — a tracked signal VICTOR edited is amended in the feed (and an
`amend` journalled), and a tracked signal he deleted is removed (and a
`revoke` journalled), exactly like the live edit/delete events. With
`auto --apply-signal-edits` running, the executor then flattens and
(for an edit) re-places those signals at the corrected levels on its
next cycle.

For longer gaps, export the channel from Telegram Desktop (HTML format)
and sync the feed from the export:

```bash
python tools/telegram_export_to_signals.py "ChatExport_*/messages*.html" --merge-into victor_signals.txt
```

It runs the listener's own parse → typo-correction → dedup → rendering
pipeline over every 🥇 message in the export, so a backfilled section is
exactly what the listener would have appended live. `--merge-into`
brings the feed to the channel's latest state for the exported days:
each covered date section is replaced wholesale (VICTOR's edits applied
in their final form, signals he deleted dropped), other days stay
untouched, and re-running with the same export is a no-op. Use `--out
backfill.txt` instead to inspect the sections before touching the feed.
Any 🥇 message that still fails to parse is reported on stderr — handle
those like a quarantine entry, by hand.

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
- **Late TP1/TP2 catch-up locks, it no longer flattens.** Backtest models
  the lock SL triggering exactly at the lock level; when live is late, the
  catch-up now places/ratchets a protective stop toward that level (market
  close only as a last resort), so the realized exit can differ from the
  model by the recovery path — but a missed lock can no longer turn a
  modeled profit into a stop-loss.
- **Executor trailing stop is interval-based.** SL can lag the backtest by up
  to one closed M1 bar plus the Auto/manage watch interval, plus slippage.
