"""Live parity hardening from the 2026-06-12 SELF reconciliation.

Three live-only behaviors, decided after live -$165.51 vs backtest +$441.55:

1. Late lock-exit catch-up PROTECTS the leg instead of flattening at market:
   SL moves to the model's lock level when broker-legal, falls back to the
   closest legal stop otherwise, ratchets toward the level as price recovers,
   and only closes at market when no legal stop exists / the modify fails.
2. Entries the replay still holds OPEN but missing from MT5 (closed by hand)
   are re-opened at market — MT5 mirrors the replay.
3. A signal whose magic already has deal history is never freshly placed
   again (the prune -> re-place loop that traded 2026-06-12#10 twice).

Stubbed MT5 -- no terminal needed.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from xauusd_trading import (
    DEFAULT_CONFIG, Mt5Executor, NewSignalPlan, PlannedOrder,
    open_position, parse_one_signal, signal_to_magic,
)
from xauusd_trading.execution import mt5_executor_tp2
from xauusd_trading.execution.mt5_executor import mt5_entry_comment


# --- stub MT5 ---------------------------------------------------------------

class _Resp:
    def __init__(self, retcode=10009, comment="done", order=123):
        self.retcode = retcode
        self.comment = comment
        self.order = order


class _Sym:
    digits = 2
    trade_stops_level = 0
    filling_mode = 2  # SYMBOL_FILLING_IOC


class _Tick:
    def __init__(self, bid: float, ask: float):
        self.bid = bid
        self.ask = ask


class _FakePos:
    def __init__(self, *, ticket, magic, comment, type_, sl, tp=0.0,
                 volume=0.01, time=0, price_open=0.0):
        self.ticket = ticket
        self.magic = magic
        self.comment = comment
        self.type = type_
        self.sl = sl
        self.tp = tp
        self.volume = volume
        self.time = time
        self.price_open = price_open


class _FakeDeal:
    def __init__(self, magic):
        self.magic = magic


class _FakeMt5:
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_REMOVE = 8
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_RETURN = 2
    SYMBOL_FILLING_IOC = 2
    SYMBOL_FILLING_FOK = 1

    def __init__(self, *, bid: float, ask: float, positions=None,
                 history_deals=None, fail_actions=()):
        self._tick = _Tick(bid, ask)
        self._positions = list(positions or [])
        self._history = list(history_deals or [])
        self._fail_actions = set(fail_actions)
        self.requests = []

    def symbol_info(self, symbol):
        return _Sym()

    def symbol_info_tick(self, symbol):
        return self._tick

    def positions_get(self, symbol=None):
        return list(self._positions)

    def orders_get(self, symbol=None):
        return []

    def history_deals_get(self, date_from, date_to):
        return list(self._history)

    def order_send(self, request):
        self.requests.append(dict(request))
        if request.get("action") in self._fail_actions:
            return _Resp(retcode=10013, comment="rejected")
        return _Resp(order=1000 + len(self.requests))

    def last_error(self):
        return (0, "ok")


class _Conn:
    def __init__(self, mt5):
        self.mt5 = mt5


_CFG = replace(DEFAULT_CONFIG, entry_count=6, entry_ladder="range_to_sl",
               entry_sl_gap=0.5, sl_multiplier=2.1, minimum_lot=0.01, lot_step=0.01)
_SIGNAL = "1. BUY XAUUSD 4211 - 4209 SL 4206.50 TP1 4215.50 TP2 4218 TP3 4220 1:00 PM"


def _reset_executor_guards():
    Mt5Executor._session_skipped_inactive_signal_keys.clear()
    Mt5Executor._session_skipped_stale_entries.clear()
    Mt5Executor._session_skipped_expired_signal_keys.clear()
    Mt5Executor._session_failed_signal_keys.clear()
    Mt5Executor._session_skipped_traded_signal_keys.clear()


def _freeze_wall_clock(monkeypatch, when):
    monkeypatch.setattr(mt5_executor_tp2, "_wall_clock_chart_now", lambda: when)


def _sig():
    return parse_one_signal(_SIGNAL, "2026-06-12", 3)


def _executor(mt5):
    return Mt5Executor(_Conn(mt5), "XAUUSD", min_lot=0.01, lot_step=0.01, server_offset_hours=3)


def _pos_with_lock_exited_leg(status="LOCK_TP1"):
    """Engine position whose leg #1 already lock-exited in the replay."""
    pos = open_position(_sig(), 5000.0, _CFG)
    pos.entries[0].status = status
    pos.entries[0].fill_time = pos.activation_time
    pos.entries[0].exit_price = _sig().tp1
    pos.entries[0].pnl = 4.5
    return pos


def _live_leg(pos, *, sl):
    key = pos.signal.signal_key
    return _FakePos(
        ticket=77, magic=signal_to_magic(key),
        comment=mt5_entry_comment(key, 0),
        type_=_FakeMt5.POSITION_TYPE_BUY, sl=sl, tp=pos.signal.tp3,
        price_open=pos.entries[0].entry_price,
    )


def _manage(monkeypatch, mt5, pos):
    _reset_executor_guards()
    _freeze_wall_clock(monkeypatch, pos.activation_time + timedelta(minutes=10))
    return _executor(mt5).manage_position(pos, _CFG, pos.activation_time + timedelta(minutes=10))


# --- 1. late lock-exit catch-up locks instead of closing ---------------------

def test_late_lock_moves_sl_to_tp1_when_price_still_beyond_it(monkeypatch):
    pos = _pos_with_lock_exited_leg()
    live = _live_leg(pos, sl=pos.entries[0].initial_sl)
    mt5 = _FakeMt5(bid=4217.00, ask=4217.30, positions=[live])

    _manage(monkeypatch, mt5, pos)

    sltp = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_SLTP]
    deals = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_DEAL]
    assert len(sltp) == 1 and sltp[0]["sl"] == pos.signal.tp1
    assert deals == []  # the old behavior (market close) must be gone


def test_late_lock_falls_back_to_buffered_stop_when_price_through_tp1(monkeypatch):
    pos = _pos_with_lock_exited_leg()
    live = _live_leg(pos, sl=pos.entries[0].initial_sl)
    mt5 = _FakeMt5(bid=4213.00, ask=4213.30, positions=[live])  # below TP1 4215.50

    _manage(monkeypatch, mt5, pos)

    sltp = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_SLTP]
    deals = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_DEAL]
    assert len(sltp) == 1 and sltp[0]["sl"] == 4212.50  # bid - 0.5 buffer
    assert deals == []


def test_late_lock_ratchets_fallback_to_tp1_once_price_recovers(monkeypatch):
    pos = _pos_with_lock_exited_leg()
    live = _live_leg(pos, sl=4212.50)  # prior cycle's fallback stop
    mt5 = _FakeMt5(bid=4216.40, ask=4216.70, positions=[live])  # recovered past TP1

    _manage(monkeypatch, mt5, pos)

    sltp = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_SLTP]
    assert len(sltp) == 1 and sltp[0]["sl"] == pos.signal.tp1


def test_late_lock_skips_sub_step_ratchets_and_never_moves_backwards(monkeypatch):
    pos = _pos_with_lock_exited_leg()
    live = _live_leg(pos, sl=4212.50)
    # bid up only 0.10: fallback would improve by < LATE_LOCK_MIN_STEP.
    mt5 = _FakeMt5(bid=4213.10, ask=4213.40, positions=[live])

    _manage(monkeypatch, mt5, pos)
    assert [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_SLTP] == []

    # And a stop already at/beyond the level is left alone entirely.
    live2 = _live_leg(pos, sl=pos.signal.tp1)
    mt5b = _FakeMt5(bid=4217.00, ask=4217.30, positions=[live2])
    _manage(monkeypatch, mt5b, pos)
    assert mt5b.requests == []


def test_late_lock_closes_at_market_only_when_modify_fails(monkeypatch):
    pos = _pos_with_lock_exited_leg()
    live = _live_leg(pos, sl=pos.entries[0].initial_sl)
    mt5 = _FakeMt5(bid=4217.00, ask=4217.30, positions=[live],
                   fail_actions={_FakeMt5.TRADE_ACTION_SLTP})

    _manage(monkeypatch, mt5, pos)

    deals = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_DEAL]
    assert len(deals) == 1  # protective stop rejected -> last-resort close
    assert deals[0]["comment"].endswith("/late-tp1")


def test_late_tp2_lock_uses_tp2_level(monkeypatch):
    pos = _pos_with_lock_exited_leg(status="LOCK_TP2")
    live = _live_leg(pos, sl=pos.signal.tp1)
    mt5 = _FakeMt5(bid=4219.50, ask=4219.80, positions=[live])  # above TP2 4218

    _manage(monkeypatch, mt5, pos)

    sltp = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_SLTP]
    assert len(sltp) == 1 and sltp[0]["sl"] == pos.signal.tp2


# --- 2. re-open entries the replay still holds OPEN --------------------------

def _pos_with_open_leg():
    pos = open_position(_sig(), 5000.0, _CFG)
    pos.entries[0].status = "OPEN"
    pos.entries[0].fill_time = pos.activation_time
    return pos


def test_reopen_restores_hand_closed_leg_at_market(monkeypatch):
    _reset_executor_guards()
    pos = _pos_with_open_leg()
    _freeze_wall_clock(monkeypatch, pos.activation_time + timedelta(minutes=10))
    mt5 = _FakeMt5(bid=4212.00, ask=4212.30)  # no positions: leg closed by hand

    log = _executor(mt5).reopen_missing_open_positions(pos, _CFG)

    assert log.placed == 1
    (req,) = mt5.requests
    assert req["action"] == mt5.TRADE_ACTION_DEAL
    assert req["type"] == mt5.ORDER_TYPE_BUY
    assert req["price"] == 4212.30  # ask
    assert req["comment"] == mt5_entry_comment(pos.signal.signal_key, 0)
    assert req["volume"] == pos.entries[0].lot
    assert req["sl"] == round(pos.entries[0].initial_sl, 2)  # stage 0 -> original SL
    assert req["tp"] == round(pos.target_level, 2)


def test_reopen_leaves_present_and_terminal_legs_alone(monkeypatch):
    _reset_executor_guards()
    pos = _pos_with_open_leg()
    _freeze_wall_clock(monkeypatch, pos.activation_time + timedelta(minutes=10))
    live = _live_leg(pos, sl=pos.entries[0].initial_sl)
    mt5 = _FakeMt5(bid=4212.00, ask=4212.30, positions=[live])

    assert _executor(mt5).reopen_missing_open_positions(pos, _CFG).placed == 0
    assert mt5.requests == []

    pos.entries[0].status = "LOCK_TP1"  # replay exited it: nothing to restore
    mt5b = _FakeMt5(bid=4212.00, ask=4212.30)
    assert _executor(mt5b).reopen_missing_open_positions(pos, _CFG).placed == 0
    assert mt5b.requests == []


def test_reopen_does_not_race_the_time_exit(monkeypatch):
    _reset_executor_guards()
    pos = _pos_with_open_leg()
    pos.first_fill_time = pos.activation_time
    pos.time_exit_deadline = pos.activation_time + timedelta(minutes=30)
    _freeze_wall_clock(monkeypatch, pos.time_exit_deadline + timedelta(minutes=1))
    mt5 = _FakeMt5(bid=4212.00, ask=4212.30)

    assert _executor(mt5).reopen_missing_open_positions(pos, _CFG).placed == 0
    assert mt5.requests == []


def test_reopen_clamps_locked_stop_to_legal_level(monkeypatch):
    _reset_executor_guards()
    pos = _pos_with_open_leg()
    pos.stage = 1
    pos.stage1_time = pos.activation_time + timedelta(minutes=1)
    _freeze_wall_clock(monkeypatch, pos.activation_time + timedelta(minutes=10))
    # Replay's effective stop is TP1 (4215.50) but live bid is below it: the
    # re-opened position cannot carry an illegal SL, so it is buffered down.
    mt5 = _FakeMt5(bid=4213.00, ask=4213.30)

    log = _executor(mt5).reopen_missing_open_positions(pos, _CFG)

    assert log.placed == 1
    (req,) = mt5.requests
    assert req["sl"] == 4212.50  # bid - 0.5, not the illegal 4215.50


# --- 3. never re-place a signal that already traded --------------------------

def _plan(signal):
    activation = signal.signal_time_chart + timedelta(minutes=_CFG.activation_delay_minutes)
    return NewSignalPlan(
        signal=signal, action="FOLLOW", rationale="test",
        orders=[PlannedOrder(entry_index=0, side=signal.side, entry_price=4209.0,
                             initial_sl=4201.55, lot=0.01, risk_dollars=7.45)],
        pending_expires_at=activation + timedelta(minutes=_CFG.pending_expiry_minutes),
        final_target_label="TP3", final_target_price=signal.tp3,
        total_initial_risk_dollars=7.45,
        pending_activates_at=activation,
    )


def test_fresh_placement_blocked_by_deal_history_for_magic(monkeypatch):
    _reset_executor_guards()
    signal = _sig()
    _freeze_wall_clock(monkeypatch, signal.signal_time_chart + timedelta(minutes=5))
    mt5 = _FakeMt5(bid=4212.00, ask=4212.30,
                   history_deals=[_FakeDeal(magic=signal_to_magic(signal.signal_key))])

    log = _executor(mt5).place_signal(signal, _plan(signal))

    assert log.placed == 0
    assert mt5.requests == []
    assert any("already traded" in a for a in log.actions)


def test_fresh_placement_allowed_without_deal_history(monkeypatch):
    _reset_executor_guards()
    signal = _sig()
    _freeze_wall_clock(monkeypatch, signal.signal_time_chart + timedelta(minutes=5))
    mt5 = _FakeMt5(bid=4212.00, ask=4212.30,
                   history_deals=[_FakeDeal(magic=12345)])  # someone else's magic

    log = _executor(mt5).place_signal(signal, _plan(signal))

    assert log.placed == 1
    assert mt5.requests[0]["type"] == mt5.ORDER_TYPE_BUY_LIMIT
