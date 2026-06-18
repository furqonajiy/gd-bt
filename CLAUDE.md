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
  `victor_signals.txt`). New **and edited** messages pass
  `apply_signal_corrections` first — a logic-only typo fixer (wrong-side SL/TP,
  TP order, extra-zero / **wrong-hundreds** typos) that now also repairs a
  **directionally-valid but implausibly-far SL** via a clean ±100·n shift
  (e.g. BUY `4319-4321 SL 4214` → `4314`, or `4327-4325 SL 4219` → `4319`); it
  never tunes risk:reward and leaves a stop it can't cleanly repair as-posted.
  The feed follows the channel's latest state: edits amend the line in place
  (same `N.`/signal_key) and deletions remove it, each appending an
  `amend`/`revoke` record to `signal_overrides.jsonl`; a separate provider-filter
  (`tools/live_provider_signal_filter.py --watch`) regenerates the filtered live
  feed (`generated/victor_live.txt`) from the raw feed on every change; startup
  catch-up reconciles the 24 h lookback so downtime edits/deletions are applied
  too. **`auto --apply-signal-edits`** (opt-in) consumes that journal so
  the live executor follows the corrected feed: on `amend` it **flattens** the
  signal's MT5 footprint (`Mt5Executor.flatten_signal` — cancel pendings + close
  any open position) and **re-places at the corrected levels** (close-and-reopen,
  bypassing only the already-traded history gate via the per-cycle
  `_amended_force_replace_keys`), on `revoke` it flattens and untracks — matched
  by the **tagged** magic, idempotent through a byte-offset sidecar that anchors
  at EOF on first run (the pre-existing backlog, already in the feed, is never
  replayed). For longer outages, `tools/telegram_export_to_signals.py
  --merge-into` syncs the feed from a Telegram Desktop HTML export through the
  same parse pipeline.
- `tools/live_feed_loop.py` — the **live self-signal feed loop**: one process
  that refetches the current month (`fetch --months 1`) and regenerates a
  generator's feed **only when a new CLOSED M1 bar exists** (idle otherwise),
  with `--gen-start-days`/`--gen-recent-months` rolling the start + narrowing
  charts in-process for speed. `--gen-start-days` **rewrites** an existing
  `--start`/`--start-date` and is **injected** (family-aware) when the
  pass-through omits one — so it is never a silent no-op; without that injection
  the feed emitted the whole loaded chart window (cold-start bars and all),
  diverging from a full-archive backtest (the 2026-06-18 live-vs-backtest signal
  drift). It imports the generator module unmodified, so
  the live feed stays byte-identical to the backtest archive; logs like `auto`
  (header, then `[ts] Add Signal …` per new signal). `--family` picks the
  generator; args after `--` pass through. `fetch` gained `--months N` (default
  2; live loops use 1 since rolled-over months are immutable).
- `reporting/excel_report.py` — three-sheet backtest workbook (Summary /
  Daily Breakdown / Per-Entry Detail; the Per-Entry sheet splits ORIGINAL
  signal vs EXECUTED result, realized risk:reward rendered as `1:N`). The
  Summary's Monthly Breakdown carries a **Regime** column — each month is
  classified (R1quiet/R2bull/R3strong/R4parab) from its own M1 bars.
- `strategy/regime.py` — the **volatility-regime detector** (`detect_regime` /
  `read_current_regime` via smoothed M15 ATR + trend), re-exported from the
  package root. It labels months in the report and drives the **regime
  auto-switch**. `strategy/regime_adaptive.py` is the shared resolver
  (`champion_config` loads `CHAMPION_<regime>.json` under `--champions-dir`,
  fallback to the incumbent; `make_regime_config_resolver` maps a signal's time →
  regime → champion config from a *trailing* window, no lookahead). Both paths use
  it: **`auto --adaptive`** (live, per cycle) and **`backtest_explicit.py
  --adaptive`** (the same switch in backtest, via `run_backtest(config_resolver=)`).
  Champions live in **`champions/CHAMPION_<regime>.json`** (committed on main).
  **R2bull and R3strong promote SC24 + `entry_count 8` ("SC24T24E8",
  tp1_lock_delay 24)** — #1 on the reliable forward-fit metrics (OOS *and*
  fixed-lot edge) in both (R2 OOS 52,831 / R3 110,907), at DD ≤ 40%; there the
  lever is *more entries* (e8 > e7 > e6 on OOS). **R4parab now promotes the e5
  DD-compliant winner** (e5 / range_uniform / slm2.3 / gap0.0 / max_hold 90 /
  tp1_lock_delay 20 / tp1_lock_fraction 0.25 / lock_after_tp2 off / shared_sl):
  on current 2026 data the prior SC24T24E8's concurrent DD rose to **58.3%**
  (over the 40% gate), so among DD ≤ 40%-compliant configs **e5 is the edge+OOS
  leader** (edge $39,062 / OOS $6,671 / DD 33.3%) — fewer entries + a tighter 2.3
  stop + a 90-min hold beat the over-DD e8 once the gate binds. The e5 feed is
  now **RSI-filtered** (generator `--rsi-buy-max 70 --rsi-sell-min 30`: skip
  overbought BUYs / oversold SELLs): the **R4 entry-feature sweep** (16
  generator-feature variants × strategy/geometry, slippage-aware 2.0/1.0,
  DD ≤ 40% & OOS > 0) found RSI the **only** entry feature that beats the
  unfiltered `base` feed on **both edge and OOS** (rsi edge $39,508 / OOS $7,199 /
  DD 33.4% vs base $39,020 / $6,629 / 33.3%, apples-to-apples in one sweep). It's
  a **thin** filter (~0.5% of signals dropped) so the lift is modest and the
  **strategy params are unchanged** — a feed change, not a strategy change, hence
  low-risk; ADX/Bollinger/VWAP/HTF/S-R/session variants all lost on edge or OOS.
  (SC24T24E8 had
  itself superseded the earlier R4 pick SC24T15E6, which led only on the
  compounded net+bonus mirage.) R1quiet stays seeded with SC24 until the sweep
  advances to it.
  `tools/regime_router.py` is a back-compat shim; `tools/regime_auto.py` is the
  one-shot advisory CLI.
- `tests/` — `pytest` suite, heavy on live/backtest parity.
- `docs/` — `MT5_SETUP.md`, `OPERATIONS_PLAYBOOK.md`,
  `demo_runbook_trailing_open.md`, `SWEEP_RUNBOOK.md`,
  `VICTOR_SWEEP_RUNBOOK.md`, and **`BACKTEST_REALISM.md`** — the single source of
  truth for what the backtest must model to match live (LOCK_TP1/TP2 slippage
  2.0/1.0, spread, commission 0, swap, min-stop) and **what the user provides**
  to keep it calibrated (broker spec via `tools/dump_mt5_spec.py`, a clean
  `ReportHistory` HTML to reconcile via `tools/reconcile_report_html.py`). Read
  it instead of re-asking what we need.

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
  for all entries, anchored on entry #1, with per-leg risk sizing; when a
  leg is filled by **trailing-open**, its stop is re-anchored to that leg's
  planned distance to the shared level taken from the actual fill — never the
  frozen shared price, which a deep trailing fill can leave on the wrong side
  of the entry — so backtest matches what the executor sends live),
  `per_entry_targets` (a per-entry tuple from `{TP1,TP2,TP3,RUN}`; `RUN`
  legs trail past `runner_trail_from` by `trailing_close_distance`), and
  `bep_after_move` (per-leg break-even+ once a leg is N price units in
  favour) — all default to **disabled** and are enabled **explicitly per run
  via CLI flags** (the full surface lives in `tools/backtest_explicit.py` /
  `tools/auto_explicit.py`). The **`bep_plus_half_tp1` profit-lock mode**'s
  early-arm stop (a leg that moves `bep_trigger_distance` *before* TP1) now parks
  at **entry ± `bep_buffer`** instead of exactly entry — `bep_buffer` defaults
  **0.0** so it is exact break-even (byte-identical to before, parity preserved),
  and a positive buffer locks "+ small points" of profit on a leg that spikes
  toward TP1 then reverses before the TP1 lock can ratchet up (the wild-bar
  give-back measured live on SC24-0618 #17). The stage-1 (fractional) TP1 lock
  stays the ceiling. This is the lever the **`self-scalper-bep-sweep.yml`** sweep
  + `sweep_self_limit.py --bep-policy` explore per regime (base vs bep, deploy
  only on an edge **and** OOS win at DD ≤ 40%). They are deliberately NOT read from environment
  vars, so `DEFAULT_CONFIG` is always reproducible regardless of shell
  state. Don't add env-var config reads. **Locked-exit slippage** is a
  **backtest-realism** model: a *locked* protective stop (LOCK_TP1/LOCK_TP2)
  fills at market on the retrace — live can't always place the lock exactly at
  the level (broker stops/freeze level rejects a stop too close to price, so
  `execution/sl_safety.clamp_sltp_sl` clamps it to the best legal price and the
  executor ratchets it toward the true level as price recovers), and the residual
  window + market-fill cost a point or two. `lock_exit_slippage_points`
  (`--lock-exit-slippage`) is the **uniform** knob; `lock_tp1_exit_slippage_points`
  / `lock_tp2_exit_slippage_points` (`--lock-tp1-exit-slippage` /
  `--lock-tp2-exit-slippage`) model the measured **asymmetry** (~2 pt TP1, ~1 pt
  TP2 from the 2026-06-16 reconciliation) and override the uniform per stage when
  either is >0 — resolved by `core.config.lock_slippage_points`. The give-back is
  applied in the **real lifecycle** (`core/trailing_positions.py:_locked_exit_fill`,
  where `advance_bars` closes a triggered stop; a diagnostic mirror lives in
  `strategy/path_analysis.py:_stop_exit_fill`), clamped to the trigger bar so it
  never models more slip than the bar allows; **raw SL and TP1/TP2/TP3 targets
  are untouched** (only `LOCK_*` exits slip). **The sweep scores against it**:
  `tools/sweep.base_config_dict` carries 2.0/1.0 so every candidate + incumbent is
  **decided on real fills, not the idealized exact-level fill** (without it the
  sweep picks an over-optimistic champion that leans too hard on locked exits).
  All three fields default **0**, and **DEFAULT_CONFIG / live / `decide` / parity
  tests stay at 0** — the live executor places stops at exact levels and the
  *broker* adds the slip, so the live↔backtest-model parity contract holds at 0.
  These are **backtest-only** and never change live order placement. From the
  2026-06-16 reconciliation: at equal lot, live tracks the backtest on entries,
  TP3, and SL to the cent; the only gap is locked exits, which this models.
- **Signal R:R / SL-source policy** (`strategy/backtest.apply_signal_rr_policy`,
  applied per-signal in `run_backtest`; **all default OFF → parity**). For
  provider feeds whose posted TP/SL vary in quality — Victor's 2024–25 signals
  have TP1 R:R ~0.5 (≈100% below 1:1), but he **rewrote his generator in 2026**
  to ~1.0 / TP3 ~4.4 — the backtest/sweep can: **filter** (`signal_min_rr`, skip
  weak setups), **rewrite** TPs (`rewrite_tp1/2/3_rr` → entry ± rr·risk), source
  the stop from **ATR** instead of the posted SL (`sl_source="atr"`,
  `atr_period`, `atr_sl_mult` → entry ∓ ATR·mult, i.e. our generator's geometry
  on Victor's entries), and measure R:R on the **nominal** or **effective**
  (×sl_multiplier) risk (`signal_rr_reference`). entry_edge = range_high (BUY) /
  range_low (SELL). These are the Victor-sweep dimensions; **Victor's 2026 style
  ≠ 2025, so sweep per regime** (R3=2025, R4=2026 ≈ current). Only `LOCK_*`
  exits get slippage; this policy is orthogonal and also backtest/sweep-only.
- **Chart timezone is Eastern European (EET/EEST)** — UTC+2 in winter, UTC+3 in
  summer, switching on the EU rule (last Sunday of March / October). It is **not**
  a fixed GMT+3 (confirmed empirically from the ELEV8 archive: the weekly close
  shifts to 22:59 only during the US-vs-EU DST mismatch windows, every year).
  `core/chart_tz.py` is the single source of truth — `to_chart_tz`/`from_chart_tz`
  convert a provider signal's source tz (`--signal-tz`, Victor uses fixed GMT+7)
  to/from chart-local time DST-aware, and `utc_to_chart` gives live "now".
  `CHART_TIMEZONE_OFFSET = 3` remains only the *summer* reference. CSV charts and
  the MT5 server clock store this EET/EEST time verbatim — `fetch` with
  `--mt5-server-offset 3` (shift 0) keeps the broker clock as-is. Don't hardcode
  tz conversions outside `chart_tz`.
- **`positions.json`** is the tracked-signal registry (`SignalRegistry` in
  `execution/mt5_executor.py`). Entry shape:
  `{"signal_key", "signal", "date", "tz", "equity_at_open", "executed_at"?}`.
  It is auto-pruned by `--execute` / `auto` when a signal's MT5 magic has no
  footprint. The MT5 magic + order comment are derived from `signal_key`. The
  **magic is the identity**: `signal_to_magic(signal_key)` hashes the FULL key
  (tag + `YYYY-MM-DD` + `#DD`) to a 31-bit int, and the executor pulls a signal's
  orders with `find_orders(magic)` / `find_positions(magic)` — so that's how it
  knows which BUY/SELL LIMIT belongs to which signal, not the comment. The
  **comment** is the human label + per-entry key: `mt5_entry_comment` renders the
  compact **`[TAG-]MMDD#DD.N`** form (e.g. `VIC-0615#05.2`, `SC24-0615#05.2`,
  `0615#05.2` untagged) — tag, month-day, signal-of-day, and one-based entry.
  Only the **year** is dropped from the date (it lives in the magic + open time)
  so the whole comment fits brokers that truncate below MT5's 31-char cap (Elev8
  cuts near 16); the `.N` suffix is never trimmed, and matching is per-magic so
  dropping the year never confuses two signals. To run **two auto executors on
  one account** (e.g. Victor + a self-feed scalper), give each a distinct
  **`--strategy-tag`** (e.g. `VIC` vs `SC24`) — it is stamped onto `signal_key`
  so the two get disjoint magics/comments and never manage each other's orders.
  The tag is **capped at 4 chars** (first 4 kept) so the compact comment always
  fits, and is live-only (empty in backtests, so parity holds); each executor
  still needs its own `--positions-json`. Keep examples in docs consistent with
  this shape. `auto
  --replace-missing-entries` self-heals: each cycle it re-places only the
  entries still **PENDING** in the replay whose per-entry comment vanished
  from MT5 (e.g. limits cancelled by hand), gated on the signal still having
  ≥1 footprint — no chasing of passed prices, LIMIT-only. `auto
  --reopen-missing-positions` mirrors the replay the rest of the way: entries
  the replay still holds **OPEN** but missing from MT5 (closed by hand) are
  restored price-aware — at market when the price is at-or-better than the
  leg's entry or its stop is already locked at/beyond entry, otherwise via a
  LIMIT at the original entry inside the pending window (never chases) — and
  replay-open signals survive the prune. A leg whose live position **closed in
  the last ~3 min** (SL/lock/TP firing intrabar) is **not** re-opened — the
  bar-close replay lags the live close by up to a bar and briefly still holds the
  leg OPEN; the cooldown lets it catch up instead of resurrecting a just-closed
  leg into immediate re-close (the churn). A genuinely hand-closed leg the replay
  still holds OPEN past the cooldown is restored as normal. In this **reopen/mirror mode**,
  `place_signal` also stops skipping **partially played-out** signals: when the
  executor first meets a signal whose replay already closed some legs, it places
  the still-PENDING legs as fresh LIMITs and tracks the signal on its replay-OPEN
  legs, so the reopen pass restores the already-OPEN legs (the `_allow_partial_placement`
  gate; default OFF keeps the legacy signal-level skip, so backtests are unchanged).
  Per-entry identity holds end-to-end: the manage/reopen path recovers the
  strategy tag from the registry `signal_key`, so every managed/reopened leg
  carries the same tagged magic + `[TAG-]MMDD#DD.N` comment `place_signal` used.
  Fresh
  placement is history-gated: a magic with closed deals is never re-placed,
  so a finished signal can't trade twice. The late TP1/TP2 catch-up protects
  legs with a stop at the lock level (ratcheted toward it on recovery) and
  closes at market only as a last resort.

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
  Don't introduce behavior that fights the executor's `TRADE_ACTION_SLTP`
  (native trailing can't be set from the Python API anyway). The explicit
  runners' `--trailing-close-min-step` only throttles how often the modify is
  sent — the engine still trails continuously. A trailing-open STOP the broker
  rejects after price crossed the trigger falls back to a tick-confirmed
  market fill (`mt5_executor_trailing.py`); it never market-fills below the
  trigger.
- **`--execute` places real orders with no confirmation prompt** and implies
  `--mt5` + live equity. Be careful in any code path that reaches it.

## Parameter sweeps

When asked to run/redo a parameter sweep (after a strategy change, an engine
bug fix, or new chart data), follow **`docs/SWEEP_RUNBOOK.md`** by default — no
need to be told to. **For the Victor feed specifically, follow
`docs/VICTOR_SWEEP_RUNBOOK.md`** — the slippage-aware, per-regime (R3=2025,
R4=2026; Victor rewrote his generator in 2026), edge+$3/lot-bonus-ranked
`victor-sweep.yml` workflow + signal R:R/ATR policy. Don't rebuild it; run it.
Non-negotiables from `SWEEP_RUNBOOK.md`: **verify the M1 data is real
1-minute bars first** (daily/hourly bars get mislabeled as M1); the **baseline
is a hand-seeded config, not exhaustive search**, so a sweep must both **widen
the grid** to include the champion's values *and* **re-seed the champion** or it
can't beat it — the incumbent baseline **SC24** is defined once in
`tools/sweep.py::sc24_config()`, seeded with `sc24_neighborhood_grid()`, and is
also the sweep's **incumbent** (`tools/incumbent_baseline.py`); the grid's
automated keyfn **ranks by compounded net P&L + the $3/closed-lot bonus**
(`risk_net_profit_with_bonus`) at **DD ≤ 40% with OOS > 0** (the OOS guard rejects
in-sample blow-ups; the compounded figure is a *model upper bound* that **ranks**
configs, not a money forecast — it does reach billions/quadrillions and that is
expected), plus a **DD 40–50% "stretch" tier** surfaced only when it beats the
DD≤40% champion's net+bonus by ≥25%. **But the deploy/PROMOTE decision uses the
reliable forward-fit metrics — fixed-lot `edge` (`fixed_no_bonus_profit`) and
`OOS` (`oos_fixed_no_bonus_profit`) — NOT the compounded net+bonus when they
disagree.** Compounded net+bonus is hypersensitive to leverage/variance and
in-sample sequencing (a tighter SL or extra leverage inflates it without improving
the real per-trade edge), so it *ranks* but does not *decide*: a config only wins
if it leads on edge **and** OOS (the metrics that survive to live + fixed-lot
trading). This is why **SC24T24E8 (entry 8) was promoted for R2bull/R3strong
over the net+bonus #1** (slm1.9 in R2) — that led only on the inflated headline
while losing on edge and OOS. For **R4parab**, SC24T24E8 later breached the
DD ≤ 40% gate (58.3%) on current 2026 data and was superseded by the **e5
DD-compliant edge+OOS leader**. Keep **one writer per sweep
branch**; and run sweeps on a `research/...` branch, never on `main`.

## Commands

```bash
pip install -r requirements.txt        # pandas, openpyxl, pytest
pytest                                  # full suite
pytest tests/test_smoke.py             # quick strategy-baseline check

python -m xauusd_trading.cli backtest --signals victor_signals.txt --charts "data/XAUUSD_M1_*.csv"
python -m xauusd_trading.cli decide --signal "..." --signal-date 2026-05-07 --signal-tz 7 --charts "data/XAUUSD_M1_*.csv"
```

`backtest`/`decide` default to **`DEFAULT_CONFIG.initial_capital = $50,000`** (was
$5,000, originally $1,000). Drawdown is computed from that base, so it is the figure
the DD≤40% gate and the live executor size against. The $50k base keeps the 0.01-lot
**minimum-lot floor** from distorting risk% — at $5k many wide-stop signals floored to
0.01 lot, inflating early per-signal risk above the nominal 1% and running DD hotter;
at $50k the 1% risk is faithful. (edge/OOS are fixed-lot and capital-independent; the
concurrent-risk DD gate is ~capital-independent too — raising the base mainly cleans up
the floor distortion and the compounded path.)

Live MT5 (`mt5-info`, `decide --execute`, `manage`, `auto`, `fetch`) requires
the Windows-only `MetaTrader5` package and a running terminal — it cannot run
in this Linux/CI environment. Validate engine changes through the backtest
and `pytest`, which use CSV data and a stub MT5 layer. To resync the M1 archive
from 2020, see the standalone `cli_resync_m1_from_2020.txt` (`fetch --months 80`,
`--mt5-server-offset 3` keeps the broker EET/EEST clock verbatim). The repo-root
`cli_*.txt` files are runnable deployment-command snapshots, each with the same
sections (Signal Auto Generator live-loop / Backtest CLI / Auto CLI; Telegram
Listener only for the Victor feed). The current R4 champion is
`cli_champion_R4_scalper24_no_trailing` — the **e5 DD-compliant winner**
(e5 / range_uniform / slm2.3 / max_hold 90 / tp1_lock_delay 20 / shared_sl, on
the **RSI-filtered** scalper24 feed `--rsi-buy-max 70 --rsi-sell-min 30`;
`champions/CHAMPION_R4parab.json`), which superseded SC24T24E8 for R4 after it
breached the DD ≤ 40% gate on 2026 data (SC24T24E8 remains the R2bull/R3strong
champion); others: `cli_champion_victor` (Victor — feed
`generated/victor_live.txt`, positions `positions_victor.json`, tag VIC), `cli_R4_scalper24`,
`cli_R4_breakout`, `cli_trailing_risk02allhours`, `cli_resync_m1_from_2020`, and
`cli_adaptive_regime` (the `auto --adaptive` regime auto-switch — one executor
that runs each regime's `CHAMPION_<regime>.json` and falls back to SC24).

## Docs to keep in sync with code

When you change CLI flags, config defaults, the lifecycle, or the
`positions.json` shape, update the matching prose in `README.md`,
`docs/MT5_SETUP.md`, `docs/OPERATIONS_PLAYBOOK.md`,
`docs/demo_runbook_trailing_open.md`, and — for the parameter-sweep
methodology — `docs/SWEEP_RUNBOOK.md`. The docs are treated as part of the
contract, not afterthoughts. **`CLAUDE.md` and `AGENTS.md` mirror each other —
change both together.**

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
   open a separate branch/PR just to update `CLAUDE.md` / `AGENTS.md` or to
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

## GitHub Actions / CI

When creating or editing any workflow under `.github/workflows/`, **pin the
latest Node-24-native major versions** of the standard actions. GitHub forces
Node 20 actions to Node 24 from 2026-06-16 and removes Node 20 from runners on
2026-09-16, so older pins emit a deprecation warning and will eventually fail.
Current correct pins: `actions/checkout@v5`, `actions/setup-python@v6`, the
artifact actions on their latest major, plus a top-level
`FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"` env as a catch-all for any action
still shipping only a Node-20 major. Always check the action's GitHub Releases
for a newer major before pinning — do not copy an old `@vN` out of an existing
file. Full convention + new-workflow checklist: `.github/workflows/README.md`.
