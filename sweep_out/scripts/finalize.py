"""Stage 4: pick final champions per category from FINAL_VERDICT2.jsonl by
DEPLOYABLE compounded net (DD<=50%, risk<=5%), prefer 24h feeds (#5), and write
the two champion CLI files + FINAL_VERDICT2.md.
"""
import json
import time
from pathlib import Path

ROOT = Path('/home/user/xauusd-backtest')
ns = {'__name__': 'fin'}
exec(open(ROOT / '_orchestrate.py').read(), ns)
GEN_NEW = {
    'risk02_widetp24': "python tools/generate_aggressive_limit_risk02.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_risk02_widetp24.txt --start-date 2025-01-01 --tp1-distance 8 --tp2-distance 14 --tp3-distance 22 --execution-hours \"0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23\"",
    'scalper_strict24': "python tools/generate_scalper_signals.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_scalper_strict24.txt --start 2025-01-01 --session-start 0 --session-end 0 --signal-tz 7 --min-slope 0.06 --min-body-atr 0.15 --cooldown-minutes 10",
    'scalper_widerr24': "python tools/generate_scalper_signals.py --charts data/XAUUSD_M1_*_ELEV8.csv --output generated/self_scalper_widerr24.txt --start 2025-01-01 --session-start 0 --session-end 0 --signal-tz 7 --rr1 1.5 --rr2 2.5 --rr3 4.0",
}
ns['GEN_CMD'].update(GEN_NEW)

FEEDS_24H = {'better', 'better_wide', 'canonical', 'canonical_h1', 'canonical_wide',
             'canonical_dense', 'zones', 'zones_dense', 'zones_strict', 'zones_widesl',
             'scalper24', 'risk02_allhours', 'risk02_tight24',
             'risk02_widetp24', 'scalper_strict24', 'scalper_widerr24'}
VICTOR_NET = 718_941_972

rows = [json.loads(l) for l in (ROOT / 'sweep_out/FINAL_VERDICT2.jsonl').open()]
rows = [r for r in rows if r['pass']]


def trailing_of(r):
    c = json.loads(r['cfg'])
    return r['label'].startswith('trail_') or c.get('trailing_open_distance', 0) > 0 \
        or c.get('trailing_close_distance', 0) > 0


def pick(cands):
    """Best deployable net; prefer 24h unless a session feed is >2x better."""
    if not cands:
        return None
    c24 = [r for r in cands if r['arch'] in FEEDS_24H]
    best_all = max(cands, key=lambda r: r['net'])
    if not c24:
        return best_all
    best_24 = max(c24, key=lambda r: r['net'])
    return best_all if best_all['net'] > 2 * best_24['net'] else best_24


champ_nt = pick([r for r in rows if not trailing_of(r)])
champ_tr = pick([r for r in rows if trailing_of(r)])
stamp = time.strftime('%Y-%m-%d %H:%M:%S')


def write_cli(tag, r, fname, slug):
    cfg = json.loads(r['cfg'])
    cfg['risk_per_signal'] = r['risk']
    hdr = [
        "# =========================================================================",
        f"# FINAL CHAMPION — {tag}",
        f"# finalized {stamp} UTC | feed: {r['arch']} ({'24h' if r['arch'] in FEEDS_24H else 'session-hours'})",
        f"# DEPLOYABLE: risk {r['risk']*100:.0f}% -> net ${r['net']:,.0f} from $5k | DD {r['dd']:.1f}% (<=50%)",
        f"# vs VICTOR $718,941,972 @5% DD 49.4% -> {'BEATS VICTOR' if r['net'] > VICTOR_NET else 'does NOT beat Victor'}",
        f"# edge ${r['edge']:,.0f} fixed-lot (18mo) | OOS ${(r.get('oos') or 0):,.0f} (6mo)",
        "# Compounded $ are model upper bounds (lot caps/slippage not modeled at scale);",
        "# they rank configs. Edge/OOS are the sizing-neutral quality measures.",
        "# =========================================================================", "",
    ]
    (ROOT / fname).write_text("\n".join(hdr + ns['_cli_blocks'](r['arch'], cfg, slug)) + "\n")


md = [f"# FINAL VERDICT v2 — deployable profit, DD<=50%, risk<=5% ({stamp} UTC)",
      f"\nVictor bar: **$718,941,972** net @5% (DD 49.4%).\n",
      "| category | feed | risk | net from $5k | DD | edge | OOS | beats Victor |",
      "|---|---|---|---|---|---|---|---|"]
for tag, r, fname in (("NO-TRAILING", champ_nt, 'self_cli_no_trailing_24h.txt'),
                      ("TRAILING", champ_tr, 'self_cli_trailing.txt')):
    if r is None:
        md.append(f"| {tag} | (none passed) | | | | | | |")
        continue
    write_cli(tag, r, fname, 'no_trailing_24h' if tag == 'NO-TRAILING' else 'trailing')
    md.append(f"| {tag} | {r['arch']} | {r['risk']*100:.0f}% | ${r['net']:,.0f} | "
              f"{r['dd']:.1f}% | ${r['edge']:,.0f} | ${(r.get('oos') or 0):,.0f} | "
              f"{'**YES**' if r['net'] > VICTOR_NET else 'no'} |")
md += ["", "Top-10 deployable (all categories):", "",
       "| label | risk | net | dd | edge | oos |", "|---|---|---|---|---|---|"]
for r in sorted(rows, key=lambda x: -x['net'])[:10]:
    md.append(f"| {r['label']} | {r['risk']*100:.0f}% | ${r['net']:,.0f} | {r['dd']:.1f}% | "
              f"${r['edge']:,.0f} | ${(r.get('oos') or 0):,.0f} |")
(ROOT / 'sweep_out/FINAL_VERDICT2.md').write_text("\n".join(md) + "\n")
print("finalize complete:",
      "nt:", champ_nt and f"{champ_nt['arch']} ${champ_nt['net']:,.0f}",
      "tr:", champ_tr and f"{champ_tr['arch']} ${champ_tr['net']:,.0f}")
