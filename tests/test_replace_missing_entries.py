"""Mt5Executor.replace_missing_pending_entries: re-place LIMITs that vanished
from MT5 (e.g. cancelled by hand) while the signal is still live.

Stubbed MT5 -- no terminal needed.
"""
from __future__ import annotations

from dataclasses import replace

from trading.xauusd import DEFAULT_CONFIG, Mt5Executor, open_position, parse_one_signal, signal_to_magic
from trading.xauusd.execution.mt5_executor import mt5_entry_comment


# --- stub MT5 ---------------------------------------------------------------

class _Resp:
    def __init__(self, retcode=10009, order=123):
        self.retcode = retcode
        self.comment = "done"
        self.order = order


class _Sym:
    digits = 2
    filling_mode = 2  # SYMBOL_FILLING_IOC


class _FakePos:
    def __init__(self, magic, comment, ticket=1):
        self.ticket = ticket
        self.magic = magic
        self.comment = comment


class _FakeMt5:
    TRADE_ACTION_PENDING = 5
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_RETURN = 2
    SYMBOL_FILLING_IOC = 2
    SYMBOL_FILLING_FOK = 1

    def __init__(self, positions=None, orders=None, ticket0=1000):
        self._positions = list(positions or [])
        self._orders = list(orders or [])
        self.requests = []
        self._ticket = ticket0

    def positions_get(self, symbol=None):
        return list(self._positions)

    def orders_get(self, symbol=None):
        return list(self._orders)

    def symbol_info(self, symbol):
        return _Sym()

    def order_send(self, request):
        self.requests.append(dict(request))
        self._ticket += 1
        return _Resp(retcode=self.TRADE_RETCODE_DONE, order=self._ticket)

    def last_error(self):
        return (0, "ok")


class _Conn:
    def __init__(self, mt5):
        self.mt5 = mt5


_SIGNAL = "1. SELL XAUUSD 4314 - 4316 SL 4321.50 TP1 4306 TP2 4296 TP3 4276 1:32 PM"
_CFG = replace(DEFAULT_CONFIG, entry_count=8, entry_ladder="range_to_sl",
               entry_sl_gap=0.5, sl_multiplier=2.1, minimum_lot=0.01, lot_step=0.01)


def _sig():
    return parse_one_signal(_SIGNAL, "2026-06-08", 7)


def _pos_with_first_filled():
    """8-entry SELL position; entry #1 OPEN (filled), entries #2-8 still PENDING."""
    pos = open_position(_sig(), 5000.0, _CFG)
    pos.entries[0].status = "OPEN"
    pos.entries[0].fill_time = pos.activation_time
    return pos


def _executor(mt5):
    return Mt5Executor(_Conn(mt5), "XAUUSD", min_lot=0.01, lot_step=0.01, server_offset_hours=3)


def test_replaces_only_the_missing_pending_entries():
    pos = _pos_with_first_filled()
    key = pos.signal.signal_key
    magic = signal_to_magic(key)
    # MT5 has only the 1 filled position (comment = entry #1); the 7 limits are gone.
    mt5 = _FakeMt5(positions=[_FakePos(magic, mt5_entry_comment(key, 0))], orders=[])
    log = _executor(mt5).replace_missing_pending_entries(pos, _CFG, pos.activation_time)

    placed = [r for r in mt5.requests if r["action"] == _FakeMt5.TRADE_ACTION_PENDING]
    assert log.placed == 7
    assert len(placed) == 7
    comments = {r["comment"] for r in placed}
    assert comments == {mt5_entry_comment(key, i) for i in range(1, 8)}  # #2..#8
    assert mt5_entry_comment(key, 0) not in comments                     # not the fill
    # right order type + tagging
    assert all(r["type"] == _FakeMt5.ORDER_TYPE_SELL_LIMIT and r["magic"] == magic for r in placed)


def test_no_replace_when_signal_has_no_mt5_footprint():
    # Zero orders + zero positions -> finished/pruned or a transient query miss; never re-place.
    pos = _pos_with_first_filled()
    mt5 = _FakeMt5(positions=[], orders=[])
    log = _executor(mt5).replace_missing_pending_entries(pos, _CFG, pos.activation_time)
    assert log.placed == 0
    assert mt5.requests == []


def test_skips_entries_that_still_have_an_order():
    pos = _pos_with_first_filled()
    key = pos.signal.signal_key
    magic = signal_to_magic(key)

    class _FakeOrder:
        def __init__(self, comment):
            self.magic = magic
            self.comment = comment
    # entries #1 (filled position) and #3 (still has its order) are present; expect #2,#4..#8 = 6.
    mt5 = _FakeMt5(positions=[_FakePos(magic, mt5_entry_comment(key, 0))],
                   orders=[_FakeOrder(mt5_entry_comment(key, 2))])
    log = _executor(mt5).replace_missing_pending_entries(pos, _CFG, pos.activation_time)
    assert log.placed == 6
    comments = {r["comment"] for r in mt5.requests}
    assert mt5_entry_comment(key, 2) not in comments


def test_does_not_replace_non_pending_entries():
    pos = _pos_with_first_filled()
    key = pos.signal.signal_key
    magic = signal_to_magic(key)
    # Mark entry #2 terminal (e.g. NO_FILL/closed) -> must not be re-placed even though absent.
    pos.entries[1].status = "NO_FILL"
    mt5 = _FakeMt5(positions=[_FakePos(magic, mt5_entry_comment(key, 0))], orders=[])
    log = _executor(mt5).replace_missing_pending_entries(pos, _CFG, pos.activation_time)
    assert log.placed == 6  # #3..#8
    assert mt5_entry_comment(key, 1) not in {r["comment"] for r in mt5.requests}


def test_skips_when_trailing_open_enabled():
    pos = _pos_with_first_filled()
    key = pos.signal.signal_key
    mt5 = _FakeMt5(positions=[_FakePos(signal_to_magic(key), mt5_entry_comment(key, 0))], orders=[])
    cfg = replace(_CFG, trailing_open_distance=1.0)
    log = _executor(mt5).replace_missing_pending_entries(pos, cfg, pos.activation_time)
    assert log.placed == 0 and mt5.requests == []
