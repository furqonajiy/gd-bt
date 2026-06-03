from __future__ import annotations

from datetime import datetime
from pathlib import Path

from xauusd_trading import (
    Bar,
    RejectionSignalConfig,
    format_generated_signals,
    generate_rejection_signals,
    parse_signals_file,
)


def _bar(t: str, open_: float, high: float, low: float, close: float, spread: int = 25) -> Bar:
    return Bar(
        time=datetime.strptime(t, "%Y-%m-%d %H:%M:%S"),
        open=open_,
        high=high,
        low=low,
        close=close,
        spread_points=spread,
        spread_price=spread * 0.01,
    )


def test_rejection_signal_uses_next_minute_to_avoid_same_candle_lookahead(tmp_path: Path):
    bars = [
        _bar("2026-06-03 09:00:00", 100.0, 101.0, 99.0, 100.2),
        _bar("2026-06-03 09:01:00", 100.2, 100.8, 99.1, 100.1),
        _bar("2026-06-03 09:02:00", 100.1, 100.7, 99.2, 100.0),
        _bar("2026-06-03 09:03:00", 100.0, 100.6, 97.5, 100.4),
    ]
    config = RejectionSignalConfig(
        lookback_bars=3,
        min_wick=1.0,
        min_bar_range=1.0,
        wick_body_ratio=1.0,
        cooldown_minutes=0,
        same_zone_cooldown_minutes=0,
        session_start_hour=None,
        session_end_hour=None,
    )

    signals = generate_rejection_signals(bars, config)

    assert len(signals) == 1
    signal = signals[0]
    assert signal.side == "BUY"
    assert signal.source_bar_time == datetime(2026, 6, 3, 9, 3)
    assert signal.signal_time_chart == datetime(2026, 6, 3, 9, 4)

    rendered = format_generated_signals(signals)
    path = tmp_path / "self_signals.txt"
    path.write_text(rendered, encoding="utf-8")
    parsed = parse_signals_file(path)

    assert len(parsed) == 1
    parsed_signal = parsed[0]
    assert parsed_signal.side == "BUY"
    assert parsed_signal.signal_time_chart == datetime(2026, 6, 3, 9, 4)
    assert parsed_signal.range_high == 100.4
    assert parsed_signal.range_low == 98.4
    assert parsed_signal.sl == 93.4
    assert parsed_signal.tp1 == 110.4
    assert parsed_signal.tp2 == 120.4
    assert parsed_signal.tp3 == 140.4


def test_cooldown_blocks_overtrading_same_chop():
    bars = [
        _bar("2026-06-03 09:00:00", 100.0, 101.0, 99.0, 100.2),
        _bar("2026-06-03 09:01:00", 100.0, 100.6, 97.5, 100.4),
        _bar("2026-06-03 09:02:00", 100.4, 100.5, 99.0, 99.8),
        _bar("2026-06-03 09:03:00", 99.8, 100.5, 97.4, 100.3),
    ]
    config = RejectionSignalConfig(
        lookback_bars=1,
        min_wick=1.0,
        min_bar_range=1.0,
        wick_body_ratio=1.0,
        cooldown_minutes=20,
        same_zone_cooldown_minutes=0,
        session_start_hour=None,
        session_end_hour=None,
    )

    signals = generate_rejection_signals(bars, config)

    assert len(signals) == 1
    assert signals[0].signal_time_chart == datetime(2026, 6, 3, 9, 2)


def test_spread_filter_blocks_expensive_bars():
    bars = [
        _bar("2026-06-03 09:00:00", 100.0, 101.0, 99.0, 100.2, spread=25),
        _bar("2026-06-03 09:01:00", 100.0, 100.6, 97.5, 100.4, spread=80),
    ]
    config = RejectionSignalConfig(
        lookback_bars=1,
        min_wick=1.0,
        min_bar_range=1.0,
        wick_body_ratio=1.0,
        max_spread_points=35,
        session_start_hour=None,
        session_end_hour=None,
    )

    signals = generate_rejection_signals(bars, config)

    assert signals == []
