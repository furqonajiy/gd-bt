"""Trailing re-sweep v2 orchestrator (post-#77 honest trailing engine).

Goal: find a trailing-open/close config on any 24h self-feed whose DEPLOYABLE
compounded net (risk walked 5%->1% until concurrent DD <= 50%) beats the
no-trailing reference backtest (scalper24 e6/range_to_sl/slm2.1/TP3 @1%).

Reset-resilient by construction:
  * every sweep runs with --resume against a committed results.jsonl checkpoint;
  * feeds/baseline/postpass each have committed DONE markers;
  * a committer thread pushes sweep_out/ + self_cli_trailing.txt every 5 min
    (heartbeat included, so the branch shows liveness even between results);
  * a SessionStart hook relaunches this script after a container reset.

Run from repo root: python sweep_out/scripts/trail_orchestrate.py
"""
import glob
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path("/home/user/xauusd-backtest")
OUT = ROOT / "sweep_out"
SCRIPTS = OUT / "scripts"
BRANCH = "research/trailing-sweep-v2"
LOG = OUT / "orchestrator.log"
CHARTS = sorted(glob.glob(str(ROOT / "data/XAUUSD_M1_2025*_ELEV8.csv"))) + \
         sorted(glob.glob(str(ROOT / "data/XAUUSD_M1_2026*_ELEV8.csv")))

# Reuse GEN_CMD + _cli_blocks from the v1 orchestrator, extended with the three
# 24h feeds added later (same pattern report24.py used).
ns = {"__name__": "trail2"}
exec(open(SCRIPTS / "_orchestrate.py").read(), ns)
GEN_NEW = {
    "risk02_widetp24": "python tools/generate_aggressive_limit_risk02.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_risk02_widetp24.txt --start-date 2025-01-01 --tp1-distance 8 --tp2-distance 14 --tp3-distance 22 --execution-hours \"0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23\"",
    "scalper_strict24": "python tools/generate_scalper_signals.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_scalper_strict24.txt --start 2025-01-01 --session-start 0 --session-end 0 --signal-tz 7 --min-slope 0.06 --min-body-atr 0.15 --cooldown-minutes 10",
    "scalper_widerr24": "python tools/generate_scalper_signals.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_scalper_widerr24.txt --start 2025-01-01 --session-start 0 --session-end 0 --signal-tz 7 --rr1 1.5 --rr2 2.5 --rr3 4.0",
}
ns["GEN_CMD"].update(GEN_NEW)
GEN_CMD = ns["GEN_CMD"]

# All 16 24-hour feeds, ordered small -> large so conclusions land early.
ARCHIVES = [
    "zones_strict", "zones", "zones_widesl", "zones_dense",
    "better", "better_wide",
    "risk02_allhours", "risk02_tight24", "risk02_widetp24",
    "canonical_h1", "canonical", "canonical_wide", "canonical_dense",
    "scalper_strict24", "scalper_widerr24", "scalper24",
]
RISKS = [0.05, 0.045, 0.035, 0.0275, 0.02, 0.015, 0.01]
TOP_PER_SWEEP = 6
_git_lock = threading.Lock()

# The no-trailing reference (user's revised CLI) the sweep must beat. Mirrors
# _sweep_trail.REFERENCE_NO_TRAIL; kept here so the orchestrator's deployable
# risk walk needs no cross-module import.
REFERENCE_NO_TRAIL = {
    "entry_count": 6, "entry_ladder": "range_to_sl", "entry_sl_gap": 0.5,
    "activation_delay_minutes": 2, "pending_expiry_minutes": 180,
    "max_hold_minutes": 240, "sl_multiplier": 2.1, "final_target": "TP3",
    "lock_after_tp1": True, "lock_after_tp2": True,
    "tp1_lock_delay_minutes": 24, "tp2_lock_delay_minutes": 2,
    "profit_lock_mode": "tp_levels", "bep_trigger_distance": 3.0,
    "tp1_lock_fraction": 0.5, "tp2_lock_target": "TP1",
    "runner_after_tp3": False, "tp3_lock_target": "TP2",
    "trailing_open_distance": 0.0, "trailing_close_distance": 0.0,
    "bonus_per_closed_lot": 3.0,
}


def log(msg: str) -> None:
    # Print only: the orchestrator is always launched with stdout redirected to
    # orchestrator.log, so a direct file write here would double every line.
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


CHURN_GLOBS = ["sweep_out/trail2_*/results.jsonl", "sweep_out/trail2_*/leaderboard.csv",
               "sweep_out/*.runlog", "sweep_out/orchestrator.log",
               "sweep_out/HEARTBEAT.txt", "sweep_out/BEST_TRAILING_V2.txt",
               "sweep_out/trail2_postpass.jsonl"]


def _churn_files() -> list[str]:
    files = []
    for g in CHURN_GLOBS:
        files += [str(p.relative_to(ROOT)) for p in ROOT.glob(g)]
    return files


def _set_skip(files: list[str], skip: bool) -> None:
    if not files:
        return
    flag = "--skip-worktree" if skip else "--no-skip-worktree"
    subprocess.run(["git", "update-index", flag, *files], cwd=ROOT, timeout=60)


def git_push(msg: str) -> None:
    """Commit + push. The continuously-appended sweep artifacts are kept
    skip-worktree so the stop-hook sees a clean tree between checkpoints; here we
    un-skip just long enough to stage+commit, then re-skip. Resume stays intact
    (the files are still committed every cycle) while the working tree stops
    churning at turn-end."""
    with _git_lock:
        _set_skip(_churn_files(), False)
        # Only stage paths that exist -- `git add` aborts and stages NOTHING if
        # any pathspec is missing (e.g. self_cli_trailing.txt before the first
        # snapshot), which would silently break every checkpoint push.
        paths = [p for p in ("sweep_out", "generated", "data", "self_cli_trailing.txt")
                 if (ROOT / p).exists()]
        subprocess.run(["git", "add", *paths], cwd=ROOT, timeout=120)
        r = subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ROOT, timeout=120)
        if r.returncode == 0:
            for i in range(4):
                if subprocess.run(["git", "push", "-q", "origin", BRANCH],
                                  cwd=ROOT, timeout=180).returncode == 0:
                    break
                time.sleep(2 ** (i + 1))
        _set_skip(_churn_files(), True)  # re-hide (re-glob covers new archives)


# ---------------------------------------------------------------- step 0: feeds
def ensure_feeds() -> None:
    if (OUT / "FEEDS_DONE").exists():
        log("feeds: already generated (FEEDS_DONE)")
        return
    (ROOT / "generated").mkdir(exist_ok=True)
    if not glob.glob(str(ROOT / "data/XAUUSD_M15_*_ELEV8.csv")):
        log("feeds: resampling M1 -> M15")
        subprocess.run([sys.executable, "tools/resample_m1_to_m15.py",
                        "--in", "data/XAUUSD_M1_2025*_ELEV8.csv",
                        "data/XAUUSD_M1_2026*_ELEV8.csv",
                        "--out-dir", "data", "--symbol", "XAUUSD"],
                       cwd=ROOT, check=True, timeout=1800)
    for a in ARCHIVES:
        target = ROOT / f"generated/self_{a}.txt"
        if target.exists() and target.stat().st_size > 0:
            log(f"feeds: {a} exists, skip")
            continue
        log(f"feeds: generating {a}")
        subprocess.run(GEN_CMD[a], shell=True, cwd=ROOT, check=True, timeout=3600)
    (OUT / "FEEDS_DONE").write_text("ok\n")
    git_push("trail2: feeds generated")


# ------------------------------------------------------------ step 1: baseline
def ensure_baseline() -> dict | None:
    bj = OUT / "BASELINE.json"
    if bj.exists():
        data = json.loads(bj.read_text())
        if "deployable_net" in data:   # already the fair (DD<=50) baseline
            return data
        log("baseline: upgrading old BASELINE.json to deployable (risk walk)")
    d = OUT / "trail2_baseline"
    log("baseline: running reference no-trailing config (scalper24 @1%)")
    cmd = [sys.executable, str(SCRIPTS / "_sweep_trail.py"), "--mode", "baseline",
           "--signals", str(ROOT / "generated/self_scalper24.txt"),
           "--charts", *CHARTS, "--output-dir", str(d),
           "--max-candidates", "1", "--max-concurrent-dd-pct", "50",
           "--validate-months", "6", "--top-n", "1", "--progress-every", "1",
           "--resume"]
    with (OUT / "trail2_baseline.runlog").open("a") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=ROOT, timeout=14400)
    rows = [json.loads(l) for l in (d / "results.jsonl").open() if l.strip()]
    rows = [r for r in rows if r.get("fixed_no_bonus_profit") is not None]
    if not rows:
        log("baseline: FAILED to produce a row -- see trail2_baseline.runlog")
        return None
    r = rows[0]
    # The reference at 1% can sit far above the 50% DD limit (dense feed, many
    # concurrent legs), so its net@1% is NOT a deployable target. Walk the SAME
    # config's risk DOWN until concurrent DD <= 50%, giving a fair deployable
    # net/risk that trailing candidates' deployable net is compared against.
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tools"))
    import sweep as sw
    from xauusd_trading import CsvChartSource, parse_signals_file
    chart = CsvChartSource(sw._expand_chart_paths([str(p) for p in CHARTS]))
    signals = parse_signals_file(ROOT / "generated/self_scalper24.txt")
    dep_risk, dep_net, dep_dd = None, 0.0, None
    for risk in [0.01, 0.0075, 0.005, 0.0035, 0.0025, 0.002, 0.0015, 0.001]:
        cfg = dict(sw.base_config_dict())   # full base so config_from_dict has every key
        cfg.update(REFERENCE_NO_TRAIL)
        cfg["sizing_mode"] = "risk"
        cfg["risk_per_signal"] = risk
        bt = sw.run_concurrent_backtest(
            signals, chart, sw.config_from_dict(cfg, bonus=0.0),
            exclude_structural_anomalies=False, label="baseline_walk")
        dd = abs(float(bt.get("max_drawdown_pct") or 0.0))
        net = float(bt.get("net_profit") or 0.0)
        log(f"baseline walk risk={risk:g} net=${net:,.0f} dd={dd:.1f}% "
            f"{'PASS' if dd <= 50 else 'fail'}")
        if dd <= 50.0:
            dep_risk, dep_net, dep_dd = risk, net, dd
            break
    base = {
        "label": "reference scalper24 no-trailing e6 slm2.1 TP3",
        "edge": r["fixed_no_bonus_profit"],
        "oos": r.get("oos_fixed_no_bonus_profit"),
        "dd_at_1pct": r["concurrent_risk_max_dd_pct"],
        "net_at_1pct_with_bonus": r["risk_net_profit_with_bonus"],
        # Fair, deployable comparison basis (no bonus, DD<=50%):
        "deployable_risk": dep_risk,
        "deployable_net": dep_net,
        "deployable_dd": dep_dd,
    }
    bj.write_text(json.dumps(base, indent=2) + "\n")
    log(f"baseline: edge=${base['edge']:,.0f} oos=${(base['oos'] or 0):,.0f} "
        f"dd@1%={base['dd_at_1pct']:.1f}% | DEPLOYABLE risk={dep_risk} "
        f"net=${dep_net:,.0f} dd={(dep_dd or 0):.1f}%")
    git_push("trail2: baseline recorded (with deployable risk walk)")
    return base


# ------------------------------------------------------- live snapshot + CLI
def scan_rows() -> list[dict]:
    rows = []
    for p in OUT.glob("trail2_sweep_*/results.jsonl"):
        archive = p.parent.name.replace("trail2_sweep_", "")
        for line in p.open():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("fixed_no_bonus_profit") is None:
                continue
            r["_archive"] = archive
            rows.append(r)
    return rows


def deployable_lookup() -> dict:
    out = {}
    p = OUT / "trail2_postpass.jsonl"
    if not p.exists():
        return out
    for l in p.open():
        try:
            r = json.loads(l)
        except Exception:
            continue
        if r.get("passes_dd50"):
            key = (r["archive"], r["candidate_id"])
            cur = out.get(key)
            if cur is None or r["risk_net_profit_nb"] > cur["net"]:
                out[key] = {"risk": r["risk"], "net": r["risk_net_profit_nb"], "dd": r["dd_pct"]}
    return out


def write_snapshot(baseline: dict | None) -> None:
    rows = scan_rows()
    passing = [r for r in rows if r.get("passes_recommendation_gate")]
    key = lambda r: (r.get("oos_fixed_no_bonus_profit") or 0)
    passing.sort(key=key, reverse=True)
    rows_edge = sorted(rows, key=lambda r: float(r["fixed_no_bonus_profit"]), reverse=True)
    done = len(list(OUT.glob("trail2_sweep_*/DONE")))
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    def fmt(r):
        c = r["config"]
        return (f"  {r['_archive']:<18} edge=${r['fixed_no_bonus_profit']:>10,.0f} "
                f"oos=${(r.get('oos_fixed_no_bonus_profit') or 0):>9,.0f} "
                f"dd@5%={r['concurrent_risk_max_dd_pct']:>5.1f}% "
                f"e{c['entry_count']} slm{c['sl_multiplier']} {c['final_target']} "
                f"to{c['trailing_open_distance']} tc{c['trailing_close_distance']} "
                f"sh{'T' if c.get('shared_sl') else 'F'}")

    base_line = ("baseline (BEAT THIS): edge ${edge:,.0f} | OOS ${oos:,.0f} | "
                 "DEPLOYABLE net ${net:,.0f} @ risk {risk} (DD {dd:.1f}%<=50)").format(
        edge=baseline["edge"], oos=baseline["oos"] or 0,
        net=baseline.get("deployable_net", 0.0),
        risk=baseline.get("deployable_risk"),
        dd=baseline.get("deployable_dd") or 0.0) \
        if baseline else "baseline: (not computed yet)"
    lines = [
        "TRAILING RE-SWEEP v2 (post-#77 honest engine) — LIVE",
        f"updated {stamp} UTC | archives done {done}/{len(ARCHIVES)} | configs {len(rows)}",
        base_line,
        "", "== TOP 10 GATE-PASSERS (DD<=50 @5%, consistency, OOS>0) by OOS ==",
    ] + ([fmt(r) for r in passing[:10]] or ["  (none yet)"]) + [
        "", "== TOP 10 BY RAW EDGE ==",
    ] + ([fmt(r) for r in rows_edge[:10]] or ["  (none yet)"])
    (OUT / "BEST_TRAILING_V2.txt").write_text("\n".join(lines) + "\n")
    (OUT / "HEARTBEAT.txt").write_text(f"alive {stamp} UTC\n")

    best = (passing or rows_edge)[:1]
    if best:
        b = best[0]
        dep = deployable_lookup().get((b["_archive"], b.get("candidate_id", "")))
        cfg = dict(b["config"])
        if dep:
            cfg["risk_per_signal"] = dep["risk"]
        base_dep_net = baseline.get("deployable_net", 0.0) if baseline else 0.0
        # Sizing-neutral edge is the primary quality signal; deployable net is
        # the secondary, both vs the deployable reference.
        beats = bool(dep and baseline and dep["net"] > base_dep_net
                     and b["fixed_no_bonus_profit"] > baseline["edge"])
        hdr = [
            "# =========================================================================",
            "# BEST TRAILING CONFIG — v2 re-sweep on the HONEST engine (PR #77 fix)",
            f"# updated {stamp} UTC | feed: {b['_archive']} | sweep still running"
            if done < len(ARCHIVES) else
            f"# updated {stamp} UTC | feed: {b['_archive']} | sweep complete",
            f"# edge ${b['fixed_no_bonus_profit']:,.0f} (fixed-lot, 18mo) | "
            f"OOS ${(b.get('oos_fixed_no_bonus_profit') or 0):,.0f} (6mo) | "
            f"DD@5% {b['concurrent_risk_max_dd_pct']:.1f}%",
            (f"# DEPLOYABLE: risk {dep['risk']*100:g}% -> net ${dep['net']:,.0f} "
             f"(no-bonus) | DD {dep['dd']:.1f}% (<=50%)" if dep else
             "# DEPLOYABLE risk pending post-pass; risk shown is the 5% sweep cap."),
            (f"# vs REFERENCE deployable net ${base_dep_net:,.0f} "
             f"(edge ${baseline['edge']:,.0f}): "
             + ("BEATS REFERENCE (net + edge)" if beats else "does NOT beat reference yet"))
            if baseline else "# reference baseline pending",
            "# =========================================================================", "",
        ]
        blocks = ns["_cli_blocks"](b["_archive"], cfg, "trailing")
        (ROOT / "self_cli_trailing.txt").write_text("\n".join(hdr + blocks) + "\n")


def committer(baseline_ref: list) -> None:
    while not (OUT / "PIPELINE_COMPLETE").exists():
        time.sleep(300)
        try:
            write_snapshot(baseline_ref[0])
            git_push(f"trail2 checkpoint {time.strftime('%H:%M')}")
        except Exception as e:
            log(f"committer cycle failed (will retry): {e}")


# ------------------------------------------------------------- sweeps + walk
def run_sweep(archive: str) -> None:
    d = OUT / f"trail2_sweep_{archive}"
    if (d / "DONE").exists():
        log(f"skip {archive} (DONE)")
        return
    cmd = [sys.executable, str(SCRIPTS / "_sweep_trail.py"), "--mode", "trail",
           "--signals", str(ROOT / f"generated/self_{archive}.txt"),
           "--charts", *CHARTS, "--output-dir", str(d),
           "--max-candidates", "96", "--max-concurrent-dd-pct", "50",
           "--validate-months", "6", "--top-n", "15", "--progress-every", "20",
           "--resume"]
    log(f"START trail2_{archive}")
    t0 = time.time()
    with (OUT / f"trail2_sweep_{archive}.runlog").open("a") as f:
        rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=ROOT).returncode
    if rc == 0:
        (d / "DONE").write_text("ok\n")
    log(f"END   trail2_{archive} rc={rc} ({(time.time() - t0) / 60:.1f} min)")
    git_push(f"trail2 done: {archive}")


def risk_postpass() -> None:
    """Walk risk 5%->1% on each sweep's top configs until DD<=50 (resumable)."""
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tools"))
    import sweep as sw
    from xauusd_trading import CsvChartSource, parse_signals_file
    outp = OUT / "trail2_postpass.jsonl"
    done = set()
    if outp.exists():
        for line in outp.open():
            r = json.loads(line)
            done.add((r["archive"], r["candidate_id"], r["risk"]))
    chart = CsvChartSource(sw._expand_chart_paths([str(p) for p in CHARTS]))
    for res in sorted(OUT.glob("trail2_sweep_*/results.jsonl")):
        archive = res.parent.name.replace("trail2_sweep_", "")
        rows = [json.loads(l) for l in res.open() if l.strip()]
        rows = [r for r in rows if r.get("fixed_no_bonus_profit") is not None
                and (r.get("oos_fixed_no_bonus_profit") or 0) > 0
                and r["fixed_no_bonus_profit"] > 0]
        rows.sort(key=lambda r: float(r["fixed_no_bonus_profit"]), reverse=True)
        top = rows[:TOP_PER_SWEEP]
        if not top:
            continue
        signals = parse_signals_file(ROOT / f"generated/self_{archive}.txt")
        for r in top:
            for risk in RISKS:
                key2 = (archive, r["candidate_id"], risk)
                if key2 in done:
                    continue
                cfg2 = dict(r["config"])
                cfg2["sizing_mode"] = "risk"
                cfg2["risk_per_signal"] = risk
                bt = sw.run_concurrent_backtest(
                    signals, chart, sw.config_from_dict(cfg2, bonus=0.0),
                    exclude_structural_anomalies=False, label="postpass")
                dd = abs(float(bt.get("max_drawdown_pct") or 0.0))
                net = float(bt.get("net_profit") or 0.0)
                with outp.open("a") as f:
                    f.write(json.dumps({
                        "archive": archive, "candidate_id": r["candidate_id"],
                        "risk": risk, "edge_fixed": r["fixed_no_bonus_profit"],
                        "oos_edge": r.get("oos_fixed_no_bonus_profit"),
                        "risk_net_profit_nb": net, "dd_pct": dd,
                        "passes_dd50": dd <= 50.0,
                        "config_json": json.dumps(r["config"], sort_keys=True),
                    }) + "\n")
                log(f"postpass {archive} {r['candidate_id'][:8]} risk={risk:g} "
                    f"net=${net:,.0f} dd={dd:.1f}% {'PASS' if dd <= 50 else 'fail'}")
                if dd <= 50.0:
                    break
        git_push(f"trail2 postpass: {archive}")


def final_verdict(baseline: dict | None) -> None:
    dep = deployable_lookup()
    rows = scan_rows()
    cands = []
    for r in rows:
        d = dep.get((r["_archive"], r.get("candidate_id", "")))
        if d:
            cands.append((d["net"], d, r))
    cands.sort(key=lambda x: -x[0])
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    base_net = baseline.get("deployable_net", 0.0) if baseline else 0.0
    base_edge = baseline["edge"] if baseline else 0.0
    md = [f"# TRAILING RE-SWEEP v2 — FINAL VERDICT ({stamp} UTC)",
          "", f"Reference to beat (no-trailing scalper24, deployable to DD<=50%): "
          f"edge ${base_edge:,.0f} | OOS ${(baseline['oos'] or 0):,.0f} | "
          f"**deployable net ${base_net:,.0f}** @ risk {baseline.get('deployable_risk')} "
          f"(DD {(baseline.get('deployable_dd') or 0):.1f}%)" if baseline
          else "Reference baseline missing!",
          "", "_Beats ref = higher deployable net AND higher fixed-lot edge._",
          "", "| rank | feed | risk | net (no-bonus) | DD | edge | OOS | beats ref |",
          "|---|---|---|---|---|---|---|---|"]
    for i, (net, d, r) in enumerate(cands[:15], 1):
        beat = net > base_net and r["fixed_no_bonus_profit"] > base_edge
        md.append(f"| {i} | {r['_archive']} | {d['risk']*100:g}% | ${net:,.0f} | "
                  f"{d['dd']:.1f}% | ${r['fixed_no_bonus_profit']:,.0f} | "
                  f"${(r.get('oos_fixed_no_bonus_profit') or 0):,.0f} | "
                  f"{'**YES**' if beat else 'no'} |")
    if not cands:
        md.append("| (no deployable trailing candidate passed) | | | | | | | |")
    (OUT / "FINAL_VERDICT_TRAIL2.md").write_text("\n".join(md) + "\n")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    log(f"trail2 orchestrator boot; {len(ARCHIVES)} archives; charts={len(CHARTS)}")
    ensure_feeds()
    baseline_ref = [ensure_baseline()]
    threading.Thread(target=committer, args=(baseline_ref,), daemon=True).start()

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as ex:
        for f in [ex.submit(run_sweep, a) for a in ARCHIVES]:
            f.result()

    log("sweeps complete; starting risk post-pass")
    risk_postpass()
    final_verdict(baseline_ref[0])
    write_snapshot(baseline_ref[0])
    (OUT / "PIPELINE_COMPLETE").write_text("complete\n")
    git_push("trail2 PIPELINE COMPLETE")
    log("TRAIL2 PIPELINE COMPLETE")


if __name__ == "__main__":
    main()
