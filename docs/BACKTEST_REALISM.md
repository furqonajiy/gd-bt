# Backtest ↔ live realism contract

**The single source of truth for "what the backtest must model to match live, and
what the user provides to keep it that way."** Read this instead of re-explaining.
Verified 2026-06-16 against the live broker spec via the live reconciliation
+ `tools/dump_mt5_spec.py`.

Bottom line from the reconciliation: at equal lot, live tracks the backtest **to
the cent** on entries, TP1/TP2/TP3 targets, and SL. The **only** gap is **locked
protective stops** (LOCK_TP1 / LOCK_TP2), which fill at market on the retrace and
give back a point or two. Everything below is how each live effect is handled.

**Skip the model where you have ticks.** The slippage/spread rows below are how
the **M1 OHLC** backtest *approximates* live fills. When the **tick archive**
covers a window, `tools/backtest_hybrid.py` instead replays the **real
`Mt5Executor` against the ticks** for those signals (M1 fallback elsewhere, one
combined report, Data Source column) — no fill model needed there, those legs are
the closest-to-live read. Use it for any 2026-05+ window; the M1 model still
governs the pre-tick eras. The locked-exit slippage below remains the M1 realism
knob and is what the per-regime sweeps score against.

---

## 1. The realism ledger (every live effect → how the backtest treats it)

| Live execution effect | Backtest treatment | Current value | Set in code |
|---|---|---|---|
| **Entry fills** | exact at the ladder price (spread-aware, strict-touch) | matched to the cent | engine |
| **TP1/TP2/TP3 targets** | exact limit fill at the level | matched | engine |
| **Raw SL** | exact at the stop level (tick-confirmed, never past) | matched | engine |
| **LOCK_TP1 give-back** (lock fills past TP1 on retrace) | **modeled** | **2.0 pts** | `lock_tp1_exit_slippage_points` |
| **LOCK_TP2 give-back** | **modeled** | **1.0 pt** | `lock_tp2_exit_slippage_points` |
| (uniform fallback) | applies to all locks if the two stage fields are 0 | 0 (off) | `lock_exit_slippage_points` |
| **Spread** | **modeled** from the broker's own per-bar `<SPREAD>` in the M1 CSV | ~0.25 (live ticks ~0.28) | `chart.spread_price` |
| **Commission** | none to model | **$0** (commission-free / in-spread) | n/a |
| **Broker min-stop** (`trade_stops_level`) | not enforced, but **non-binding** | **0.40 price units** — tighter than any config's stop | n/a (see §4) |
| **Swap** (overnight) | **NOT modeled** | −5.83 / −2.65 per lot/night (~−$0.06 per 0.01 lot) | — (only bites long `max_hold` crossing 22:00) |
| **Sizing** | risk% vs fixed-lot are reported separately; **decide on fixed-lot** | fixed 0.01 for edge/OOS | sweep |

**Parity rule:** all slippage fields default **0** in `DEFAULT_CONFIG` / live /
`decide` / parity tests — the *broker* adds the slip, not the engine. The sweep
turns it on via `tools/sweep.base_config_dict` (2.0/1.0). Never set it on
DEFAULT_CONFIG.

**Slippage scales with volatility, not price — so it is era-matched.** The
2.0/1.0 is the **R4 parabolic (2026)** give-back. The locked-exit slip tracks the
regime's volatility (≈ the median absolute LOCK fill vs the level), which falls
sharply in the quieter years, so a single value applied across 2021-2026 wrongly
charges the parabolic give-back to the quiet era. The per-regime values
(anchored on R4 = 1.0×):

| Regime | Years | LOCK_TP1 / LOCK_TP2 slip | vs R4 anchor |
|---|---|---|---|
| **R4 parabolic** | 2026-01 → 2026-06 | **2.0 / 1.0** | 1.0× |
| **R3 strong** | 2025 | **0.9 / 0.45** | ~0.45× |
| **R2 bull** | 2023-10 → 2024 | **0.5 / 0.25** | ~0.26× |
| **R1 quiet** | 2021-11 → 2023-09 | **0.4 / 0.2** | ~0.20× |

The per-era 5-way sweep (`self-scalper-5way-sweep-r3r2r1.yml`) scores each regime
with its own value, and the `cli/*.txt` backtest windows are split per era so each
run uses the matched slip (sections 5–6 R4 2.0/1.0, 7 R3 0.9/0.45, 8 R2 0.5/0.25,
9 R1 0.4/0.2). These remain **backtest-only** — live still places stops at the
exact level and the broker adds the slip.

---

## 2. What you (the user) provide — and how

| You provide | How to get it | When |
|---|---|---|
| **Broker symbol/account spec** | `python tools/dump_mt5_spec.py` on the Windows box → send `mt5_spec.json` | once; re-pull only if the broker changes specs |
| **Clean multi-week `ReportHistory` HTML** | MT5 → History tab → right-click → Report → **HTML**, widest date range from a **STABLE** run | each recalibration (§3) |
| **Fresh M1 charts** | `fetch` (Windows MT5), `--mt5-server-offset 3` keeps the EET/EEST clock | when new months exist |
| **The Victor feed** `victor_signals.txt` | the Telegram listener (`listeners/telegram/listener.py`) | as it grows |
| *(optional)* tick data `time,bid,ask` | MT5 → Symbols → XAUUSD → Ticks → Export | only to refine **swap** on long holds |

**That's the whole list.** I do **not** need: the spec again, full tick history,
or more chart data than the months you're testing.

---

## 3. Recalibrating the slippage (the loop)

The 2.0/1.0 came from **one clean day** (2026-06-16). To refine it: deploy
**stably** (clean config, #130 churn fix live, no manual closes) for ~2–4 weeks,
then:

```bash
python tools/reconcile_report_html.py \
    --report <history.html> --backtest <reports/BEST_*.xlsx> --tag VIC
```

It prints **avg LOCK_TP1 / LOCK_TP2 slip in points** = the new give-back (SL/TP3
should be ~0; ignore churn-noisy windows). Put those numbers into:

- `tools/sweep.py` → `SWEEP_LOCK_TP1_SLIPPAGE` / `SWEEP_LOCK_TP2_SLIPPAGE`
- the champion CLI snapshots' `--lock-tp1-exit-slippage` / `--lock-tp2-exit-slippage`

then re-sweep. Full loop: `docs/VICTOR_SWEEP_RUNBOOK.md` §8.

---

## 4. Known limits (don't mistake these for bugs)

- **Min-stop not enforced on the entry SL.** Harmless here: the effective stop is
  `(entry−SL)×sl_multiplier`, never below ~0.6 price units even at `sl_multiplier
  0.6`, vs the broker's 0.40 floor — so no swept config is unplaceable. If a
  future broker has a *larger* stops level, add a floor in `candidate_config`.
- **Swap unmodeled** → long-`max_hold` configs that hold overnight are slightly
  optimistic (~$0.06 per 0.01 lot per rollover). Flag long-hold winners.
- **Slippage is from one clean day** → treat as an estimate; prefer winners that
  stay best across slippage 1.0–3.0 until recalibrated (§3).

---

## 5. Where this is wired (file map)

| File | Role |
|---|---|
| `core/config.py` | the slippage + R:R/ATR fields (all default 0/off = parity) |
| `core/trailing_positions._locked_exit_fill` | applies the locked-exit give-back in the real lifecycle |
| `core/chart.py` | spread from the M1 `<SPREAD>` column |
| `tools/sweep.py` (`base_config_dict`, `SWEEP_LOCK_*`) | turns realism on for the sweep |
| `tools/dump_mt5_spec.py` | capture the broker spec |
| `tools/reconcile_report_html.py` | measure the live↔backtest gap from the HTML report |
| `tools/parity_reconcile.py` | same, from the executor's JSONL logs (when available) |
