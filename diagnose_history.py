"""diagnose_history.py -- isolate why has_recent_history isn't blocking re-entry.

Run from the repo root with the xauusd env active:

    python diagnose_history.py

Edit the SIGNAL_KEY / SYMBOL / SERVER_OFFSET below if your details differ.
"""
from __future__ import annotations
import calendar
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from xauusd_trading.mt5_adapter import Mt5Connection
from xauusd_trading.mt5_executor import signal_to_magic, HISTORY_LOOKBACK_HOURS


# Edit these to match the trade you're investigating.
SIGNAL_KEY = "2026-05-13#02"
SYMBOL = "XAUUSD"
SERVER_OFFSET = 3   # broker GMT offset (default 3)


def fmt(t):
    if t is None:
        return "None"
    if isinstance(t, (int, float)):
        return f"{datetime.utcfromtimestamp(int(t))} (epoch {int(t)})"
    return str(t)


def main():
    target_magic = signal_to_magic(SIGNAL_KEY)
    print("=" * 70)
    print("DIAGNOSTIC: has_recent_history")
    print("=" * 70)
    print(f"Signal key:     {SIGNAL_KEY!r}")
    print(f"Expected magic: {target_magic}")
    print(f"Symbol:         {SYMBOL}")
    print(f"Server offset:  GMT+{SERVER_OFFSET}")
    print(f"Lookback hours: {HISTORY_LOOKBACK_HOURS}")
    print(f"Local now:      {datetime.now()}  (Python local, naive)")
    print(f"UTC now:        {datetime.utcnow()}  (naive UTC)")
    print(f"System tz:      {time.tzname}, daylight={time.daylight}")
    print()

    with Mt5Connection() as conn:
        mt5 = conn.mt5

        info = mt5.account_info()
        if info:
            print(f"Account:        login={info.login} equity=${info.equity:.2f}")
        print()

        # --- live state ---
        print("LIVE POSITIONS for symbol:")
        positions = mt5.positions_get(symbol=SYMBOL) or []
        if not positions:
            print("  (none)")
        for p in positions:
            mark = "  <-- MATCH" if p.magic == target_magic else ""
            print(f"  ticket={p.ticket} magic={p.magic} type={p.type} "
                  f"open={p.price_open} sl={p.sl} comment={p.comment!r}{mark}")

        print()
        print("LIVE PENDING ORDERS for symbol:")
        orders = mt5.orders_get(symbol=SYMBOL) or []
        if not orders:
            print("  (none)")
        for o in orders:
            mark = "  <-- MATCH" if o.magic == target_magic else ""
            print(f"  ticket={o.ticket} magic={o.magic} type={o.type} "
                  f"price={o.price_open} comment={o.comment!r}{mark}")

        # --- has_recent_history's current behavior (naive local time) ---
        print()
        print("-" * 70)
        print("Method A: how has_recent_history currently queries")
        print("  (datetime.now() naive -- this is what the guard does today)")
        print("-" * 70)
        to_time_a = datetime.now() + timedelta(minutes=1)
        from_time_a = to_time_a - timedelta(hours=HISTORY_LOOKBACK_HOURS)
        print(f"  from_time = {from_time_a}")
        print(f"  to_time   = {to_time_a}")
        orders_a = mt5.history_orders_get(from_time_a, to_time_a) or []
        deals_a  = mt5.history_deals_get(from_time_a, to_time_a) or []
        print(f"  history_orders_get returned {len(orders_a)} order(s)")
        print(f"  history_deals_get  returned {len(deals_a)} deal(s)")
        matched_a = sum(
            1 for x in list(orders_a) + list(deals_a)
            if getattr(x, "magic", None) == target_magic
        )
        print(f"  Matches for magic {target_magic}: {matched_a}")

        # --- alternate method: broker-time-as-UTC-epoch ints ---
        print()
        print("-" * 70)
        print("Method B: same idea but using broker-time-as-UTC epoch ints")
        print("  (mirrors mt5_adapter._chart_time_to_mt5_epoch trick)")
        print("-" * 70)
        # Convert local-now to broker time, then encode as UTC epoch.
        local_now = datetime.now()
        # Best approximation of broker time: shift utcnow by server offset.
        broker_now = datetime.utcnow() + timedelta(hours=SERVER_OFFSET)
        from_broker = broker_now - timedelta(hours=HISTORY_LOOKBACK_HOURS)
        to_broker   = broker_now + timedelta(minutes=1)
        from_epoch = calendar.timegm(from_broker.timetuple())
        to_epoch   = calendar.timegm(to_broker.timetuple())
        print(f"  broker_now    = {broker_now}  (GMT+{SERVER_OFFSET}, naive)")
        print(f"  from (epoch)  = {from_epoch}  (= {datetime.utcfromtimestamp(from_epoch)})")
        print(f"  to   (epoch)  = {to_epoch}  (= {datetime.utcfromtimestamp(to_epoch)})")
        orders_b = mt5.history_orders_get(from_epoch, to_epoch) or []
        deals_b  = mt5.history_deals_get(from_epoch, to_epoch) or []
        print(f"  history_orders_get returned {len(orders_b)} order(s)")
        print(f"  history_deals_get  returned {len(deals_b)} deal(s)")
        matched_b = sum(
            1 for x in list(orders_b) + list(deals_b)
            if getattr(x, "magic", None) == target_magic
        )
        print(f"  Matches for magic {target_magic}: {matched_b}")

        # --- show recent symbol history under method B for visibility ---
        print()
        print("-" * 70)
        print(f"Recent {SYMBOL} deals in MT5 history (Method B's window):")
        print("-" * 70)
        symbol_deals = [d for d in deals_b if getattr(d, "symbol", None) == SYMBOL]
        if not symbol_deals:
            print("  (none in window)")
        for d in sorted(symbol_deals, key=lambda x: x.time):
            mark = "  <-- MATCH" if d.magic == target_magic else ""
            print(f"  ticket={d.ticket} magic={d.magic} "
                  f"time={fmt(d.time)} symbol={d.symbol} "
                  f"price={d.price} type={d.type} comment={d.comment!r}{mark}")

        print()
        print("=" * 70)
        print("VERDICT")
        print("=" * 70)
        if matched_a > 0:
            print("Method A FOUND the magic -- the guard should have blocked.")
            print("There's a different bug (registry, signal_key parsing, etc).")
        elif matched_b > 0:
            print("Method A MISSED the magic but Method B FOUND it.")
            print("This is the timezone bug: has_recent_history passes naive")
            print("local-time datetimes; MT5 needs broker-time-as-UTC epochs.")
            print("Fix: shift has_recent_history to the epoch-int method.")
        else:
            any_match_anywhere = any(
                getattr(d, "magic", None) == target_magic
                for d in (mt5.history_deals_get(
                    calendar.timegm((datetime.utcnow() - timedelta(days=2)).timetuple()),
                    calendar.timegm((datetime.utcnow() + timedelta(hours=2)).timetuple()),
                ) or [])
            )
            if any_match_anywhere:
                print("Neither method found magic in 12h window, but a 48h sweep")
                print("DOES find it. Lookback needs extending OR the deal closed")
                print("more than 12h before this run.")
            else:
                print("Magic NOT FOUND anywhere in MT5 history.")
                print("Most likely: the original signal was placed without the engine")
                print("(or with a different signal_key). The original close has a")
                print("different magic -- has_recent_history can't catch it.")
                print()
                print("Recent symbol deals (any magic) to look at manually:")
                wide_deals = mt5.history_deals_get(
                    calendar.timegm((datetime.utcnow() - timedelta(days=2)).timetuple()),
                    calendar.timegm((datetime.utcnow() + timedelta(hours=2)).timetuple()),
                ) or []
                for d in sorted(wide_deals, key=lambda x: x.time)[-10:]:
                    if getattr(d, "symbol", None) == SYMBOL:
                        print(f"  ticket={d.ticket} magic={d.magic} "
                              f"time={fmt(d.time)} price={d.price} "
                              f"comment={d.comment!r}")


if __name__ == "__main__":
    main()