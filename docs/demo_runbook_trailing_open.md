# Demo Runbook — Trailing-Open Candidate

**Goal of the demo:** confirm that the live trailing-open/close mechanism reproduces the backtest edge before any real capital. The strategy *is* the trailing entry + trailing exit, and M1-sim-vs-live-tick trailing is the one fragile seam — ~90% of exits are trailing-stop driven, so most of the P&L flows through it. This runbook is a parity test, not a profit test.

**Account:** DEMO only. Verify the MT5 terminal `auto_explicit.py` attaches to is logged into your demo login (check `trade_mode`), not live.

---

## 1. Config under test (frozen — do not change mid-demo)

```
entries 1 | range_uniform | entry_sl_gap 1 | activation_delay 1
pending_expiry 900 | max_hold 45 | sl_multiplier 1.15 | final_target TP3
lock_after_tp1 true | lock_after_tp2 true | tp1_lock_delay 3 | tp2_lock_delay 5
profit_lock_mode tp_levels | tp2_lock_target TP2
trailing_open 1.0 | trailing_close 2.0   (both ≥ broker stops_level 0.40 → valid)
sizing risk | risk 0.02 | bonus 0
```

**Runner-cap note.** `trailing_close 2.0 > 0`, so under the current
`--runner-final-cap auto` default this config is an **uncapped runner**:
`final_target TP3` is only a reference level and the trailing-close SL rides past
it (bounded by `max_hold 45`); the `--runner-after-tp3 false` / `--tp3-lock-target
TP2` flags in the commands below no longer gate the exit. The reference stats below
were measured **capped at TP3** — to reproduce them, add **`--runner-final-cap
tp3`** to the Section 3 (runner) and Section 5b (parity backtest) commands (keep
both identical so live↔backtest parity holds).

Backtest reference (2025–26, fixed-lot, no-bonus, **capped at TP3**): **+$2,898 net, 79.9% win, 24.9% concurrent DD, $162/closed-lot.** The demo is judged against the *per-signal behaviour* behind those numbers, not the headline $.

---

## 2. Pipeline bring-up (4 terminals)

Run in order. **The same generated feed file must drive both the live runner and the parity backtest — this is the seam that keeps them comparable.**

1. **Listener** — `listeners/telegram/listener.py` → writes raw `signals.txt`
2. **Filter** — `tools/live_provider_signal_filter.py --preset high_growth_hour_side` → writes the filtered feed `signals/live_provider_high_growth_hour_side.txt`
3. **Runner** — `tools/auto_explicit.py` (command below), reads that filtered feed
4. **(later) Parity** — `tools/backtest_explicit.py` on the *same* feed + freshly-fetched bars (Section 5)

Use a demo-specific positions file (`positions_demo.json`) so no state collides with anything else.

---

## 3. The demo runner command

Set `--initial-capital` to your actual demo balance so risk-sizing matches.

```powershell
python tools/auto_explicit.py `
  --signals signals/live_provider_high_growth_hour_side.txt `
  --positions-json positions_demo.json `
  --watch-interval 5 `
  --mt5-symbol XAUUSD `
  --mt5-server-offset 3 `
  --mt5-history-bars 3000 `
  --initial-capital <DEMO_BALANCE> `
  --sizing-mode risk `
  --lot 0.5 `
  --risk 0.02 `
  --minimum-lot 0.01 `
  --lot-step 0.01 `
  --bonus-per-closed-lot 0.0 `
  --entries 1 `
  --entry-ladder range_uniform `
  --entry-sl-gap 1.0 `
  --activation-delay 1 `
  --pending-expiry 900 `
  --max-hold 45 `
  --sl-multiplier 1.15 `
  --final-target TP3 `
  --lock-after-tp1 true `
  --lock-after-tp2 true `
  --tp1-lock-delay-minutes 3 `
  --tp2-lock-delay-minutes 5 `
  --profit-lock-mode tp_levels `
  --bep-trigger-distance 3.0 `
  --tp1-lock-fraction 0.5 `
  --tp2-lock-target TP2 `
  --runner-after-tp3 false `
  --tp3-lock-target TP2 `
  --trailing-open-distance 1.0 `
  --trailing-close-distance 2.0
```

(`--lot 0.5` is `lot_per_entry`, ignored under `sizing-mode risk` — kept so the command is byte-identical to the parity backtest config.)

**Live STOP mechanics to know during the demo:**
- **STOP-reject market fallback.** If the broker rejects the trailing-open STOP because price crossed the trigger inside the placement race (a BUY STOP must sit above Ask), the executor re-reads the tick and — only when it confirms the trigger was genuinely passed — fills the leg **at market**, keeping the planned stop distance anchored on the actual fill. Look for `FILLED AT MARKET` in the log; the small fill-price gap vs the modeled trigger is expected slippage, not a parity break. It never market-fills below the trigger.
- **`--trailing-close-min-step N` (optional, default 0).** Broker-traffic throttle: the trailing-close SLTP modify is sent only once the stop has improved by ≥ `N` price units on the broker's current SL (first protective set always goes out). The engine still trails continuously; live SL can lag the model by up to `N`. MT5's native right-click Trailing Stop is **not** an alternative — the Python API cannot set it and it would fight the executor.
- **Hand-closing a leg → re-arm, not a LIMIT (with `--reopen-missing-positions`).** If you close a trailing leg by hand to bank a small profit while the replay still holds it, `auto` **re-arms the trailing-open STOP** at the leg's original levels (waits for the dip-rebound, same basis as the first entry) — it never rests a flat LIMIT for a trailing strategy. The re-entry gate is **reason-aware**: an **SL/TP/stop-out/engine close** counts as "already traded" and is *not* re-armed, but a **manual** close (deal reason CLIENT/MOBILE/WEB) is. So during the demo, expect a hand-closed leg to come back as a trailing-open arm; to keep it out, let it hit SL/TP or remove the feed line.

---

## 4. What gets logged + what to watch daily

Already emitted by the system — you don't add anything:
- **`forensic.py` → per-cycle JSONL** — every manage cycle, per signal. Inspect with `tools/dump_forensic.py --signal <key> --summary` (also `--kind`, `--cycle`). Size-bounded: it rotates to a single `forensic.jsonl.1` backup at `--forensic-max-mb` (default 50 MB) so it can't fill the disk on a long-running executor; the newest events stay in the active file, the prior window is the `.1`.
- **`notifications.py` → `notifications.jsonl`** → forwarded to Telegram Saved Messages — your running live feed (placements, fills, locks, exits). Also rotates to a `.1` backup at `--notifications-max-mb` (default 10 MB).

Watch each day:
- **Fills vs no-fills** — is the trailing-open STOP entry actually filling? (backtest fill rate ≈ 81%.)
- **Trailing-close exits** — the price each position trails out at. This is the make-or-break field.
- **Lock events** — SL → TP1 on TP1 touch, SL → TP2 on TP2 touch (`tp2_lock_target TP2`), and the 3-/5-min lock delays firing.
- **Spread at fill** (~25–27 normal) — abnormal spread → abnormal fill.
- Demo equity/balance curve from MT5 (export end-of-day).

---

## 5. The parity check (the actual point of the demo)

**Timing rule — do NOT run the parity backtest too early.** A signal can stay pending up to `pending_expiry 900` min and then hold `max_hold 45` min, so it isn't fully resolved until ~16h after it was issued. Wait until the **newest signal in your window is ≥ ~16h old**, or you'll book truncated open-position artifacts.

Then:

**(a) Fetch the demo-period bars into `data/`.** `auto` reads MT5 history into memory and never writes the archive — only your `fetch` subcommand does. Run `fetch` for XAUUSD covering the demo dates so `backtest_explicit.py` has real bars to replay.

**(b) Run the parity backtest — identical config to live**, on the same feed and the fetched bars. Fill in the demo months/dates. `--start-date`/`--end-date` are read in the **signal's own feed zone** by default (the GMT+7 signal codes), so use the dates as they appear in the feed headers; add `--date-tz 3` only if you want chart-time (EET/EEST) bounds instead:

```powershell
python tools/backtest_explicit.py `
  --signals signals/live_provider_high_growth_hour_side.txt `
  --all-signals signals.txt `
  --filter-preset high_growth_hour_side `
  --charts data/XAUUSD_M1_<DEMO_MONTHS>_ELEV8.csv `
  --start-date <DEMO_START> --end-date <DEMO_END> `
  --output-dir reports/demo_parity `
  --max-drawdown-limit-pct 95 `
  --progress-interval-seconds 30 `
  --initial-capital <DEMO_BALANCE> --sizing-mode risk --lot 0.5 --risk 0.02 `
  --minimum-lot 0.01 --lot-step 0.01 --bonus-per-closed-lot 0.0 `
  --entries 1 --entry-ladder range_uniform --entry-sl-gap 1 `
  --activation-delay 1 --pending-expiry 900 --max-hold 45 --sl-multiplier 1.15 `
  --final-target TP3 --lock-after-tp1 true --lock-after-tp2 true `
  --tp1-lock-delay-minutes 3 --tp2-lock-delay-minutes 5 `
  --profit-lock-mode tp_levels --bep-trigger-distance 3 --tp1-lock-fraction 0.5 `
  --tp2-lock-target TP2 --runner-after-tp3 false --tp3-lock-target TP2 `
  --trailing-open-distance 1 --trailing-close-distance 2
```

**(c) Compare live vs backtest, per signal_key (`{date}#{day_id}`).** Live side = `dump_forensic.py --summary` + MT5 trade history; backtest side = the xlsx `Per-Entry Detail` / `All Signals Audit`. For each signal check:

| Field | Live source | Backtest source |
|---|---|---|
| Status (WIN/LOSS/NO_FILL) | MT5 / forensic | `Signal Status` |
| Fill price + time | MT5 fill | `Entry Price` / `Fill Time` |
| Exit price + time | MT5 close | `Exit Price` / `Exit Time` |
| Exit reason (which stop) | forensic | `Stop @ Exit` |
| P&L sign | MT5 | `Trading P&L` |

---

## 6. Read the comparison — expected noise vs red flags

**Expected, benign** (documented structural divergences — a handful of these is normal):
- Fill price/time differs slightly (backtest OHLC heuristic vs live ticks).
- SL→TP1/TP2 lock lands one manage cycle (~5s) later live than the instant backtest lock.
- Late TP1/TP2 catch-up closes at market (live) vs at the level (backtest).
- Time-exit at bar close (backtest) vs live tick; same-bar SL+TP resolved stop-wins in backtest.

**Red flags** (investigate / do not go live):
- **Systematic sign flips** — backtest WIN but live LOSS (or vice versa) recurring on trailing-close exits. A few from tick timing are fine; a pattern means the trailing parity is broken.
- **Trailing-close exits consistently worse live** than backtest exit price — the M1-vs-tick trailing gap eating the edge.
- **Entry parity breaking** — backtest fills that live NO_FILL'd (or vice versa) at high rate → the STOP entry isn't reproducing.
- **Per-lot net materially below backtest** ($/closed-lot well under the ~$162 reference) once divergences are accounted for.

---

## 7. Go / abort

- **Run length:** ≥ 2 weeks AND ≥ ~50 resolved signals (enough trailing-close exits to judge the seam). Re-run Section 5 once or twice across the period.
- **Go to live (small size):** per-signal status agreement is high, trailing-close exit prices track the backtest (only small symmetric tick noise), and live per-lot net is in the backtest's neighbourhood.
- **Abort / fix first:** any red flag in Section 6, especially recurring trailing-close sign flips or a one-sided exit-price gap.
- **Sizing on go-live:** the edge is ~2026-trend-concentrated and the 24.9% DD was measured through high-vol 2026 — start well under 2% risk and size up only after live DD is observed, not assumed.