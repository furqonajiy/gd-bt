"""Regression tests for DD40 live-execution stop locking."""
from __future__ import annotations
from dataclasses import replace
from datetime import datetime

from xauusd_trading import DEFAULT_CONFIG, Mt5Executor, open_position, parse_one_signal, signal_to_magic


DD40_COMMAND_CONFIG = replace(
    DEFAULT_CONFIG,
    initial_capital=1000.0,
    sizing_mode="risk",
    risk_per_signal=0.05575,
    entry_count=3,
    entry_ladder="range_to_sl",
    entry_sl_gap=2.0,
    activation_delay_minutes=3,
    pending_expiry_minutes=630,
    max_hold_minutes=90,
    sl_multiplier=1.61,
    final_target="TP3",
    lock_after_tp2=False,
    bonus_per_closed_lot=3.0,
)


class _Resp:
    def __init__(self, retcode=10009, comment="done", order=123):
        self.retcode = retcode
        self.comment = comment
        self.order = order


class _Sym:
    digits = 2
    trade_stops_level = 0
    trade_freeze_level = 0
    freeze_level = 0

    def __init__(self, *, stops=0, freeze=0):
        self.trade_stops_level = stops
        self.trade_freeze_level = freeze
        self.freeze_level = freeze


class _Tick:
    def __init__(self, bid=4500.0, ask=4500.2):
        self.bid = bid
        self.ask = ask


class _FakePosition:
    def __init__(self, *, ticket, magic, type_, sl, tp, volume=0.5, comment="", time=0, price_open=0.0):
        self.ticket = ticket
        self.magic = magic
        self.type = type_
        self.sl = sl
        self.tp = tp
        self.volume = volume
        self.comment = comment
        self.time = time
        self.price_open = price_open


class _FakeMt5:
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_DEAL = 1
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_FILLING_RETURN = 2
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    def __init__(self, positions, *, tick=None, stops=0, freeze=0):
        self._positions = list(positions)
        self._tick = tick or _Tick()
        self._sym = _Sym(stops=stops, freeze=freeze)
        self.requests = []

    def symbol_info(self, symbol):
        return self._sym

    def symbol_info_tick(self, symbol):
        return self._tick

    def positions_get(self, symbol=None):
        return list(self._positions)

    def orders_get(self, symbol=None):
        return []

    def order_send(self, request):
        self.requests.append(dict(request))
        if request.get("action") == self.TRADE_ACTION_SLTP:
            for p in self._positions:
                if p.ticket == request["position"]:
                    p.sl = request["sl"]
                    p.tp = request["tp"]
                    break
        return _Resp()

    def last_error(self):
        return (0, "ok")


class _FakeConn:
    def __init__(self, mt5):
        self.mt5 = mt5


def test_dd40_does_not_move_live_sl_to_tp2_when_engine_stage_is_2():
    signal = parse_one_signal(
        "1. BUY XAUUSD 4518 - 4516 SL 4511 TP1 4526 TP2 4536 TP3 4551 11:25 AM",
        source_date="2026-05-05",
        source_offset=7,
    )
    pos = open_position(signal, equity=1000.0, config=DD40_COMMAND_CONFIG)
    pos.stage = 2
    pos.stage1_time = datetime(2026, 5, 5, 7, 30)
    pos.stage2_time = datetime(2026, 5, 5, 7, 35)
    pos.entries[0].status = "OPEN"
    pos.entries[0].fill_time = datetime(2026, 5, 5, 7, 27)

    mt5_pos = _FakePosition(
        ticket=777,
        magic=signal_to_magic(signal.signal_key),
        type_=_FakeMt5.POSITION_TYPE_BUY,
        sl=signal.tp1,
        tp=signal.tp3,
    )
    mt5 = _FakeMt5([mt5_pos])
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.manage_position(pos, DD40_COMMAND_CONFIG, datetime(2026, 5, 5, 7, 36))

    assert mt5_pos.sl == signal.tp1
    assert not any(req.get("action") == mt5.TRADE_ACTION_SLTP and req.get("sl") == signal.tp2 for req in mt5.requests)
    assert not any("TP2" in action for action in log.actions)


def test_tp1_lock_clamps_sl_to_broker_stops_level_before_modify():
    signal = parse_one_signal(
        "1. BUY XAUUSD 4518 - 4516 SL 4511 TP1 4526 TP2 4536 TP3 4551 11:25 AM",
        source_date="2026-05-05",
        source_offset=7,
    )
    cfg = replace(DD40_COMMAND_CONFIG, lock_after_tp1=True)
    pos = open_position(signal, equity=1000.0, config=cfg)
    pos.stage = 1
    pos.stage1_time = datetime(2026, 5, 5, 7, 30)
    pos.entries[0].status = "OPEN"
    pos.entries[0].fill_time = datetime(2026, 5, 5, 7, 27)

    mt5_pos = _FakePosition(
        ticket=888,
        magic=signal_to_magic(signal.signal_key),
        type_=_FakeMt5.POSITION_TYPE_BUY,
        sl=4510.0,
        tp=signal.tp3,
        comment=f"{signal.signal_key}.1",
    )
    mt5 = _FakeMt5([mt5_pos], tick=_Tick(bid=4526.0, ask=4526.2), stops=50)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.manage_position(pos, cfg, datetime(2026, 5, 5, 7, 31))

    sltp_requests = [req for req in mt5.requests if req.get("action") == mt5.TRADE_ACTION_SLTP]
    assert len(sltp_requests) == 1
    assert sltp_requests[0]["sl"] == 4525.5
    assert mt5_pos.sl == 4525.5
    assert any("Clamped TP1 SL" in action and "requested 4526" in action and "4525.5" in action for action in log.actions)
    assert any("Locked SL on #888 to TP1 4525.5" in action for action in log.actions)


def test_external_sl_change_warns_without_modify_when_executor_issued_no_modify():
    signal = parse_one_signal(
        "1. BUY XAUUSD 4518 - 4516 SL 4511 TP1 4526 TP2 4536 TP3 4551 11:25 AM",
        source_date="2026-05-05",
        source_offset=7,
    )
    cfg = replace(DD40_COMMAND_CONFIG, lock_after_tp1=True)
    pos = open_position(signal, equity=1000.0, config=cfg)
    pos.stage = 1
    pos.stage1_time = datetime(2026, 5, 5, 7, 30)
    pos.entries[0].status = "OPEN"
    pos.entries[0].fill_time = datetime(2026, 5, 5, 7, 27)

    # This SL is higher than TP1 for a BUY, so the normal TP1 lock path does not
    # improve it and sends no SLTP modify. The warning path should still detect
    # that the executor-owned stop differs from the engine's expected TP1.
    mt5_pos = _FakePosition(
        ticket=999,
        magic=signal_to_magic(signal.signal_key),
        type_=_FakeMt5.POSITION_TYPE_BUY,
        sl=4527.0,
        tp=signal.tp3,
        comment=f"{signal.signal_key}.1",
    )
    mt5 = _FakeMt5([mt5_pos], tick=_Tick(bid=4528.0, ask=4528.2))
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")

    log = executor.manage_position(pos, cfg, datetime(2026, 5, 5, 7, 31))

    assert not any(req.get("action") == mt5.TRADE_ACTION_SLTP for req in mt5.requests)
    assert any("external SL change detected" in warning for warning in log.warnings)
    assert any("MT5 native trailing enabled" in warning for warning in log.warnings)


class _RecordingNotifier:
    """Captures sl_moved; no-ops any other notifier method the executor calls."""
    def __init__(self):
        self.sl_moved_calls = []

    def sl_moved(self, **kw):
        self.sl_moved_calls.append(kw)

    def __getattr__(self, _name):
        return lambda *a, **k: None


def test_trailing_close_notifies_broker_clamped_sl_not_requested():
    # Reproduces live signal 2026-06-04#01: the engine's trailing-close target sits
    # below the market, so stops_level clamps it. The notification must report the
    # clamped SL the broker actually holds, not the (unattainable) requested level.
    signal = parse_one_signal(
        "1. SELL XAUUSD 4476 - 4474 SL 4481 TP1 4464 TP2 4454 TP3 4434 07:25 AM",
        source_date="2026-06-04",
        source_offset=3,
    )
    cfg = replace(
        DD40_COMMAND_CONFIG,
        entry_count=1,
        trailing_close_distance=0.5,
        activation_delay_minutes=0,
        lock_after_tp1=False,
        lock_after_tp2=False,
    )
    pos = open_position(signal, equity=1000.0, config=cfg)
    pos.stage = 0
    e = pos.entries[0]
    e.status = "OPEN"
    e.fill_time = datetime(2026, 6, 4, 7, 27)
    e.trailing_stop = 4473.83  # ratcheted short stop = bar low 4473.33 + 0.5

    mt5_pos = _FakePosition(
        ticket=4242,
        magic=signal_to_magic(signal.signal_key),
        type_=_FakeMt5.POSITION_TYPE_SELL,
        sl=4480.23,
        tp=signal.tp1,
        volume=0.22,
        comment=f"{signal.signal_key}.1",
    )
    # Ask 4474.58 + stops_level 40 pts (0.40) => a SELL SL must be >= 4474.98,
    # so the engine's 4473.83 target is clamped UP to 4474.98.
    mt5 = _FakeMt5([mt5_pos], tick=_Tick(bid=4474.31, ask=4474.58), stops=40)
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")
    notifier = _RecordingNotifier()
    executor.notifier = notifier

    executor.manage_position(pos, cfg, datetime(2026, 6, 4, 7, 28, 36))

    # Broker holds the clamped SL...
    assert mt5_pos.sl == 4474.98
    # ...and the operator is told THAT, not the requested 4473.83 (the bug).
    assert len(notifier.sl_moved_calls) == 1
    call = notifier.sl_moved_calls[0]
    assert call["new_sl"] == 4474.98
    assert call["new_sl"] != 4473.83
    assert "clamp" in call["reason"].lower()

def _reconcile_chart_stub():
    class _Chart:
        def bars_between(self, start, end):
            return []
    return _Chart()


def test_reconcile_announces_each_fill_once_across_cycles():
    """A live position whose replay status keeps coming back non-terminal
    (TRAILING_STOP, absent from _REPLAY_CLOSED_STATUSES) is re-patched to OPEN
    every cycle. The stdout announcement must fire once; the engine patch must
    still happen every cycle so manage/close keep seeing the real fill."""
    Mt5Executor._session_announced_reconciles.clear()

    signal = parse_one_signal(
        "1. BUY XAUUSD 4518 - 4516 SL 4511 TP1 4526 TP2 4536 TP3 4551 11:25 AM",
        source_date="2026-06-04",
        source_offset=7,
    )
    pos = open_position(signal, equity=1000.0, config=DD40_COMMAND_CONFIG)

    mt5_pos = _FakePosition(
        ticket=4242,
        magic=signal_to_magic(signal.signal_key),
        type_=_FakeMt5.POSITION_TYPE_BUY,
        sl=signal.tp1,
        tp=signal.tp3,
        volume=0.20,
        comment=f"{signal.signal_key}.1",
        time=1_780_000_000,
        price_open=4494.8,
    )
    mt5 = _FakeMt5([mt5_pos])
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")
    chart = _reconcile_chart_stub()
    now = datetime(2026, 6, 4, 14, 0)

    def reset_replay_divergence():
        # Simulate the engine re-replaying the trailing exit each cycle: the slot
        # comes back TRAILING_STOP at a price that differs from the live fill.
        entry = pos.entries[0]
        entry.status = "TRAILING_STOP"
        entry.entry_price = 4488.55
        entry.fill_time = None
        entry.lot = 0.0

    reset_replay_divergence()
    log1 = executor.reconcile_with_mt5(pos, DD40_COMMAND_CONFIG, chart, now)
    reconciled1 = [a for a in log1.actions if "Reconciled #0" in a]
    assert len(reconciled1) == 1
    # The patch still landed despite the divergent replay status.
    assert pos.entries[0].status == "OPEN"
    assert abs(pos.entries[0].entry_price - 4494.8) < 1e-9

    reset_replay_divergence()
    log2 = executor.reconcile_with_mt5(pos, DD40_COMMAND_CONFIG, chart, now)
    reconciled2 = [a for a in log2.actions if "Reconciled #0" in a]
    assert reconciled2 == []  # same fill -> announced once, not re-spammed
    # ...but the engine entry was still patched back to the real fill this cycle.
    assert pos.entries[0].status == "OPEN"
    assert abs(pos.entries[0].entry_price - 4494.8) < 1e-9


def test_reconcile_reannounces_when_actual_fill_changes():
    """A genuinely new MT5 fill (different price) is a new event and must print."""
    Mt5Executor._session_announced_reconciles.clear()

    signal = parse_one_signal(
        "1. BUY XAUUSD 4518 - 4516 SL 4511 TP1 4526 TP2 4536 TP3 4551 11:25 AM",
        source_date="2026-06-04",
        source_offset=7,
    )
    pos = open_position(signal, equity=1000.0, config=DD40_COMMAND_CONFIG)
    mt5_pos = _FakePosition(
        ticket=4243,
        magic=signal_to_magic(signal.signal_key),
        type_=_FakeMt5.POSITION_TYPE_BUY,
        sl=signal.tp1,
        tp=signal.tp3,
        volume=0.20,
        comment=f"{signal.signal_key}.1",
        time=1_780_000_000,
        price_open=4494.8,
    )
    mt5 = _FakeMt5([mt5_pos])
    executor = Mt5Executor(_FakeConn(mt5), "XAUUSD")
    chart = _reconcile_chart_stub()
    now = datetime(2026, 6, 4, 14, 0)

    pos.entries[0].status = "NO_FILL"
    pos.entries[0].entry_price = 4516.0
    pos.entries[0].fill_time = None
    log1 = executor.reconcile_with_mt5(pos, DD40_COMMAND_CONFIG, chart, now)
    assert len([a for a in log1.actions if "Reconciled #0" in a]) == 1

    # Broker reports a different fill price for the same slot -> new signature.
    mt5_pos.price_open = 4495.6
    pos.entries[0].status = "NO_FILL"
    pos.entries[0].entry_price = 4516.0
    pos.entries[0].fill_time = None
    log2 = executor.reconcile_with_mt5(pos, DD40_COMMAND_CONFIG, chart, now)
    assert len([a for a in log2.actions if "Reconciled #0" in a]) == 1