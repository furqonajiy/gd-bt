# ChatGPT project instructions — xauusd-backtest

Paste the body below into the ChatGPT Project "Instructions" box (kept under the
8,000-character limit). It is the **must-do** summary; the full reference (CLI
flags, config defaults, command examples) lives in `CLAUDE.md` and the code.

---

You are assisting on **xauusd-backtest**: a Python engine that backtests and
live-trades Victor-style XAUUSD (gold) text signals through MetaTrader 5. The
same engine drives both the backtest and live execution, so they must stay in
parity. Full reference (CLI flags, config defaults, commands) is in `CLAUDE.md`
and the code — this file is the required rules.

## Repository layout (essentials)

- `xauusd_trading/` — the engine: signal parsing, position lifecycle, backtest,
  MT5 adapter, executor, CLI. Almost all logic lives here.
- `btcusd_trading/` — BTC backtest reusing the engine path.
- `tools/` — research/ops scripts, incl. `auto_explicit.py` /
  `backtest_explicit.py` (the full strategy-flag surface).
- `tools/live_feed_loop.py` — live self-signal feed loop: regenerates a
  generator's feed only on a new CLOSED M1 bar (parity with the backtest
  archive), logs like `auto` (header, then `[ts] Add Signal …` per new one).
- `listener/telegram_listener.py`, `tests/` (pytest, parity-heavy), `docs/`.
  The listener keeps the feed at the channel's latest state (edits amend in
  place, deletions remove + MT5 amend/revoke; startup catch-up reconciles the
  24 h lookback). `tools/telegram_export_to_signals.py --merge-into` does the
  same from a Telegram HTML export for longer outages.

## Architecture — must follow

- Import from the package root: `from xauusd_trading import X`. Everything is
  re-exported from `__init__.py` (dependency-ordered); never deep-import
  `xauusd_trading.core.foo`. When you move a symbol, update `__init__.py`.
- `cli.py` is a thin wrapper over `cli_orig.py` and overrides only the `auto`
  console output. Add subcommands/flags in `cli_orig.py`'s `build_parser()`.
  Entry point: `python -m xauusd_trading.cli`.
- `decide` lives in `strategy.trailing_engine`.
- `core/config.py` `DEFAULT_CONFIG` is the validated contract. Trailing-open /
  trailing-close / trend-runner default OFF and are enabled only via CLI flags,
  never env vars — keep it reproducible. Do not add env-var config reads.
- Chart timezone is GMT+3; signal times arrive in `--signal-tz` (Victor GMT+7)
  and convert internally. Do not hardcode tz conversions.
- `positions.json` registry entry shape: `{"signal_key", "signal", "date",
  "tz", "equity_at_open", "executed_at"?}`; auto-pruned by `--execute` / `auto`.
- Live self-heal flags on `auto`: `--replace-missing-entries` (re-place
  hand-cancelled PENDING limits), `--reopen-missing-positions` (restore
  hand-closed positions the replay still holds OPEN — price-aware per leg:
  market only at-or-better than the entry or when the stop is locked beyond
  it, else a LIMIT at the original entry; never chases. Such signals survive
  the prune). Fresh placement is history-gated (a magic with closed deals never
  re-places), and the late TP1/TP2 catch-up locks a protective stop instead of
  flattening at market (close only as last resort).

## Invariants — must respect

- Do not tune `core/config.py` to chase a bigger backtest number; go through
  `tools/sweep.py`, lock `tests/test_smoke.py`, then deploy.
- Live/backtest parity is the contract: keep `test_*parity*`, `test_reconcile`,
  `test_intrabar_*lock*`, `test_live_wall_clock_expiry` green. Change them only
  on an intentional behavior change, and update the prose docs to match.
- The executor owns protective trailing SL via `TRADE_ACTION_SLTP`; do not add
  behavior that fights it.
- `--execute` places real orders with no confirmation and implies `--mt5` plus
  live equity. Be careful in any path that reaches it.
- Lot sizing floors to `minimum_lot` (0.01), never 0, for a sizeable signal
  (both `compute_lot` and the executor's `round_lot`).

## Git workflow — the required flow

Ship every change (including docs-only) this way:

1. Branch off `main`, descriptive `feature/<what-changed>` (hyphens, never
   spaces, never named after a person). Always `feature/...`, never an
   agent/session-generated name like `claude/...` — migrate such work to a
   `feature/...` branch before the PR and delete the agent branch.
2. Author as `git config user.name "C - Furqon Aji Yudhistira"` and
   `user.email "furqonajiy@gmail.com"` — both author and committer, not a bot.
3. Representative commit subject, no placeholder.
4. Open a PR into `main`.
5. Merge with **no fast-forward** (a real merge commit; never squash/ff).
6. The merge-commit title must be representative and end with the PR number —
   `Description (#NN)`, e.g. `Update Project Instructions to the Latest State
   (#47)`. Never the default `Merge pull request #NN from …`.
7. Keep docs/project instructions in sync inside this same feature branch and
   run `pytest` first (green before merge). Never open a separate branch/PR
   just to edit `CLAUDE.md` / `CHATGPT.md` or bump the marker — fold them into
   the feature branch that carries the change; a standalone instructions/marker
   PR is noise.
8. Bump the sync-marker in this same branch before merge: the repo root holds
   one empty `YYYY-MM-DD_HHMM.txt` stamp in **Jakarta time (WIB, UTC+7)**.
   Rename it to now — `git mv <old>.txt "$(TZ='Asia/Jakarta' date
   +%Y-%m-%d_%H%M).txt"` (pin the zone; this environment is UTC). Keep exactly
   one such file.
9. Delete the feature branch after merge (or note if deletion is blocked).

## Working style

Match the surrounding code: `from __future__ import annotations`, PEP 604
unions (`X | None`), dataclasses for config/state, and the existing why-focused
comments. Add no new runtime deps beyond pandas/openpyxl without strong reason.
Live MT5 paths need the Windows-only `MetaTrader5` package; validate engine
changes via the backtest and pytest (CSV data + a stub MT5 layer). When you
change CLI flags, config, the lifecycle, or `positions.json`, update `README.md`
and `docs/*` in the same change.
Artifact names are dot-free: report dirs, positions registries, and other
parameter-derived output names carry no `.` outside the real file extension —
`slm21`/`gap05`, e.g. `reports/BEST_slm21_gap05_tp1delay24_risk005_2025`,
never `BEST_slm2.1_gap0.5_…`. Enforced where files are generated
(`_backtest_output_path` sanitizes the workbook stem); keep new writers on
the same convention.
</content>
