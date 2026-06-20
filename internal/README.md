# XAUUSD signal backtester & live MT5 executor

A Python engine that turns Victor-style XAUUSD (gold) text signals into a
fully-modeled trade lifecycle — entry laddering, stop/target management,
SL-to-TP locking, time-exit, and optional trailing entry/exit — and runs it
two ways from the **same engine and the same numbers**:

- **Backtest** against historical M1 CSV charts.
- **Live** against a running MetaTrader 5 terminal (read-only diagnostics, or
  real order placement with `--execute` / `auto`).

A signal looks like:

```
1. BUY XAUUSD 4543 - 4541 SL 4536 TP1 4551 TP2 4561 TP3 4576 2:02 PM
```

> This overview lives under `internal/` (not the repo root) on purpose, so it
> isn't auto-rendered on the public landing page. Doc links below are relative to
> the repo root (`../`).

## Repository layout

| Path | What it is |
|------|------------|
| `trading/engine/` | The shared, pair-agnostic engine. Signal parsing, position lifecycle, backtest, MT5 adapter, executor, CLI. Import from its root: `from trading.engine import X`. |
| `trading/engine/cli.py` | CLI entry point: `python -m trading.engine.cli <subcommand>` (also reachable as `python -m trading.xauusd.cli`). |
| `trading/engine/strategy/regime.py` | Volatility-regime detector (smoothed M15 ATR + trend → R1quiet/R2bull/R3strong/R4parab); labels report months and drives `auto --adaptive`. |
| `trading/xauusd/` | XAUUSD pair package — a thin facade that re-exports `trading.engine` and provides the `trading.xauusd.cli` entry. |
| `trading/btcusd/` | BTC pair package: a self-rejection backtest runner that imports the shared `trading.engine`. |
| `tools/` | Research/ops scripts: parameter sweeps, signal generators, explicit-config live/backtest runners, `live_feed_loop.py` (live self-signal feed loop), forensic dumper, tick tooling, `reconcile_report_html.py` (live↔backtest reconcile), `regime_granularity_assessment.py` / `regime_split_validation.py`. |
| `tools/generate_scalper_signals.py` | The self-feed (scalper24) generator. Optional entry filters: RSI, Bollinger (%B + bandwidth squeeze), R:R geometry (`--rr1/2/3`), Support/Resistance (prior-day H/L + round levels), and Supply/Demand (Rally-Base-Rally / Drop-Base-Drop zones, `--sd-mode rbr_dbd`). |
| `listeners/` | Per-platform signal-source listeners (one subfolder per source: `telegram/`, future `whatsapp/`, …). `listeners/telegram/listener.py` — ingests Victor's Telegram channel into `signals.txt` (override with `--signals-file`, e.g. `victor_signals.txt`). New and edited messages pass a logic-only typo fixer first (wrong-side SL/TP, TP order, extra-zero / wrong-hundreds, and a directionally-valid but implausibly-far SL like `4214`→`4314`). The feed tracks the channel's latest state: edits amend the line in place, deletions remove it (each journalled to `signal_overrides.jsonl`), and startup catch-up reconciles changes made while the listener was down. `auto --apply-signal-edits` consumes that journal so the live executor follows the corrected feed (edit = flatten + re-place corrected; delete = flatten + untrack). |
| `champions/` | `CHAMPION_<regime>.json` — the deployable config per volatility regime, read by `auto --adaptive`. |
| `tests/` | `pytest` suite (live/backtest parity, reconcile, sizing, listener, etc.). |
| `docs/` | Setup and operations guides (see below). |

## Deployed champions (per regime)

Champions live in `champions/CHAMPION_<regime>.json` and are picked by volatility
regime. Current state:

| Regime | Champion | Notes |
|--------|----------|-------|
| **R4parab** | **`rsi75_sqz6_rr40`** (tag **SQZ6**) | e8 / range_to_sl / slm2.1 / max_hold 240 / tp1_lock_delay 24 / lock_after_tp2, on a triple-filtered feed (RSI 75/25 + Bollinger squeeze `bw≥0.0006` + R:R 1.0/2.0/4.0). edge $63,940 / OOS $11,633 / DD 38.4%. |
| **R3strong / R2bull** | **`SC24T24E8`** | SC24 + `entry_count 8`, `tp1_lock_delay 24`; #1 on edge AND OOS in both. |
| **R1quiet** | **SC24** (seeded) | until the sweep advances to it. |

A regime-determination assessment — including why the absolute-ATR metric is
price-biased and why the deployed champion is volatility-scale-invariant (so finer
regimes do **not** change champion selection) — is in
[`../docs/REGIME_ASSESSMENT.md`](../docs/REGIME_ASSESSMENT.md).

## Install

Requires Python 3.10+ (the code uses `from __future__ import annotations`
and PEP 604 `X | Y` typing).

```bash
pip install -r requirements.txt   # pandas, openpyxl, pytest
```

For **live MT5 use**, additionally install the Windows-only MT5 package:

```powershell
pip install MetaTrader5
```

See [`../docs/MT5_SETUP.md`](../docs/MT5_SETUP.md) for the full live-data setup.

## Quick start — backtest

```bash
python -m trading.engine.cli backtest \
  --signals victor_signals.txt \
  --charts "data/XAUUSD_M1_*.csv"
```

Writes an Excel report (`backtest_results*.xlsx`) with three sheets:

- **Summary** — config, overall stats, entry-outcome counts (entries
  skipped / filled / TP / SL), realized risk:reward shown as `1:N` ratios,
  and the monthly breakdown (each month carries a **Regime** column).
- **Daily Breakdown** — one row per traded day (pre-start padding excluded),
  with per-entry outcome counts and realized R per day.
- **Per-Entry Detail** — one row per Entry slot, split into **ORIGINAL**
  (the signal as written) vs **EXECUTED** (backtest result) column groups.

Charts are M1 OHLC CSVs in the broker's **Eastern European (EET/EEST)** chart
timezone — UTC+2 winter / UTC+3 summer, EU rule (`core/chart_tz.py`). To compare
a backtest against what really filled on MT5, overlay a native MT5 history
export with the explicit runner's `--mt5-history FILE` (see
[`../docs/MT5_SETUP.md`](../docs/MT5_SETUP.md)), or reconcile an exported MT5
Trade History HTML with `tools/reconcile_report_html.py`.

## Quick start — decide on one signal

```bash
python -m trading.engine.cli decide \
  --signal "1. BUY XAUUSD 4543 - 4541 SL 4536 TP1 4551 TP2 4561 TP3 4576 2:02 PM" \
  --signal-date 2026-05-07 --signal-tz 7 \
  --charts "data/XAUUSD_M1_*.csv"
```

Swap `--charts ...` for `--mt5 --equity-from-mt5` to run against live MT5
data instead of CSV. Add `--execute` to actually place the orders (it
implies `--mt5` and live equity — no confirmation prompt).

## CLI subcommands

`python -m trading.engine.cli <subcommand>` (prog name `xauusd`):

| Subcommand | Purpose |
|------------|---------|
| `backtest` | Run a historical backtest over signals + M1 charts; writes an Excel report. |
| `decide`   | Evaluate one signal. With `--execute`, place and manage orders on MT5. |
| `manage`   | Manage tracked signals: lock SL to TP1, cancel expired pendings, time-close. `--watch` loops. |
| `auto`     | Continuous live trading: read `signals.txt`, place orders, manage positions, append-only event log. `--replace-missing-entries` self-heals limit orders cancelled by hand; `--reopen-missing-positions` restores positions closed by hand while the replay still holds them OPEN (price-aware: market only at-or-better than the entry, else a LIMIT at the original entry — never chases) **and** places partially played-out signals per entry instead of skipping them. `--adaptive` auto-switches by **volatility regime**: each cycle it classifies the current market and runs that regime's published champion config (`CHAMPION_<regime>.json`), falling back to the incumbent when none exists. |
| `mt5-info` | Diagnostic: latest bar, account equity, open MT5 positions/orders for the symbol. |
| `fetch`    | Pull the last N months of M1 history into `data/` (per-month CSV archive; `--months`, default 2 — live feed loops use 1). Keep `--mt5-server-offset 3` year-round so the broker's EET/EEST server clock is stored verbatim (shift 0); do **not** drop to 2 in winter (that adds an hour and corrupts timestamps). To rebuild the M1 archive from 2020, see the standalone `cli/resync_m1_from_2020.txt` at the repo root (`fetch --months 80`). |

Common flag groups (added to most subcommands):

- **Strategy overrides:** `--initial-capital`, `--risk`, `--entries`,
  `--entry-ladder {range_uniform,range_to_sl}`, `--entry-sl-gap`.
- **Research toggles (default OFF):** `--trailing-open-distance`,
  `--trailing-close-distance`, `--trend-runner` (+ `--trend-runner-ema-fast`,
  `--trend-runner-ema-slow`, `--trend-runner-atr-period`,
  `--trend-runner-atr-multiplier`). Live trailing-open uses broker STOP
  orders re-checked every cycle; if the broker rejects a STOP because price
  crossed the trigger in the placement race, the executor confirms with a
  fresh tick and fills that leg at market (never below the trigger).
  Trailing-close is executor-owned Python (`TRADE_ACTION_SLTP`), not MT5
  native trailing (the API can't set it); the explicit runners'
  `--trailing-close-min-step` throttles how often the SL modify is sent.
- **Research strategy modes (explicit runners only, default OFF):** the full
  flag surface in `tools/backtest_explicit.py` / `tools/auto_explicit.py`
  adds `--shared-sl` (all entries share one stop anchored on entry #1, with
  per-leg risk sizing; a leg filled by trailing-open re-anchors its stop to
  the same planned distance taken from the actual fill, so a deep trailing
  fill never inherits a stop on the wrong side of the entry),
  `--entry-targets T1,T2,...` (per-entry targets from
  `{TP1,TP2,TP3,RUN}`, one per entry; `RUN` legs trail past
  `--runner-trail-from {TP1,TP2,TP3}` by `--trailing-close-distance`),
  `--bep-after-move` (per-leg break-even+ once a leg is N price units in
  favour), and `--sync-charts` (refetch M1 before a backtest, default on).
  Live sizing uses the real MT5 equity, so `--initial-capital` /
  `--bonus-per-closed-lot` / `--tp3-lock-target` are **optional** in
  `auto_explicit.py` (they only feed the startup banner / backtest reports).
- **MT5 connection:** `--mt5-symbol` (default `XAUUSD`),
  `--mt5-server-offset` (default `3`), `--mt5-history-bars` (default `5000`),
  `--mt5-path`, `--mt5-login`, `--mt5-password`, `--mt5-server`.
- **Notifications / forensics:** `--notifications` / `--no-notifications`,
  `--forensic-log` / `--no-forensic`.

For the full strategy parameter surface (max-hold, pending-expiry,
sl-multiplier, lock delays, profit-lock model, etc.) used in the
trailing-open research config, see the explicit runners
`tools/auto_explicit.py` and `tools/backtest_explicit.py`, documented in
[`../docs/demo_runbook_trailing_open.md`](../docs/demo_runbook_trailing_open.md).

## Default strategy config

Defaults live in `trading/engine/core/config.py` (`DEFAULT_CONFIG`) — the
validated DD40-compatible provider contract. Headlines:

| Setting | Default |
|---------|---------|
| `initial_capital` | `50000` |
| `sizing_mode` | `risk` |
| `risk_per_signal` | `0.05575` |
| `entry_count` | `3` |
| `entry_ladder` | `range_to_sl` |
| `activation_delay_minutes` | `3` |
| `pending_expiry_minutes` | `630` |
| `max_hold_minutes` | `90` |
| `sl_multiplier` | `1.61` |
| `final_target` | `TP3` |
| `lock_after_tp1` / `lock_after_tp2` | `True` / `False` |
| `trailing_open_distance` / `trailing_close_distance` | `0.0` / `0.0` (disabled) |
| `shared_sl` | `False` (per-entry stops) |
| `per_entry_targets` | `()` (single `final_target` for every leg) |
| `bep_after_move` | `0.0` (disabled) |
| `runner_trail_from` | `TP3` |
| locked-exit slippage (`lock_*_exit_slippage_points`) | `0.0` live (backtest/sweep model only) |

Trailing-open, trailing-close, trend-runner, shared-SL, per-entry-targets,
and break-even-after-move are **off by default** and are enabled explicitly
per run via CLI flags — they are deliberately not read from the environment,
so backtests are reproducible regardless of shell state.

## Live trading

`positions.json` is the registry of currently-tracked signals. Each entry is
`{"signal_key", "signal", "date", "tz", "equity_at_open", "executed_at"}`
(the last is optional). `--execute` / `auto` auto-prune entries whose MT5
footprint is gone.

Each order's **magic number** is the signal identity — `signal_to_magic(signal_key)`
hashes the full `signal_key` (tag + date + signal-of-day), and the executor
manages a signal by querying MT5 for that magic, so it always knows which
BUY/SELL LIMIT belongs to which signal. The order **comment** is the human label
plus per-entry key, rendered compact as **`[TAG-]MMDD#DD.N`** (e.g. `VIC-0615#05.2`)
so the tag, month-day, signal-of-day, and entry stay visible even on brokers that
truncate comments below MT5's 31-char cap.

To run **two auto executors on one MT5 account** (e.g. Victor + a self-feed
scalper), give each a distinct **`--strategy-tag`** (e.g. `VIC` vs `SQZ6`, capped
at 4 chars) and its own `--positions-json`. The tag is stamped onto `signal_key`,
so the two get disjoint magics + comments and never manage each other's orders.
It is live-only (empty in backtests, so parity holds).

**One distinct identity per strategy.** Every deployed strategy gets its OWN
names for *all four* artifacts — `--strategy-tag`, `--positions-json`, the
generated signal/feed `.txt`, and the backtest report (Excel) dir — all keyed off
the same short tag, so nothing collides and every file traces to one strategy at
a glance. The R4 champion is tag `SQZ6` → `positions_sqz6.json`,
`signals/sqz6.txt` / `signals/sqz6_live.txt`, `reports/SQZ6_2026xx`; Victor is
`VIC` → `positions_victor.json`, `signals/victor_live.txt`.

Two live modes:

- **Mode A — one-shot `decide --execute`:** manual, signal-by-signal.
- **Mode B — continuous `auto`:** run the Telegram listener + `auto`
  side-by-side, hands-off.

For **self-generated signals** (no Telegram), `tools/live_feed_loop.py` is the
live feed side: one process that refetches the current month and regenerates
the signal feed **only when a new closed M1 bar exists** (idle on weekends /
daily breaks), narrows to the most recent months and a rolling start window for
speed, and logs like `auto` — a header, then one `[ts] Add Signal N. ...` line
per new signal. Point `auto`'s `--signals` at its `*_live.txt` output:

```powershell
# window 1 — generate (rolls start + narrows charts internally; logs each new signal)
python tools/live_feed_loop.py --family scalper --interval 30 `
  --gen-start-days 3 --gen-recent-months 2 --mt5-symbol XAUUSD --mt5-server-offset 3 `
  -- --charts data/XAUUSD_M1_*_ELEV8.csv --output signals/sqz6_live.txt --session-start 0 --session-end 0 --signal-tz 7 --rsi-buy-max 75 --rsi-sell-min 25 --bb-bandwidth-min 0.0006 --rr1 1.0 --rr2 2.0 --rr3 4.0

# window 2 — execute that feed (auto_explicit.py with the strategy flags)
```

`--family` selects the generator (`scalper`/`risk02`/`canonical`/`better`/`zones`);
everything after `--` is passed verbatim to that generator, so the live feed is
byte-identical to the backtest archive for the same bars. The repo-root
`cli_*.txt` files are runnable deployment-command snapshots — the current R4
champion is `cli/champion_R4_SQZ6_no_trailing.txt`; Victor is `cli/champion_victor.txt`.

Full procedures, sanity checks, and failure recovery are in
[`../docs/OPERATIONS_PLAYBOOK.md`](../docs/OPERATIONS_PLAYBOOK.md). MT5 connection
setup is in [`../docs/MT5_SETUP.md`](../docs/MT5_SETUP.md).

## Tests

```bash
pytest
```

The suite includes live/backtest execution-parity tests that pin the engine
behavior the docs describe (SL-to-TP1 lock parity, reconcile, wall-clock
expiry, terminal catch-up, sizing, listener overrides).

## Documentation

- [`../docs/MT5_SETUP.md`](../docs/MT5_SETUP.md) — connect the engine to a live MT5 terminal.
- [`../docs/OPERATIONS_PLAYBOOK.md`](../docs/OPERATIONS_PLAYBOOK.md) — daily live-trading procedures (Modes A and B).
- [`../docs/BACKTEST_REALISM.md`](../docs/BACKTEST_REALISM.md) — what the backtest models to match live (slippage, spread, swap, min-stop) and what the user provides to calibrate it.
- [`../docs/SWEEP_RUNBOOK.md`](../docs/SWEEP_RUNBOOK.md) — parameter-sweep methodology (feed-filter combinations, per-regime realistic slippage).
- [`../docs/VICTOR_SWEEP_RUNBOOK.md`](../docs/VICTOR_SWEEP_RUNBOOK.md) — the Victor-feed, per-regime, signal-R:R/ATR-policy sweep.
- [`../docs/REGIME_ASSESSMENT.md`](../docs/REGIME_ASSESSMENT.md) — regime-determination assessment (price-normalized metric, scale-invariance verdict).
- [`../docs/demo_runbook_trailing_open.md`](../docs/demo_runbook_trailing_open.md) — demo parity protocol for the trailing-open candidate config.
- [`../CLAUDE.md`](../CLAUDE.md) — project instructions for Claude / Claude Code.
- [`../AGENTS.md`](../AGENTS.md) — project instructions for coding agents (mirrors `CLAUDE.md`).
