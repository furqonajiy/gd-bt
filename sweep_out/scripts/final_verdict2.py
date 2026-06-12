"""Deployable verdict v2: for the top-2 edge configs of EVERY archive (limit +
trailing dirs), walk risk 5->1% and record compounded net (bonus3, $5k) at the
highest risk whose concurrent DD<=50%. Checkpointed/resumable via jsonl.
Victor bar: $718,941,972 net ($710.2M trading) @5%, DD 49.4%.
"""
import glob
import json
import sys
import time
from pathlib import Path

ROOT = Path('/home/user/xauusd-backtest')
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'tools'))
import sweep  # noqa: E402
from xauusd_trading import CsvChartSource, parse_signals_file  # noqa: E402

CHARTS = sorted(glob.glob(str(ROOT / 'data/XAUUSD_M1_2025*_ELEV8.csv'))) + \
         sorted(glob.glob(str(ROOT / 'data/XAUUSD_M1_2026*_ELEV8.csv')))
chart = CsvChartSource(sweep._expand_chart_paths([str(p) for p in CHARTS]))
RISKS = [0.05, 0.04, 0.03, 0.02, 0.01]
OUT = ROOT / 'sweep_out/FINAL_VERDICT2.jsonl'

done = set()
if OUT.exists():
    for l in OUT.open():
        r = json.loads(l)
        done.add((r['label'], r['cid']))

sig_cache = {}
for p in sorted(glob.glob(str(ROOT / 'sweep_out/self_sweep_*/results.jsonl'))):
    name = Path(p).parent.name
    arch = name.replace('self_sweep_trail_', '').replace('self_sweep_', '')
    label = ('trail_' if name.startswith('self_sweep_trail_') else '') + arch
    rows = [json.loads(l) for l in open(p) if l.strip()]
    rows = [r for r in rows if r.get('fixed_no_bonus_profit') is not None]
    rows.sort(key=lambda r: r['fixed_no_bonus_profit'], reverse=True)
    for r in rows[:2]:
        cid = r.get('candidate_id', '')[:8]
        if (label, cid) in done:
            continue
        if arch not in sig_cache:
            sig_cache[arch] = parse_signals_file(ROOT / f'generated/self_{arch}.txt')
        sigs = sig_cache[arch]
        cfg = dict(r['config'])
        rec = None
        for risk in RISKS:
            c2 = dict(cfg)
            c2.update(sizing_mode='risk', risk_per_signal=risk,
                      initial_capital=5000.0, bonus_per_closed_lot=3.0)
            bt = sweep.run_concurrent_backtest(
                sigs, chart, sweep.config_from_dict(c2, bonus=3.0), label='v2')
            dd = abs(float(bt.get('max_drawdown_pct') or 0))
            net = float(bt.get('net_profit') or 0)
            rec = {'risk': risk, 'net': net, 'dd': dd}
            if dd <= 50.0:
                break
        out = {'label': label, 'arch': arch, 'cid': cid,
               'edge': r['fixed_no_bonus_profit'],
               'oos': r.get('oos_fixed_no_bonus_profit'),
               'risk': rec['risk'], 'net': rec['net'], 'dd': rec['dd'],
               'pass': rec['dd'] <= 50.0,
               'cfg': json.dumps(cfg, sort_keys=True)}
        with OUT.open('a') as f:
            f.write(json.dumps(out) + '\n')
        print(f"{label:24s} {cid} risk={rec['risk']*100:.0f}% net=${rec['net']:,.0f} "
              f"dd={rec['dd']:.1f}% {'PASS' if out['pass'] else 'FAIL'}", flush=True)
print('VERDICT2 COMPLETE')
