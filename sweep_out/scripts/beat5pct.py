"""Beat-Victor-at-5% evaluator. For every self-config that passes the DD gate,
run it at EXACTLY 5% risk (bonus=0, same metric as current_cli's trading_pnl)
and rank by compounded trading P&L vs Victor's $710.2M / 49.4% DD baseline.
Checkpointed to sweep_out/beat5pct.jsonl; resumable."""
import glob, json, sys, time
from pathlib import Path
ROOT = Path("/home/user/xauusd-backtest")
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import sweep
from xauusd_trading import CsvChartSource, parse_signals_file

VICTOR_PNL, VICTOR_DD = 710_219_435.0, 49.4
OUT = ROOT / "sweep_out/beat5pct.jsonl"
TOP_PER_ARCHIVE = 6
CHARTS = sorted(glob.glob(str(ROOT / "data/XAUUSD_M1_2025*_ELEV8.csv"))) + \
         sorted(glob.glob(str(ROOT / "data/XAUUSD_M1_2026*_ELEV8.csv")))


def main():
    chart = CsvChartSource(sweep._expand_chart_paths([str(p) for p in CHARTS]))
    sigcache = {}
    done = set()
    if OUT.exists():
        for l in OUT.open():
            r = json.loads(l); done.add((r["archive"], r["cid"]))
    for res in sorted(glob.glob(str(ROOT / "sweep_out/self_sweep_*/results.jsonl"))):
        name = Path(res).parent.name
        archive = name.replace("self_sweep_trail_", "").replace("self_sweep_", "")
        rows = [json.loads(l) for l in open(res) if l.strip()]
        rows = [r for r in rows if r.get("fixed_no_bonus_profit") is not None]
        # rank candidates by raw edge; gate decided here at true 5%
        rows.sort(key=lambda r: float(r["fixed_no_bonus_profit"]), reverse=True)
        for r in rows[:TOP_PER_ARCHIVE]:
            cid = r.get("candidate_id", "?")
            if (archive, cid) in done:
                continue
            if archive not in sigcache:
                sigcache[archive] = parse_signals_file(ROOT / f"generated/self_{archive}.txt")
            cfg = dict(r["config"]); cfg["sizing_mode"] = "risk"
            cfg["risk_per_signal"] = 0.05; cfg["initial_capital"] = 5000.0
            t0 = time.time()
            bt = sweep.run_concurrent_backtest(
                sigcache[archive], chart, sweep.config_from_dict(cfg, bonus=0.0), label="b5")
            dd = abs(float(bt.get("max_drawdown_pct") or 0)); pnl = float(bt.get("net_profit") or 0)
            rec = {"archive": archive, "cid": cid, "pnl_5pct": pnl, "dd_5pct": dd,
                   "passes_dd50": dd <= 50.0, "beats_victor": (pnl > VICTOR_PNL and dd <= 50.0),
                   "edge": r["fixed_no_bonus_profit"], "n_sig": len(sigcache[archive]),
                   "config": r["config"], "secs": round(time.time() - t0, 1)}
            with OUT.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            flag = "*** BEATS VICTOR ***" if rec["beats_victor"] else ("ok" if dd <= 50 else "DD-fail")
            c = r["config"]
            print(f"{archive:18s} e{c['entry_count']} slm{c['sl_multiplier']} d{c['tp1_lock_delay_minutes']}: "
                  f"pnl=${pnl:>16,.0f} dd={dd:5.1f}% {flag}", flush=True)
    print("BEAT5PCT COMPLETE")


if __name__ == "__main__":
    main()
