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

## Repository layout

| Path | What it is |
|------|------------|
| `xauusd_trading/` | The engine. Signal parsing, position lifecycle, backtest, MT5 adapter, executor, CLI. |
| `xauusd_trading/cli.py` | CLI entry point: `python -m xauusd_trading.cli <subcommand>`. |
| `btcusd_trading/` | BTC self-rejection backtest runner that reuses the XAUUSD engine path. |
| `tools/` | Research/ops scripts: parameter sweeps, signal generators, explicit-config live/backtest runners, forensic dumper, tick tooling, Telegram-export backfill converter. |
| `listener/` | `telegram_listener.py` — ingests Victor's Telegram channel into `signals.txt` (override with `--signals-file`, e.g. `victor_signals.txt`). The feed tracks the channel's latest state: edits amend the line in place, deletions remove it (each queueing an MT5 amend/revoke), and startup catch-up reconciles changes made while the listener was down. |
| `tests/` | `pytest` suite (live/backtest parity, reconcile, sizing, listener, etc.). |
| `docs/` | Setup and operations guides (see below). |

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

See [`docs/MT5_SETUP.md`](docs/MT5_SETUP.md) for the full live-data setup.

## Quick start — backtest

```bash
python -m xauusd_trading.cli backtest \
  --signals victor_signals.txt \
  --charts "data/XAUUSD_M1_*.csv"
```

Writes an Excel report (`backtest_results*.xlsx`) with three sheets:

- **Summary** — config, overall stats, entry-outcome counts (entries
  skipped / filled / TP / SL), realized risk:reward shown as `1:N` ratios,
  and the monthly breakdown.
- **Daily Breakdown** — one row per traded day (pre-start padding excluded),
  with per-entry outcome counts and realized R per day.
- **Per-Entry Detail** — one row per Entry slot, split into **ORIGINAL**
  (the signal as written) vs **EXECUTED** (backtest result) column groups.

Charts are M1 OHLC CSVs in the broker's **GMT+3** chart timezone. To compare
a backtest against what really filled on MT5, overlay a native MT5 history
export with the explicit runner's `--mt5-history FILE` (see
[`docs/MT5_SETUP.md`](docs/MT5_SETUP.md)).

## Quick start — decide on one signal

```bash
python -m xauusd_trading.cli decide \
  --signal "1. BUY XAUUSD 4543 - 4541 SL 4536 TP1 4551 TP2 4561 TP3 4576 2:02 PM" \
  --signal-date 2026-05-07 --signal-tz 7 \
  --charts "data/XAUUSD_M1_*.csv"
```

Swap `--charts ...` for `--mt5 --equity-from-mt5` to run against live MT5
data instead of CSV. Add `--execute` to actually place the orders (it
implies `--mt5` and live equity — no confirmation prompt).

## CLI subcommands

`python -m xauusd_trading.cli <subcommand>` (prog name `xauusd`):

| Subcommand | Purpose |
|------------|---------|
| `backtest` | Run a historical backtest over signals + M1 charts; writes an Excel report. |
| `decide`   | Evaluate one signal. With `--execute`, place and manage orders on MT5. |
| `manage`   | Manage tracked signals: lock SL to TP1, cancel expired pendings, time-close. `--watch` loops. |
| `auto`     | Continuous live trading: read `signals.txt`, place orders, manage positions, append-only event log. `--replace-missing-entries` self-heals limit orders cancelled by hand; `--reopen-missing-positions` restores positions closed by hand while the replay still holds them OPEN (price-aware: market only at-or-better than the entry, else a LIMIT at the original entry — never chases). |
| `mt5-info` | Diagnostic: latest bar, account equity, open MT5 positions/orders for the symbol. |
| `fetch`    | Pull the last N months of M1 history into `data/` (per-month CSV archive; `--months`, default 2 — live feed loops use 1). |

Common flag groups (added to most subcommands):

- **Strategy overrides:** `--initial-capital`, `--risk`, `--entries`,
  `--entry-ladder {range_uniform,range_to_sl}`, `--entry-sl-gap`.
- **Research toggles (default OFF):** `--trailing-open-distance`,
  `--trailing-close-distance`, `--trend-runner` (+ `--trend-runner-ema-fast`,
  `--trend-runner-ema-slow`, `--trend-runner-atr-period`,
  `--trend-runner-atr-multiplier`).
- **Research strategy modes (explicit runners only, default OFF):** the full
  flag surface in `tools/backtest_explicit.py` / `tools/auto_explicit.py`
  adds `--shared-sl` (all entries share one stop anchored on entry #1, with
  per-leg risk sizing), `--entry-targets T1,T2,...` (per-entry targets from
  `{TP1,TP2,TP3,RUN}`, one per entry; `RUN` legs trail past
  `--runner-trail-from {TP1,TP2,TP3}` by `--trailing-close-distance`),
  `--bep-after-move` (per-leg break-even+ once a leg is N price units in
  favour), and `--sync-charts` (refetch M1 before a backtest, default on).
- **MT5 connection:** `--mt5-symbol` (default `XAUUSD`),
  `--mt5-server-offset` (default `3`), `--mt5-history-bars` (default `5000`),
  `--mt5-path`, `--mt5-login`, `--mt5-password`, `--mt5-server`.
- **Notifications / forensics:** `--notifications` / `--no-notifications`,
  `--forensic-log` / `--no-forensic`.

For the full strategy parameter surface (max-hold, pending-expiry,
sl-multiplier, lock delays, profit-lock model, etc.) used in the
trailing-open research config, see the explicit runners
`tools/auto_explicit.py` and `tools/backtest_explicit.py`, documented in
[`docs/demo_runbook_trailing_open.md`](docs/demo_runbook_trailing_open.md).

## Default strategy config

Defaults live in `xauusd_trading/core/config.py` (`DEFAULT_CONFIG`) — the
validated DD40-compatible provider contract. Headlines:

| Setting | Default |
|---------|---------|
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

Trailing-open, trailing-close, trend-runner, shared-SL, per-entry-targets,
and break-even-after-move are **off by default** and are enabled explicitly
per run via CLI flags — they are deliberately not read from the environment,
so backtests are reproducible regardless of shell state.

## Live trading

`positions.json` is the registry of currently-tracked signals. Each entry is
`{"signal_key", "signal", "date", "tz", "equity_at_open", "executed_at"}`
(the last is optional). `--execute` / `auto` auto-prune entries whose MT5
footprint is gone.

Two live modes:

- **Mode A — one-shot `decide --execute`:** manual, signal-by-signal.
- **Mode B — continuous `auto`:** run the Telegram listener + `auto`
  side-by-side, hands-off.

Full procedures, sanity checks, and failure recovery are in
[`docs/OPERATIONS_PLAYBOOK.md`](docs/OPERATIONS_PLAYBOOK.md). MT5 connection
setup is in [`docs/MT5_SETUP.md`](docs/MT5_SETUP.md).

## Tests

```bash
pytest
```

The suite includes live/backtest execution-parity tests that pin the engine
behavior the docs describe (SL-to-TP1 lock parity, reconcile, wall-clock
expiry, terminal catch-up, sizing, listener overrides).

## Documentation

- [`docs/MT5_SETUP.md`](docs/MT5_SETUP.md) — connect the engine to a live MT5 terminal.
- [`docs/OPERATIONS_PLAYBOOK.md`](docs/OPERATIONS_PLAYBOOK.md) — daily live-trading procedures (Modes A and B).
- [`docs/demo_runbook_trailing_open.md`](docs/demo_runbook_trailing_open.md) — demo parity protocol for the trailing-open candidate config.
- [`CLAUDE.md`](CLAUDE.md) — project instructions for Claude / Claude Code.
- [`CHATGPT.md`](CHATGPT.md) — project instructions for ChatGPT.
</content>
