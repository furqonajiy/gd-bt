# Operations Playbook — daily live trading

Quick reference for running the v2 engine against a live MT5 account.
For full setup, see `MT5_SETUP.md`. For repo overview, see `README.md`.

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

## When a signal arrives

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

Set `--signal-date` to today's date. Set `--signal-tz` to whatever GMT
offset the signal time was given in (the channel above used GMT+7). The
engine converts to GMT+3 internally.

What you'll see in the report:

- **NEW SIGNAL** section: 3 LIMIT orders with computed entry prices, SL,
  lot size, dollar risk per entry. Total risk = 5% of current equity.
- **OPEN POSITIONS** section: status of each tracked signal — pending
  with deadline, filled with floating P&L, locked at TP1, etc.
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
2. Manages every existing tracked signal: cancels expired pendings, locks
   SL to TP1 on positions that touched TP1, time-closes positions past
   the 90-min deadline
3. Places 3 LIMIT orders for the new signal with SL and TP2 attached
4. Adds the new signal to `positions.json`, prunes closed ones

Real money moves. There is **no confirmation prompt** between you running
the command and orders being placed.

### Step 3 — confirm

Re-run `mt5-info` to verify the orders landed:

```powershell
python -m xauusd_trading.cli mt5-info --mt5-symbol XAUUSD
```

You should see 3 new pending orders (BUY_LIMIT or SELL_LIMIT) tagged with
the signal_key in their `comment` field, and `positions.json` should
contain the new entry.

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
  signal text to `positions.json` manually (see `sample_positions.json`
  for the shape).
- If MT5 orders are stale and should be cancelled, do it manually in MT5
  or wait for the engine's expiry handling (4-hour pending limit) to
  catch them on the next `--execute`.

### A signal closed but `positions.json` still has it

`--execute` auto-prunes entries whose MT5 magic has zero orders+positions.
It only runs the prune during execute, so if you've been preview-only,
the JSON might be stale. Run any `--execute` (even on a fresh signal) and
the cleanup happens.

To force a clean-up without a new signal, the simplest path is to delete
`positions.json` — the next session rebuilds it from new signals.

## Rules I stick to

1. **Always preview before execute.** A 5-second preview run prevents
   real-money mistakes. The cost is negligible.
2. **One signal at a time.** Don't run `--execute` for two signals in
   parallel from different terminals.
3. **Don't edit `config.py` to chase a bigger number.** If you want to
   change strategy parameters, run `sweep.py` first, lock the new
   `tests/test_smoke.py`, then deploy. Otherwise the engine drifts.
4. **Don't delete entries from `positions.json` while orders are still
   live on MT5.** That orphans the orders — they'll execute without engine
   management (no SL→TP1 lock, no time-exit). If you want out, close on
   MT5 first, then the next `--execute` auto-prunes.
5. **Drawdown tolerance is 50%.** If realized DD goes deeper than that,
   stop trading and re-evaluate. The v2 backtest peaked at -42.8% — going
   notably past that in live is a regime-change signal, not a "wait it
   out" signal.

## Known limits

- **Forward expectation: 2–10× per month**, not the headline backtest
  numbers. April–May 2026 backtest was an unusually favorable regime;
  Jan–March was much closer to 2× monthly.
- **Backtest equity ≠ live equity.** Different broker tick data produces
  different fills. Treat the headline figures as upper bounds, not
  predictions.
- **Win rate ~56%, not ~70%.** v2 has lower win rate than v1 because
  the tighter SL multiplier triggers more SLs. The math works because
  each SL is also smaller dollar-wise. Don't panic at consecutive losses.
- **MT5 only retains ~103 days of M1 history.** Run `fetch` daily (or
  let `decide --mt5` do it) to accumulate the archive. Without it,
  re-running historical backtests on broker data gets harder over time.
