"""auto-regime router: ONE command that picks the right regime champion.

Detects the live market regime from recent M1 data (via `tools.regime_router`'s
smoothed-ATR + trend classifier), looks up that regime's published champion in
`sweep_regime_out_grid/CHAMPION_<regime>.json`, and emits the exact runnable
`backtest_explicit.py` CLI for it. So instead of manually choosing R3 vs R4, you
run this and it tells you which champion to run *now* based on volatility.

    python tools/regime_auto.py                       # detect from all charts, print champion CLI
    python tools/regime_auto.py --window-days 30      # use a 30-day window to classify
    python tools/regime_auto.py --charts 'data/XAUUSD_M1_2026*_ELEV8.csv'

When no champion exists for the detected regime yet, it falls back to "run your
incumbent" so you're never left without an answer. This is the decision brain
the live `auto --adaptive` loop will call each cycle.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

# Allow `python tools/regime_auto.py` (script dir is tools/, not the repo root)
# as well as `python -m tools.regime_auto`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.regime_router import (
    DEFAULT_LIVE_REGIME,
    RegimeThresholds,
    read_current_regime,
    regime_thresholds_from_json_file,
)
from tools.champions_report import render_champion_cli


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


def _thresholds_from_args(args: argparse.Namespace) -> RegimeThresholds | None:
    thresholds = (
        regime_thresholds_from_json_file(
            args.regime_thresholds_json,
            use_learned_boundaries=args.regime_use_learned_boundaries,
        )
        if args.regime_thresholds_json else None
    )
    if (
        thresholds is None
        and args.regime_vol_tier_low_max is None
        and args.regime_vol_tier_mid_max is None
        and args.regime_bull_trend_min is None
    ):
        return None
    base = thresholds or RegimeThresholds()
    return replace(
        base,
        vol_tier_low_max=(
            base.vol_tier_low_max if args.regime_vol_tier_low_max is None
            else args.regime_vol_tier_low_max
        ),
        vol_tier_mid_max=(
            base.vol_tier_mid_max if args.regime_vol_tier_mid_max is None
            else args.regime_vol_tier_mid_max
        ),
        bull_trend_min=(
            base.bull_trend_min if args.regime_bull_trend_min is None
            else args.regime_bull_trend_min
        ),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--charts", default="data/XAUUSD_M1_*_ELEV8.csv",
                    help="chart glob; the most recent window is used to classify.")
    ap.add_argument("--champions-dir", default="sweep_regime_out_grid",
                    help="where CHAMPION_<regime>.json live.")
    ap.add_argument("--window-days", type=int, default=20,
                    help="trailing window (days) used to read current regime.")
    ap.add_argument("--regime-thresholds-json", default=None,
                    help="Optional JSON with router thresholds. Also accepts the "
                         "regime calibration report JSON with --regime-use-learned-boundaries.")
    ap.add_argument("--regime-use-learned-boundaries", action="store_true",
                    help="Use learned R2/R3 and R3/R4 ATR boundaries from a calibration report JSON.")
    ap.add_argument("--regime-vol-tier-low-max", type=float, default=None,
                    help="Override the low/mid ATR boundary.")
    ap.add_argument("--regime-vol-tier-mid-max", type=float, default=None,
                    help="Override the mid/high ATR boundary.")
    ap.add_argument("--regime-bull-trend-min", type=float, default=None,
                    help="Override the low-vol trend threshold for R1 vs R2.")
    args = ap.parse_args()

    m1 = _load_recent_m1(args.charts, args.window_days)
    thresholds = _thresholds_from_args(args)
    r = read_current_regime(m1, thresholds=thresholds)
    print(f"# detected regime: {r.regime}   "
          f"(M15 ATR ${r.m15_atr:.2f}, trend {r.trend:+.3f}, "
          f"window={args.window_days}d ending {m1.index.max():%Y-%m-%d})")
    if thresholds is not None:
        print(f"# thresholds: low<{thresholds.vol_tier_low_max:.2f} "
              f"mid<{thresholds.vol_tier_mid_max:.2f} "
              f"bull>={thresholds.bull_trend_min:.3f}")

    champ_path = Path(args.champions_dir) / f"CHAMPION_{r.regime}.json"
    if not champ_path.exists():
        print(f"# no champion published for {r.regime} yet "
              f"-> HOLD: keep running your incumbent config.")
        if r.regime != DEFAULT_LIVE_REGIME:
            print(f"# (today's reference live regime is {DEFAULT_LIVE_REGIME}.)")
        return

    champ = json.loads(champ_path.read_text())
    print(f"# champion: feed={champ.get('feed')} "
          f"edge=${float(champ.get('edge', 0)):.0f} "
          f"oos=${float(champ.get('oos', 0)):.0f} "
          f"dd={float(champ.get('dd', 0)):.1f}%")
    print(render_champion_cli(champ.get("config") or {},
                              regime=r.regime, feed=champ.get("feed") or ""))


if __name__ == "__main__":
    main()
