#!/bin/bash
# All-hours completion batch: widetp24 (already running) -> strict24 -> widerr24.
# Each sweep checkpoints via --resume; this script commits+pushes every 5 min
# and after each archive. Writes sweep_out/ALL_DONE_24H when finished.
cd /home/user/xauusd-backtest
M1=$(ls data/XAUUSD_M1_2025*_ELEV8.csv data/XAUUSD_M1_2026*_ELEV8.csv)

push() {
  git add sweep_out generated 2>/dev/null
  git commit -q -m "$1" 2>/dev/null
  for i in 1 2 3; do git push -q origin research/self-signal-sweep 2>/dev/null && break; sleep $((2**i)); done
}

# background committer
( while [ ! -f sweep_out/ALL_DONE_24H ]; do sleep 300; push "sweep24h wip checkpoint $(date -u +%H:%M)"; done ) &
COMMITTER=$!

run() {  # run <archive>
  a=$1
  [ -f "sweep_out/self_sweep_$a/DONE" ] && { echo "skip $a (DONE)"; return; }
  echo "[$(date -u +%H:%M)] START $a"
  python _sweep_self.py --mode limit \
    --signals "generated/self_$a.txt" --charts $M1 \
    --output-dir "sweep_out/self_sweep_$a" \
    --max-candidates 120 --max-concurrent-dd-pct 50 --validate-months 6 \
    --top-n 15 --progress-every 25 --resume >> "sweep_out/self_sweep_$a.runlog" 2>&1 \
    && touch "sweep_out/self_sweep_$a/DONE"
  echo "[$(date -u +%H:%M)] END $a"
  push "sweep24h done: $a"
}

# wait for the already-running widetp24 process to finish (don't double-run)
while pgrep -f "self_risk02_widetp24" > /dev/null; do sleep 60; done
[ -f sweep_out/self_sweep_risk02_widetp24/results.jsonl ] && \
  [ "$(wc -l < sweep_out/self_sweep_risk02_widetp24/results.jsonl)" -ge 126 ] && \
  touch sweep_out/self_sweep_risk02_widetp24/DONE
run risk02_widetp24   # resumes/completes if the running one died early
run scalper_strict24
run scalper_widerr24

touch sweep_out/ALL_DONE_24H
push "sweep24h ALL COMPLETE"
kill $COMMITTER 2>/dev/null
echo "ALL_DONE_24H"
