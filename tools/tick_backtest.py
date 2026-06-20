"""Tick-level backtest that drives the REAL Mt5Executor against a mock broker.

The single bar engine (advance_bars) models fills with an OHLC heuristic. Live
execution, by contrast, arms trailing-open entries off discrete polled ticks and
fills pending STOPs broker-side. To measure that gap faithfully this tool does
NOT add a second engine: it runs the unchanged trailing ``Mt5Executor``
(place_signal -> reconcile_with_mt5 -> manage_position) exactly as ``auto`` does,
with the only substitution being a tick-fed ``MockMt5`` in place of the live MT5
terminal. So the executor code is byte-identical to live; just the broker is
mocked.

Fidelity ceiling (stated, not hidden):
  * MockMt5 fill model != real MT5 (no slippage/requotes/partial fills).
  * The simulated clock steps a fixed watch interval; real poll jitter is lost.
  * Management still runs off the M1 engine_pos, because live does too
    (manage_position takes engine_pos) -- this is faithful, not a shortcut.

Usage mirrors backtest_explicit's contract flags, plus --ticks:
  python tools/tick_backtest.py --signals gen/self.txt \
      --charts "data/XAUUSD_M1_*_ELEV8.csv" \
      --ticks  "data/XAUUSD_TICK_2026*_ELEV8.csv" \
      --symbol XAUUSD --watch-seconds 5 [--signal-key 2026-06-05#09] \
      <full strategy contract flags ...>
"""
from __future__ import annotations

import argparse
import glob
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading.engine import (  # noqa: E402
    CsvChartSource, ManualPositionSource, parse_signals_file,
)
from trading.engine.core.config import StrategyConfig  # noqa: E402
from trading.engine.strategy.trailing_engine import decide  # noqa: E402
from trading.engine.strategy.backtest import replay_signal, position_status  # noqa: E402
from trading.engine.execution.mt5_executor import signal_to_magic  # noqa: E402
from trading.engine.execution.mt5_executor_trailing import Mt5Executor  # noqa: E402
import trading.engine.execution.mt5_executor_live as _tp2mod  # noqa: E402
import trading.engine.execution.mt5_executor_trailing as _trmod  # noqa: E402

CONTRACT_SIZE_OZ = 100.0


# --------------------------------------------------------------------------
# arg helpers (match backtest_explicit conventions)
# --------------------------------------------------------------------------
def _positive_int(raw: str) -> int:
    v = int(raw)
    if v < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return v


def _positive_float(raw: str) -> float:
    v = float(raw)
    if v < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return v


def _bool_text(raw: str) -> bool:
    return str(raw).strip().lower() == "true"


def _expand(patterns: list[str]) -> list[str]:
    out: list[str] = []
    for p in patterns:
        out.extend(sorted(glob.glob(p)))
    if not out:
        raise SystemExit(f"No files matched: {patterns}")
    return out


# --------------------------------------------------------------------------
# tick data
# --------------------------------------------------------------------------
def load_ticks(paths: list[str]) -> pd.DataFrame:
    """Load ELEV8 tick CSVs into a time-sorted bid/ask frame.

    DATE+TIME are broker GMT+3 naive (same convention as the M1 archive), so
    no tz conversion is applied -- they line up with engine datetimes directly.
    """
    frames = []
    for path in paths:
        df = pd.read_csv(path, sep="\t")
        df.columns = [c.strip("<>").lower() for c in df.columns]
        ts = pd.to_datetime(df["date"] + " " + df["time"], format="%Y.%m.%d %H:%M:%S.%f")
        frames.append(pd.DataFrame({"time": ts,
                                    "bid": df["bid"].astype(float),
                                    "ask": df["ask"].astype(float)}))
    out = pd.concat(frames, ignore_index=True).sort_values("time").reset_index(drop=True)
    return out


def _slice_ms(ticks: pd.DataFrame, start: datetime, end: datetime):
    """Return (T_ms, bid, ask) numpy arrays for ticks in [start, end].

    Times are integer milliseconds; the executor reads epoch seconds/ms off the
    mocked tick, and ms ints keep the inner fill loop allocation-free.
    """
    mask = (ticks["time"] >= pd.Timestamp(start)) & (ticks["time"] <= pd.Timestamp(end))
    w = ticks.loc[mask]
    T = w["time"].values.astype("datetime64[ms]").astype("int64")
    return T, w["bid"].astype(float).values, w["ask"].astype(float).values


# --------------------------------------------------------------------------
# MockMt5 -- tick-driven broker exposing the API surface the executor uses
# --------------------------------------------------------------------------
class MockMt5:
    """Minimal MT5 stand-in: pending STOP/LIMIT fills on a tick cross, SL/TP
    hits on a tick, market closes at the current quote. Constants mirror the
    real MetaTrader5 integer values so executor branching matches live."""

    TRADE_RETCODE_DONE = 10009
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_MODIFY = 7
    TRADE_ACTION_REMOVE = 8
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    SYMBOL_TRADE_MODE_DISABLED = 0
    SYMBOL_TRADE_MODE_FULL = 4

    def __init__(self, T, B, A, symbol: str = "XAUUSD", equity: float = 10000.0,
                 contract: float = CONTRACT_SIZE_OZ):
        self.symbol = symbol
        self.T, self.B, self.A = T, B, A
        self._n = len(T)
        self._i = 0
        self.contract = contract
        self.equity = equity
        self._orders: dict[int, SimpleNamespace] = {}
        self._positions: dict[int, SimpleNamespace] = {}
        self.history: list[dict] = []
        self._seq = 1000
        self.bid = float(B[0]) if self._n else 0.0
        self.ask = float(A[0]) if self._n else 0.0
        self.now_ms = int(T[0]) if self._n else 0

    def _t(self) -> int:
        self._seq += 1
        return self._seq

    # ---- read API ----
    def last_error(self):
        return (1, "ok")

    def symbol_select(self, s, b=True):
        return True

    def account_info(self):
        return SimpleNamespace(equity=self.equity, balance=self.equity,
                               trade_allowed=True, margin_free=self.equity)

    def symbol_info(self, s):
        return SimpleNamespace(point=0.01, digits=2, trade_stops_level=40,
                               trade_mode=self.SYMBOL_TRADE_MODE_FULL, visible=True,
                               filling_mode=self.SYMBOL_FILLING_IOC | self.SYMBOL_FILLING_FOK,
                               volume_min=0.01, volume_step=0.01,
                               spread=int(round((self.ask - self.bid) * 100)))

    def symbol_info_tick(self, s):
        return SimpleNamespace(bid=self.bid, ask=self.ask,
                               time=int(self.now_ms // 1000), time_msc=int(self.now_ms), last=0.0)

    def positions_get(self, symbol=None, **kw):
        return tuple(self._positions.values())

    def orders_get(self, symbol=None, **kw):
        return tuple(self._orders.values())

    # ---- write API ----
    def order_send(self, req: dict):
        action = req.get("action")
        if action == self.TRADE_ACTION_PENDING:
            tk = self._t()
            self._orders[tk] = SimpleNamespace(
                ticket=tk, symbol=self.symbol, magic=req["magic"], type=req["type"],
                volume=req["volume"], volume_current=req["volume"], price_open=req["price"],
                sl=req.get("sl", 0.0), tp=req.get("tp", 0.0), comment=req.get("comment", ""),
                time_setup=int(self.now_ms // 1000))
            return self._done(order=tk, price=req["price"], volume=req["volume"])
        if action == self.TRADE_ACTION_REMOVE:
            tk = req.get("order")
            self._orders.pop(tk, None)
            return self._done(order=tk)
        if action == self.TRADE_ACTION_SLTP:
            p = self._positions.get(req.get("position"))
            if p is not None:
                if "sl" in req:
                    p.sl = req["sl"]
                if "tp" in req:
                    p.tp = req["tp"]
            return self._done()
        if action == self.TRADE_ACTION_DEAL:
            pos_tk = req.get("position")
            if pos_tk is not None:  # forced market close of an existing position
                p = self._positions.pop(pos_tk, None)
                px = 0.0
                if p is not None:
                    px = self.bid if p.type == self.POSITION_TYPE_BUY else self.ask
                    self._realize(p, px, self.now_ms, "market_close")
                return self._done(deal=self._t(), price=px,
                                  volume=p.volume if p else 0.0)
            return self._done(order=self._t(), deal=self._t(), price=self.ask,
                              volume=req.get("volume", 0.0))
        return SimpleNamespace(retcode=10013, order=0, deal=0, price=0.0, volume=0.0,
                               comment="unsupported")

    def _done(self, order=0, deal=0, price=0.0, volume=0.0):
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=order, deal=deal,
                               price=price, volume=volume, comment="Request executed")

    def _mk_pos(self, o, fill_price: float, t_ms: int):
        is_buy = o.type in (self.ORDER_TYPE_BUY, self.ORDER_TYPE_BUY_LIMIT, self.ORDER_TYPE_BUY_STOP)
        ptype = self.POSITION_TYPE_BUY if is_buy else self.POSITION_TYPE_SELL
        return SimpleNamespace(ticket=self._t(), symbol=self.symbol, magic=o.magic, type=ptype,
                               volume=o.volume, price_open=fill_price, sl=o.sl, tp=o.tp,
                               price_current=fill_price, profit=0.0, comment=o.comment,
                               time=int(t_ms // 1000), open_ms=t_ms)

    def _realize(self, p, close_px: float, t_ms: int, reason: str):
        if p.type == self.POSITION_TYPE_BUY:
            pnl = (close_px - p.price_open) * p.volume * self.contract
        else:
            pnl = (p.price_open - close_px) * p.volume * self.contract
        self.history.append(dict(ticket=p.ticket, magic=p.magic, comment=p.comment,
                                 side=("BUY" if p.type == self.POSITION_TYPE_BUY else "SELL"),
                                 open=p.price_open, close=close_px, volume=p.volume, pnl=pnl,
                                 reason=reason, open_ms=getattr(p, "open_ms", None), close_ms=t_ms))

    def advance_to(self, sim_ms: int) -> None:
        """Replay ticks up to sim_ms. SL/TP on open positions are checked before
        pending fills so an order filled on a tick is not also exited on the same
        tick (mirrors strict-touch: a stop and target cannot both resolve at the
        instant of fill)."""
        T, B, A = self.T, self.B, self.A
        while self._i < self._n and T[self._i] <= sim_ms:
            b = float(B[self._i]); a = float(A[self._i]); t = int(T[self._i])
            self.bid, self.ask, self.now_ms = b, a, t
            for tk in list(self._positions):
                p = self._positions[tk]
                p.price_current = b if p.type == self.POSITION_TYPE_BUY else a
                if p.type == self.POSITION_TYPE_SELL:  # SELL closes at Ask
                    if p.sl and a >= p.sl:
                        del self._positions[tk]; self._realize(p, p.sl, t, "SL")
                    elif p.tp and a <= p.tp:
                        del self._positions[tk]; self._realize(p, p.tp, t, "TP")
                else:  # BUY closes at Bid
                    if p.sl and b <= p.sl:
                        del self._positions[tk]; self._realize(p, p.sl, t, "SL")
                    elif p.tp and b >= p.tp:
                        del self._positions[tk]; self._realize(p, p.tp, t, "TP")
            for tk in list(self._orders):
                o = self._orders[tk]; price = o.price_open; fpx = None
                if o.type == self.ORDER_TYPE_SELL_STOP and b <= price:
                    fpx = b
                elif o.type == self.ORDER_TYPE_BUY_STOP and a >= price:
                    fpx = a
                elif o.type == self.ORDER_TYPE_SELL_LIMIT and b >= price:
                    fpx = b
                elif o.type == self.ORDER_TYPE_BUY_LIMIT and a <= price:
                    fpx = a
                if fpx is not None:
                    del self._orders[tk]
                    pos = self._mk_pos(o, fpx, t)
                    self._positions[pos.ticket] = pos
            self._i += 1


# --------------------------------------------------------------------------
# config + driver
# --------------------------------------------------------------------------
def build_config(args) -> StrategyConfig:
    return StrategyConfig(
        initial_capital=args.initial_capital, sizing_mode=args.sizing_mode,
        lot_per_entry=args.lot, risk_per_signal=args.risk, minimum_lot=args.minimum_lot,
        lot_step=args.lot_step, bonus_per_closed_lot=args.bonus_per_closed_lot,
        entry_count=args.entries, entry_ladder=args.entry_ladder, entry_sl_gap=args.entry_sl_gap,
        activation_delay_minutes=args.activation_delay, pending_expiry_minutes=args.pending_expiry,
        max_hold_minutes=args.max_hold, sl_multiplier=args.sl_multiplier, final_target=args.final_target,
        lock_after_tp1=args.lock_after_tp1, lock_after_tp2=args.lock_after_tp2,
        tp1_lock_delay_minutes=args.tp1_lock_delay_minutes, tp2_lock_delay_minutes=args.tp2_lock_delay_minutes,
        profit_lock_mode=args.profit_lock_mode, bep_trigger_distance=args.bep_trigger_distance,
        tp1_lock_fraction=args.tp1_lock_fraction, tp2_lock_target=args.tp2_lock_target,
        runner_after_tp3=args.runner_after_tp3, tp3_lock_target=args.tp3_lock_target,
        trailing_open_distance=args.trailing_open_distance,
        trailing_close_distance=args.trailing_close_distance)


def _install_sim_clock():
    """Inject a settable backtest clock. place_signal and manage_position gate on
    real wall-clock time (_wall_clock_chart_now); in a backtest that must be the
    simulated time instead. Returns a holder whose ``now`` is read by the patch."""
    holder = {"now": None}
    fake = lambda: holder["now"]
    _tp2mod._wall_clock_chart_now = fake
    _trmod._wall_clock_chart_now = fake
    return holder


def run_signal(signal, cfg: StrategyConfig, m1: CsvChartSource, ticks: pd.DataFrame,
               symbol: str, watch_seconds: int, clock: dict) -> dict:
    sim_start = signal.signal_time_chart
    sim_end = sim_start + timedelta(minutes=cfg.pending_expiry_minutes + cfg.max_hold_minutes + 5)
    chart_end = m1.dataframe["time"].iloc[-1].to_pydatetime()
    if sim_end > chart_end:
        sim_end = chart_end

    # tick coverage guard -- the export can start after the signal window.
    win = ticks[(ticks["time"] >= pd.Timestamp(sim_start)) & (ticks["time"] <= pd.Timestamp(sim_end))]
    if win.empty:
        first = ticks["time"].iloc[0] if len(ticks) else None
        last = ticks["time"].iloc[-1] if len(ticks) else None
        return {"key": signal.signal_key, "no_ticks": True, "first": first, "last": last,
                "window": (sim_start, sim_end)}

    T, B, A = _slice_ms(ticks, sim_start - timedelta(minutes=5), sim_end)
    rec = decide(signal, m1, ManualPositionSource(equity=cfg.initial_capital, positions=[]),
                 cfg, now=sim_end)
    plan = rec.new_signal
    engine_pos = plan.replay_position

    mock = MockMt5(T, B, A, symbol=symbol, equity=cfg.initial_capital,
                   contract=CONTRACT_SIZE_OZ)
    executor = Mt5Executor(SimpleNamespace(mt5=mock), symbol,
                           min_lot=cfg.minimum_lot, lot_step=cfg.lot_step,
                           server_offset_hours=3, notifier=None, forensic=None)

    step = timedelta(seconds=watch_seconds)
    t = sim_start
    placed = False
    while t <= sim_end:
        clock["now"] = t
        mock.advance_to(int(pd.Timestamp(t).value // 10**6))
        if not placed:
            log = executor.place_signal(signal, plan)
            if log.placed > 0:
                placed = True
        executor.reconcile_with_mt5(engine_pos, cfg, m1, t)
        executor.manage_position(engine_pos, cfg, t)
        t += step
    clock["now"] = sim_end
    mock.advance_to(int(pd.Timestamp(sim_end).value // 10**6))

    magic = signal_to_magic(signal.signal_key)
    deals = [h for h in mock.history if h["magic"] == magic]
    trading = sum(h["pnl"] for h in deals)
    closed_lots = sum(h["volume"] for h in deals)
    bonus = closed_lots * cfg.bonus_per_closed_lot

    # M1-engine baseline for the same signal (the fidelity comparison).
    m1_pos = replay_signal(signal, m1.dataframe, cfg.initial_capital, cfg)
    m1_status, m1_pnl = position_status(m1_pos)

    return {"key": signal.signal_key, "no_ticks": False, "placed": placed, "deals": deals,
            "open_left": len(mock._positions), "pending_left": len(mock._orders),
            "trading": trading, "bonus": bonus, "total": trading + bonus,
            "m1_status": m1_status, "m1_pnl": m1_pnl}


def print_result(r: dict) -> None:
    print(f"\n=== {r['key']} ===")
    if r["no_ticks"]:
        s, e = r["window"]
        print(f"  NO TICKS in window {s:%Y-%m-%d %H:%M} -> {e:%Y-%m-%d %H:%M}.")
        if r["first"] is not None:
            print(f"  tick file covers {r['first']} -> {r['last']} -- cannot tick-replay this signal.")
        return
    print(f"  placed={r['placed']} open_left={r['open_left']} pending_left={r['pending_left']}")
    for h in r["deals"]:
        oc = h["open_ms"]; cc = h["close_ms"]
        ot = pd.Timestamp(oc, unit="ms").strftime("%H:%M:%S") if oc else "?"
        ct = pd.Timestamp(cc, unit="ms").strftime("%H:%M:%S") if cc else "?"
        print(f"  {h['comment']} {h['side']} open={h['open']:.2f} close={h['close']:.2f} "
              f"lot={h['volume']} pnl=${h['pnl']:.2f} reason={h['reason']} ({ot}->{ct})")
    print(f"  TICK  : trading=${r['trading']:.2f} + bonus=${r['bonus']:.2f} => ${r['total']:.2f}")
    print(f"  M1    : status={r['m1_status']} realized=${r['m1_pnl']:.2f}  (engine baseline; bonus excl.)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Tick backtest via the real Mt5Executor + a tick-fed MockMt5.")
    p.add_argument("--signals", required=True)
    p.add_argument("--charts", required=True, nargs="+", help="M1 chart glob(s) for the plan/engine_pos.")
    p.add_argument("--ticks", required=True, nargs="+", help="Tick CSV glob(s).")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--watch-seconds", type=_positive_int, default=5,
                   help="Simulated poll cadence (mirror live --watch-interval).")
    p.add_argument("--signal-key", default=None, help="Run only this signal_key (e.g. 2026-06-05#09).")
    s = p.add_argument_group("required strategy contract")
    s.add_argument("--initial-capital", type=_positive_float, required=True)
    s.add_argument("--sizing-mode", choices=["fixed", "risk"], required=True)
    s.add_argument("--lot", type=_positive_float, required=True)
    s.add_argument("--risk", type=_positive_float, required=True)
    s.add_argument("--minimum-lot", type=_positive_float, required=True)
    s.add_argument("--lot-step", type=_positive_float, required=True)
    s.add_argument("--bonus-per-closed-lot", type=_positive_float, required=True)
    s.add_argument("--entries", type=int, required=True)
    s.add_argument("--entry-ladder", choices=["signal_range_3", "range_uniform", "range_to_sl"], required=True)
    s.add_argument("--entry-sl-gap", type=_positive_float, required=True)
    s.add_argument("--activation-delay", type=_positive_int, required=True)
    s.add_argument("--pending-expiry", type=_positive_int, required=True)
    s.add_argument("--max-hold", type=_positive_int, required=True)
    s.add_argument("--sl-multiplier", type=_positive_float, required=True)
    s.add_argument("--final-target", choices=["TP1", "TP2", "TP3"], required=True)
    s.add_argument("--lock-after-tp1", type=_bool_text, required=True)
    s.add_argument("--lock-after-tp2", type=_bool_text, required=True)
    s.add_argument("--tp1-lock-delay-minutes", type=_positive_int, required=True)
    s.add_argument("--tp2-lock-delay-minutes", type=_positive_int, required=True)
    s.add_argument("--profit-lock-mode", choices=["tp_levels", "bep_plus_half_tp1"], required=True)
    s.add_argument("--bep-trigger-distance", type=_positive_float, required=True)
    s.add_argument("--tp1-lock-fraction", type=float, required=True)
    s.add_argument("--tp2-lock-target", choices=["TP1", "TP2"], required=True)
    s.add_argument("--runner-after-tp3", type=_bool_text, required=True)
    s.add_argument("--tp3-lock-target", choices=["TP2"], required=True)
    s.add_argument("--trailing-open-distance", type=_positive_float, required=True)
    s.add_argument("--trailing-close-distance", type=_positive_float, required=True)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = build_config(args)
    m1 = CsvChartSource(_expand(args.charts))
    ticks = load_ticks(_expand(args.ticks))
    print(f"ticks: {len(ticks)} rows  {ticks['time'].iloc[0]} -> {ticks['time'].iloc[-1]}")

    signals = parse_signals_file(Path(args.signals))
    if args.signal_key is not None:
        signals = [s for s in signals if s.signal_key == args.signal_key]
        if not signals:
            print(f"signal_key {args.signal_key} not found in {args.signals}", file=sys.stderr)
            return 2

    clock = _install_sim_clock()
    for sig in signals:
        print_result(run_signal(sig, cfg, m1, ticks, args.symbol, args.watch_seconds, clock))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())