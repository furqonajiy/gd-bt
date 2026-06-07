# ChatGPT project instructions — xauusd-backtest

Paste the body below into the ChatGPT Project "Instructions" box (it is kept
under the 8,000-character limit). It mirrors `CLAUDE.md` for parity.

---

You are assisting on **xauusd-backtest**, a Python engine that backtests and
live-trades Victor-style XAUUSD (gold) text signals through MetaTrader 5. The
same engine drives both paths, so a backtest and a live run produce the same
modeled trade lifecycle. A signal line looks like:

`1. BUY XAUUSD 4543 - 4541 SL 4536 TP1 4551 TP2 4561 TP3 4576 2:02 PM`

Lifecycle: entry laddering -> pending activation/expiry -> fill -> SL/target
management -> SL-to-TP1 (and optional TP2) lock -> time-exit at max_hold ->
optional virtual trailing-open entry, trailing-close exit, and trend runner.

## Repository layout

- `xauusd_trading/` - the engine: signal parsing, position lifecycle,
  backtest, MT5 adapter, executor, CLI. Almost all real logic lives here.
- `btcusd_trading/` - BTC self-rejection backtest reusing the XAUUSD engine
  path; never mutate the blessed strategy config for a research run.
- `tools/` - research/ops scripts: parameter sweeps, signal generators, the
  explicit full-parameter runners `auto_explicit.py` / `backtest_explicit.py`,
  `dump_forensic.py`, tick tooling.
- `listener/telegram_listener.py` - ingests Victor's Telegram channel into
  `signals.txt`.
- `tests/` - pytest suite, heavy on live/backtest parity.
- `docs/` - `MT5_SETUP.md`, `OPERATIONS_PLAYBOOK.md`,
  `demo_runbook_trailing_open.md`.

## Architecture conventions (follow these)

- Import from the package root. Everything is re-exported from
  `xauusd_trading/__init__.py`. Internal modules, CLI, tests, and tools all do
  `from xauusd_trading import X`, never `from xauusd_trading.core.foo import
  X`. The re-export block is dependency-ordered; when you move a symbol
  between files, update `__init__.py` and keep the ordering valid.
- CLI structure. `xauusd_trading/cli.py` is a thin wrapper that imports the
  historical implementation from `cli_orig.py` and overrides only the `auto`
  console presentation (append-only event log). New subcommands/flags go in
  `cli_orig.py`'s `build_parser()`; keep `cli.py` delegating. Entry point is
  `python -m xauusd_trading.cli`.
- `decide` lives in `strategy.trailing_engine` (re-exported as
  `xauusd_trading.decide`). It preserves the legacy lifecycle when trailing
  distances are 0 and adds trailing behavior when enabled.
- Config. `core/config.py` `DEFAULT_CONFIG` is the validated
  DD40-compatible provider contract. Trailing-open / trailing-close /
  trend-runner default to disabled and are enabled explicitly per run via CLI
  flags. They are deliberately NOT read from environment variables, so
  `DEFAULT_CONFIG` stays reproducible regardless of shell state. Do not add
  env-var config reads.
- Chart timezone is GMT+3 (`CHART_TIMEZONE_OFFSET = 3`). CSV charts and MT5
  server time are GMT+3; signal times arrive in a source tz (`--signal-tz`,
  Victor uses GMT+7) and are converted internally. Do not hardcode tz
  conversions outside the existing helpers.
- `positions.json` is the tracked-signal registry (`SignalRegistry` in
  `execution/mt5_executor.py`). Entry shape: `{"signal_key", "signal",
  "date", "tz", "equity_at_open", "executed_at"?}`. It is auto-pruned by
  `--execute` / `auto` when a signal's MT5 magic has no footprint. Keep doc
  examples consistent with this shape.

## Invariants to respect

- Do not tune `core/config.py` to chase a bigger backtest number. Strategy
  changes go through `tools/sweep.py`, then lock the result into
  `tests/test_smoke.py`, then deploy (see `docs/OPERATIONS_PLAYBOOK.md`).
- Live/backtest parity is the contract. Tests like `test_*parity*.py`,
  `test_reconcile.py`, `test_intrabar_*lock*.py`, and
  `test_live_wall_clock_expiry.py` pin the behavior the docs promise. If you
  change lifecycle logic, run them and keep them green; update them only when
  intentionally changing behavior, and update the prose docs to match.
- The executor owns protective trailing SL via `TRADE_ACTION_SLTP`, not MT5
  native trailing. Do not introduce behavior that fights the executor.
- `--execute` places real orders with no confirmation prompt and implies
  `--mt5` plus live equity. Be careful in any code path that reaches it.

## CLI subcommands

`python -m xauusd_trading.cli <subcommand>`:

- `backtest` - historical backtest over signals + M1 CSV charts; writes an
  Excel report.
- `decide` - evaluate one signal; with `--execute`, place and manage orders.
- `manage` - lock SL to TP1, cancel expired pendings, time-close; `--watch`
  loops.
- `auto` - continuous live trading from `signals.txt`; append-only event log.
- `mt5-info` - diagnostic: latest bar, equity, open MT5 objects.
- `fetch` - pull ~2 months of M1 history into `data/`.

Strategy override flags: `--initial-capital`, `--risk`, `--entries`,
`--entry-ladder {range_uniform,range_to_sl}`, `--entry-sl-gap`. Research
toggles (default OFF): `--trailing-open-distance`, `--trailing-close-distance`,
`--trend-runner` (+ EMA/ATR sub-flags). MT5 flags: `--mt5-symbol` (default
`XAUUSD`), `--mt5-server-offset` (default 3), `--mt5-history-bars` (default
5000), plus `--mt5-path/login/password/server`. The full strategy parameter
surface (max-hold, pending-expiry, sl-multiplier, lock delays, profit-lock
model) lives in `tools/auto_explicit.py` and `tools/backtest_explicit.py`.

## Default config headlines (`core/config.py`)

sizing_mode=risk; risk_per_signal=0.05575; entry_count=3;
entry_ladder=range_to_sl; activation_delay_minutes=3;
pending_expiry_minutes=630; max_hold_minutes=90; sl_multiplier=1.61;
final_target=TP3; lock_after_tp1=True; lock_after_tp2=False;
trailing_open_distance=0.0; trailing_close_distance=0.0 (both disabled).

## Commands

```
pip install -r requirements.txt   # pandas, openpyxl, pytest
pytest                            # full suite
pytest tests/test_smoke.py        # quick strategy-baseline check
python -m xauusd_trading.cli backtest --signals victor_signals.txt --charts "data/XAUUSD_M1_*.csv"
```

Live MT5 paths (`mt5-info`, `decide --execute`, `manage`, `auto`, `fetch`)
need the Windows-only `MetaTrader5` package and a running terminal; they
cannot run in Linux/CI. Validate engine changes via the backtest and pytest,
which use CSV data and a stub MT5 layer.

## Git workflow

Ship every change (including docs-only) the same way: (1) branch off `main`
with a descriptive `feature/<what-changed>` name (hyphens, never spaces, never
named after a person); (2) set `git config user.name "C - Furqon Aji
Yudhistira"` and `user.email "furqonajiy@gmail.com"` so commits carry that
author, not a bot; (3) write a representative commit subject, not a
placeholder; (4) open a PR into `main`; (5) merge with no fast-forward (a real
merge commit — never squash/fast-forward); (6) give the merge commit a
representative message too, not the default `Merge pull request #NN from …`;
(7) keep docs in sync in the same change and run `pytest` first; (8) bump the
sync-marker file — the repo root holds one empty `YYYY-MM-DD_HHMM.txt` file
whose name is the "last synced" stamp, so on every update rename it to the
current time (`git mv <old>.txt "$(date +%Y-%m-%d_%H%M).txt"`), keeping exactly
one such file; (9) delete the feature branch after merge (or note if branch
deletion is blocked).

## Working style

Match the surrounding code: `from __future__ import annotations`, PEP 604
unions (`X | None`), dataclasses for config/state, and the existing (often
detailed, why-focused) comment density. Add no new runtime dependencies
beyond pandas/openpyxl without strong reason. When you change CLI flags,
config defaults, the lifecycle, or the `positions.json` shape, update the
matching prose in `README.md`, `docs/MT5_SETUP.md`,
`docs/OPERATIONS_PLAYBOOK.md`, and `docs/demo_runbook_trailing_open.md` — the
docs are part of the contract.
</content>
