#!/bin/bash
# MASTER PIPELINE — runs every remaining stage to conclusion, idempotently.
# Re-runnable after any interruption: each stage skips via DONE markers and
# sweeps --resume from their committed results.jsonl checkpoints.
# Marker protocol: sweep_out/PIPELINE_COMPLETE = everything finished.
cd /home/user/xauusd-backtest || exit 0
exec 8>/tmp/pipeline.lock
flock -n 8 || exit 0          # single instance only
[ -f sweep_out/PIPELINE_COMPLETE ] && exit 0

# restore gitignored scratch drivers from tracked copies
[ -f _sweep_self.py ]  || cp sweep_out/scripts/_sweep_self.py  _sweep_self.py
[ -f _orchestrate.py ] || cp sweep_out/scripts/_orchestrate.py _orchestrate.py
git config user.name  "C - Furqon Aji Yudhistira" >/dev/null 2>&1
git config user.email "furqonajiy@gmail.com"      >/dev/null 2>&1
M1=$(ls data/XAUUSD_M1_2025*_ELEV8.csv data/XAUUSD_M1_2026*_ELEV8.csv)

push() {
  git add sweep_out generated self_cli_trailing.txt self_cli_no_trailing_24h.txt 2>/dev/null
  git commit -q -m "$1" 2>/dev/null
  for i in 1 2 3; do git push -q origin research/self-signal-sweep 2>/dev/null && break; sleep $((2**i)); done
}

# 5-minute committer (requirement #8)
( while [ ! -f sweep_out/PIPELINE_COMPLETE ]; do sleep 300; push "pipeline checkpoint $(date -u +%H:%M)"; done ) &

# live reporter (requirement #7): updates BEST_24H_SO_FAR.txt + champion CLIs
( python3 sweep_out/scripts/report24.py >> /tmp/report24.log 2>&1 ) &

run() {  # run <archive> <mode:limit|trailopen>
  local a=$1 m=$2 d
  d="sweep_out/self_sweep_$([ "$m" = trailopen ] && echo trail_)$a"
  [ -f "$d/DONE" ] && { echo "skip $d"; return; }
  echo "[$(date -u +%H:%M)] START $d ($m)"
  python _sweep_self.py --mode "$m" \
    --signals "generated/self_$a.txt" --charts $M1 \
    --output-dir "$d" --max-candidates 120 --max-concurrent-dd-pct 50 \
    --validate-months 6 --top-n 15 --progress-every 25 --resume \
    >> "$d.runlog" 2>&1 && touch "$d/DONE"
  echo "[$(date -u +%H:%M)] END $d"
  push "pipeline done: $d"
}

# ---- Stage 1: finish the 24h LIMIT sweeps
run risk02_widetp24  limit
run scalper_strict24 limit
run scalper_widerr24 limit
touch sweep_out/ALL_DONE_24H

# ---- Stage 2: trailing-open sweeps on the new 24h archives
run risk02_widetp24  trailopen
run scalper_strict24 trailopen
run scalper_widerr24 trailopen

# ---- Stage 3: deployable risk walk (max-safe risk <=5%, bonus3) on all archives
if [ ! -f sweep_out/VERDICT2_DONE ]; then
  python3 sweep_out/scripts/final_verdict2.py >> sweep_out/FINAL_VERDICT2.log 2>&1 \
    && touch sweep_out/VERDICT2_DONE
  push "pipeline: deployable verdict v2"
fi

# ---- Stage 4: write final champion CLIs + verdict doc
python3 sweep_out/scripts/finalize.py >> sweep_out/finalize.log 2>&1
touch sweep_out/PIPELINE_COMPLETE
push "pipeline ALL COMPLETE — final champions written"
echo "PIPELINE_COMPLETE"
