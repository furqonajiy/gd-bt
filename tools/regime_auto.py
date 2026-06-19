"""auto-regime router: ONE command that picks the right regime champion.

Detects the live market regime from recent M1 data (via `tools.regime_router`'s
smoothed-ATR + trend classifier), looks up that regime's published champion in
`sweep_regime_out_grid/CHAMPION_<regime>.json`, and emits the exact runnable
deployment commands for it. So instead of manually choosing R3 vs R4, you run
this and it tells you which champion to run *now* based on volatility.

    python tools/regime_auto.py                       # detect from all charts, print deployment CLI
    python tools/regime_auto.py --window-days 30      # use a 30-day window to classify
    python tools/regime_auto.py --charts 'data/XAUUSD_M1_2026*_ELEV8.csv'
    python tools/regime_auto.py --backtest-only       # print only the regime backtest command

When no champion exists for the detected regime yet, it falls back to "run your
incumbent" so you're never left without an answer. This is the decision brain
the live `auto --adaptive` loop will call each cycle.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import pandas as pd

# Allow `python tools/regime_auto.py` (script dir is tools/, not the repo root)
# as well as `python -m tools.regime_auto`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.regime_router import read_current_regime, DEFAULT_LIVE_REGIME
from tools.champions_report import render_champion_cli, render_deployment_cli


def _load_recent_m1(charts_glob: str, window_days: int) -> pd.DataFrame:
    """Load the most recent `window_days` of M1 OHLC from the chart files."""
    files = sorted(glob.glob(charts_glob))
    if not files:
        raise SystemExit(f"no chart files match {charts_glob!r}")
    frames = []
    for f in files[-2:]:  # last 1-2 monthly files cover any sane window
        df = pd.read_csv(f, sep="\t")
        df.columns = [c.strip("<>").lower() for c in df.columns]
        df["dt"] = pd.to_datetime(df["date"] + " " + df["time"], format="%Y.%m.%d %H:%M:%S")
        frames.append(df.set_index("dt")[["open", "high", "low", "close"]])
    m1 = pd.concat(frames).sort_index()
    cutoff = m1.index.max() - pd.Timedelta(days=window_days)
    return m1[m1.index >= cutoff]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--charts", default="data/XAUUSD_M1_*_ELEV8.csv",
                    help="chart glob; the most recent window is used to classify.")
    ap.add_argument("--champions-dir", default="sweep_regime_out_grid",
                    help="where CHAMPION_<regime>.json live.")
    ap.add_argument("--window-days", type=int, default=20,
                    help="trailing window (days) used to read current regime.")
    ap.add_argument("--backtest-only", action="store_true",
                    help="emit only the runnable regime-slice backtest command.")
    args = ap.parse_args(argv)

    m1 = _load_recent_m1(args.charts, args.window_days)
    r = read_current_regime(m1)
    print(f"# detected regime: {r.regime}   "
          f"(M15 ATR ${r.m15_atr:.2f}, trend {r.trend:+.3f}, "
          f"window={args.window_days}d ending {m1.index.max():%Y-%m-%d})")

    champ_path = Path(args.champions_dir) / f"CHAMPION_{r.regime}.json"
    if not champ_path.exists():
        print(f"# no champion published for {r.regime} yet "
              f"-> HOLD: keep running your incumbent config.")
        if r.regime != DEFAULT_LIVE_REGIME:
            print(f"# (today's reference live regime is {DEFAULT_LIVE_REGIME}.)")
        return 0

    champ = json.loads(champ_path.read_text())
    print(f"# champion: feed={champ.get('feed')} "
          f"edge=${float(champ.get('edge', 0)):.0f} "
          f"oos=${float(champ.get('oos', 0)):.0f} "
          f"dd={float(champ.get('dd', 0)):.1f}%")
    if args.backtest_only:
        print(render_champion_cli(champ.get("config") or {},
                                  regime=r.regime, feed=champ.get("feed") or ""))
    else:
        print(render_deployment_cli(
            champ.get("config") or {},
            regime=r.regime,
            feed=champ.get("feed") or "",
            edge=champ.get("edge"),
            oos=champ.get("oos"),
            dd=champ.get("dd"),
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
