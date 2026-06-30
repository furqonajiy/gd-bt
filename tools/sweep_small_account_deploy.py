#!/usr/bin/env python3
"""Small-account ($2K) SAFE-DEPLOYMENT validation for the TSL18 / T818 scalper.

This is NOT a parameter sweep for alpha. It answers one operational question:
**can the existing profitable-but-volatile 8-entry trailing scalper be run on a
$2,000 account, and if not, what deployment constraints make it survivable?**

It compares a SMALL fixed set of configs on the SAME C160/TSL18 feed and the
SAME geometry, changing ONLY the deployment wrapper:

  base_8entry_50k   reference -- full TSL18 at the engine's $50k base (the known
                    profile; what the strategy "is").
  base_8entry_2k    full 8-entry TSL18 forced onto $2k -- shows the danger: the
                    0.01-lot floor means one failed 8-leg zone can blow the day.
  ts2k_e2_c1_d5_z6  the proposed safe wrapper: entries 2, max 1 concurrent
                    signal, 5% daily-loss breaker, risk-budget gate (zone 6% /
                    single 4%), 0.01 lot, $2k.
  ts2k_e2_c1_d6_z6  same with a 6% daily breaker.
  ts2k_e3_c1_d5_z6  entries 3 variant.

ALWAYS TICK: every cell runs through ``run_hybrid_backtest`` against the
committed ELEV8 tick archive (real fills where covered, M1 only before tick
coverage) -- the closest-to-live evaluation.

It writes ``reports/SMALL_ACCOUNT_<window>/summary.md`` (+ ``metrics.csv``) with
account-risk metrics, the stop-distance distribution, the gate rejection counts,
and the MINIMUM-ACCOUNT-SIZE floor implied by the observed stops. Reporting only
-- it changes no config and promotes nothing (TS2K is research/demo).
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

from trading.engine import (  # noqa: E402
    CONTRACT_SIZE_OZ, CsvChartSource, StrategyConfig, parse_signals_file,
)
import backtest_explicit as bx  # noqa: E402
import tick_backtest as tk  # noqa: E402
from backtest_hybrid import run_hybrid_backtest  # noqa: E402

# TSL18 / T818 geometry (cli/candidate_T818_trailing_tick.txt) -- the SHARED base
# for every cell. Only entries + the deployment gates vary per variant.
TSL18_GEOMETRY = dict(
    sizing_mode="risk", lot_per_entry=0.01, risk_per_signal=0.01,
    minimum_lot=0.01, maximum_lot=500.0, lot_step=0.01, bonus_per_closed_lot=3.0,
    entry_count=8, entry_ladder="range_to_sl", entry_sl_gap=0.7, shared_sl=False,
    activation_delay_minutes=0, pending_expiry_minutes=180, max_hold_minutes=150,
    sl_multiplier=1.8, final_target="TP3", lock_after_tp1=True, lock_after_tp2=True,
    tp1_lock_delay_minutes=24, tp2_lock_delay_minutes=24, profit_lock_mode="tp_levels",
    bep_trigger_distance=3.0, tp1_lock_fraction=0.75, tp2_lock_target="TP1",
    tp3_lock_target="TP2", runner_after_tp3=False, trailing_open_distance=0.5,
    trailing_close_distance=0.5, trailing_close_after_stage=2,
    # R4 era-matched locked-exit slippage (backtest realism; never sent live).
    lock_tp1_exit_slippage_points=2.0, lock_tp2_exit_slippage_points=1.0,
)

WINDOWS = {  # end is EXCLUSIVE (backtest_hybrid --end-date), so 07-01 keeps Jun 30
    "june": ("2026-06-01", "2026-07-01"),
    "jan_jun": ("2026-01-01", "2026-07-01"),
}


def _cfg(initial_capital, **over) -> StrategyConfig:
    return StrategyConfig(initial_capital=initial_capital, **{**TSL18_GEOMETRY, **over})


def variants() -> dict[str, StrategyConfig]:
    gate2 = dict(risk_budget_gate=True, max_single_entry_risk_pct=0.04,
                 max_zone_risk_pct=0.06, max_open_signals=1)
    return {
        "base_8entry_50k": _cfg(50_000.0),
        "base_8entry_2k": _cfg(2_000.0),
        "ts2k_e2_c1_d5_z6": _cfg(2_000.0, entry_count=2, daily_loss_limit_pct=0.05, **gate2),
        "ts2k_e2_c1_d6_z6": _cfg(2_000.0, entry_count=2, daily_loss_limit_pct=0.06, **gate2),
        "ts2k_e3_c1_d5_z6": _cfg(2_000.0, entry_count=3, daily_loss_limit_pct=0.05, **gate2),
    }


def _pct(values, q):
    return float(np.percentile(values, q)) if len(values) else 0.0


def _peak_concurrency_and_lots(entry_rows):
    """Reconstruct max concurrent open SIGNALS and max concurrent open LOTS from
    filled-entry [fill, exit] windows (sweep-line). Signal groups are keyed by
    signal_key; a group is open from its first fill to its last exit."""
    # group fills/exits per signal
    grp_open, grp_close = {}, {}
    leg_events = []  # (time, +/-lot)
    for er in entry_rows:
        ft, xt = er.get("fill_time"), er.get("exit_time")
        if ft is None:
            continue
        key = er["signal_key"]
        grp_open[key] = min(grp_open.get(key, ft), ft)
        # an unexited filled leg keeps the group open to +inf
        if xt is None:
            grp_close[key] = None
        elif grp_close.get(key, xt) is not None:
            grp_close[key] = max(grp_close.get(key, xt), xt)
        lot = float(er.get("lot") or 0.0)
        if xt is not None and lot:
            leg_events.append((ft, lot)); leg_events.append((xt, -lot))
    # signal-group concurrency
    sig_events = []
    BIG = max([t for t in grp_open.values()], default=None)
    for key, o in grp_open.items():
        c = grp_close.get(key)
        sig_events.append((o, 1))
        sig_events.append((c if c is not None else None, -1))
    def _sweep(events):
        opens = sorted((t, d) for t, d in events if t is not None)
        # None-close events (still open at end) never decrement -> contribute to peak
        n_open_forever = sum(1 for t, d in events if t is None and d == -1)
        cur = peak = 0
        # process decrements after increments at same timestamp (exit then re-enter)
        for t, d in sorted(opens, key=lambda e: (e[0], e[1])):
            cur += d
            peak = max(peak, cur)
        return peak + n_open_forever
    peak_sig = _sweep(sig_events)
    # lots: sum of +lot/-lot
    lot_events = sorted(leg_events, key=lambda e: (e[0], e[1]))
    cur = peak_lots = 0.0
    for _, d in lot_events:
        cur += d
        peak_lots = max(peak_lots, cur)
    return peak_sig, round(peak_lots, 4)


def metrics(result, config) -> dict:
    rows = result["rows"]
    entry_rows = result["entry_rows"]
    min_lot, contract = config.minimum_lot, CONTRACT_SIZE_OZ

    # signal-level P&L + streak (a signal loses if its total P&L < 0)
    sig_pnls = [(r["signal_time_source"], r["pnl"]) for r in rows if r["pnl"] is not None]
    sig_wins = sum(1 for _, p in sig_pnls if p > 0)
    sig_n = len(sig_pnls)
    streak = max_streak = 0
    for _, p in sig_pnls:
        if p < 0:
            streak += 1; max_streak = max(max_streak, streak)
        else:
            streak = 0

    # daily P&L (feed-zone/source day, matching the breaker + report)
    daily = defaultdict(float)
    for r in rows:
        if r["pnl"] is not None:
            daily[r["signal_time_source"].date()] += r["pnl"]
    dvals = list(daily.values())
    day_win = sum(1 for v in dvals if v > 0)
    start_cap = config.initial_capital
    max_daily_loss_pct = (min(dvals) / start_cap * 100.0) if dvals else 0.0

    # entry-level outcomes
    filled = [er for er in entry_rows if er.get("fill_time") is not None
              and (er.get("entry_status") or er.get("signal_status")) != "NO_FILL"]
    e_wins = [er["pnl"] for er in filled if (er.get("pnl") or 0) > 0]
    e_losses = [er["pnl"] for er in filled if (er.get("pnl") or 0) < 0]
    n_filled = len(filled) or 1
    avg_win = sum(e_wins) / len(e_wins) if e_wins else 0.0
    avg_loss = sum(e_losses) / len(e_losses) if e_losses else 0.0
    gross_win, gross_loss = sum(e_wins), -sum(e_losses)
    pf = (gross_win / gross_loss) if gross_loss else float("inf")
    payoff = (avg_win / abs(avg_loss)) if avg_loss else float("inf")

    # exit mix (TP1/TP2/TP3 / SL / TIME_EXIT / TRAILING_STOP) from entry status
    status_counts = defaultdict(int)
    for er in filled:
        status_counts[er.get("entry_status") or er.get("signal_status")] += 1

    # stop-distance distribution -> min-lot single-entry risk $ ( = dist*min_lot*contract )
    leg_risk = []  # $ per min-lot leg over ALL planned legs (filled or not)
    for er in entry_rows:
        ep, sl = er.get("entry_price"), er.get("effective_SL")
        if ep is None or sl is None:
            continue
        leg_risk.append(abs(float(ep) - float(sl)) * min_lot * contract)
    # zone risk per signal = sum of its legs' min-lot risk
    zone_risk = defaultdict(float)
    for er in entry_rows:
        ep, sl = er.get("entry_price"), er.get("effective_SL")
        if ep is None or sl is None:
            continue
        zone_risk[er["signal_key"]] += abs(float(ep) - float(sl)) * min_lot * contract
    zvals = list(zone_risk.values())

    peak_sig, peak_lots = _peak_concurrency_and_lots(entry_rows)
    gate = result.get("deployment_gate", {}) or {}
    rej = gate.get("rejected", {})

    return {
        "initial_capital": start_cap,
        "final_equity": result["final_equity"],
        "net_pnl": result["net_profit"],
        "return_pct": result["net_profit"] / start_cap * 100.0,
        "max_drawdown_pct": result["max_drawdown_pct"],
        "max_daily_loss_pct": max_daily_loss_pct,
        "daily_win_rate": day_win / len(dvals) * 100.0 if dvals else 0.0,
        "trading_days": len(dvals),
        "signals": sig_n,
        "entries_filled": len(filled),
        "entry_win_rate": len(e_wins) / n_filled * 100.0,
        "signal_win_rate": sig_wins / sig_n * 100.0 if sig_n else 0.0,
        "profit_factor": pf,
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff_ratio": payoff,
        "max_consecutive_losing_signals": max_streak,
        "max_concurrent_open_signals_seen": peak_sig,
        "max_open_lots_seen": peak_lots,
        "tp3_hits": status_counts.get("TP3", 0),
        "tp2_hits": status_counts.get("TP2", 0),
        "tp1_hits": status_counts.get("TP1", 0),
        "sl_hits": status_counts.get("SL", 0),
        "time_exits": status_counts.get("TIME_EXIT", 0),
        "trailing_stops": status_counts.get("TRAILING_STOP", 0),
        "signals_rejected_by_daily_loss": rej.get("daily_loss_breaker", 0),
        "signals_rejected_by_concurrency": rej.get("max_open_signals", 0),
        "signals_rejected_by_risk_budget": rej.get("risk_budget_single", 0) + rej.get("risk_budget_zone", 0),
        "data_sources": result.get("data_sources", {}),
        # distributions (kept for the floor calc + report)
        "_leg_risk": leg_risk,
        "_zone_risk": zvals,
    }


def account_floor_table(leg_risk) -> list[dict]:
    """Min-account-size floor from observed per-min-lot-leg dollar stop risk D.
    faithful 1% per leg = 100*D ; full 8-entry <=4% zone = 200*D ;
    safe 2-entry <=6% zone = 33.33*D."""
    out = []
    for label, q in [("p50", 50), ("p75", 75), ("p90", 90), ("p95", 95), ("max", 100)]:
        D = _pct(leg_risk, q)
        out.append({
            "pct": label, "stop_$_at_min_lot": D,
            "faithful_1pct_floor": 100.0 * D,
            "full_8entry_4pct_floor": 200.0 * D,
            "safe_2entry_6pct_floor": (2.0 / 0.06) * D,
        })
    return out


def run(window: str, signals_path: str, charts, ticks, variant_names, watch_seconds):
    start, end = WINDOWS[window]
    out_dir = ROOT / "reports" / f"SMALL_ACCOUNT_{window}"
    out_dir.mkdir(parents=True, exist_ok=True)

    sigs_all = bx.filter_signals_by_date(parse_signals_file(Path(signals_path)), start, end)
    chart = CsvChartSource(bx._expand_chart_paths(charts))
    tick_paths = tk._expand(ticks)
    tickdf = tk.load_ticks(tick_paths) if tick_paths else None
    print(f"[small-acct] {window}: {len(sigs_all)} signals  "
          f"ticks={0 if tickdf is None else len(tickdf)} rows", flush=True)

    allv = variants()
    results = {}
    for name in variant_names:
        cfg = allv[name]
        print(f"[small-acct] {window}: running {name} (cap ${cfg.initial_capital:,.0f}, "
              f"entries {cfg.entry_count}) ...", flush=True)
        res = run_hybrid_backtest(list(sigs_all), chart, tickdf, cfg,
                                  watch_seconds=watch_seconds)
        m = metrics(res, cfg)
        results[name] = (cfg, m)
        ds = m["data_sources"]
        print(f"[small-acct]   {name}: net ${m['net_pnl']:,.0f}  DD {m['max_drawdown_pct']:.1f}%  "
              f"maxDayLoss {m['max_daily_loss_pct']:.1f}%  dailyWR {m['daily_win_rate']:.0f}%  "
              f"sigWR {m['signal_win_rate']:.0f}%  maxLoseStreak {m['max_consecutive_losing_signals']}  "
              f"peakConc {m['max_concurrent_open_signals_seen']}  "
              f"rejRB {m['signals_rejected_by_risk_budget']} rejDay {m['signals_rejected_by_daily_loss']} "
              f"rejConc {m['signals_rejected_by_concurrency']}  "
              f"[{ds.get('tick_signals',0)}T/{ds.get('m1_signals',0)}M1]", flush=True)

    _write_reports(window, out_dir, results)
    return out_dir


def _fmt(v, money=False, pct=False):
    if isinstance(v, float):
        if v == float("inf"):
            return "inf"
        if money:
            return f"${v:,.0f}"
        if pct:
            return f"{v:.1f}%"
        return f"{v:.2f}"
    return str(v)


def _write_reports(window, out_dir, results):
    # metrics.csv
    keys = [k for k in next(iter(results.values()))[1] if not k.startswith("_")
            and k != "data_sources"]
    with open(out_dir / "metrics.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["variant"] + keys)
        for name, (_, m) in results.items():
            w.writerow([name] + [m[k] for k in keys])

    lines = [f"# Small-account ($2K) safe-deployment validation -- {window}", ""]
    lines.append("TSL18 / T818 feed + geometry, TICK-preferred. Only entries + the "
                 "deployment gates change between cells. Reporting only -- promotes nothing.")
    lines.append("")
    # headline table
    cols = [("variant", "variant"), ("initial_capital", "cap"), ("net_pnl", "net"),
            ("return_pct", "ret%"), ("max_drawdown_pct", "maxDD%"),
            ("max_daily_loss_pct", "worstDay%"), ("daily_win_rate", "dailyWR%"),
            ("signal_win_rate", "sigWR%"), ("entry_win_rate", "entryWR%"),
            ("payoff_ratio", "payoff"), ("profit_factor", "PF"),
            ("max_consecutive_losing_signals", "maxLoseStreak"),
            ("max_concurrent_open_signals_seen", "peakConcSig"),
            ("max_open_lots_seen", "peakLots")]
    lines.append("| " + " | ".join(c[1] for c in cols) + " |")
    lines.append("|" + "|".join("---" for _ in cols) + "|")
    for name, (_, m) in results.items():
        cells = []
        for key, _ in cols:
            if key == "variant":
                cells.append(name)
            elif key == "initial_capital":
                cells.append(f"${m[key]:,.0f}")
            elif key == "net_pnl":
                cells.append(f"${m[key]:,.0f}")
            elif key.endswith("_pct") or key.endswith("_rate"):
                cells.append(f"{m[key]:.1f}")
            elif key in ("payoff_ratio", "profit_factor"):
                cells.append("inf" if m[key] == float("inf") else f"{m[key]:.2f}")
            else:
                cells.append(str(m[key]))
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    # exit mix + rejections
    lines.append("## Exit mix & gate rejections")
    lines.append("")
    lines.append("| variant | TP3 | TP2 | TP1 | SL | TIME | TRAIL | rejRiskBudget | rejDaily | rejConc |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for name, (_, m) in results.items():
        lines.append(f"| {name} | {m['tp3_hits']} | {m['tp2_hits']} | {m['tp1_hits']} | "
                     f"{m['sl_hits']} | {m['time_exits']} | {m['trailing_stops']} | "
                     f"{m['signals_rejected_by_risk_budget']} | {m['signals_rejected_by_daily_loss']} | "
                     f"{m['signals_rejected_by_concurrency']} |")
    lines.append("")

    # account-size floor (from the base 8-entry leg-risk distribution -- the true feed)
    base = results.get("base_8entry_50k") or next(iter(results.values()))
    floor = account_floor_table(base[1]["_leg_risk"])
    zvals = base[1]["_zone_risk"]
    lines.append("## Minimum account-size floor (from observed stop distances)")
    lines.append("")
    lines.append("`D` = dollar risk of ONE 0.01-lot leg if stopped out "
                 "(= stop distance in price, since 0.01 lot x 100 = $1/pt). "
                 "Floors: faithful 1%/leg = 100xD; full 8-entry zone <=4% = 200xD; "
                 "safe 2-entry zone <=6% = 33.3xD.")
    lines.append("")
    lines.append("| stop pct | D ($/0.01 leg) | faithful 1%/leg floor | full-8-entry <=4% floor | safe-2-entry <=6% floor |")
    lines.append("|---|---|---|---|---|")
    for r in floor:
        lines.append(f"| {r['pct']} | ${r['stop_$_at_min_lot']:.1f} | "
                     f"${r['faithful_1pct_floor']:,.0f} | ${r['full_8entry_4pct_floor']:,.0f} | "
                     f"${r['safe_2entry_6pct_floor']:,.0f} |")
    lines.append("")
    if zvals:
        lines.append(f"Observed 8-entry ZONE risk at 0.01 lot (whole ladder, $): "
                     f"p50 ${_pct(zvals,50):.0f}  p90 ${_pct(zvals,90):.0f}  "
                     f"p95 ${_pct(zvals,95):.0f}  max ${_pct(zvals,100):.0f}. "
                     f"On $2k, a p95 zone is {_pct(zvals,95)/2000*100:.0f}% of the account.")
        lines.append("")

    lines.append("## Verdict")
    lines.append("")
    b2k = results.get("base_8entry_2k")
    ts = results.get("ts2k_e2_c1_d5_z6")
    if b2k and ts:
        lines.append(f"- Full 8-entry TSL18 at $2k: worst day {b2k[1]['max_daily_loss_pct']:.1f}%, "
                     f"max DD {b2k[1]['max_drawdown_pct']:.1f}%, max losing-signal streak "
                     f"{b2k[1]['max_consecutive_losing_signals']}.")
        lines.append(f"- TS2K (e2/conc1/daily5/zone6) at $2k: worst day {ts[1]['max_daily_loss_pct']:.1f}%, "
                     f"max DD {ts[1]['max_drawdown_pct']:.1f}%, net {_fmt(ts[1]['net_pnl'], money=True)}, "
                     f"return {ts[1]['return_pct']:.1f}%, daily win rate {ts[1]['daily_win_rate']:.0f}%.")
        lines.append(f"- Drawdown reduction: {b2k[1]['max_drawdown_pct']:.1f}% -> "
                     f"{ts[1]['max_drawdown_pct']:.1f}%; worst day "
                     f"{b2k[1]['max_daily_loss_pct']:.1f}% -> {ts[1]['max_daily_loss_pct']:.1f}%.")
    lines.append("")
    (out_dir / "summary.md").write_text("\n".join(lines))
    print(f"[small-acct] wrote {out_dir/'summary.md'} and metrics.csv", flush=True)


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--window", choices=list(WINDOWS), default="june")
    p.add_argument("--signals", default="signals/t818.txt",
                   help="TSL18/T818 (C160) feed; default signals/t818.txt")
    p.add_argument("--charts", nargs="+", default=["data/XAUUSD_M1_*_ELEV8.csv"])
    p.add_argument("--ticks", nargs="+", default=["data/ticks/XAUUSD_TICK_*_ELEV8.csv"])
    p.add_argument("--variants", nargs="+", default=list(variants().keys()),
                   help="subset of variant names to run (default: all)")
    p.add_argument("--watch-seconds", type=int, default=3)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    run(a.window, a.signals, a.charts, a.ticks, a.variants, a.watch_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
