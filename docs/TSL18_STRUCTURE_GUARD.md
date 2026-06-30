# TSL18 anti-wrong-side-structure guard

A **testable, OFF-by-default** feed layer for the live TSL18 self-scalper that
vetoes entries taken **against the larger market structure**. This document is
the contract for what it is, why it exists, how to validate it, and the bar it
must clear before it can replace live TSL18.

## Why this is NOT another generic sweep

TSL18 has already been through the generic optimisation passes:

- the **RSI × Bollinger × R:R** feed sweep (Phase 4) — the feed champion,
- the **trailing-open/close × risk** tick sweeps — the geometry,
- the SL-multiplier / max-hold / lock-delay neighbourhoods.

Re-running those would just re-discover the same parameters. The remaining
problem is **structural, not parametric**: TSL18 is a trend-pullback scalper, so
in a strong move it keeps firing **same-side pullback entries**. When the *larger*
structure is against that side — BUY signals while H1 is bearish, SELL signals
while H1 is bullish — those entries cluster into **sequential losses** (the wild
give-back days). No RSI/SL/TP tweak fixes "right setup, wrong side of the bigger
picture". This guard addresses exactly that one failure mode and is judged on it.

## What it does

It gates the **existing** ema-pullback entry in
`tools/generate_scalper_signals.py` with side-specific vetoes. It can only
**remove** signals — it never invents a trade — so its only downside risk is
filtering future winners, which the validation explicitly measures.

Components (first failing veto wins):

1. **HTF trend agreement (core, always on when enabled).** An independent
   higher-timeframe EMA (`--structure-htf-minutes`, `--structure-ema-fast`,
   `--structure-ema-slow`; default H1 20/50). Reject **BUY when fast−slow < 0**
   (bearish HTF), **SELL when fast−slow > 0** (bullish HTF). A NaN HTF (warmup)
   vetoes — it can't be confirmed. **No lookahead:** each HTF bucket's value is
   stamped at its *completion* time (bucket-start + interval) before it is
   forward-filled onto the M1 bars, so an entry at 10:05 reads the **last fully
   completed** H1 candle (09:00–10:00), never the in-progress 10:00–11:00 one —
   the same data live has at that instant (pinned by
   `test_htf_structure_has_no_lookahead`).
2. **VWAP side** (`--structure-require-vwap-side`): reject BUY below / SELL above
   session VWAP.
3. **Impulse cooldown** (`--structure-impulse-cooldown-bars` + `--structure-impulse-atr`):
   reject BUY within N bars of a **bearish impulse** (a down candle with
   |close−open| ≥ X·ATR, or a close below the prior swing low); mirror for SELL.
   Both knobs must be > 0 to arm it.
4. **Structure score** (`--structure-min-score`, 0..4): +1 each for HTF-agree,
   VWAP-side, no-opposite-impulse, swing-intact; reject below the threshold.

All of these are **OFF by default** (`--structure-filter` defaults to off). With
the flag absent the generator is **byte-identical** to today's TSL18 feed —
parity is pinned by `tests/test_structure_guard.py::test_default_flags_preserve_generation_parity`.

### Diagnostics (why a signal was rejected)

`--structure-diagnostics <csv>` writes one row per **base-setup bar** with:
`time, side, close, htf_state, vwap_side, impulse_state, score, reject_reason`.
`reject_reason` is one of `accept | htf_nan | htf_bearish_buy | htf_bullish_sell
| vwap_wrong_side | impulse_cooldown | score_below_min`. This is how you audit
the guard instead of trusting it.

## The shadow strategy

`cli/candidate_TSL18_structure_guard_tick.txt` — **tag TSG18**, identical TSL18
execution geometry, structure guard ON in the feed. It is **SHADOW / RESEARCH**:
it has its own positions / feed / report / log artifacts
(`positions_tsg18.json`, `signals/tsg18*.txt`, `reports/TSG18_*`,
`forensic_tsg18.jsonl`) so it can be demo-A/B'd beside TSL18 without colliding.
Runnable now by unique filename substring (`python cli/run.py tsl18_structure_guard 5`);
**no `cli/run.py` alias is added until it is validated.**

## How to validate

The sweep scores base TSL18 vs guarded variants on the **wrong-side / sequential**
metrics, not headline profit.

**June 2026 first (pure TICK):**

```bash
python tools/sweep_structure_guard.py --window june
```

**Then Jan → Jun 2026 (TICK where covered, M1 before):**

```bash
python tools/sweep_structure_guard.py --window jan_jun
```

Both windows end on **2026-07-01** internally — `backtest_hybrid` treats
`--end-date` as **exclusive**, so this is how June 30 is kept in the window
(`june` = 06-01→07-01, `jan_jun` = 01-01→07-01).

Output: `reports/STRUCTURE_GUARD_<window>/summary.md` with, per variant:
net P&L, max drawdown, win rate, profit factor, total trades, loss count,
max daily loss, BUY-losses-during-bearish-HTF, SELL-losses-during-bullish-HTF,
filtered winners vs filtered losers — and, crucially, **two streak levels**:

- `max_consecutive_losing_entries` — the entry-level streak (kept, but renamed:
  TSL18 opens up to 8 entries per signal, so this **over-counts** the felt pain).
- `max_consecutive_losing_signals` — entries grouped per signal (signal loses
  when its total P&L < 0); this is what the operator actually experiences.
- `max_consecutive_wrong_side_losing_signals` — consecutive losing signals that
  were wrong-side (losing BUY in a bearish HTF / losing SELL in a bullish HTF).

**Read the SIGNAL columns first.** Filtered winners/losers are matched at the
signal level on the **chart timestamp + side** (stable across feeds — the Entry
Key's per-day index renumbers when signals are dropped, and the source date can
differ from the chart date around the GMT+7/EET midnight boundary).

## When it is safe to promote to live

Promote TSG18's guard into TSL18 **only if all hold** on both windows:

1. **It cuts the target failure mode** — lower `max_consecutive_losing_signals`
   and `max_consecutive_wrong_side_losing_signals` (the SIGNAL-level streaks),
   lower max-daily-loss, and materially fewer BUY-loss-in-bearish-HTF /
   SELL-loss-in-bullish-HTF than base. (Judge on the signal-level streaks, not
   the entry-level one.)
2. **It removes mostly losers** — `filtered losers ≫ filtered winners`. If it
   filters mostly winners it is hurting; do not promote.
3. **No material edge/OOS regression** — net P&L and drawdown stay within the
   DD ≤ 40% gate; it must not trade a sequential-loss problem for a worse overall
   curve.
4. **Demo A/B confirms it** — run TSG18 beside TSL18 on demo (separate positions)
   and check the live guarded feed matches the backtest before sizing up.

If those clear, fold the winning `--structure-*` flags into the TSL18 feed
sections and re-validate parity; otherwise TSG18 stays a shadow and TSL18 is
unchanged. The expected outcome of *this* change is the **validation path**, not
a proven better strategy.
