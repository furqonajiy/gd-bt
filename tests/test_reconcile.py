"""Tests for Mt5Executor.reconcile_with_mt5 under the DD40 command contract.

The DD40 command used for live/backtest parity is:
risk sizing, $1000 initial capital, 0.05575 risk, 3 range_to_sl entries,
entry_sl_gap=2, activation delay=3, pending expiry=630, max hold=90,
SL multiplier=1.61, TP3 final target, no TP2 lock, and $3 closed-lot bonus.
"""
from __future__ import annotations
import calendar
from dataclasses import replace
from datetime import datetime, timedelta

from trading.xauusd import DEFAULT_CONFIG, parse_one_signal
from trading.xauusd import Bar
from trading.xauusd import POINT_VALUE
from trading.xauusd import Mt5Executor, signal_to_magic
from trading.xauusd import advance_bars, open_position


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


# ---------------------------------------------------------------------------
# fakes (no real MT5 needed)
# ---------------------------------------------------------------------------

class _FakeMt5Position:
    def __init__(self, *, ticket, magic, type_, price_open, sl, tp, volume, time):
        self.ticket = ticket
        self.magic = magic
        self.type = type_
        self.price_open = price_open
        self.sl = sl
        self.tp = tp
        self.volume = volume
        self.time = time
        self.comment = ""


class _FakeMt5Module:
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    def __init__(self, positions=None):
        self._positions = list(positions or [])

    def positions_get(self, symbol=None):
        return list(self._positions)

    def orders_get(self, symbol=None):
        return []


class _FakeMt5Conn:
    def __init__(self, mt5):
        self.mt5 = mt5


class _FakeChart:
    def __init__(self, bars):
        self._bars = list(bars)

    def bars_between(self, start, end):
        return [b for b in self._bars if start <= b.time <= end]

    def latest(self, at_or_before=None):
        relevant = ([b for b in self._bars if b.time <= at_or_before]
                    if at_or_before else list(self._bars))
        return relevant[-1] if relevant else None

    def first_time(self):
        return self._bars[0].time if self._bars else None

    def last_time(self):
        return self._bars[-1].time if self._bars else None


def _bar(t, o, h, l, c, spread_points=20):
    return Bar(
        time=t, open=o, high=h, low=l, close=c,
        spread_points=spread_points,
        spread_price=spread_points * POINT_VALUE,
    )


def _mt5_epoch(chart_time, server_offset=3):
    """Chart-time (GMT+3) → MT5's broker-tz-as-UTC epoch."""
    broker_naive = chart_time - timedelta(hours=3 - server_offset)
    return calendar.timegm(broker_naive.timetuple())


def _make_executor(positions):
    executor = Mt5Executor(
        _FakeMt5Conn(_FakeMt5Module(positions=positions)),
        "XAUUSD", server_offset_hours=3,
    )

    def _safe_broker_epoch_to_chart_time(epoch: int) -> datetime:
        broker_naive = datetime(1970, 1, 1) + timedelta(seconds=int(epoch))
        return broker_naive + timedelta(hours=3 - executor.server_offset_hours)

    executor._broker_epoch_to_chart_time = _safe_broker_epoch_to_chart_time
    return executor


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_reconcile_patches_pending_entry_when_mt5_filled_in_same_minute():
    """14 May 2026 scenario: order placed at 07:00:03, MT5 fills at
    07:00:38 at 4668.44 (positive slippage from 4670 limit), price runs
    straight up to TP1 without re-touching entry. The bar replay misses
    the fill. Reconcile must patch entry #0 to OPEN at 4668.44 and
    re-advance so the subsequent TP1 touch transitions stage to 1.
    """
    signal = parse_one_signal(
        "1. BUY XAUUSD 4670 - 4668 SL 4663 TP1 4678 TP2 4688 TP3 4708 10:44 AM",
        source_date="2026-05-14", source_offset=7,
    )
    assert signal.signal_time_chart == datetime(2026, 5, 14, 6, 44)

    pos = open_position(signal, equity=1000.0, config=DD40_COMMAND_CONFIG)
    assert pos.entries[0].entry_price == 4670
    assert pos.entries[0].initial_sl == 4658.73
    assert pos.entries[0].status == "PENDING"

    # Engine's bar-replay sees no fill on entry #0 (low never reaches
    # 4670 - 0.20 = 4669.80), but TP1 IS touched and price holds above it.
    bars = [
        _bar(datetime(2026, 5, 14, 7, 1), 4672, 4676, 4670, 4675),
        _bar(datetime(2026, 5, 14, 7, 2), 4675, 4678.5, 4674, 4678),
        _bar(datetime(2026, 5, 14, 7, 3), 4678, 4680, 4678.1, 4679),
        _bar(datetime(2026, 5, 14, 7, 4), 4679, 4682, 4678.5, 4681),
    ]
    chart = _FakeChart(bars)
    now = datetime(2026, 5, 14, 7, 10)

    # Actual replay (from executed_at) misses the fill.
    executed_at = datetime(2026, 5, 14, 7, 0, 3)
    advance_bars(pos, chart.bars_between(executed_at, now), DD40_COMMAND_CONFIG)
    assert pos.entries[0].status == "PENDING"
    assert pos.first_fill_time is None
    assert pos.stage == 0

    # MT5 reality: entry #0 actually filled at 07:00:38 at 4668.44.
    fill_time_chart = datetime(2026, 5, 14, 7, 0, 38)
    mt5_pos = _FakeMt5Position(
        ticket=12345,
        magic=signal_to_magic(signal.signal_key),
        type_=0, price_open=4668.44, sl=4658.73, tp=4708.0,
        volume=0.16, time=_mt5_epoch(fill_time_chart),
    )
    executor = _make_executor([mt5_pos])

    log = executor.reconcile_with_mt5(pos, DD40_COMMAND_CONFIG, chart, now)

    assert pos.entries[0].status == "OPEN"
    assert pos.entries[0].fill_time == fill_time_chart
    assert pos.entries[0].entry_price == 4668.44
    assert pos.entries[0].lot == 0.16
    # initial_sl unchanged — the cheaper fill gives better R:R, not a wider stop.
    assert pos.entries[0].initial_sl == 4658.73

    # Stage transitioned to 1 because TP1 was touched on the 07:02 bar
    # during the re-advance.
    assert pos.stage == 1

    assert pos.first_fill_time == fill_time_chart
    assert pos.time_exit_deadline == (
            fill_time_chart + timedelta(minutes=DD40_COMMAND_CONFIG.max_hold_minutes)
    )

    assert any("Reconciled #0" in a for a in log.actions)


def test_reconcile_is_noop_when_engine_already_in_sync():
    """If the bar-replay already registered the fill, reconcile leaves
    the entry alone.
    """
    signal = parse_one_signal(
        "1. BUY XAUUSD 4670 - 4668 SL 4663 TP1 4678 TP2 4688 TP3 4708 10:44 AM",
        source_date="2026-05-14", source_offset=7,
    )
    pos = open_position(signal, equity=1000.0, config=DD40_COMMAND_CONFIG)

    # Low=4669 reaches 4670 - 0.20 = 4669.80, so entry #0 fills.
    bars = [
        _bar(datetime(2026, 5, 14, 7, 1), 4672, 4675, 4669, 4673),
        _bar(datetime(2026, 5, 14, 7, 2), 4673, 4676, 4672, 4674),
    ]
    chart = _FakeChart(bars)
    advance_bars(pos, bars, DD40_COMMAND_CONFIG)
    assert pos.entries[0].status == "OPEN"

    original_fill_time = pos.entries[0].fill_time
    original_price = pos.entries[0].entry_price

    mt5_pos = _FakeMt5Position(
        ticket=99999,
        magic=signal_to_magic(signal.signal_key),
        type_=0, price_open=4670.0, sl=pos.entries[0].initial_sl, tp=4708.0,
        volume=pos.entries[0].lot,
        time=_mt5_epoch(pos.entries[0].fill_time),
    )
    executor = _make_executor([mt5_pos])

    log = executor.reconcile_with_mt5(
        pos, DD40_COMMAND_CONFIG, chart, datetime(2026, 5, 14, 7, 10),
    )

    assert log.actions == []
    assert pos.entries[0].status == "OPEN"
    assert pos.entries[0].fill_time == original_fill_time
    assert pos.entries[0].entry_price == original_price


def test_reconcile_warns_when_mt5_has_more_positions_than_slots():
    """Defensive: if MT5 has more positions for a magic than the engine
    has slots (out-of-sync registry, manual placement), skip reconciliation
    rather than mis-mapping positions to slots.
    """
    signal = parse_one_signal(
        "1. BUY XAUUSD 4670 - 4668 SL 4663 TP1 4678 TP2 4688 TP3 4708 10:44 AM",
        source_date="2026-05-14", source_offset=7,
    )
    pos = open_position(signal, equity=1000.0, config=DD40_COMMAND_CONFIG)
    magic = signal_to_magic(signal.signal_key)

    mt5_positions = [
        _FakeMt5Position(
            ticket=10000 + i, magic=magic, type_=0,
            price_open=4670 - i * 0.5, sl=pos.entries[0].initial_sl, tp=4708.0,
            volume=0.1,
            time=_mt5_epoch(datetime(2026, 5, 14, 7, 0, i)),
        )
        for i in range(len(pos.entries) + 1)
    ]
    executor = _make_executor(mt5_positions)

    log = executor.reconcile_with_mt5(
        pos, DD40_COMMAND_CONFIG, _FakeChart([]), datetime(2026, 5, 14, 7, 10),
    )

    assert any("MT5 has" in w and "engine has only" in w for w in log.warnings)
    assert all(e.status == "PENDING" for e in pos.entries)
