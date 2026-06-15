from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from xauusd_trading import Bar, format_generated_signals, parse_signals_file
from xauusd_trading.strategy.zone_signals import (
    ZoneConfig,
    detect_zones,
    generate_zone_signals,
)

BASE = datetime(2024, 3, 1, 0, 0)


def _bar(idx: int, o: float, h: float, l: float, c: float, spread: int = 25) -> Bar:
    return Bar(
        time=BASE + timedelta(hours=idx),
        open=o, high=h, low=l, close=c,
        spread_points=spread, spread_price=spread * 0.01,
    )


# Supply pivot high at idx 2, demand pivot low at idx 7 (the demand confirms
# after the supply, so only the demand can see a confirmed target above it).
_BUY_SERIES = [
    _bar(0, 100, 102, 99, 101),
    _bar(1, 101, 104, 100, 103),
    _bar(2, 103, 120, 102, 118),
    _bar(3, 118, 116, 110, 112),
    _bar(4, 112, 114, 108, 110),
    _bar(5, 110, 112, 104, 106),
    _bar(6, 106, 108, 98, 100),
    _bar(7, 100, 101, 95, 96),
    _bar(8, 96, 100, 96, 99),
    _bar(9, 99, 103, 98, 102),
    _bar(10, 102, 105, 100, 104),
    _bar(11, 104, 106, 101, 103),
]

# Demand pivot low at idx 2, supply pivot high at idx 7 (mirror: only the
# supply can see a confirmed demand target below it -> a single SELL).
_SELL_SERIES = [
    _bar(0, 100, 101, 98, 99),
    _bar(1, 99, 100, 96, 97),
    _bar(2, 97, 98, 95, 96),
    _bar(3, 96, 102, 95.5, 101),
    _bar(4, 101, 106, 100, 105),
    _bar(5, 105, 110, 104, 109),
    _bar(6, 109, 118, 108, 116),
    _bar(7, 116, 120, 115, 118),
    _bar(8, 118, 117, 112, 114),
    _bar(9, 114, 116, 110, 112),
    _bar(10, 112, 114, 109, 111),
    _bar(11, 111, 113, 108, 110),
]

# swing_len 2 keeps the fixtures small; the ATR distance/size gates are disabled
# so the geometry assertions are independent of ATR scaling on tiny series.
_CFG = ZoneConfig(
    swing_len=2,
    atr_period=3,
    max_zone_atr=0.0,
    min_zone_atr=0.0,
    min_target_atr=0.0,
    max_target_atr=1000.0,
    min_separation_bars=1,
    sl_buffer=0.40,
)


def test_demand_zone_emits_buy_and_round_trips(tmp_path: Path):
    signals = generate_zone_signals(_BUY_SERIES, _CFG)
    assert len(signals) == 1
    s = signals[0]
    assert s.side == "BUY"
    assert s.r1 == 100.0 and s.r2 == 95.0       # proximal (top) then distal (bottom)
    assert s.sl == 94.60                          # distal - sl_buffer
    assert s.sl < s.r2                            # stop below the demand zone
    assert s.tp1 < s.tp2 < s.tp3                  # ladder ascends into supply above
    assert s.tp1 > s.r1

    rendered = format_generated_signals(signals)
    path = tmp_path / "zones.txt"
    path.write_text(rendered, encoding="utf-8")
    parsed = parse_signals_file(path)

    assert len(parsed) == 1
    p = parsed[0]
    assert p.side == "BUY"
    assert p.range_high == 100.0 and p.range_low == 95.0
    assert p.sl == 94.60
    assert p.tp1 == 103.0 and p.tp2 == 111.5 and p.tp3 == 120.0
    assert p.signal_time_chart == datetime(2024, 3, 1, 10, 0)


def test_signal_time_is_confirmation_not_pivot():
    signals = generate_zone_signals(_BUY_SERIES, _CFG)
    s = signals[0]
    pivot_time = BASE + timedelta(hours=7)            # demand pivot bar
    confirmed = BASE + timedelta(hours=9 + 1)          # pivot + swing_len bars, next close
    assert s.signal_time_chart == confirmed
    assert s.signal_time_chart > pivot_time            # never the pivot bar itself


def test_supply_zone_emits_sell_with_mirror_geometry(tmp_path: Path):
    signals = generate_zone_signals(_SELL_SERIES, _CFG)
    assert len(signals) == 1
    s = signals[0]
    assert s.side == "SELL"
    assert s.r1 == 116.0 and s.r2 == 120.0       # proximal (bottom) then distal (top)
    assert s.sl == 120.40                          # distal + sl_buffer
    assert s.sl > s.r2                             # stop above the supply zone
    assert s.tp1 > s.tp2 > s.tp3                   # ladder descends into demand below

    rendered = format_generated_signals(signals)
    path = tmp_path / "zones.txt"
    path.write_text(rendered, encoding="utf-8")
    parsed = parse_signals_file(path)
    p = parsed[0]
    assert p.side == "SELL"
    assert p.range_high == 120.0 and p.range_low == 116.0
    assert p.sl == 120.40
    assert p.tp1 == 97.0 and p.tp2 == 96.0 and p.tp3 == 95.0


def test_no_opposite_zone_skips_signal():
    # A descending staircase: demand pivots form but nothing confirmed sits above
    # them, so no BUY can be targeted.
    falling = [
        _bar(0, 120, 121, 118, 119),
        _bar(1, 119, 120, 116, 117),
        _bar(2, 117, 118, 110, 111),
        _bar(3, 111, 113, 108, 109),
        _bar(4, 109, 110, 105, 106),
        _bar(5, 106, 107, 100, 101),
        _bar(6, 101, 103, 99, 100),
        _bar(7, 100, 102, 98, 99),
        _bar(8, 99, 101, 97, 98),
    ]
    signals = generate_zone_signals(falling, _CFG)
    assert [s for s in signals if s.side == "BUY"] == []


def test_min_zone_atr_rejects_thin_zone():
    # idx 2 is a genuine pivot low with a 0.20 zone height while neighbouring
    # bars carry wide ranges (ATR ~1.5), so the thin-zone filter drops it.
    series = [
        _bar(0, 102, 103, 101, 101.5),
        _bar(1, 101.5, 102, 100.5, 101),
        _bar(2, 100.20, 100.40, 100.00, 100.10),
        _bar(3, 100.30, 101.50, 100.20, 101.20),
        _bar(4, 101.20, 102.50, 101.00, 102.00),
    ]
    off = ZoneConfig(swing_len=2, atr_period=3, max_zone_atr=0.0, min_zone_atr=0.0, min_separation_bars=1)
    on = ZoneConfig(swing_len=2, atr_period=3, max_zone_atr=0.0, min_zone_atr=1.0, min_separation_bars=1)
    assert len([z for z in detect_zones(series, off) if z.kind == "demand"]) == 1
    assert len([z for z in detect_zones(series, on) if z.kind == "demand"]) == 0