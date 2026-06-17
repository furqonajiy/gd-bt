"""Live executor side of provider edit/delete propagation.

Covers the pieces that make the live executor follow a corrected/removed VICTOR
signal:

  * ``Mt5Executor.flatten_signal`` -- cancel every pending and close every open
    position for a signal's magic (the "delete existing VIC signal" step),
    touching only that magic.
  * ``place_signal`` history-gate bypass for an amended key -- a deliberate
    close-and-reopen must be allowed to re-place even though the magic now has
    closed deals, while every other path still refuses to re-trade a finished
    magic.
  * ``_consume_signal_overrides`` -- read the listener's amend/revoke journal,
    flatten + untrack, queue an amend for re-placement, and stay idempotent via
    the byte-offset sidecar (first run anchors at EOF so the backlog is skipped).

All MT5 is stubbed; no terminal needed.
"""
from __future__ import annotations

from datetime import datetime

from xauusd_trading import (
    DEFAULT_CONFIG, ExecutionLog, Mt5Executor, SignalRegistry,
    parse_one_signal, signal_to_magic,
)
from xauusd_trading.strategy.engine import NewSignalPlan, PlannedOrder
from xauusd_trading.cli_orig import _consume_signal_overrides


# --- stub MT5 ---------------------------------------------------------------

class _Resp:
    def __init__(self, retcode, order=0):
        self.retcode = retcode
        self.comment = "done"
        self.order = order


class _Sym:
    digits = 2
    filling_mode = 2  # SYMBOL_FILLING_IOC


class _Tick:
    def __init__(self, bid=4500.0, ask=4500.2):
        self.bid = bid
        self.ask = ask


class _Order:
    def __init__(self, ticket, magic, comment=""):
        self.ticket = ticket
        self.magic = magic
        self.comment = comment


class _Pos:
    def __init__(self, ticket, magic, type_, volume=0.1, comment="", time=0):
        self.ticket = ticket
        self.magic = magic
        self.type = type_
        self.volume = volume
        self.comment = comment
        self.time = time
        self.sl = 0.0
        self.tp = 0.0
        self.price_open = 0.0


class _Deal:
    def __init__(self, magic):
        self.magic = magic
        self.entry = 0  # DEAL_ENTRY_IN


class _Mt5:
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_REMOVE = 2
    TRADE_ACTION_SLTP = 6
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
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    def __init__(self, *, positions=None, orders=None, deals=None, tick=None):
        self._positions = list(positions or [])
        self._orders = list(orders or [])
        self._deals = list(deals or [])
        self._tick = tick or _Tick()
        self.requests = []
        self._ticket = 5000

    def symbol_info(self, symbol):
        return _Sym()

    def symbol_info_tick(self, symbol):
        return self._tick

    def positions_get(self, symbol=None):
        return list(self._positions)

    def orders_get(self, symbol=None):
        return list(self._orders)

    def history_deals_get(self, since, until):
        return list(self._deals)

    def last_error(self):
        return (0, "ok")

    def order_send(self, request):
        self.requests.append(dict(request))
        action = request.get("action")
        if action == self.TRADE_ACTION_REMOVE:
            self._orders = [o for o in self._orders if o.ticket != request["order"]]
        elif action == self.TRADE_ACTION_DEAL and "position" in request:
            self._positions = [p for p in self._positions if p.ticket != request["position"]]
        self._ticket += 1
        return _Resp(self.TRADE_RETCODE_DONE, order=self._ticket)


class _Conn:
    def __init__(self, mt5):
        self.mt5 = mt5


def _executor(mt5):
    return Mt5Executor(_Conn(mt5), "XAUUSD", min_lot=0.01, lot_step=0.01, server_offset_hours=3)


# --- flatten_signal ---------------------------------------------------------

def test_flatten_signal_cancels_pendings_and_closes_only_its_magic():
    key = "VIC-2026-06-17#02"
    magic = signal_to_magic(key)
    other = signal_to_magic("VIC-2026-06-17#01")
    mt5 = _Mt5(
        orders=[_Order(1, magic), _Order(2, magic), _Order(9, other)],
        positions=[_Pos(10, magic, _Mt5.POSITION_TYPE_BUY),
                   _Pos(99, other, _Mt5.POSITION_TYPE_SELL)],
    )
    ex = _executor(mt5)

    log = ex.flatten_signal(key, reason="amend")

    assert log.cancelled == 2
    assert log.closed == 1
    # The signal's own footprint is gone...
    assert ex.find_orders(magic) == []
    assert ex.find_positions(magic) == []
    # ...while the neighbour signal's magic is untouched.
    assert [o.ticket for o in ex.find_orders(other)] == [9]
    assert [p.ticket for p in ex.find_positions(other)] == [99]


def test_flatten_signal_noop_when_no_footprint():
    mt5 = _Mt5()
    log = _executor(mt5).flatten_signal("VIC-2026-06-17#02", reason="revoke")
    assert log.cancelled == 0 and log.closed == 0
    assert mt5.requests == []


# --- place_signal history-gate bypass --------------------------------------

def _plan(signal):
    return NewSignalPlan(
        signal=signal,
        action="FOLLOW",
        rationale="",
        orders=[PlannedOrder(entry_index=0, side="BUY", entry_price=4490.0,
                             initial_sl=4480.0, lot=0.1, risk_dollars=10.0)],
        pending_expires_at=datetime(2999, 1, 1),
        final_target_label="TP3",
        final_target_price=4530.0,
        total_initial_risk_dollars=10.0,
        pending_activates_at=datetime(2000, 1, 1),
    )


def test_already_traded_magic_is_not_re_placed_without_an_amend():
    sig = parse_one_signal(
        "2. BUY XAUUSD 4495 - 4493 SL 4480 TP1 4505 TP2 4515 TP3 4530 11:15 AM",
        source_date="2026-06-18", source_offset=7)
    magic = signal_to_magic(sig.signal_key)
    mt5 = _Mt5(deals=[_Deal(magic)])  # closed deals in history -> finished magic
    ex = _executor(mt5)

    log = ex.place_signal(sig, _plan(sig))

    assert log.placed == 0
    assert not any(r.get("action") == _Mt5.TRADE_ACTION_PENDING for r in mt5.requests)


def test_amended_key_bypasses_the_already_traded_guard():
    sig = parse_one_signal(
        "2. BUY XAUUSD 4495 - 4493 SL 4480 TP1 4505 TP2 4515 TP3 4530 11:15 AM",
        source_date="2026-06-19", source_offset=7)
    magic = signal_to_magic(sig.signal_key)
    mt5 = _Mt5(deals=[_Deal(magic)])
    ex = _executor(mt5)
    # The override consumer marks the key after a deliberate flatten.
    ex._amended_force_replace_keys = {sig.signal_key}

    log = ex.place_signal(sig, _plan(sig))

    assert log.placed == 1
    placed = [r for r in mt5.requests if r.get("action") == _Mt5.TRADE_ACTION_PENDING]
    assert len(placed) == 1
    assert placed[0]["magic"] == magic


# --- _consume_signal_overrides ---------------------------------------------

class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _tracked_signal(line, date="2026-06-17", tag="VIC"):
    sig = parse_one_signal(line, source_date=date, source_offset=7)
    sig.tag = tag
    return sig


def _setup(tmp_path, mt5, *, tag="VIC"):
    overrides = tmp_path / "signal_overrides.jsonl"
    overrides.write_text("", encoding="utf-8")
    registry = SignalRegistry(tmp_path / "positions_victor.json")
    args = _Args(signal_overrides_file=str(overrides),
                 positions_json=str(registry.path),
                 strategy_tag=tag, apply_signal_edits=True)
    ex = _executor(mt5)
    return overrides, registry, args, ex


def _append(overrides, obj):
    import json
    with overrides.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, separators=(",", ":")) + "\n")


def test_first_run_anchors_at_eof_and_skips_backlog(tmp_path):
    mt5 = _Mt5(orders=[_Order(1, signal_to_magic("VIC-2026-06-17#02"))])
    overrides, registry, args, ex = _setup(tmp_path, mt5)
    # A pre-existing record that predates the consumer must NOT be replayed.
    _append(overrides, {"signal_key": "2026-06-17#02", "action": "amend"})

    revoked = _consume_signal_overrides(args, ex, registry, log=ExecutionLog())

    assert revoked == set()
    assert mt5.requests == []  # backlog skipped, nothing flattened
    # The offset sidecar now exists, anchored at end-of-file.
    assert (tmp_path / "signal_overrides.jsonl.positions_victor.offset").exists()


def test_amend_flattens_untracks_and_queues_replacement(tmp_path):
    key = "VIC-2026-06-17#02"
    magic = signal_to_magic(key)
    mt5 = _Mt5(orders=[_Order(1, magic)],
               positions=[_Pos(10, magic, _Mt5.POSITION_TYPE_BUY)])
    overrides, registry, args, ex = _setup(tmp_path, mt5)
    registry.add(_tracked_signal(
        "2. BUY XAUUSD 4321 - 4319 SL 4314 TP1 4329 TP2 4339 TP3 4359 11:15 AM"), 5000.0)

    # Anchor at EOF, then the provider edits the signal.
    _consume_signal_overrides(args, ex, registry, log=ExecutionLog())
    _append(overrides, {"signal_key": "2026-06-17#02", "action": "amend",
                        "new": {"side": "BUY", "sl": 4314}})

    log = ExecutionLog()
    revoked = _consume_signal_overrides(args, ex, registry, log=log)

    assert revoked == set()                       # amend is not a revoke
    assert log.cancelled == 1 and log.closed == 1  # flattened
    assert {e["signal_key"] for e in registry.load()} == set()  # untracked
    assert key in ex._amended_force_replace_keys   # queued for re-placement


def test_revoke_flattens_untracks_and_is_held_out_of_placement(tmp_path):
    key = "VIC-2026-06-17#02"
    magic = signal_to_magic(key)
    mt5 = _Mt5(orders=[_Order(1, magic)])
    overrides, registry, args, ex = _setup(tmp_path, mt5)
    registry.add(_tracked_signal(
        "2. BUY XAUUSD 4321 - 4319 SL 4314 TP1 4329 TP2 4339 TP3 4359 11:15 AM"), 5000.0)

    _consume_signal_overrides(args, ex, registry, log=ExecutionLog())
    _append(overrides, {"signal_key": "2026-06-17#02", "action": "revoke"})

    revoked = _consume_signal_overrides(args, ex, registry, log=ExecutionLog())

    assert revoked == {key}
    assert {e["signal_key"] for e in registry.load()} == set()
    assert getattr(ex, "_amended_force_replace_keys", set()) == set()  # no re-place on delete


def test_idempotent_second_pass_does_nothing(tmp_path):
    key = "VIC-2026-06-17#02"
    magic = signal_to_magic(key)
    mt5 = _Mt5(orders=[_Order(1, magic)])
    overrides, registry, args, ex = _setup(tmp_path, mt5)

    _consume_signal_overrides(args, ex, registry, log=ExecutionLog())
    _append(overrides, {"signal_key": "2026-06-17#02", "action": "revoke"})
    _consume_signal_overrides(args, ex, registry, log=ExecutionLog())
    n_after_first = len(mt5.requests)

    # No new records appended: a second pass is a no-op (offset already advanced).
    revoked = _consume_signal_overrides(args, ex, registry, log=ExecutionLog())
    assert revoked == set()
    assert len(mt5.requests) == n_after_first
