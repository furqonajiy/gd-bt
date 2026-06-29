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
3. Then score by tick P&L, win rate, profit factor, drawdown penalty, loss count, and worst single-signal loss.

Important columns:

- `tick_pnl`: total tick-replayed P&L including the configured closed-lot bonus.
- `max_drawdown_pct`: chronological per-signal tick drawdown proxy on the $50k base.
- `win_rate_pct`: winning signals / closed non-flat signals.
- `profit_factor`: gross win divided by absolute gross loss.
- `tick_coverage_pct`: share of generated signals that actually had tick coverage.
- `passes_dd25_gate` / `passes_dd40_gate`: quick deployment-readiness screens, not proof of live edge.

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

Aggregate local shard output:

```bash
python tools/sweep_tick_loss_filters.py aggregate \
  --phase june \
  --inputs 'sweep_reports/twl25_loss_sweep/smoke/**/*.jsonl' \
  --output-dir sweep_reports/twl25_loss_sweep/smoke_agg \
  --top-n 25
```

Validate top June rows on Jan-Jun available ticks:

```bash
python tools/sweep_tick_loss_filters.py validate-top \
  --phase jan_jun \
  --candidate-jsonl sweep_reports/twl25_loss_sweep/june/top_candidates.jsonl \
  --output-dir sweep_reports/twl25_loss_sweep/jan_jun \
  --top-n 25
```

## Deployable research snapshot

`cli/candidate_TWL25_loss_filtered_tick.txt` is a conservative starting cell so the runner can be launched with the same UX as TSL18:

```bash
python cli/run.py twl25 2
python cli/run.py twl25 3
python cli/run.py twl25 5
python cli/run.py twl25 6
```

Treat that snapshot as a hypothesis until the workflow publishes a stronger winner.  If the sweep winner differs, copy the winning signal flags and strategy overrides from `sweep_reports/twl25_loss_sweep/jan_jun/top_candidates.jsonl` into the CLI snapshot before live use.

## Safety notes

- This sweep does not tune `DEFAULT_CONFIG`.
- It uses the existing tick replay path (`tools/tick_backtest.py`) so trailing-open/close is decided on real tick fills rather than optimistic M1 order sequencing.
- The drawdown is a per-signal chronological proxy, not a full concurrent live margin model.
- Any winner remains research until demo-forward validation proves the broker fill path behaves similarly.
