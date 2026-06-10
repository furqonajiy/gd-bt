# CLAUDE.md

Project instructions for Claude / Claude Code working in this repository.

## What this project is

A Python engine that backtests and live-trades Victor-style XAUUSD (gold)
text signals through MetaTrader 5. The same engine drives both paths, so a
backtest and a live run produce the same modeled lifecycle. A signal line:

```
1. BUY XAUUSD 4543 - 4541 SL 4536 TP1 4551 TP2 4561 TP3 4576 2:02 PM
```

The lifecycle: entry laddering → pending activation/expiry → fill → SL/target
management → SL-to-TP1 (and optional TP2) lock → time-exit at `max_hold` →
optional virtual trailing-open entry and trailing-close exit / trend runner.

## Layout

- `xauusd_trading/` — the engine (parsing, lifecycle, backtest, MT5 adapter,
  executor, CLI). This is where almost all real logic lives.
- `btcusd_trading/` — a BTC self-rejection backtest that reuses the XAUUSD
  engine path; never mutate the blessed strategy config for a research run.
- `tools/` — research/ops scripts (sweeps, signal generators, the explicit
  full-parameter runners `auto_explicit.py` / `backtest_explicit.py`,
  `dump_forensic.py`, tick tooling).
- `listener/telegram_listener.py` — ingests Victor's Telegram channel into
  `signals.txt` (override the output feed with `--signals-file`, e.g.
  `victor_signals.txt`). The feed follows the channel's latest state: edits
  amend the line in place (same `N.`/signal_key) and deletions remove it,
  each queueing the matching MT5 amend/revoke; startup catch-up reconciles
  the 24 h lookback so downtime edits/deletions are applied too. For longer
  outages, `tools/telegram_export_to_signals.py --merge-into` syncs the feed
  from a Telegram Desktop HTML export through the same parse pipeline.
- `reporting/excel_report.py` — three-sheet backtest workbook (Summary /
  Daily Breakdown / Per-Entry Detail; the Per-Entry sheet splits ORIGINAL
  signal vs EXECUTED result, realized risk:reward rendered as `1:N`).
- `tests/` — `pytest` suite, heavy on live/backtest parity.
- `docs/` — `MT5_SETUP.md`, `OPERATIONS_PLAYBOOK.md`,
  `demo_runbook_trailing_open.md`.

## Architecture conventions — follow these

- **Import from the package root.** Everything is re-exported from
  `xauusd_trading/__init__.py`. Internal modules, CLI, tests, and tools all
  do `from xauusd_trading import X`, never `from xauusd_trading.core.foo
  import X`. The re-export block is dependency-ordered — when you move a
  symbol between files, update `__init__.py` and keep the ordering valid.
- **CLI structure.** `xauusd_trading/cli.py` is a thin wrapper that
  `import *`s the historical implementation from `cli_orig.py` and overrides
  **only** the `auto` console presentation (append-only event log). New
  subcommands/flags go in `cli_orig.py`'s `build_parser()`; keep `cli.py`
  delegating. Entry point is `python -m xauusd_trading.cli`.
- **`decide` lives in `strategy.trailing_engine`** (re-exported as
  `xauusd_trading.decide`). The wrapper preserves the legacy lifecycle when
  trailing distances are 0 and adds trailing behavior when enabled.
- **Config.** `core/config.py` `DEFAULT_CONFIG` is the validated
  DD40-compatible provider contract. Trailing-open / trailing-close /
  trend-runner and the newer research modes — `shared_sl` (one stop level
  for all entries, anchored on entry #1, with per-leg risk sizing),
  `per_entry_targets` (a per-entry tuple from `{TP1,TP2,TP3,RUN}`; `RUN`
  legs trail past `runner_trail_from` by `trailing_close_distance`), and
  `bep_after_move` (per-leg break-even+ once a leg is N price units in
  favour) — all default to **disabled** and are enabled **explicitly per run
  via CLI flags** (the full surface lives in `tools/backtest_explicit.py` /
  `tools/auto_explicit.py`). They are deliberately NOT read from environment
  vars, so `DEFAULT_CONFIG` is always reproducible regardless of shell
  state. Don't add env-var config reads.
- **Chart timezone is GMT+3** (`CHART_TIMEZONE_OFFSET = 3`). CSV charts and
  MT5 server time are GMT+3; signal times come in some source tz
  (`--signal-tz`, Victor uses GMT+7) and are converted internally. Don't
  hardcode tz conversions outside the existing helpers.
- **`positions.json`** is the tracked-signal registry (`SignalRegistry` in
  `execution/mt5_executor.py`). Entry shape:
  `{"signal_key", "signal", "date", "tz", "equity_at_open", "executed_at"?}`.
  It is auto-pruned by `--execute` / `auto` when a signal's MT5 magic has no
  footprint. Keep examples in docs consistent with this shape. `auto
  --replace-missing-entries` self-heals: each cycle it re-places only the
  entries still **PENDING** in the replay whose per-entry comment vanished
  from MT5 (e.g. limits cancelled by hand), gated on the signal still having
  ≥1 footprint — no chasing of passed prices, LIMIT-only.

## Invariants to respect

- **Don't tune `core/config.py` to chase a bigger backtest number.** Strategy
  changes go through `tools/sweep.py`, then lock the result into
  `tests/test_smoke.py`, then deploy. See `docs/OPERATIONS_PLAYBOOK.md`.
- **Live/backtest parity is the contract.** Many tests
  (`test_*parity*.py`, `test_reconcile.py`, `test_intrabar_*lock*.py`,
  `test_live_wall_clock_expiry.py`) pin the exact behavior the docs promise.
  If you change lifecycle logic, run them and keep them green — update them
  only when intentionally changing behavior, and update the prose docs to
  match.
- **The executor owns protective trailing SL**, not MT5 native trailing.
  Don't introduce behavior that fights the executor's `TRADE_ACTION_SLTP`.
- **`--execute` places real orders with no confirmation prompt** and implies
  `--mt5` + live equity. Be careful in any code path that reaches it.

## Commands

```bash
pip install -r requirements.txt        # pandas, openpyxl, pytest
pytest                                  # full suite
pytest tests/test_smoke.py             # quick strategy-baseline check

python -m xauusd_trading.cli backtest --signals victor_signals.txt --charts "data/XAUUSD_M1_*.csv"
python -m xauusd_trading.cli decide --signal "..." --signal-date 2026-05-07 --signal-tz 7 --charts "data/XAUUSD_M1_*.csv"
```

Live MT5 (`mt5-info`, `decide --execute`, `manage`, `auto`, `fetch`) requires
the Windows-only `MetaTrader5` package and a running terminal — it cannot run
in this Linux/CI environment. Validate engine changes through the backtest
and `pytest`, which use CSV data and a stub MT5 layer.

## Docs to keep in sync with code

When you change CLI flags, config defaults, the lifecycle, or the
`positions.json` shape, update the matching prose in `README.md`,
`docs/MT5_SETUP.md`, `docs/OPERATIONS_PLAYBOOK.md`, and
`docs/demo_runbook_trailing_open.md`. The docs are treated as part of the
contract, not afterthoughts.

## Git workflow & contribution process

Every change ships through the same flow — apply it even to docs-only changes
(including edits to this file):

1. **Branch off `main` with a descriptive name — always `feature/...`.** The
   name says *what you are updating*, not who is doing it:
   `feature/<what-changed>` (e.g. `feature/sync-docs-with-code`,
   `feature/document-git-workflow`). Use hyphens, never spaces — Git rejects
   spaces in ref names. Do not name a branch after a person, and never ship
   work on agent/session-generated branches like `claude/...` — if a tool or
   harness pre-creates one, migrate the commits to a `feature/...` branch
   before opening the PR and delete the agent branch.
2. **Author commits as the project owner.** Set
   `git config user.name "C - Furqon Aji Yudhistira"` and
   `git config user.email "furqonajiy@gmail.com"` so both author and committer
   carry that identity. Don't leave commits authored as `Claude` / a bot.
3. **Write a representative commit subject** that describes the change
   (`docs: sync markdown with code, add Claude/ChatGPT project instructions`),
   not a generic placeholder.
4. **Open a PR into `main`** with a summary of what changed and how it was
   verified.
5. **Merge with no fast-forward** so a real merge commit is recorded
   (`git merge --no-ff`, or the GitHub merge method `merge`). Never squash or
   fast-forward.
6. **Give the merge commit a representative message that ends with the PR
   number.** Set an explicit merge commit title (e.g. via the merge API's
   `commit_title`) in the form `Representative description (#NN)` — for
   example `Update Project Instructions to the Latest State (#47)`. Never
   accept the default `Merge pull request #NN from …` subject.
7. **Keep docs and project instructions in sync inside this same feature
   branch** (see the section above), and run `pytest` before merging. Never
   open a separate branch/PR just to update `CLAUDE.md` / `CHATGPT.md` or to
   bump the marker — fold those into the feature branch that carries the
   change, before merging it. A standalone instructions-only or marker-only PR
   is noise.
8. **Bump the sync-marker file (Jakarta time) in this same branch, before the
   merge.** The repo root holds a single empty marker file named for a
   timestamp in **Jakarta time (WIB, UTC+7)**, `YYYY-MM-DD_HHMM.txt` (e.g.
   `2026-06-08_0120.txt`). On every update, rename it to the current Jakarta
   time in the same change — always pin the zone, do not rely on the machine
   clock (this environment is UTC):
   `git mv <old>.txt "$(TZ='Asia/Jakarta' date +%Y-%m-%d_%H%M).txt"`.
   Its filename is the "last synced" stamp used to check whether the tree is
   up to date, so there must always be exactly one such file and it must
   reflect this change's Jakarta-time moment.
9. **Delete the feature branch after merge.** (If branch deletion is blocked
   in the current environment, say so and leave it for the maintainer.)

## Style

Match the surrounding code: `from __future__ import annotations`, PEP 604
unions (`X | None`), dataclasses for config/state, and the existing comment
density (these files explain *why*, often at length — keep that where it adds
signal). No new runtime dependencies beyond `pandas` / `openpyxl` without a
strong reason.

**Artifact names are dot-free.** Backtest report dirs, positions registries,
and other parameter-derived output names must not contain `.` anywhere except
the real file extension — render parameter values without the dot:
`sl-multiplier 2.1` → `slm21`, `entry-sl-gap 0.5` → `gap05`. So
`reports/BEST_slm21_gap05_tp1delay24_risk005_2025` and
`positions_best_slm21_tp1delay24.json`, never `BEST_slm2.1_gap0.5_…`.
The engine enforces this where it generates files: `_backtest_output_path`
(`strategy/backtest.py`) renders the workbook stem dot-free, so even a dotted
run name can't be truncated at its last "extension" again — keep any new
file-writing code on the same convention.
</content>
