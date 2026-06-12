"""Final-stage risk post-pass (tracked copy so it survives resets).

For each finished sweep, take the top-edge configs and walk risk down from 5%
to the highest level whose CONCURRENT drawdown stays <= 50%, recording the
risk-sized compounded trading P&L (no bonus) at that risk. Checkpointed +
resumable via reports... here writes to sweep_out/risk_postpass.jsonl.

Run from repo root:  python sweep_out/scripts/risk_postpass.py
"""
import glob
import json
import sys
import time
from pathlib import Path

ROOT = Path("/home/user/xauusd-backtest")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import sweep  # noqa: E402
from xauusd_trading import CsvChartSource, parse_signals_file  # noqa: E402

RISKS = [0.05, 0.045, 0.035, 0.0275, 0.02]
TOP_PER_SWEEP = 8
OUT = ROOT / "sweep_out/risk_postpass.jsonl"
CHARTS = sorted(glob.glob(str(ROOT / "data/XAUUSD_M1_2025*_ELEV8.csv"))) + \
         sorted(glob.glob(str(ROOT / "data/XAUUSD_M1_2026*_ELEV8.csv")))


def main() -> None:
    chart = CsvChartSource(sweep._expand_chart_paths([str(p) for p in CHARTS]))
    sig_cache: dict[str, list] = {}
    done = set()
    if OUT.exists():
        for line in OUT.open():
            r = json.loads(line)
            done.add((r["sweep"], r["candidate_id"], r["risk"]))

    for res in sorted(glob.glob(str(ROOT / "sweep_out/self_sweep_*/results.jsonl"))):
        sweep_name = Path(res).parent.name
        archive = sweep_name.replace("self_sweep_trail_", "").replace("self_sweep_", "")
        rows = [json.loads(l) for l in open(res) if l.strip()]
        rows = [r for r in rows if r.get("fixed_no_bonus_profit") is not None]
        rows.sort(key=lambda r: float(r["fixed_no_bonus_profit"]), reverse=True)
        top = rows[:TOP_PER_SWEEP]
        if not top:
            continue
        if archive not in sig_cache:
            sig_cache[archive] = parse_signals_file(ROOT / f"generated/self_{archive}.txt")
        signals = sig_cache[archive]
        for r in top:
            cfg = dict(r["config"])
            for risk in RISKS:
                key = (sweep_name, r["candidate_id"], risk)
                if key in done:
                    continue
                cfg2 = dict(cfg); cfg2["sizing_mode"] = "risk"; cfg2["risk_per_signal"] = risk
                t0 = time.time()
                bt = sweep.run_concurrent_backtest(
                    signals, chart, sweep.config_from_dict(cfg2, bonus=0.0),
                    exclude_structural_anomalies=False, label="postpass")
                dd = abs(float(bt.get("max_drawdown_pct") or 0.0))
                net = float(bt.get("net_profit") or 0.0)
                row = {"sweep": sweep_name, "archive": archive,
                       "candidate_id": r["candidate_id"], "risk": risk,
                       "edge_fixed": r["fixed_no_bonus_profit"],
                       "oos_edge": r.get("oos_fixed_no_bonus_profit"),
                       "risk_net_profit_nb": net, "dd_pct": dd,
                       "passes_dd50": dd <= 50.0,
                       "config_json": json.dumps(cfg, sort_keys=True),
                       "secs": round(time.time() - t0, 1)}
                with OUT.open("a") as f:
                    f.write(json.dumps(row) + "\n")
                print(f"{sweep_name:30s} {r['candidate_id'][:8]} risk={risk:<6} "
                      f"net={net:>13.0f} dd={dd:5.1f} {'PASS' if dd <= 50 else 'fail'}",
                      flush=True)
                if dd <= 50.0:
                    break
    print("POSTPASS COMPLETE")


if __name__ == "__main__":
    main()
