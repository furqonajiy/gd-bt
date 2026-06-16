#!/usr/bin/env python3
"""Dump the live MT5 broker spec for backtest-realism calibration.

Run this on the WINDOWS box where MT5 + the `MetaTrader5` Python package are
installed and the terminal is logged in (it cannot run in Linux/CI). It captures
everything the backtest needs to match live execution -- the broker minimum stop
distance (the tight-stop realism floor), contract/lot constraints, swap, and the
*real* spread distribution sampled from recent ticks -- into one JSON file you
can hand back.

    python tools/dump_mt5_spec.py                 # XAUUSD -> mt5_spec.json
    python tools/dump_mt5_spec.py --symbol XAUUSD --out mt5_spec.json --tick-hours 4

No repo imports: it is self-contained so it runs even from a bare checkout.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta


def _asdict(obj):
    """MT5 namedtuple-ish -> plain dict (its _asdict drops nothing we need)."""
    if obj is None:
        return None
    try:
        return dict(obj._asdict())
    except AttributeError:
        return {k: getattr(obj, k) for k in dir(obj)
                if not k.startswith("_") and not callable(getattr(obj, k))}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--out", default="mt5_spec.json")
    p.add_argument("--tick-hours", type=float, default=4.0,
                   help="Sample recent ticks over this many hours for real spread stats.")
    args = p.parse_args()

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("ERROR: the `MetaTrader5` package is not installed.\n"
              "  pip install MetaTrader5   (Windows, same Python that talks to your terminal)")
        return 2

    if not mt5.initialize():
        print(f"ERROR: mt5.initialize() failed: {mt5.last_error()}\n"
              "  Make sure the MT5 terminal is OPEN and LOGGED IN to the account.")
        return 3

    try:
        if not mt5.symbol_select(args.symbol, True):
            print(f"WARN: could not select {args.symbol} in Market Watch; trying anyway.")

        sym = _asdict(mt5.symbol_info(args.symbol))
        acc = _asdict(mt5.account_info())
        tick = _asdict(mt5.symbol_info_tick(args.symbol))

        # Real spread distribution from recent ticks (ask-bid in price units).
        spread_stats = None
        try:
            now = datetime.now()
            ticks = mt5.copy_ticks_from(args.symbol, now - timedelta(hours=args.tick_hours),
                                        200000, mt5.COPY_TICKS_ALL)
            if ticks is not None and len(ticks):
                spreads = [float(t["ask"]) - float(t["bid"]) for t in ticks
                           if t["ask"] > 0 and t["bid"] > 0 and t["ask"] >= t["bid"]]
                spreads.sort()
                if spreads:
                    n = len(spreads)
                    pct = lambda q: spreads[min(n - 1, int(q * n))]
                    spread_stats = {
                        "n_ticks": n,
                        "median_price": round(pct(0.50), 5),
                        "p90_price": round(pct(0.90), 5),
                        "p99_price": round(pct(0.99), 5),
                        "max_price": round(spreads[-1], 5),
                    }
        except Exception as exc:  # tick history may be unavailable; spec still dumped
            spread_stats = {"error": repr(exc)}

        out = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "terminal": _asdict(mt5.terminal_info()),
            "symbol_info": sym,
            "account_info": acc,
            "current_tick": tick,
            "recent_spread_stats": spread_stats,
        }
        with open(args.out, "w") as f:
            json.dump(out, f, indent=2, default=str)

        # Human summary of the fields that matter for backtest realism.
        g = (sym or {}).get
        pt = g("point") or 0.0
        stops_pts = g("trade_stops_level")
        freeze_pts = g("trade_freeze_level")
        print(f"\nwrote {args.out}\n--- {args.symbol} realism summary ---")
        print(f"  digits           : {g('digits')}   point: {pt}")
        print(f"  STOPS level      : {stops_pts} points"
              + (f"  = {stops_pts * pt:.2f} price units (the min-SL floor)" if pt and stops_pts is not None else ""))
        print(f"  FREEZE level     : {freeze_pts} points"
              + (f"  = {freeze_pts * pt:.2f} price" if pt and freeze_pts is not None else ""))
        print(f"  contract_size    : {g('trade_contract_size')}")
        print(f"  volume min/step/max: {g('volume_min')} / {g('volume_step')} / {g('volume_max')}")
        print(f"  swap long/short  : {g('swap_long')} / {g('swap_short')}  (mode {g('swap_mode')})")
        print(f"  tick value/size  : {g('trade_tick_value')} / {g('trade_tick_size')}")
        print(f"  current spread   : {g('spread')} points")
        if spread_stats and "median_price" in spread_stats:
            print(f"  real spread (ticks): median {spread_stats['median_price']} / "
                  f"p90 {spread_stats['p90_price']} / p99 {spread_stats['p99_price']} price units")
        if acc:
            print(f"  account leverage : 1:{acc.get('leverage')}  | stop-out so_so {acc.get('margin_so_so')}%")
        print("\nSend me the JSON file (mt5_spec.json).")
    finally:
        mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
