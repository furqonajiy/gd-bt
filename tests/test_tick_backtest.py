"""Unit/integration tests for tools/tick_backtest.py (synthetic ticks; no data/MT5).

Covers the two load-bearing pieces:
  1. MockMt5 fill/close semantics and P&L sign (the broker model).
  2. The REAL trailing Mt5Executor.place_signal placing into MockMt5 (proves the
     mock is API-compatible with the unchanged executor), then a tick-driven
     fill + take-profit close.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from pytest import approx

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.tick_backtest import MockMt5, _install_sim_clock  # noqa: E402
from xauusd_trading import parse_signals_file  # noqa: E402
from xauusd_trading.execution.mt5_executor import (  # noqa: E402
    signal_to_magic, mt5_entry_comment,
)
from xauusd_trading.execution.mt5_executor_trailing import Mt5Executor  # noqa: E402


def _ticks(quotes):
    """quotes: list of (bid, ask). Returns (T_ms, bid, ask) arrays at 1s spacing."""
    n = len(quotes)
    T = np.arange(n, dtype=np.int64) * 1000 + 1_700_000_000_000
    B = np.array([q[0] for q in quotes], dtype=float)
    A = np.array([q[1] for q in quotes], dtype=float)
    return T, B, A


def _pending(mock, side_stop, price, sl, tp, *, magic=1, comment="x.1", volume=0.10):
    otype = mock.ORDER_TYPE_SELL_STOP if side_stop == "SELL" else mock.ORDER_TYPE_BUY_STOP
    return mock.order_send({"action": mock.TRADE_ACTION_PENDING, "symbol": mock.symbol,
                            "volume": volume, "type": otype, "price": price, "sl": sl, "tp": tp,
                            "magic": magic, "comment": comment,
                            "type_time": mock.ORDER_TIME_GTC,
                            "type_filling": mock.ORDER_FILLING_IOC})


# --------------------------------------------------------------------------
# MockMt5 broker model
# --------------------------------------------------------------------------
def test_sell_stop_fills_on_bid_cross_then_tp_is_positive():
    # Bid above the stop (no fill), then below (fill), then down through TP.
    T, B, A = _ticks([(2001.0, 2001.3), (2000.4, 2000.7), (1995.0, 1995.3), (1989.5, 1989.8)])
    m = MockMt5(T, B, A)
    res = _pending(m, "SELL", price=2000.5, sl=2003.5, tp=1990.0)
    assert res.retcode == m.TRADE_RETCODE_DONE
    m.advance_to(int(T[-1]))
    assert not m._positions and not m._orders
    assert len(m.history) == 1
    h = m.history[0]
    assert h["side"] == "SELL" and h["reason"] == "TP"
    assert h["open"] == approx(2000.4)          # filled at the crossing bid
    assert h["close"] == approx(1990.0)         # closed at the TP level
    assert h["pnl"] == approx((2000.4 - 1990.0) * 0.10 * 100.0)
    assert h["pnl"] > 0


def test_sell_stop_loss_is_negative():
    # Fill, then price runs up through the SL.
    T, B, A = _ticks([(2000.4, 2000.7), (2003.7, 2003.9)])
    m = MockMt5(T, B, A)
    _pending(m, "SELL", price=2000.5, sl=2003.5, tp=1990.0)
    m.advance_to(int(T[-1]))
    assert len(m.history) == 1
    h = m.history[0]
    assert h["reason"] == "SL"
    assert h["close"] == approx(2003.5)
    assert h["pnl"] == approx((2000.4 - 2003.5) * 0.10 * 100.0)
    assert h["pnl"] < 0


def test_buy_stop_symmetry_tp_positive():
    # BUY_STOP fills when Ask >= price; BUY closes at Bid; TP above entry.
    T, B, A = _ticks([(1999.3, 1999.6), (2000.4, 2000.7), (2010.1, 2010.4)])
    m = MockMt5(T, B, A)
    _pending(m, "BUY", price=2000.5, sl=1997.5, tp=2010.0)
    m.advance_to(int(T[-1]))
    assert len(m.history) == 1
    h = m.history[0]
    assert h["side"] == "BUY" and h["reason"] == "TP"
    assert h["open"] == approx(2000.7)          # filled at the crossing ask
    assert h["pnl"] == approx((2010.0 - 2000.7) * 0.10 * 100.0)
    assert h["pnl"] > 0


def test_no_same_tick_fill_and_exit():
    # A single tick that both crosses the stop and sits beyond the TP must only
    # fill (SL/TP is evaluated before fills), leaving the exit for a later tick.
    T, B, A = _ticks([(1990.0, 1990.1)])
    m = MockMt5(T, B, A)
    _pending(m, "SELL", price=2000.5, sl=2003.5, tp=1990.0)
    m.advance_to(int(T[-1]))
    assert len(m._positions) == 1 and not m.history


# --------------------------------------------------------------------------
# real executor place_signal -> MockMt5 (API compatibility) + tick fill/close
# --------------------------------------------------------------------------
def _build_plan():
    order = SimpleNamespace(entry_index=0, lot=0.10, entry_price=2000.0, initial_sl=2003.0)
    return SimpleNamespace(orders=[order], final_target_price=1990.0,
                           replay_position=SimpleNamespace(entries=[object()]),
                           trailing_open_distance=0.5,
                           pending_activates_at=None, pending_expires_at=None)


def test_real_place_signal_arms_into_mock_then_fills_and_takes_profit(tmp_path):
    sig_file = tmp_path / "sig.txt"
    sig_file.write_text("2026-06-04 GMT+3\n"
                        "1. SELL XAUUSD 2000.00 - 2002.00 SL 2003.00 "
                        "TP1 1995.00 TP2 1990.00 TP3 1985.00 7:25 AM\n")
    signal = parse_signals_file(sig_file)[0]
    plan = _build_plan()

    # Arm at high Bid, fill on the drop, close at TP.
    T, B, A = _ticks([(2001.0, 2001.3), (2000.4, 2000.7), (1989.5, 1989.8)])
    m = MockMt5(T, B, A)
    ex = Mt5Executor(SimpleNamespace(mt5=m), m.symbol, min_lot=0.01, lot_step=0.01,
                     server_offset_hours=3, notifier=None, forensic=None)

    clock = _install_sim_clock()
    clock["now"] = signal.signal_time_chart

    m.advance_to(int(T[0]))                     # Bid 2001 -> SELL arms (>= 2000.5)
    log = ex.place_signal(signal, plan)
    assert log.placed == 1
    assert len(m._orders) == 1
    o = next(iter(m._orders.values()))
    assert o.type == m.ORDER_TYPE_SELL_STOP
    assert o.price_open == approx(2000.5)       # Bid - trailing_open_distance
    assert o.magic == signal_to_magic(signal.signal_key)
    assert o.comment == mt5_entry_comment(signal.signal_key, 0)

    m.advance_to(int(T[-1]))                     # fill on the drop, then TP close
    assert not m._orders and not m._positions
    assert len(m.history) == 1
    h = m.history[0]
    assert h["reason"] == "TP" and h["pnl"] > 0