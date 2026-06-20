"""Self-generated XAUUSD signal candidates.

V1 is deliberately conservative: it emits closed-candle rejection-zone signals
that can be fed into the existing parser/backtest/auto pipeline as normal
signal text. The signal timestamp is the minute AFTER the rejected candle, so
M1 backtest and Option-B live execution do not use the same candle for both
signal generation and trade execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from trading.xauusd import Bar
from trading.xauusd.core import chart_tz


@dataclass(frozen=True)
class RejectionSignalConfig:
    lookback_bars: int = 20
    min_wick: float = 1.0
    min_bar_range: float = 1.5
    wick_body_ratio: float = 1.2
    zone_buffer: float = 0.25
    zone_size: float = 1.0
    cooldown_minutes: int = 20
    same_zone_cooldown_minutes: int = 120
    max_spread_points: int | None = 35
    session_start_hour: int | None = 7
    session_end_hour: int | None = 22
    entry_range_width: float = 2.0
    sl_distance: float = 5.0
    tp1_distance: float = 10.0
    tp2_distance: float = 20.0
    tp3_distance: float = 40.0
    price_digits: int = 2


@dataclass(frozen=True)
class GeneratedSignal:
    signal_time_chart: datetime
    side: str
    r1: float
    r2: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    source_bar_time: datetime
    entry_anchor: float
    recent_level: float
    wick_size: float
    body_size: float
    bar_range: float
    spread_points: int
    reason: str


def _validate_config(config: RejectionSignalConfig) -> None:
    if config.lookback_bars < 1:
        raise ValueError("lookback_bars must be >= 1")
    if config.entry_range_width <= 0:
        raise ValueError("entry_range_width must be > 0")
    if config.sl_distance <= 0:
        raise ValueError("sl_distance must be > 0")
    if config.tp1_distance <= 0 or config.tp2_distance <= config.tp1_distance or config.tp3_distance <= config.tp2_distance:
        raise ValueError("TP distances must be positive and ordered TP1 < TP2 < TP3")
    if config.zone_size <= 0:
        raise ValueError("zone_size must be > 0")
    for name, hour in (
        ("session_start_hour", config.session_start_hour),
        ("session_end_hour", config.session_end_hour),
    ):
        if hour is not None and not (0 <= hour <= 24):
            raise ValueError(f"{name} must be between 0 and 24, or None")


def _round_price(value: float, digits: int) -> float:
    return round(float(value), int(digits))


def _in_session(dt: datetime, start_hour: int | None, end_hour: int | None) -> bool:
    if start_hour is None or end_hour is None:
        return True
    hour = dt.hour
    if start_hour == end_hour:
        return True
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def _zone_key(side: str, entry_anchor: float, zone_size: float) -> tuple[str, int]:
    return side, int(round(entry_anchor / zone_size))


def _make_signal(
    *,
    side: str,
    entry_anchor: float,
    signal_time: datetime,
    source_bar: Bar,
    recent_level: float,
    wick_size: float,
    body_size: float,
    bar_range: float,
    config: RejectionSignalConfig,
    reason: str,
) -> GeneratedSignal:
    entry = _round_price(entry_anchor, config.price_digits)
    width = config.entry_range_width

    if side == "BUY":
        r1 = entry
        r2 = _round_price(entry - width, config.price_digits)
        sl = _round_price(r2 - config.sl_distance, config.price_digits)
        tp1 = _round_price(entry + config.tp1_distance, config.price_digits)
        tp2 = _round_price(entry + config.tp2_distance, config.price_digits)
        tp3 = _round_price(entry + config.tp3_distance, config.price_digits)
    elif side == "SELL":
        r1 = entry
        r2 = _round_price(entry + width, config.price_digits)
        sl = _round_price(r2 + config.sl_distance, config.price_digits)
        tp1 = _round_price(entry - config.tp1_distance, config.price_digits)
        tp2 = _round_price(entry - config.tp2_distance, config.price_digits)
        tp3 = _round_price(entry - config.tp3_distance, config.price_digits)
    else:
        raise ValueError(f"Unsupported side: {side!r}")

    return GeneratedSignal(
        signal_time_chart=signal_time,
        side=side,
        r1=r1,
        r2=r2,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        source_bar_time=source_bar.time,
        entry_anchor=entry,
        recent_level=_round_price(recent_level, config.price_digits),
        wick_size=_round_price(wick_size, config.price_digits),
        body_size=_round_price(body_size, config.price_digits),
        bar_range=_round_price(bar_range, config.price_digits),
        spread_points=int(source_bar.spread_points),
        reason=reason,
    )


def generate_rejection_signals(
    bars: Iterable[Bar],
    config: RejectionSignalConfig = RejectionSignalConfig(),
) -> list[GeneratedSignal]:
    """Generate parser-compatible signal candidates from closed M1 bars.

    BUY: current candle rejects a recent low with a large lower wick.
    SELL: current candle rejects a recent high with a large upper wick.
    """
    _validate_config(config)
    ordered = sorted(list(bars), key=lambda b: b.time)
    if len(ordered) <= config.lookback_bars:
        return []

    out: list[GeneratedSignal] = []
    last_any_signal: datetime | None = None
    last_zone_signal: dict[tuple[str, int], datetime] = {}

    for i in range(config.lookback_bars, len(ordered)):
        bar = ordered[i]
        signal_time = bar.time + timedelta(minutes=1)

        if not _in_session(signal_time, config.session_start_hour, config.session_end_hour):
            continue
        if config.max_spread_points is not None and bar.spread_points > config.max_spread_points:
            continue

        previous = ordered[i - config.lookback_bars:i]
        recent_high = max(b.high for b in previous)
        recent_low = min(b.low for b in previous)

        bar_range = bar.high - bar.low
        if bar_range < config.min_bar_range:
            continue

        body = abs(bar.close - bar.open)
        safe_body = max(body, 0.01)
        upper_wick = bar.high - max(bar.open, bar.close)
        lower_wick = min(bar.open, bar.close) - bar.low

        candidates: list[tuple[float, str, float, float, str]] = []

        if (
            bar.low <= recent_low + config.zone_buffer
            and lower_wick >= config.min_wick
            and lower_wick / safe_body >= config.wick_body_ratio
            and bar.close > bar.open
        ):
            score = lower_wick / safe_body
            candidates.append((
                score,
                "BUY",
                lower_wick,
                recent_low,
                (
                    f"lower_wick={lower_wick:.2f}; body={body:.2f}; "
                    f"recent_low={recent_low:.2f}; closed_after_rejection"
                ),
            ))

        if (
            bar.high >= recent_high - config.zone_buffer
            and upper_wick >= config.min_wick
            and upper_wick / safe_body >= config.wick_body_ratio
            and bar.close < bar.open
        ):
            score = upper_wick / safe_body
            candidates.append((
                score,
                "SELL",
                upper_wick,
                recent_high,
                (
                    f"upper_wick={upper_wick:.2f}; body={body:.2f}; "
                    f"recent_high={recent_high:.2f}; closed_after_rejection"
                ),
            ))

        if not candidates:
            continue

        _, side, wick, recent_level, reason = max(candidates, key=lambda item: item[0])

        if last_any_signal is not None:
            minutes_since_last = (signal_time - last_any_signal).total_seconds() / 60.0
            if minutes_since_last < config.cooldown_minutes:
                continue

        entry_anchor = bar.close
        zone_key = _zone_key(side, entry_anchor, config.zone_size)
        last_same_zone = last_zone_signal.get(zone_key)
        if last_same_zone is not None:
            minutes_since_zone = (signal_time - last_same_zone).total_seconds() / 60.0
            if minutes_since_zone < config.same_zone_cooldown_minutes:
                continue

        signal = _make_signal(
            side=side,
            entry_anchor=entry_anchor,
            signal_time=signal_time,
            source_bar=bar,
            recent_level=recent_level,
            wick_size=wick,
            body_size=body,
            bar_range=bar_range,
            config=config,
            reason=reason,
        )
        out.append(signal)
        last_any_signal = signal_time
        last_zone_signal[zone_key] = signal_time

    return out


def _price_text(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def format_generated_signals(
    signals: Iterable[GeneratedSignal],
    *,
    source_tz_offset: int = 3,
    price_digits: int = 2,
) -> str:
    """Render generated signals in the existing human signal-file format.

    Signals are generated in CHART time (GMT+3, EET/EEST). The feed is DISPLAYED
    in ``source_tz_offset`` (header GMT+N + per-line clock), so each chart-local
    time is converted to the display tz via ``chart_tz.from_chart_tz`` -- which is
    DST-aware (EET/EEST), so the engine's GMT+N -> chart round-trip lands on the
    exact bar even in winter (+2). Group/sort by the DISPLAY datetime so a signal
    shifted across midnight lands in the right block. Offset 3 in summer is a
    no-op, keeping the output byte-identical to the legacy behavior.
    """
    converted = [
        (chart_tz.from_chart_tz(s.signal_time_chart, source_tz_offset), s)
        for s in signals
    ]
    ordered = sorted(converted, key=lambda ds: (ds[0], ds[1].side))
    if not ordered:
        return ""

    lines: list[str] = []
    current_date: str | None = None
    day_counter = 0
    tz_label = f"GMT+{source_tz_offset}" if source_tz_offset >= 0 else f"GMT{source_tz_offset}"

    for disp, signal in ordered:
        date_text = disp.date().isoformat()
        if date_text != current_date:
            if lines:
                lines.append("")
            lines.append(f"{date_text} {tz_label}")
            current_date = date_text
            day_counter = 0

        day_counter += 1
        time_text = disp.strftime("%I:%M %p")
        lines.append(
            f"{day_counter}. {signal.side} XAUUSD "
            f"{_price_text(signal.r1, price_digits)} - {_price_text(signal.r2, price_digits)} "
            f"SL {_price_text(signal.sl, price_digits)} "
            f"TP1 {_price_text(signal.tp1, price_digits)} "
            f"TP2 {_price_text(signal.tp2, price_digits)} "
            f"TP3 {_price_text(signal.tp3, price_digits)} "
            f"{time_text}"
        )

    return "\n".join(lines) + "\n"
