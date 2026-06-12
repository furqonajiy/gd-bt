"""Final deployable verdict: for each archive's best configs, find the max risk
in {5,4,3,2,1%} that keeps concurrent DD<=50%, and record the DEPLOYABLE
compounded net (bonus=3, $5k start) at that risk — the same metric as Victor's
$718.9M. Ranks self vs Victor. Checkpoints to sweep_out/FINAL_VERDICT.*"""
import glob, json, sys, time
sys.path.insert(0, '.'); sys.path.insert(0, 'tools')
import sweep
from xauusd_trading import CsvChartSource, parse_signals_file

CHARTS = sorted(glob.glob('data/XAUUSD_M1_2025*_ELEV8.csv')) + sorted(glob.glob('data/XAUUSD_M1_2026*_ELEV8.csv'))
chart = CsvChartSource(sweep._expand_chart_paths(CHARTS))
RISKS = [0.05, 0.04, 0.03, 0.02, 0.01]
VICTOR = 718_941_972  # current_cli @5%, bonus3, DD 49.4%
OUT = 'sweep_out/FINAL_VERDICT.jsonl'

done = set()
try:
    for l in open(OUT):
        r = json.loads(l); done.add((r['archive'], r['cid']))
except FileNotFoundError:
    pass

sig_cache = {}
results = []
paths = sorted(glob.glob('sweep_out/self_sweep_*/results.jsonl'))
for p in paths:
    name = p.split('/')[1]
    archive = name.replace('self_sweep_trail_', '').replace('self_sweep_', '')
    is_trail = name.startswith('self_sweep_trail_')
    label = ('trail_' if is_trail else '') + archive
    rows = [json.loads(l) for l in open(p) if l.strip()]
    rows = [r for r in rows if r.get('fixed_no_bonus_profit') is not None]
    rows.sort(key=lambda r: r['fixed_no_bonus_profit'], reverse=True)
    top = rows[:3]
    if not top:
        continue
    if archive not in sig_cache:
        sig_cache[archive] = parse_signals_file(f'generated/self_{archive}.txt')
    sigs = sig_cache[archive]
    for r in top:
        cid = r.get('candidate_id', '')[:8]
        if (label, cid) in done:
            continue
        cfg = dict(r['config'])
        best = None
        for risk in RISKS:
            c2 = dict(cfg); c2['sizing_mode'] = 'risk'; c2['risk_per_signal'] = risk
            c2['initial_capital'] = 5000.0; c2['bonus_per_closed_lot'] = 3.0
            bt = sweep.run_concurrent_backtest(sigs, chart, sweep.config_from_dict(c2, bonus=3.0), label='fv')
            dd = abs(float(bt.get('max_drawdown_pct') or 0)); net = float(bt.get('net_profit') or 0)
            if dd <= 50.0:
                best = {'risk': risk, 'net': net, 'dd': dd}
                break
            if best is None:
                best = {'risk': risk, 'net': net, 'dd': dd}  # record even if no pass
        row = {'archive': label, 'cid': cid, 'edge': r['fixed_no_bonus_profit'],
               'oos': r.get('oos_fixed_no_bonus_profit'),
               'risk': best['risk'], 'net': best['net'], 'dd': best['dd'],
               'pass': best['dd'] <= 50.0, 'beats_victor': best['dd'] <= 50.0 and best['net'] > VICTOR,
               'cfg': json.dumps(cfg, sort_keys=True)}
        with open(OUT, 'a') as f:
            f.write(json.dumps(row) + '\n')
        results.append(row)
        flag = 'BEATS VICTOR' if row['beats_victor'] else ('pass' if row['pass'] else 'FAIL-DD')
        print(f"{label:22s} risk={best['risk']*100:.0f}% net=${best['net']:>16,.0f} dd={best['dd']:5.1f}% {flag}", flush=True)

print('\nFINAL VERDICT COMPLETE')
