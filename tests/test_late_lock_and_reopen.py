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
    Mt5Executor._session_skipped_recently_closed.clear()


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


def test_late_lock_closes_at_market_when_through_lock_but_in_profit(monkeypatch):
    # Price retraced back through TP1 (4215.50) but the leg is still in profit
    # vs its 4211 entry: take the profit at market now instead of parking a
    # below-lock stop that could run to a loss (the 0618#04 give-back).
    pos = _pos_with_lock_exited_leg()
    live = _live_leg(pos, sl=pos.entries[0].initial_sl)
    mt5 = _FakeMt5(bid=4213.00, ask=4213.30, positions=[live])  # below TP1, above entry

    _manage(monkeypatch, mt5, pos)

    sltp = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_SLTP]
    deals = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_DEAL]
    assert sltp == []                       # no below-lock stop parked
    assert len(deals) == 1                  # closed at market in profit
    assert deals[0]["comment"].endswith("/late-tp1")


def test_late_lock_protects_underwater_leg_without_market_dumping(monkeypatch):
    # Same late-lock state but the leg is now UNDERWATER (below the 4211 entry):
    # do NOT crystallize the loss at market (the 2026-06-12 lesson). Park the
    # closest legal protective stop and let later cycles ratchet it up.
    pos = _pos_with_lock_exited_leg()
    entry = pos.entries[0].entry_price                  # 4211.0
    live = _live_leg(pos, sl=pos.entries[0].initial_sl)  # 4201.55
    bid = round(entry - 3.0, 2)                          # 4208.0, underwater
    mt5 = _FakeMt5(bid=bid, ask=round(bid + 0.30, 2), positions=[live])

    _manage(monkeypatch, mt5, pos)

    sltp = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_SLTP]
    deals = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_DEAL]
    assert len(sltp) == 1 and sltp[0]["sl"] == round(bid - 0.5, 2)  # buffered stop
    assert deals == []                                              # never dumped


def test_late_lock_ratchets_fallback_to_tp1_once_price_recovers(monkeypatch):
    pos = _pos_with_lock_exited_leg()
    live = _live_leg(pos, sl=4212.50)  # prior cycle's fallback stop
    mt5 = _FakeMt5(bid=4216.40, ask=4216.70, positions=[live])  # recovered past TP1

    _manage(monkeypatch, mt5, pos)

    sltp = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_SLTP]
    assert len(sltp) == 1 and sltp[0]["sl"] == pos.signal.tp1


def test_late_lock_leaves_an_already_protected_stop_alone(monkeypatch):
    # Stop already at/beyond the lock level and price still beyond it: nothing
    # to do -- no modify, no close.
    pos = _pos_with_lock_exited_leg()
    live = _live_leg(pos, sl=pos.signal.tp1)
    mt5 = _FakeMt5(bid=4217.00, ask=4217.30, positions=[live])
    _manage(monkeypatch, mt5, pos)
    assert mt5.requests == []


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
    # Favorable price (ask 4210.30 <= entry 4211): market re-open captures a
    # better basis than the model, with the original stop and target.
    _reset_executor_guards()
    pos = _pos_with_open_leg()
    _freeze_wall_clock(monkeypatch, pos.activation_time + timedelta(minutes=10))
    mt5 = _FakeMt5(bid=4210.00, ask=4210.30)  # no positions: leg closed by hand

    log = _executor(mt5).reopen_missing_open_positions(pos, _CFG)

    assert log.placed == 1
    (req,) = mt5.requests
    assert req["action"] == mt5.TRADE_ACTION_DEAL
    assert req["type"] == mt5.ORDER_TYPE_BUY
    assert req["price"] == 4210.30  # ask
    assert req["comment"] == mt5_entry_comment(pos.signal.signal_key, 0)
    assert req["volume"] == pos.entries[0].lot
    assert req["sl"] == round(pos.entries[0].initial_sl, 2)  # stage 0 -> original SL
    assert req["tp"] == round(pos.target_level, 2)


def _out_deal(magic, comment, *, epoch, pid=999, entry=1):
    from types import SimpleNamespace
    return SimpleNamespace(magic=magic, entry=entry, position_id=pid,
                           comment=comment, time=epoch)


def _epoch_chart(dt):
    # MT5 deal.time is the server wall-clock as an epoch; read back as a naive UTC
    # datetime it equals chart-local time, so mirror that for the fixture.
    from datetime import timezone
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def test_reopen_suppressed_when_leg_closed_recently(monkeypatch):
    # Leg #0 closed live ~30s ago (SL/lock/TP fired) while the bar-close replay
    # still holds it OPEN -> reopen must NOT resurrect it (the churn guard).
    _reset_executor_guards()
    pos = _pos_with_open_leg()
    now = pos.activation_time + timedelta(minutes=10)
    _freeze_wall_clock(monkeypatch, now)
    key = pos.signal.signal_key
    magic = signal_to_magic(key)
    in_d = _out_deal(magic, mt5_entry_comment(key, 0),
                     epoch=_epoch_chart(now - timedelta(seconds=90)), entry=0)
    out_d = _out_deal(magic, "sl", epoch=_epoch_chart(now - timedelta(seconds=30)))
    mt5 = _FakeMt5(bid=4210.00, ask=4210.30, history_deals=[in_d, out_d])

    log = _executor(mt5).reopen_missing_open_positions(pos, _CFG)

    assert log.placed == 0
    assert mt5.requests == []
    assert any("not re-opened" in a for a in log.actions)


def test_reopen_allowed_when_close_is_old(monkeypatch):
    # Same leg, but the close was 10 min ago (> cooldown) and the replay STILL
    # holds it OPEN -> a genuine early hand-close, restored as normal.
    _reset_executor_guards()
    pos = _pos_with_open_leg()
    now = pos.activation_time + timedelta(minutes=20)
    _freeze_wall_clock(monkeypatch, now)
    key = pos.signal.signal_key
    magic = signal_to_magic(key)
    in_d = _out_deal(magic, mt5_entry_comment(key, 0),
                     epoch=_epoch_chart(now - timedelta(minutes=15)), entry=0)
    out_d = _out_deal(magic, "sl", epoch=_epoch_chart(now - timedelta(minutes=10)))
    mt5 = _FakeMt5(bid=4210.00, ask=4210.30, history_deals=[in_d, out_d])

    log = _executor(mt5).reopen_missing_open_positions(pos, _CFG)

    assert log.placed == 1
    assert mt5.requests and mt5.requests[0]["action"] == mt5.TRADE_ACTION_DEAL


def test_reopen_unfavorable_price_places_limit_at_entry(monkeypatch):
    # Price above a BUY entry: never chase. Re-place a LIMIT at the original
    # entry (inside the pending window) so a fill can only happen at the
    # modeled basis -- original SL and target attached.
    _reset_executor_guards()
    pos = _pos_with_open_leg()
    _freeze_wall_clock(monkeypatch, pos.activation_time + timedelta(minutes=10))
    mt5 = _FakeMt5(bid=4212.00, ask=4212.30)  # ask > entry 4211

    log = _executor(mt5).reopen_missing_open_positions(pos, _CFG)

    assert log.placed == 1
    (req,) = mt5.requests
    assert req["action"] == mt5.TRADE_ACTION_PENDING
    assert req["type"] == mt5.ORDER_TYPE_BUY_LIMIT
    assert req["price"] == round(pos.entries[0].entry_price, 2)
    assert req["sl"] == round(pos.entries[0].initial_sl, 2)
    assert req["tp"] == round(pos.target_level, 2)
    assert req["comment"] == mt5_entry_comment(pos.signal.signal_key, 0)


def test_reopen_unfavorable_past_window_does_not_chase(monkeypatch):
    # Past expiry_time the manage pass cancels every pending each cycle; a
    # re-placed limit would ping-pong forever. Accept the miss instead.
    _reset_executor_guards()
    pos = _pos_with_open_leg()
    _freeze_wall_clock(monkeypatch, pos.expiry_time + timedelta(minutes=1))
    mt5 = _FakeMt5(bid=4212.00, ask=4212.30)

    log = _executor(mt5).reopen_missing_open_positions(pos, _CFG)
    assert log.placed == 0
    assert mt5.requests == []


def test_reopen_locked_leg_stays_market_even_at_unfavorable_price(monkeypatch):
    # TP1-locked replay stop sits above the entry: the no-chase rule does NOT
    # apply (a limit at the entry would fill into its own stop). The leg keeps
    # the #69 market re-open; with price beyond the lock the stop rides as-is.
    _reset_executor_guards()
    pos = _pos_with_open_leg()
    pos.stage = 1
    pos.stage1_time = pos.activation_time + timedelta(minutes=1)
    _freeze_wall_clock(monkeypatch, pos.activation_time + timedelta(minutes=10))
    mt5 = _FakeMt5(bid=4216.40, ask=4216.70)  # ask > entry 4211, bid > TP1 lock

    log = _executor(mt5).reopen_missing_open_positions(pos, _CFG)
    assert log.placed == 1
    (req,) = mt5.requests
    assert req["action"] == mt5.TRADE_ACTION_DEAL
    assert req["type"] == mt5.ORDER_TYPE_BUY
    assert req["sl"] == 4215.50  # the TP1 lock, legal since bid 4216.40 above it


def test_reopen_sell_sides_mirror_the_price_rule(monkeypatch):
    # SELL favorable = bid >= entry (market); unfavorable = bid < entry (limit).
    _reset_executor_guards()
    sell = parse_one_signal(
        "2. SELL XAUUSD 4211 - 4213 SL 4217.50 TP1 4206 TP2 4203 TP3 4200 1:00 PM",
        "2026-06-12", 3)
    pos = open_position(sell, 5000.0, _CFG)
    pos.entries[0].status = "OPEN"
    pos.entries[0].fill_time = pos.activation_time
    entry = pos.entries[0].entry_price  # 4211 (top of band toward SL ladder)
    _freeze_wall_clock(monkeypatch, pos.activation_time + timedelta(minutes=10))

    mt5_fav = _FakeMt5(bid=entry + 1.0, ask=entry + 1.3)   # bid above entry
    _executor(mt5_fav).reopen_missing_open_positions(pos, _CFG)
    assert mt5_fav.requests[0]["action"] == _FakeMt5.TRADE_ACTION_DEAL
    assert mt5_fav.requests[0]["type"] == _FakeMt5.ORDER_TYPE_SELL

    mt5_unf = _FakeMt5(bid=entry - 1.0, ask=entry - 0.7)   # bid below entry
    _executor(mt5_unf).reopen_missing_open_positions(pos, _CFG)
    assert mt5_unf.requests[0]["action"] == _FakeMt5.TRADE_ACTION_PENDING
    assert mt5_unf.requests[0]["type"] == _FakeMt5.ORDER_TYPE_SELL_LIMIT
    assert mt5_unf.requests[0]["price"] == round(entry, 2)


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


def _partial_plan(signal):
    """A partial FOLLOW plan: replay has 6 entries, only the last 2 still
    placeable as fresh LIMITs (the rest played out in the replay)."""
    from types import SimpleNamespace
    activation = signal.signal_time_chart + timedelta(minutes=_CFG.activation_delay_minutes)
    replay = SimpleNamespace(
        entries=[SimpleNamespace(entry_index=i, status="CLOSED") for i in range(6)])
    return NewSignalPlan(
        signal=signal, action="FOLLOW", rationale="partial",
        orders=[PlannedOrder(entry_index=4, side=signal.side, entry_price=4209.0,
                             initial_sl=4201.55, lot=0.01, risk_dollars=7.45),
                PlannedOrder(entry_index=5, side=signal.side, entry_price=4208.0,
                             initial_sl=4201.55, lot=0.01, risk_dollars=7.45)],
        pending_expires_at=activation + timedelta(minutes=_CFG.pending_expiry_minutes),
        final_target_label="TP3", final_target_price=signal.tp3,
        total_initial_risk_dollars=14.9,
        pending_activates_at=activation,
        replay_position=replay,
    )


def test_partial_placement_skipped_by_default(monkeypatch):
    # Legacy/backtest parity: without reopen mode a partial signal is skipped.
    _reset_executor_guards()
    signal = _sig()
    _freeze_wall_clock(monkeypatch, signal.signal_time_chart + timedelta(minutes=5))
    mt5 = _FakeMt5(bid=4212.00, ask=4212.30)

    log = _executor(mt5).place_signal(signal, _partial_plan(signal))

    assert log.placed == 0
    assert mt5.requests == []
    assert any("skipped partial placement" in a for a in log.actions)


def test_partial_placement_places_fresh_legs_in_reopen_mode(monkeypatch):
    # With reopen/mirror mode the fresh LIMIT legs ARE placed (the OPEN legs are
    # left for reopen_missing_open_positions to restore at market).
    _reset_executor_guards()
    signal = _sig()
    _freeze_wall_clock(monkeypatch, signal.signal_time_chart + timedelta(minutes=5))
    mt5 = _FakeMt5(bid=4212.00, ask=4212.30)
    ex = _executor(mt5)
    ex._allow_partial_placement = True

    log = ex.place_signal(signal, _partial_plan(signal))

    assert log.placed == 2
    assert len(mt5.requests) == 2
    assert all(r["type"] == mt5.ORDER_TYPE_BUY_LIMIT for r in mt5.requests)
    assert {r["comment"] for r in mt5.requests} == {
        mt5_entry_comment(signal.signal_key, 4),
        mt5_entry_comment(signal.signal_key, 5),
    }


# --- 4. time-exit deadline fallback when the replay never recorded a fill -----

def _live_open_leg(pos, *, open_chart):
    """A live BUY position whose broker open time maps to open_chart."""
    key = pos.signal.signal_key
    return _FakePos(
        ticket=88, magic=signal_to_magic(key),
        comment=mt5_entry_comment(key, 0),
        type_=_FakeMt5.POSITION_TYPE_BUY, sl=pos.entries[0].initial_sl,
        tp=pos.signal.tp3, price_open=pos.entries[0].entry_price,
        time=_epoch_chart(open_chart),
    )


def test_time_exit_fires_via_deadline_fallback_when_replay_never_set_it(monkeypatch):
    # Orphan case: a live position is open past max-hold but the replay never
    # set time_exit_deadline (reconcile missed the fill / executor restarted).
    # The fallback derives the deadline from the live position's own open time,
    # so the leg still time-exits instead of sitting open forever.
    _reset_executor_guards()
    pos = _pos_with_open_leg()
    pos.first_fill_time = None
    pos.time_exit_deadline = None
    now = pos.activation_time + timedelta(minutes=_CFG.max_hold_minutes + 30)
    _freeze_wall_clock(monkeypatch, now)
    open_chart = now - timedelta(minutes=_CFG.max_hold_minutes + 10)  # past max-hold
    mt5 = _FakeMt5(bid=4212.00, ask=4212.30,
                   positions=[_live_open_leg(pos, open_chart=open_chart)])

    log = _executor(mt5).manage_position(pos, _CFG, now)

    deals = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_DEAL]
    assert len(deals) == 1  # the orphaned leg was timed out
    assert any("Time-exit" in a for a in log.actions)


def test_deadline_fallback_keeps_a_fresh_leg_open(monkeypatch):
    # Same None-deadline state, but the live position opened well within max-hold:
    # the fallback must NOT close it early.
    _reset_executor_guards()
    pos = _pos_with_open_leg()
    pos.first_fill_time = None
    pos.time_exit_deadline = None
    now = pos.activation_time + timedelta(minutes=10)
    _freeze_wall_clock(monkeypatch, now)
    open_chart = now - timedelta(minutes=5)  # fresh
    mt5 = _FakeMt5(bid=4212.00, ask=4212.30,
                   positions=[_live_open_leg(pos, open_chart=open_chart)])

    log = _executor(mt5).manage_position(pos, _CFG, now)

    deals = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_DEAL]
    assert deals == []  # not at max-hold yet -> leg stays open
