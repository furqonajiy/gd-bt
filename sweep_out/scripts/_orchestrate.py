"""Reset-resilient self-signal sweep orchestrator (gitignored scratch).

Writes results to the TRACKED sweep_out/ dir and commits+pushes after every
archive, plus a periodic WIP committer thread, so a container reset loses at
most a few minutes. Each sweep runs with --resume, so a restarted archive
continues from its committed checkpoint. Re-runnable: skips archives whose
DONE marker is already committed.
"""
import glob
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path("/home/user/xauusd-backtest")
OUT = ROOT / "sweep_out"
CHARTS = sorted(glob.glob(str(ROOT / "data/XAUUSD_M1_2025*_ELEV8.csv"))) + \
         sorted(glob.glob(str(ROOT / "data/XAUUSD_M1_2026*_ELEV8.csv")))
LOG = OUT / "orchestrator.log"

# Ordered small -> large by signal count, so complete results (and a usable
# per-family conclusion) land early and the heavy archives never block them.
ARCHIVES = [
    "zones_strict", "zones", "zones_widesl", "zones_dense",          # ~0.5-1.2k
    "risk02", "risk02_tight", "risk02_widetp", "better", "better_wide",  # ~4-7k
    "risk02_allhours", "risk02_tight24", "canonical_h1",             # ~8k
    "scalper_prime", "canonical", "canonical_wide", "scalper_strict",  # ~12k
    "canonical_dense", "scalper", "scalper_widerr", "scalper24",     # ~21-30k
]
FAMILY = {a: a.split("_")[0] for a in ARCHIVES}

_git_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    OUT.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def git_push(msg: str) -> None:
    with _git_lock:
        subprocess.run(["git", "add", "sweep_out", "self_cli.txt"], cwd=ROOT, timeout=60)
        r = subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ROOT, timeout=60)
        if r.returncode == 0:
            for i in range(4):
                if subprocess.run(["git", "push", "-q", "origin",
                                   "research/self-signal-sweep"], cwd=ROOT, timeout=120).returncode == 0:
                    break
                time.sleep(2 ** (i + 1))


GEN_CMD = {
    "canonical":       "python tools/generate_self_signals.py --m15-charts data/XAUUSD_M15_*_ELEV8.csv --m1-charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_canonical.txt --start-date 2025-01-01",
    "canonical_h1":    "python tools/generate_self_signals.py --m15-charts data/XAUUSD_M15_*_ELEV8.csv --m1-charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_canonical_h1.txt --start-date 2025-01-01 --h1-trend-filter",
    "canonical_wide":  "python tools/generate_self_signals.py --m15-charts data/XAUUSD_M15_*_ELEV8.csv --m1-charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_canonical_wide.txt --start-date 2025-01-01 --tp1-distance 6 --tp2-distance 10 --tp3-distance 18 --sl-gap-from-range 5.0",
    "canonical_dense": "python tools/generate_self_signals.py --m15-charts data/XAUUSD_M15_*_ELEV8.csv --m1-charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_canonical_dense.txt --start-date 2025-01-01 --ema-fast 9 --ema-slow 34 --same-side-spacing-minutes 15 --max-signals-per-day 60",
    "better":          "python tools/generate_better_self_signals.py --m15-charts data/XAUUSD_M15_*_ELEV8.csv --m1-charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_better.txt --start-date 2025-01-01",
    "better_wide":     "python tools/generate_better_self_signals.py --m15-charts data/XAUUSD_M15_*_ELEV8.csv --m1-charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_better_wide.txt --start-date 2025-01-01 --tp1-distance 6 --tp2-distance 10 --tp3-distance 18",
    "zones":           "python tools/gen_zone_signals.py --m1-charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_zones.txt",
    "zones_dense":     "python tools/gen_zone_signals.py --m1-charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_zones_dense.txt --swing-len 3",
    "zones_strict":    "python tools/gen_zone_signals.py --m1-charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_zones_strict.txt --swing-len 8 --min-zone-atr 0.5",
    "zones_widesl":    "python tools/gen_zone_signals.py --m1-charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_zones_widesl.txt --sl-buffer 1.0",
    "scalper":         "python tools/generate_scalper_signals.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_scalper.txt --start 2025-01-01 --signal-tz 7",
    "scalper_strict":  "python tools/generate_scalper_signals.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_scalper_strict.txt --start 2025-01-01 --min-slope 0.06 --min-body-atr 0.15 --cooldown-minutes 10 --signal-tz 7",
    "scalper_widerr":  "python tools/generate_scalper_signals.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_scalper_widerr.txt --start 2025-01-01 --rr1 1.5 --rr2 2.5 --rr3 4.0 --signal-tz 7",
    "scalper_prime":   "python tools/generate_scalper_signals.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_scalper_prime.txt --start 2025-01-01 --session-start 10 --session-end 19 --signal-tz 7",
    "risk02":          "python tools/generate_aggressive_limit_risk02.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_risk02.txt --start-date 2025-01-01",
    "risk02_allhours": "python tools/generate_aggressive_limit_risk02.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_risk02_allhours.txt --start-date 2025-01-01 --execution-hours \"0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23\"",
    "risk02_tight":    "python tools/generate_aggressive_limit_risk02.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_risk02_tight.txt --start-date 2025-01-01 --anchor-distance 5.0 --signal-width 1.5 --raw-sl-distance 8.0",
    "risk02_tight24":  "python tools/generate_aggressive_limit_risk02.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_risk02_tight24.txt --start-date 2025-01-01 --anchor-distance 5.0 --signal-width 1.5 --raw-sl-distance 8.0 --execution-hours \"0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23\"",
    "scalper24":       "python tools/generate_scalper_signals.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_scalper24.txt --start 2025-01-01 --session-start 0 --session-end 0 --signal-tz 7",
    "risk02_widetp":   "python tools/generate_aggressive_limit_risk02.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_risk02_widetp.txt --start-date 2025-01-01 --tp1-distance 8 --tp2-distance 14 --tp3-distance 22",
}


def _bool(v) -> str:
    return "true" if v else "false"


def _strategy_flags(c: dict) -> list[str]:
    """Config dict -> the shared strategy flag block of the explicit runners."""
    fl = [
        "--sizing-mode risk",
        "--lot 0.01",
        f"--risk {c.get('risk_per_signal', 0.05)}",
        "--minimum-lot 0.01",
        "--lot-step 0.01",
        f"--bonus-per-closed-lot {c.get('bonus_per_closed_lot', 3.0)}",
        f"--entries {c['entry_count']}",
        f"--entry-ladder {c['entry_ladder']}",
        f"--entry-sl-gap {c['entry_sl_gap']}",
        f"--activation-delay {c['activation_delay_minutes']}",
        f"--pending-expiry {c['pending_expiry_minutes']}",
        f"--max-hold {c['max_hold_minutes']}",
        f"--sl-multiplier {c['sl_multiplier']}",
        f"--final-target {c['final_target']}",
        f"--lock-after-tp1 {_bool(c['lock_after_tp1'])}",
        f"--lock-after-tp2 {_bool(c['lock_after_tp2'])}",
        f"--tp1-lock-delay-minutes {c.get('tp1_lock_delay_minutes', 0)}",
        f"--tp2-lock-delay-minutes {c.get('tp2_lock_delay_minutes', 0)}",
        f"--profit-lock-mode {c.get('profit_lock_mode', 'tp_levels')}",
        f"--bep-trigger-distance {c.get('bep_trigger_distance', 3.0)}",
        f"--tp1-lock-fraction {c.get('tp1_lock_fraction', 0.5)}",
        f"--tp2-lock-target {c.get('tp2_lock_target', 'TP1')}",
        f"--runner-after-tp3 {_bool(c.get('runner_after_tp3', False))}",
        f"--tp3-lock-target {c.get('tp3_lock_target', 'TP2')}",
        f"--trailing-open-distance {c.get('trailing_open_distance', 0.0)}",
        f"--trailing-close-distance {c.get('trailing_close_distance', 0.0)}",
        f"--trailing-close-after-stage {c.get('trailing_close_after_stage', 0)}",
    ]
    if c.get("scale_out_at_tp1"):
        fl.append("--scale-out-at-tp1 true")
    if c.get("scale_out_at_tp2"):
        fl.append("--scale-out-at-tp2 true")
    if c.get("bep_after_tp1"):
        fl += ["--bep-after-tp1 true", f"--bep-buffer {c.get('bep_buffer', 0.0)}"]
    if c.get("shared_sl"):
        fl.append("--shared-sl true")
    if c.get("per_entry_targets"):
        fl += [f"--entry-targets {','.join(c['per_entry_targets'])}",
               f"--bep-after-move {c.get('bep_after_move', 0.0)}",
               f"--runner-trail-from {c.get('runner_trail_from', 'TP3')}"]
    if c.get("runner_no_final_cap"):
        fl.append("--runner-final-cap none")
    return fl


def _cli_blocks(archive: str, c: dict, slug: str = "best") -> list[str]:
    # slug: dot-free tag making output-dir / positions / Excel names unique per CLI.
    # Every CLI carries the same 4 sections as current_cli.txt:
    #   1 Telegram Listener  2 Live Signal Filter/Generator  3 Auto Executor  4 Backtest.
    # For SELF feeds the Telegram listener is NOT used (signals are generated
    # locally from price), and section 2 is the live REGENERATION loop.
    cont = " `\n  "
    strat = _strategy_flags(c)
    static = GEN_CMD.get(archive, f"# (no generator command recorded for {archive})")
    live_feed = f"generated/self_{archive}_live.txt"
    # FAST live variant: regenerating 18 months every cycle is far too slow when
    # a scalper fires dozens of signals/day. Narrow to the current+previous month
    # of charts (PowerShell $m0/$m1) and a 2-day --start so each pass is seconds,
    # then refresh every 30s to catch freshly-closed M1 bars. EMAs stay warm
    # because two months of history is loaded.
    family = archive.split("_")[0]
    if family.startswith("scalper") or archive == "scalper24":
        family = "scalper"
    # generator args for the live feed: same knobs, live output. live_feed_loop
    # rolls --start and narrows --charts itself (--gen-start-days/--gen-recent-months),
    # so the command is static and single -- no shell loop or date math.
    gen_tail = (static.split(".py ", 1)[1]
                .replace(f"self_{archive}.txt", f"self_{archive}_live.txt"))
    live_loop = cont.join(
        ["python tools/live_feed_loop.py",
         f"--family {family}",
         "--interval 30",
         "--gen-start-days 3",
         "--gen-recent-months 2",
         "--mt5-symbol XAUUSD",
         "--mt5-server-offset 3",
         f"-- {gen_tail}"])
    # --initial-capital and --start-date go LAST: they are the most frequently
    # adjusted parameters, so they sit at the end of every command for easy edits.
    def _bt(outdir_suffix: str, start_date: str) -> str:
        return cont.join(
            ["python tools/backtest_explicit.py",
             f"--signals generated/self_{archive}.txt",
             "--charts data/XAUUSD_M1_*_ELEV8.csv",
             f"--output-dir reports/SELF_{slug.upper()}{outdir_suffix}",
             "--max-drawdown-limit-pct 50",
             "--progress-interval-seconds 30",
             "--sync-charts true"] + strat +
            ["--initial-capital 5000",
             f"--start-date {start_date}"])
    backtest = _bt("", "2025-01-01")
    backtest_june = _bt("_FROM_JUNE", "2026-06-01")
    auto = cont.join(
        ["python tools/auto_explicit.py",
         f"--signals {live_feed}",
         f"--positions-json positions_self_{slug}.json",
         "--watch-interval 15",
         "--mt5-symbol XAUUSD",
         "--mt5-server-offset 3",
         "--mt5-history-bars 5000",
         "--replace-missing-entries false",
         "--reopen-missing-positions true"] + strat +
        ["--initial-capital 5000"])
    return [
        "# ===== 1. TELEGRAM LISTENER =====",
        "# NOT USED for self-generated signals — these are generated locally from",
        "# MT5 price action (section 2), not ingested from Victor's Telegram channel.",
        "# (Only run a listener if you also want to mirror Victor's feed in parallel.)",
        "",
        "# ===== 2. LIVE SIGNAL GENERATOR (single command; logs each new signal) =====",
        "# One process, no shell loop. Refetches the current month + regenerates ONLY",
        "# when a new closed M1 bar exists, narrows to the 2 most recent months and a",
        "# rolling 3-day window (fast), and prints like `auto`: a header, then one line",
        "# per NEW signal -- e.g. [ts] Add Signal 75. BUY XAUUSD ...",
        live_loop,
        "",
        "# ===== 3. AUTO EXECUTOR (reads the live feed, places + manages orders) =====",
        "# --replace-missing-entries false: self feeds are two-sided (hedge close-by",
        "# re-entry risk). Window B, runs beside the generator loop above.",
        auto,
        "",
        "# ===== 4. PARITY BACKTEST (regenerate full history, then backtest) =====",
        f"# STEP 4a — (re)generate the FULL static archive generated/self_{archive}.txt",
        "# from ALL chart history so the backtest reflects the latest data. This is the",
        "# 18-month feed (slow, ~15s) — DIFFERENT from the live 2-day feed in section 2.",
        "# Run this whenever you want a fresh backtest:",
        static,
        f"# STEP 4b — backtest that freshly-regenerated archive (full history):",
        backtest,
        "",
        "# ===== 5. RECENT BACKTEST (from 2026-06-01 only; quick current-regime check) =====",
        backtest_june,
    ]


def write_best_so_far() -> None:
    """Live snapshot -> sweep_out/BEST_SO_FAR.txt + self_cli.txt (full CLIs)."""
    import json
    rows = []
    for p in OUT.glob("self_sweep_*/results.jsonl"):
        mode = "TRAIL-OPEN>0" if p.parent.name.startswith("self_sweep_trail_") else "LIMIT"
        archive = p.parent.name.replace("self_sweep_trail_", "").replace("self_sweep_", "")
        for line in p.open():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("fixed_no_bonus_profit") is None:
                continue
            r["_archive"], r["_mode"] = archive, mode
            rows.append(r)
    done = len(list(OUT.glob("self_sweep_*/DONE")))
    passing = [r for r in rows if r.get("passes_recommendation_gate")]
    by_edge = sorted(rows, key=lambda r: float(r["fixed_no_bonus_profit"]), reverse=True)
    by_edge_pass = sorted(passing, key=lambda r: float(r["fixed_no_bonus_profit"]), reverse=True)

    def fmt(r):
        c = r["config"]
        return (f"  {r['_archive']:<18} {r['_mode']:<12} edge=${r['fixed_no_bonus_profit']:>10,.0f} "
                f"dd={r['concurrent_risk_max_dd_pct']:>5.1f}% oos=${(r.get('oos_fixed_no_bonus_profit') or 0):>9,.0f}  "
                f"e{c['entry_count']} {c['entry_ladder']} gap{c['entry_sl_gap']} slm{c['sl_multiplier']} "
                f"{c['final_target']} d{c['tp1_lock_delay_minutes']}/{c['tp2_lock_delay_minutes']} "
                f"to{c['trailing_open_distance']} tc{c['trailing_close_distance']}")

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    head = [
        "SELF-SIGNAL SWEEP — BEST SO FAR",
        f"updated: {stamp} UTC | archives done: {done}/20 | configs evaluated: {len(rows)}",
        "goal: MAX PROFIT, max drawdown 50%, risk <=5% | edge = fixed-lot no-bonus profit",
        "final risk is set by the post-pass (walks 5%->2% until concurrent DD<=50%).",
        "",
        "== TOP 10, PASSES ALL GATES (DD<=50% @5% risk + consistency + OOS>0) ==",
    ]
    head += [fmt(r) for r in by_edge_pass[:10]] or ["  (none yet)"]
    head += ["", "== TOP 10 BY RAW EDGE (gate ignored; risk post-pass may rescue) =="]
    head += [fmt(r) for r in by_edge[:10]] or ["  (none yet)"]
    (OUT / "BEST_SO_FAR.txt").write_text("\n".join(head) + "\n")

    best = (by_edge_pass or by_edge)[:1]
    if best:
        b = best[0]
        cli = [
            "# =========================================================================",
            "# SELF-SIGNAL BEST CONFIG — LIVE SNAPSHOT (sweep still running!)",
            f"# updated {stamp} UTC | archives done {done}/20 | configs evaluated {len(rows)}",
            f"# winner so far: {b['_archive']} ({b['_mode']}) | "
            f"edge ${b['fixed_no_bonus_profit']:,.0f} fixed-lot no-bonus | "
            f"concurrent DD @5% risk {b['concurrent_risk_max_dd_pct']:.1f}% | "
            f"OOS ${(b.get('oos_fixed_no_bonus_profit') or 0):,.0f}",
            "# gates: " + ("ALL PASS (DD<=50 @5%, consistency, OOS>0)" if by_edge_pass
                           else "RAW EDGE LEADER — fails DD gate at 5% risk; "
                                "final risk% comes from the post-pass"),
            "# =========================================================================",
            "",
        ] + _cli_blocks(b["_archive"], b["config"])
        (ROOT / "self_cli.txt").write_text("\n".join(cli) + "\n")


def committer() -> None:
    while not (OUT / "ALL_DONE").exists():
        time.sleep(300)
        try:
            write_best_so_far()
            git_push(f"sweep wip checkpoint {time.strftime('%H:%M')}")
        except Exception as e:
            log(f"committer cycle failed (will retry): {e}")


def run_sweep(archive: str, mode: str, name: str, candidates: int) -> None:
    d = OUT / name
    if (d / "DONE").exists():
        log(f"skip {name} (DONE)")
        return
    cmd = [sys.executable, str(ROOT / "_sweep_self.py"), "--mode", mode,
           "--signals", str(ROOT / f"generated/self_{archive}.txt"),
           "--charts", *CHARTS, "--output-dir", str(d),
           "--max-candidates", str(candidates), "--max-concurrent-dd-pct", "50",
           "--validate-months", "6", "--top-n", "15", "--progress-every", "25",
           "--resume"]
    log(f"START {name} (mode={mode}, n={candidates})")
    t0 = time.time()
    with (d.parent / f"{name}.runlog").open("a") as f:
        rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=ROOT).returncode
    if rc == 0:
        (d / "DONE").write_text("ok\n")
    log(f"END   {name} rc={rc} ({(time.time() - t0) / 60:.1f} min)")
    try:
        git_push(f"sweep done: {name}")
    except Exception as e:
        log(f"post-archive push failed (committer will catch up): {e}")


def pool(tasks, workers=2) -> None:
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in [ex.submit(*t) for t in tasks]:
            f.result()


def best_archive(family: str) -> str:
    import json
    members = [a for a in ARCHIVES if FAMILY[a] == family]
    best, best_key = members[0], (-1, -1e18)
    for m in members:
        p = OUT / f"self_sweep_{m}" / "results.jsonl"
        if not p.exists():
            continue
        rows = [json.loads(l) for l in p.open() if l.strip()]
        rows = [r for r in rows if r.get("fixed_no_bonus_profit") is not None]
        if not rows:
            continue
        passing = [r for r in rows if r.get("passes_recommendation_gate")]
        pool_rows = passing or rows
        top = max(pool_rows, key=lambda r: float(r.get("fixed_no_bonus_profit")))
        key = (1 if passing else 0, float(top["fixed_no_bonus_profit"]))
        if key > best_key:
            best_key, best = key, m
    return best


def main() -> None:
    OUT.mkdir(exist_ok=True)
    threading.Thread(target=committer, daemon=True).start()
    log(f"orchestrator boot; {len(ARCHIVES)} archives; charts={len(CHARTS)}")

    # Stage A: LIMIT sweeps for all archives (2 parallel)
    pool([(run_sweep, a, "limit", f"self_sweep_{a}", 120) for a in ARCHIVES], workers=2)

    # Stage B: trailing-open>0 sweep on best archive per family
    fams = sorted({FAMILY[a] for a in ARCHIVES})
    picks = sorted({best_archive(f) for f in fams})
    log(f"trailing picks: {picks}")
    pool([(run_sweep, a, "trailopen", f"self_sweep_trail_{a}", 150) for a in picks], workers=2)

    (OUT / "ALL_DONE").write_text("complete\n")
    git_push("sweep ALL COMPLETE")
    log("ORCHESTRATOR COMPLETE")


if __name__ == "__main__":
    main()
