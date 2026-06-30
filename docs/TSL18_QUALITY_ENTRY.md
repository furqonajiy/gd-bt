# TSL18 quality-entry research layer

A **testable, OFF-by-default** research layer for the TSL18 self-scalper feed that
**classifies** every would-be entry by *quality*, optionally **filters** the feed
by a quality profile, adds a buy-bottom / sell-top **extreme-entry mode**, and
scores candidates with a **rebate-aware** objective so a feed that is green only
on the broker rebate is never promoted. This document is the contract: what it is,
why it exists, how to use it, and the bar it must clear.

It is the sibling of `docs/TSL18_STRUCTURE_GUARD.md`. The structure guard answers
"is this entry on the wrong side of the bigger picture?"; the quality layer answers
"**how good is this entry**, and is the strategy's profit real trading edge or just
rebate?" Like the structure guard it **only removes signals** — it never invents a
trade — so its only downside risk is filtering future winners, which the sweep
measures explicitly.

All of it is **OFF by default**. With the flags absent the generator is
**byte-identical** to today's TSL18 feed — parity is pinned by
`tests/test_tsl18_quality_entry.py::test_default_flags_preserve_generation_parity`.

## 1. The quality classifier (`--entry-quality-classifier`)

When on, every base ema-pullback setup is LABELLED (no feed change unless a
profile filters — see §2) with a **class** and a **0..1 score**, computed from
**no-lookahead** features only:

- **HTF trend** — its own **completion-stamped** higher-timeframe EMA
  (`--htf-minutes/--htf-ema-fast/--htf-ema-slow`), so a bar reads only the last
  *fully completed* HTF candle (no in-progress-bucket leak; pinned by
  `test_quality_htf_has_no_lookahead`). This is a separate series from the plain
  `--htf-filter` `htf_diff`, which is not completion-stamped.
- **RSI / Bollinger %B + bandwidth / ADX**, session **VWAP** distance, distance to
  **EMA-mid** (pullback depth), proximity to **support/resistance** (prior-day
  H/L, optional round grid, BB band edges), **supply/demand** zone bands (RBR/DBD,
  confirmed K bars after the base), and a **recent opposite impulse** flag.

### Classes

| class | meaning |
|---|---|
| `trend_pullback` | HTF-aligned pullback, not extended, not at a level |
| `deep_trend_pullback` | HTF-aligned pullback that is deep (≥ `--quality-deep-pullback-atr` from EMA-mid) or at a level/zone |
| `countertrend_reversal` | against the HTF but at a support/demand (BUY) / resistance/supply (SELL) extreme |
| `range_extreme_reversal` | HTF flat (ranging) and at a range extreme |
| `low_quality_chase` | aligned-but-chasing (overbought BUY / oversold SELL, or far from VWAP) with no level, or counter-HTF with no level |
| `unknown` | HTF / RSI / ATR not yet available (warmup) |

### Score (0..1)

A documented heuristic composite (it **ranks** within a feed; it is **not** a
probability). Positive components: at a level (+0.20) / in a zone (+0.15),
aligned-and-not-chasing (+0.25) **or** reversal-with-an-RSI-trigger (+0.25, or
+0.10 at a level without the trigger), no recent opposite impulse (+0.15), trend
strength / a live band (+0.10 / +0.05). Penalty: chasing (−0.20), and a
`low_quality_chase` is capped at 0.25. Clamped to [0, 1]. The exact weights live
in `_quality_score` (`tools/generate_scalper_signals.py`).

### Diagnostics (`--quality-diagnostics <csv>`)

One row per base-setup bar: `time, side, close, quality_class, quality_score,
htf_state, vwap_side, rsi, bb_pctb, bb_bandwidth, adx, near_support,
near_resistance, near_demand, near_supply, recent_opposite_impulse,
distance_to_vwap_atr, distance_to_ema_mid_atr, quality_reject_reason,
extreme_mode_reason, extreme_level_type, extreme_level_price,
distance_to_extreme_atr`. This is how you audit the layer instead of trusting it.

## 2. Quality profiles (`--quality-profile`, `--min-quality-score`)

The profile filters the feed by class + score. `--min-quality-score` is the
"high-quality" lever (default 0 keeps every in-class candidate).

| profile | keeps |
|---|---|
| `off` | everything (no filtering — the default) |
| `trend_only` | `trend_pullback` / `deep_trend_pullback` |
| `reversal_extreme` | `countertrend_reversal` / `range_extreme_reversal` with `score ≥ --min-quality-score` |
| `hybrid_quality` | trend pullbacks (always) + high-score reversal/extreme; rejects `low_quality_chase` / `unknown` |
| `high_frequency_quality` | everything except `low_quality_chase`, subject to `--min-quality-score` |

Pinned by `test_trend_only_filters_non_trend`,
`test_hybrid_quality_rejects_low_quality_chase`, and the end-to-end
`test_trend_only_filters_and_never_adds`.

## 3. Extreme-entry mode (`--extreme-entry-mode`)

Buy-bottom / sell-top scalping for the trailing-open executor: keep only entries
**near a price extreme**.

| mode | keeps |
|---|---|
| `off` | no extreme gating (default) |
| `support_demand` | BUYs near support/demand/lower-BB/prior-low/round-support (SELLs out of scope) |
| `supply_resistance` | SELLs near resistance/supply/upper-BB/prior-high/round-resistance (BUYs out of scope) |
| `both` | each side at its own extreme |

"Near" = within `--extreme-proximity-atr * ATR`. The matched level (type + price +
ATR distance) is recorded in the diagnostics (`extreme_level_type`,
`extreme_level_price`, `distance_to_extreme_atr`). No lookahead — every level is
completed/known data. Pinned by `test_extreme_buy_near_support_demand`,
`test_extreme_sell_near_resistance_supply`, `test_extreme_mode_rejects_off_side`.

## 4. Rebate-aware scoring (`tools/rebate_scoring.py`)

"Rebate" is the engine's **$3 / closed-lot bonus** (the workbook's *Closed-lot
bonus*). A dense feed can post a positive **net** P&L that is mostly rebate while
its **pure** trading P&L is flat/negative — *rebate-farming*, not edge. The scorer
separates them:

    pure_trading_pnl, rebate_pnl, net_pnl, closed_lots,
    pure_pnl_per_lot, net_pnl_per_lot, rebate_share_of_profit

Guards (`passes_rebate_guards`): reject when `pure_trading_pnl < --min-pure-trading-pnl`
(the primary guard — a negative-pure / positive-net candidate is always rejected)
or when `rebate_share_of_profit > --max-rebate-share-of-profit` (default 0.50).
Objectives (`--score-objective`):

- `net_pnl` — raw net (rebate-blind).
- `pure_pnl` — trading edge only.
- `edge_plus_rebate_guarded` — net **only when the guards pass**, else the pure
  edge alone (so a rebate-farm scores on its often-negative pure P&L). **Default.**
- `dd_adjusted_net` — net penalised by drawdown.

Pinned by `tests/test_rebate_scoring.py` (math + the negative-pure-positive-net
flag + objective behaviour).

## 5. The sweep skeleton (`tools/sweep_tsl18_quality_entry.py`)

Compares the base TSL18 feed against quality-entry variants under the SAME TSL18
geometry, on TICK where covered, ranked by the rebate-aware objective.

```bash
# tiny structural check — emits the schema with placeholder rows, NO backtests:
python tools/sweep_tsl18_quality_entry.py --mode smoke --skeleton

# tiny real smoke (last few June days, pure TICK):
python tools/sweep_tsl18_quality_entry.py --mode smoke
```

Modes: `smoke` (2026-06-27 .. 07-01), `full_june` (06-01 .. 07-01), `validate_top`
(01-01 .. 07-01, re-score a prior run's `top_candidates.json` via `--top-json`).
`--end-date` is **exclusive**, so a window ends the day after the last day kept.

Gates (each an explicit flag, all reflected in the results columns):

- **rebate guards** — `--min-pure-trading-pnl` / `--max-rebate-share-of-profit`.
- **partial-tick-lifecycle exclusion** (`--require-full-tick-lifecycle`) — drop a
  candidate whose window MIXES TICK and M1 (the tick archive doesn't cover the
  whole lifecycle), so only clean pure-TICK / pure-M1 windows rank.
- **open/pending-left** (`--exclude-open-or-pending`) — drop a candidate that left
  positions OPEN/PENDING at window end (incomplete P&L).

Outputs (under `reports/TSL18_QUALITY_<mode>/`): `results.csv` (full schema),
`top_candidates.json` (ranked survivors), `summary.md`. The `collision_*` columns
in `results.csv` are **placeholders only** — collision policy is a **separate
branch** and is deliberately **not implemented here**.

**Do not run the full aggressive sweep from this branch.** The skeleton +
`--mode smoke` is the structural check; promotion follows the same forward-fit
bar as every other strategy (`docs/SWEEP_RUNBOOK.md`).

## When it is safe to promote

A quality profile / extreme mode replaces or augments live TSL18 **only if all hold**:

1. **It improves the real metrics** — fixed-lot **pure** edge and OOS up (or DD
   materially down at flat edge), judged on the rebate-aware objective, not net
   headline. A net gain that is mostly rebate does **not** count.
2. **It removes mostly losers** — the filtered set is losers ≫ winners (the same
   bar as the structure guard).
3. **No DD regression** — stays within the DD ≤ 40 % gate.
4. **Demo A/B confirms it** — run the variant beside TSL18 on demo (separate
   positions / tag / feed / report / log per the one-identity rule) before sizing.

If those clear, fold the winning `--quality-*` / `--extreme-*` flags into the
TSL18 feed sections and re-validate parity; otherwise the layer stays research and
TSL18 is unchanged. The expected outcome of *this* change is the **validation
path**, not a proven better strategy.
