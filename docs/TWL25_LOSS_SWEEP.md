# TWL25 loss-filter tick sweep

TWL25 is the loss-first research track for the TSL18/T818 self-scalper family.  The starting hypothesis is that TSL18's exit geometry is already decent, but the broad C160 feed still admits too many chop/overextension entries.  TWL25 therefore sweeps **entry quality first**, then strategy geometry.

## What is swept

The sweep is implemented in `tools/sweep_tick_loss_filters.py` and run by `.github/workflows/twl25-loss-tick-sweep.yml`.

Signal/feed dimensions:

- Base C160 feed as the control cell.
- Stricter RSI pullback filters, for example BUY only when RSI is not too high and SELL only when RSI is not too low.
- Bollinger bandwidth and %B filters to avoid dead squeeze and late band-chasing entries.
- ADX, higher-timeframe EMA agreement, and VWAP side filters to avoid chop.
- Round-number support/resistance and RBR/DBD supply-demand return filters.
- All-day, London/NY, and NY-core sessions.

Strategy dimensions:

- TSL18 base geometry as the control cell.
- Lower-entry defensive variants, faster TP2 variants, BEP/half-TP1 lock variants, and scale-out/BEP variants.
- Tick-tradeable trailing-open/close distances only; no sub-min-stop trailing distances.

## Ranking

The first stage ranks on **June 2026 ticks**, because that is the immediate live regime.  The second stage validates the June winners on **Jan 2026 through Jun 2026 ticks where available**.  If the repository only has May/June tick files, the `tick_coverage_pct` and `no_tick_signals` columns make that explicit.

The leaderboard is loss-first:

1. Prefer candidates passing the DD25 gate.
2. Then candidates passing the DD40 gate.
3. Then score, which is deliberately **sequential-loss-first**: it penalizes
   `max_consecutive_losing_signals` (×400) and `max_daily_loss` (×2.0) heavily,
   on top of the usual tick P&L / win-rate / profit-factor / DD>25 / loss-count /
   worst-single-signal terms. This targets the *structural* TSL18 failure mode
   (runs of same-side losers and one ugly day), not just total P&L — see
   `docs/TSL18_STRUCTURE_GUARD.md`.

Important columns:

- `tick_pnl`: total tick-replayed P&L including the configured closed-lot bonus.
- `max_drawdown_pct`: chronological per-signal tick drawdown proxy on the $50k base.
- `win_rate_pct`: winning signals / closed non-flat signals.
- `profit_factor`: gross win divided by absolute gross loss.
- `max_consecutive_losing_signals`: longest run of consecutive losing signals
  (the sequential-loss metric TWL25 ranks against).
- `max_daily_loss`: positive magnitude of the worst feed-zone day's net P&L.
- `loss_count`: number of losing signals.
- `worst_single_signal_loss`: positive magnitude of the single worst signal.
- `tick_coverage_pct`: share of generated signals that actually had tick coverage.
- `passes_dd25_gate` / `passes_dd40_gate`: quick deployment-readiness screens, not proof of live edge.
- `error`: populated when a cell failed to evaluate (e.g. too few signals); a
  failed cell never crashes the shard/validation job, it is logged with this field.

The wrong-side-HTF loss metrics (BUY-loss-in-bearish-HTF etc.) named in
`docs/TSL18_STRUCTURE_GUARD.md` are not yet plumbed through the tick replay path,
so they are out of scope for this leaderboard; the sequential-loss and
worst-day metrics above are the loss-first proxies TWL25 ranks on today.

### Backtest-only locked-exit slippage

TWL25 June and Jan-Jun **scoring** uses the R4 measured locked-exit realism model
— TP1 lock slippage **2.0** pts, TP2 lock slippage **1.0** pts
(`docs/BACKTEST_REALISM.md`) — set explicitly in
`sweep_tick_loss_filters.py::strategy_config()` (not via env vars). This is a
**backtest-only** realism knob so the sweep can't pick an over-optimistic
champion that leans on idealized locked exits. **Live order placement is
unchanged**: `DEFAULT_CONFIG` and the live executor keep slippage at 0 because
the broker adds the real slippage on the fill.

## How to run

Manual GitHub Actions path:

1. Open **Actions**.
2. Run **twl25-loss-tick-sweep**.
3. Keep `shards=12` for the full run.
4. Use `max_cells_per_shard=0` for the full run, or a small number for a smoke run.
5. Keep `validate_top_n=25` unless the Jan-Jun validation job becomes too slow.

Local equivalent for a smoke shard:

```bash
python tools/sweep_tick_loss_filters.py run-shard \
  --phase june \
  --shards 12 \
  --shard 0 \
  --max-cells 2 \
  --output-dir sweep_reports/twl25_loss_sweep/smoke
```

Aggregate local shard output. **Match ONLY the raw shard result files**
(`results_<phase>_shard*.jsonl`) — never `**/*.jsonl`, which would also pull in
each shard's `all_results_*.jsonl` and `top_candidates.jsonl` and count every
candidate 3× (the 432-row-vs-144-row bug). `aggregate` now fails loudly if it
detects duplicate `(phase, candidate_id)` rows:

```bash
python tools/sweep_tick_loss_filters.py aggregate \
  --phase june \
  --inputs 'sweep_reports/twl25_loss_sweep/smoke/**/results_june_shard*.jsonl' \
  --output-dir sweep_reports/twl25_loss_sweep/smoke_agg \
  --top-n 25
```

The full grid is **3 sessions × 6 filters × 8 strategies = 144** candidates, so a
correct June leaderboard has ~144 rows, not 432.

Validate top June rows on Jan-Jun available ticks:

```bash
python tools/sweep_tick_loss_filters.py validate-top \
  --phase jan_jun \
  --candidate-jsonl sweep_reports/twl25_loss_sweep/june/top_candidates.jsonl \
  --output-dir sweep_reports/twl25_loss_sweep/jan_jun \
  --top-n 25
```

## Research snapshot (NOT deployed)

`cli/candidate_TWL25_loss_filtered_tick.txt` is a conservative starting cell so the runner can be launched with the same UX as TSL18. It is resolved by the `twl25` alias in `cli/run.py` (`twl25 -> candidate_TWL25_loss_filtered_tick`):

```bash
python cli/run.py twl25 2
python cli/run.py twl25 3
python cli/run.py twl25 5
python cli/run.py twl25 6
```

Treat that snapshot as a hypothesis until the workflow publishes a stronger winner.  If the sweep winner differs, copy the winning signal flags and strategy overrides from `sweep_reports/twl25_loss_sweep/jan_jun/top_candidates.jsonl` into the CLI snapshot before live use.

**TWL25 stays research / draft — it is not a deployed strategy — until ALL of:**

1. the **June sweep** workflow succeeds (real `tick_pnl` / `score`, not `-1e18`);
2. the **Jan-Jun validation** job succeeds and produces an artifact;
3. a candidate **passes the DD25/DD40 loss-first gates** (positive tick P&L, DD within the gate, acceptable sequential-loss / worst-day);
4. a **demo-forward** run confirms the broker fill path behaves like the tick model.

Until then do not point real money at it; TSL18/T818 stay the live books.

## Safety notes

- This sweep does not tune `DEFAULT_CONFIG`.
- It uses the existing tick replay path (`tools/tick_backtest.py`) so trailing-open/close is decided on real tick fills rather than optimistic M1 order sequencing.
- The drawdown is a per-signal chronological proxy, not a full concurrent live margin model.
- Any winner remains research until demo-forward validation proves the broker fill path behaves similarly.
