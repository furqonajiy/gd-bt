"""Same-cycle MARKET open for favourably price-passed legs (reopen/mirror mode).

Operator rule (2026-06-19): "if we should open a SELL LIMIT at 4193 and price
is suddenly 4197, execute at market now -- don't wait for a LIMIT that can never
rest there." When price has already moved FAVOURABLY past a leg's entry
(BUY: ask <= entry, cheaper; SELL: bid >= entry, higher), ``place_signal`` opens
that leg at MARKET in the SAME cycle, at the better basis, with the leg's planned
stop+target -- instead of deferring it to the next-cycle reopen pass.

Guards that keep this safe / parity-preserving:
  * only in reopen/mirror mode (``_allow_partial_placement``);
  * only when a replay is present AND still holds the leg (not terminal) -- so a
    leg the backtest already closed is never resurrected;
  * an in-band-not-yet-reached leg (within the broker freeze band but on the
    wrong side of the entry for a market fill) still DEFERS;
  * without a replay (old direct-construction callers / backtests) the legacy
    defer is unchanged.

Stubbed MT5 -- no terminal needed.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from trading.engine import (
    DEFAULT_CONFIG, Mt5Executor, NewSignalPlan, PlannedOrder,
    parse_one_signal, signal_to_magic,
)
from trading.engine.execution import mt5_executor_live
from trading.engine.execution.mt5_executor import mt5_entry_comment


class _Resp:
    def __init__(self, retcode=10009, comment="done", order=123):
        self.retcode = retcode
        self.comment = comment
        self.order = order


class _Sym:
    digits = 2
    trade_stops_level = 0      # no freeze band unless a test overrides
    filling_mode = 2           # SYMBOL_FILLING_IOC


class _Tick:
    def __init__(self, bid: float, ask: float):
        self.bid = bid
        self.ask = ask


class _FakeMt5:
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_DEAL = 1
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_RETURN = 2
    SYMBOL_FILLING_IOC = 2
    SYMBOL_FILLING_FOK = 1

    def __init__(self, *, bid: float, ask: float, stops_level: int = 0):
        self._tick = _Tick(bid, ask)
        self._stops_level = stops_level
        self.requests = []

    def symbol_info(self, symbol):
        sym = _Sym()
        sym.trade_stops_level = self._stops_level
        return sym

    def symbol_info_tick(self, symbol):
        return self._tick

    def positions_get(self, symbol=None):
        return []

    def orders_get(self, symbol=None):
        return []

    def history_deals_get(self, date_from, date_to):
        return []

    def order_send(self, request):
        self.requests.append(dict(request))
        return _Resp(order=1000 + len(self.requests))

    def last_error(self):
        return (0, "ok")


class _Conn:
    def __init__(self, mt5):
        self.mt5 = mt5


def _reset_guards():
    Mt5Executor._session_skipped_inactive_signal_keys.clear()
    Mt5Executor._session_skipped_stale_entries.clear()
    Mt5Executor._session_skipped_expired_signal_keys.clear()
    Mt5Executor._session_failed_signal_keys.clear()
    Mt5Executor._session_skipped_traded_signal_keys.clear()
    Mt5Executor._session_market_opened_entries.clear()


def _freeze(monkeypatch, when):
    monkeypatch.setattr(mt5_executor_live, "_wall_clock_chart_now", lambda: when)


def _executor(mt5):
    return Mt5Executor(_Conn(mt5), "XAUUSD", min_lot=0.01, lot_step=0.01,
                       server_offset_hours=3)


def _plan(signal, entries, *, statuses, initial_sl, target):
    activation = signal.signal_time_chart + timedelta(
        minutes=DEFAULT_CONFIG.activation_delay_minutes)
    orders = [
        PlannedOrder(entry_index=i, side=signal.side, entry_price=p,
                     initial_sl=initial_sl, lot=0.10, risk_dollars=10.0)
        for i, p in enumerate(entries)
    ]
    replay = SimpleNamespace(
        entries=[SimpleNamespace(entry_index=i, status=s) for i, s in enumerate(statuses)])
    return NewSignalPlan(
        signal=signal, action="FOLLOW", rationale="test", orders=orders,
        pending_expires_at=activation + timedelta(minutes=DEFAULT_CONFIG.pending_expiry_minutes),
        final_target_label="TP3", final_target_price=target,
        total_initial_risk_dollars=30.0,
        pending_activates_at=activation,
        replay_position=replay,
    )


def _sell_signal():
    # SELL ladder; entries climb toward the SL above. Range 4127-4132-ish.
    return parse_one_signal(
        "37. SELL XAUUSD 4127 - 4129 SL 4143 TP1 4117 TP2 4107 TP3 4106.5 02:02 PM",
        source_date="2026-06-19", source_offset=3)


def _buy_signal():
    return parse_one_signal(
        "9. BUY XAUUSD 4193 - 4191 SL 4180 TP1 4200 TP2 4210 TP3 4220 02:02 PM",
        source_date="2026-06-19", source_offset=3)


def _after_activation(signal):
    return signal.signal_time_chart + timedelta(
        minutes=DEFAULT_CONFIG.activation_delay_minutes + 1)


# --- SELL: favourably-passed legs open at market, fresh legs stay LIMIT --------

def test_sell_passed_legs_open_at_market_same_cycle(monkeypatch):
    _reset_guards()
    sig = _sell_signal()
    _freeze(monkeypatch, _after_activation(sig))
    # bid 4129.04: entries 4127 & 4128 are price-passed (<= bid, sell higher =
    # better); entry 4131 still rests above as a valid SELL LIMIT.
    mt5 = _FakeMt5(bid=4129.04, ask=4129.34)
    ex = _executor(mt5)
    ex._allow_partial_placement = True
    plan = _plan(sig, [4127.0, 4128.0, 4131.0],
                 statuses=["OPEN", "OPEN", "PENDING"],
                 initial_sl=4143.0, target=4106.5)

    log = ex.place_signal(sig, plan)

    deals = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_DEAL]
    limits = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_PENDING]
    assert log.placed == 3
    # two market sells at the current bid, with the planned target and a legal SL
    assert len(deals) == 2
    assert all(r["type"] == mt5.ORDER_TYPE_SELL for r in deals)
    assert all(r["price"] == 4129.04 for r in deals)
    assert all(r["tp"] == 4106.5 for r in deals)
    assert all(r["sl"] == 4143.0 for r in deals)  # max(4143, ask+0.5) = 4143
    assert {r["comment"] for r in deals} == {
        mt5_entry_comment(sig.signal_key, 0), mt5_entry_comment(sig.signal_key, 1)}
    # the un-passed leg still rests as a SELL LIMIT at its entry
    assert len(limits) == 1
    assert limits[0]["type"] == mt5.ORDER_TYPE_SELL_LIMIT
    assert limits[0]["price"] == 4131.0
    assert any("opening at market" in a for a in log.actions)


# --- BUY mirrors the rule: passed = ask <= entry, fill at ask ------------------

def test_buy_passed_leg_opens_at_market_at_ask(monkeypatch):
    _reset_guards()
    sig = _buy_signal()
    _freeze(monkeypatch, _after_activation(sig))
    # ask 4189.70 <= entry 4193/4191: buying cheaper than planned = better basis.
    mt5 = _FakeMt5(bid=4189.40, ask=4189.70)
    ex = _executor(mt5)
    ex._allow_partial_placement = True
    plan = _plan(sig, [4193.0, 4191.0], statuses=["OPEN", "OPEN"],
                 initial_sl=4180.0, target=4220.0)

    log = ex.place_signal(sig, plan)

    deals = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_DEAL]
    assert log.placed == 2
    assert len(deals) == 2
    assert all(r["type"] == mt5.ORDER_TYPE_BUY for r in deals)
    assert all(r["price"] == 4189.70 for r in deals)   # the ask
    assert all(r["sl"] == 4180.0 for r in deals)        # min(4180, bid-0.5) = 4180
    assert all(r["tp"] == 4220.0 for r in deals)


# --- safety: a leg the replay already closed is NOT resurrected at market ------

def test_passed_leg_terminal_in_replay_is_not_market_opened(monkeypatch):
    _reset_guards()
    sig = _sell_signal()
    _freeze(monkeypatch, _after_activation(sig))
    mt5 = _FakeMt5(bid=4129.04, ask=4129.34)
    ex = _executor(mt5)
    ex._allow_partial_placement = True
    # entry 0 already lock-exited in the replay -> must NOT be re-opened; entry 1
    # is still OPEN -> opens at market; entry 2 rests as a LIMIT.
    plan = _plan(sig, [4127.0, 4128.0, 4131.0],
                 statuses=["LOCK_TP1", "OPEN", "PENDING"],
                 initial_sl=4143.0, target=4106.5)

    log = ex.place_signal(sig, plan)

    deals = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_DEAL]
    assert log.placed == 2  # one market (#1) + one limit (#2); #0 deferred
    assert len(deals) == 1
    assert deals[0]["comment"] == mt5_entry_comment(sig.signal_key, 1)
    assert any("stale SELL LIMIT 4127" in a for a in log.actions)
    # the closed leg never reached the broker
    assert not any(r.get("comment") == mt5_entry_comment(sig.signal_key, 0)
                   for r in mt5.requests)


# --- in-band-not-reached legs still defer (market fill would be wrong-side) ----

def test_in_band_leg_still_defers_not_market(monkeypatch):
    _reset_guards()
    sig = _sell_signal()
    _freeze(monkeypatch, _after_activation(sig))
    # stops_level 50 pts * 0.01 = 0.50 min distance. bid 4129.04 ->
    #   4127     : price-passed (<= bid)          -> MARKET
    #   4129.30  : NOT passed, within 0.50 band   -> DEFER (no market, no limit)
    #   4131     : placeable SELL LIMIT
    mt5 = _FakeMt5(bid=4129.04, ask=4129.34, stops_level=50)
    ex = _executor(mt5)
    ex._allow_partial_placement = True
    plan = _plan(sig, [4127.0, 4129.30, 4131.0],
                 statuses=["OPEN", "PENDING", "PENDING"],
                 initial_sl=4143.0, target=4106.5)

    log = ex.place_signal(sig, plan)

    deals = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_DEAL]
    limits = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_PENDING]
    assert len(deals) == 1 and deals[0]["price"] == 4129.04        # #0 market
    assert len(limits) == 1 and limits[0]["price"] == 4131.0       # #2 limit
    assert log.placed == 2                                          # #1 deferred
    assert any("within broker stop/freeze level" in a for a in log.actions)


# --- legacy parity: no reopen mode -> price-passed defers, no market fill ------

def test_no_reopen_mode_keeps_legacy_defer(monkeypatch):
    _reset_guards()
    sig = _sell_signal()
    _freeze(monkeypatch, _after_activation(sig))
    mt5 = _FakeMt5(bid=4129.04, ask=4129.34)
    ex = _executor(mt5)  # _allow_partial_placement defaults False
    plan = _plan(sig, [4127.0, 4128.0, 4131.0],
                 statuses=["OPEN", "OPEN", "PENDING"],
                 initial_sl=4143.0, target=4106.5)

    log = ex.place_signal(sig, plan)

    # legacy: any stale leg without reopen mode skips the whole ladder.
    assert log.placed == 0
    assert all(r["action"] != mt5.TRADE_ACTION_DEAL for r in mt5.requests)
    assert any("skipped entire ladder" in a for a in log.actions)
