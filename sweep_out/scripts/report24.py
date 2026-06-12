"""Live reporter for the 24h completion batch. Every 5 min:
  - scans ALL 24h archives (new batch + previously swept),
  - writes sweep_out/BEST_24H_SO_FAR.txt (leaderboard vs champions vs Victor),
  - if a new config beats a champion on OOS edge, rewrites the champion CLI file
    (self_cli_no_trailing_24h.txt / self_cli_trailing.txt) with full commands.
Stops when sweep_out/ALL_DONE_24H exists. Runs alongside the batch committer.
"""
import glob
import json
import fcntl
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path('/home/user/xauusd-backtest')
_lk = open('/tmp/report24.lock', 'w')
try:
    fcntl.flock(_lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(0)   # another reporter is already running
ns = {'__name__': 'rep'}
exec(open(ROOT / '_orchestrate.py').read(), ns)   # reuse GEN_CMD + _cli_blocks

FEEDS_24H = {'better', 'better_wide', 'canonical', 'canonical_h1', 'canonical_wide',
             'canonical_dense', 'zones', 'zones_dense', 'zones_strict', 'zones_widesl',
             'scalper24', 'risk02_allhours', 'risk02_tight24',
             'risk02_widetp24', 'scalper_strict24', 'scalper_widerr24'}
GEN_NEW = {
    'risk02_widetp24': "python tools/generate_aggressive_limit_risk02.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_risk02_widetp24.txt --start-date 2025-01-01 --tp1-distance 8 --tp2-distance 14 --tp3-distance 22 --execution-hours \"0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23\"",
    'scalper_strict24': "python tools/generate_scalper_signals.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_scalper_strict24.txt --start 2025-01-01 --session-start 0 --session-end 0 --signal-tz 7 --min-slope 0.06 --min-body-atr 0.15 --cooldown-minutes 10",
    'scalper_widerr24': "python tools/generate_scalper_signals.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_scalper_widerr24.txt --start 2025-01-01 --session-start 0 --session-end 0 --signal-tz 7 --rr1 1.5 --rr2 2.5 --rr3 4.0",
}
ns['GEN_CMD'].update(GEN_NEW)

# reigning champions (OOS edge) at batch start
CHAMP = {'nt': 56210.74, 'tr': 28635.53}
VICTOR = {'edge': 29811, 'oos': 14590}


def deployable_lookup():
    out = {}
    for f in ('sweep_out/FINAL_VERDICT.jsonl', 'sweep_out/FINAL_VERDICT2.jsonl'):
        p = ROOT / f
        if not p.exists():
            continue
        for l in p.open():
            try:
                r = json.loads(l)
            except Exception:
                continue
            key = (r.get('archive') or r.get('label'), r.get('cid'))
            if r.get('pass') or r.get('passes_dd50'):
                cur = out.get(key)
                if cur is None or r['net'] > cur['net']:
                    out[key] = {'risk': r['risk'], 'net': r['net'], 'dd': r['dd']}
    return out


def scan():
    nt, tr = [], []
    for p in glob.glob(str(ROOT / 'sweep_out/self_sweep_*/results.jsonl')):
        name = Path(p).parent.name
        is_trail_dir = name.startswith('self_sweep_trail_')
        arch = name.replace('self_sweep_trail_', '').replace('self_sweep_', '')
        if arch not in FEEDS_24H:
            continue
        for l in open(p):
            try:
                r = json.loads(l)
            except Exception:
                continue
            if r.get('fixed_no_bonus_profit') is None:
                continue
            c = r['config']
            r['_arch'] = arch
            r['_trail'] = is_trail_dir
            # champions must make money over the FULL period AND out-of-sample;
            # OOS-only winners with negative 18mo edge are regime artifacts.
            if r['fixed_no_bonus_profit'] <= 0 or (r.get('oos_fixed_no_bonus_profit') or 0) <= 0:
                continue
            trailing = is_trail_dir or c.get('trailing_open_distance', 0) > 0 \
                or c.get('trailing_close_distance', 0) > 0
            (tr if trailing else nt).append(r)
    key = lambda r: (r.get('oos_fixed_no_bonus_profit') or 0)
    nt.sort(key=key, reverse=True)
    tr.sort(key=key, reverse=True)
    return nt, tr


def fmt(r):
    c = r['config']
    return (f"  {r['_arch']:20s} edge=${r['fixed_no_bonus_profit']:>9,.0f} "
            f"oos=${(r.get('oos_fixed_no_bonus_profit') or 0):>9,.0f} "
            f"dd@5%={r['concurrent_risk_max_dd_pct']:>5.1f}% "
            f"e{c['entry_count']} slm{c['sl_multiplier']} d{c['tp1_lock_delay_minutes']} "
            f"to{c['trailing_open_distance']} tc{c['trailing_close_distance']}")


def write_champion(tag, r, fname, slug):
    c = dict(r['config'])
    dep = deployable_lookup().get(
        ((('trail_' if r.get('_trail') else '') + r['_arch']), r.get('candidate_id', '')[:8]))
    if dep:
        c['risk_per_signal'] = dep['risk']
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    hdr = [
        "# =========================================================================",
        f"# BEST SELF-GENERATED 24H — {tag} (LIVE: updated while 24h batch runs)",
        f"# updated {stamp} UTC | feed: {r['_arch']}",
        f"# edge ${r['fixed_no_bonus_profit']:,.0f} (18mo) | OOS ${(r.get('oos_fixed_no_bonus_profit') or 0):,.0f} (6mo) "
        f"| DD@5% {r['concurrent_risk_max_dd_pct']:.1f}%",
        f"# VICTOR baseline: edge $29,811 | OOS $14,590 | net $718,941,972 @5%",
        (f"# DEPLOYABLE: risk {dep['risk']*100:.0f}% -> net ${dep['net']:,.0f} from $5k | DD {dep['dd']:.1f}% (<=50%)"
         if dep else
         "# DEPLOYABLE risk not yet computed for this config (final stage will set it);"),
        "# compounded $ are model upper bounds - they rank configs, not forecast money.",
        "# =========================================================================", "",
    ]
    blocks = ns['_cli_blocks'](r['_arch'], c, slug)
    (ROOT / fname).write_text("\n".join(hdr + blocks) + "\n")


def push(msg):
    subprocess.run(['git', 'add', 'sweep_out', 'self_cli_no_trailing_24h.txt',
                    'self_cli_trailing.txt'], cwd=ROOT, timeout=60)
    r = subprocess.run(['git', 'commit', '-q', '-m', msg], cwd=ROOT, timeout=60)
    if r.returncode == 0:
        for i in range(3):
            if subprocess.run(['git', 'push', '-q', 'origin', 'research/self-signal-sweep'],
                              cwd=ROOT, timeout=120).returncode == 0:
                break
            time.sleep(2 ** (i + 1))


while True:
    try:
        nt, tr = scan()
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        prog = []
        for a in ('risk02_widetp24', 'scalper_strict24', 'scalper_widerr24'):
            f = ROOT / f'sweep_out/self_sweep_{a}/results.jsonl'
            n = sum(1 for _ in open(f)) if f.exists() else 0
            prog.append(f"{a}:{n}/126")
        lines = [
            "BEST 24-HOUR SELF CONFIGS — LIVE (new batch: " + " | ".join(prog) + ")",
            f"updated {stamp} UTC | Victor: edge $29,811 / OOS $14,590",
            "", "== NO TRAILING (top 5 by OOS) ==",
        ] + [fmt(r) for r in nt[:5]] + [
            "", "== WITH TRAILING (top 5 by OOS) ==",
        ] + [fmt(r) for r in tr[:5]]
        (ROOT / 'sweep_out/BEST_24H_SO_FAR.txt').write_text("\n".join(lines) + "\n")

        changed = []
        if nt and (nt[0].get('oos_fixed_no_bonus_profit') or 0) > CHAMP['nt']:
            CHAMP['nt'] = nt[0]['oos_fixed_no_bonus_profit']
            write_champion("WITHOUT TRAILING", nt[0], 'self_cli_no_trailing_24h.txt', 'no_trailing_24h')
            changed.append('no-trailing')
        if tr and (tr[0].get('oos_fixed_no_bonus_profit') or 0) > CHAMP['tr']:
            CHAMP['tr'] = tr[0]['oos_fixed_no_bonus_profit']
            write_champion("WITH TRAILING", tr[0], 'self_cli_trailing.txt', 'trailing')
            changed.append('trailing')
        push("sweep24h: new champion " + "+".join(changed) if changed
             else f"sweep24h reporter {time.strftime('%H:%M')}")
    except Exception as e:
        print("reporter cycle failed:", e, flush=True)
    if (ROOT / 'sweep_out/PIPELINE_COMPLETE').exists():
        break
    time.sleep(300)
print("reporter done")
