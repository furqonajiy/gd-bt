"""Supply/demand zone signal generator (research strategy, XAUUSD).

Pivot-anchored demand/supply zones detected on a higher timeframe (H1) are
emitted as resting-limit signals in the canonical signal-file schema, so the
same generated file drives both the existing backtest and live auto.
Execution stays on the M1 engine; this module only decides WHERE the zones are
and what each pending order should be (entry band, stop beyond the far edge,
targets across the opposite zone).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from trading.xauusd import Bar


@dataclass(frozen=True)
class ZoneConfig:
    swing_len: int = 5
    atr_period: int = 14
    # A zone taller than max_zone_atr * ATR is a spike candle, not a base; reject.
    max_zone_atr: float = 2.0
    # 0 disables the thin-zone floor (broker_stops_level already guards the stop).
    min_zone_atr: float = 0.0
    sl_buffer: float = 0.40
    # ELEV8 enforces a 0.40 minimum distance on every SL/pending; an entry whose
    # stop cannot clear this is permanently unplaceable, so the zone is dropped.
    broker_stops_level: float = 0.40
    # The opposite zone (the take-profit) must be far enough to be worth it but
    # near enough to be reachable; both bounds are in ATR units at the entry zone.
    min_target_atr: float = 1.0
    max_target_atr: float = 12.0
    min_separation_bars: int = 6
    bar_minutes: int = 60
    price_digits: int = 2


@dataclass(frozen=True)
class Zone:
    kind: str  # "demand" | "supply"
    pivot_index: int
    pivot_time: datetime
    confirmed_time: datetime
    proximal: float  # near edge: first price touched on a return
    distal: float  # far edge: the stop sits just beyond this
    mid: float


@dataclass(frozen=True)
class ZoneSignal:
    # Fields consumed by format_generated_signals / the canonical schema.
    signal_time_chart: datetime
    side: str
    r1: float
    r2: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    # Zone provenance for forensics / later wiring.
    zone_kind: str
    proximal: float
    distal: float
    target_proximal: float
    reason: str


def _round(value: float, digits: int) -> float:
    return round(float(value), int(digits))


def _atr(bars: list[Bar], period: int) -> list[Optional[float]]:
    """Wilder ATR aligned to bars; None until `period` bars are available."""
    trs: list[float] = []
    prev_close: float | None = None
    for b in bars:
        if prev_close is None:
            tr = b.high - b.low
        else:
            tr = max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close))
        trs.append(tr)
        prev_close = b.close
    out: list[Optional[float]] = [None] * len(bars)
    if len(bars) >= period > 0:
        run = sum(trs[:period]) / period
        out[period - 1] = run
        for i in range(period, len(bars)):
            run = (run * (period - 1) + trs[i]) / period
            out[i] = run
    return out


def detect_zones(bars: Iterable[Bar], config: ZoneConfig = ZoneConfig()) -> list[Zone]:
    """Pivot-anchored demand (pivot low) / supply (pivot high) zones.

    A pivot at index i is only knowable after `swing_len` bars to its right
    close, so confirmed_time = close-time of bar[i + swing_len]. Nothing here
    references a bar beyond that, which is what keeps replay look-ahead-safe.
    """
    ordered = sorted(bars, key=lambda b: b.time)
    n = len(ordered)
    L = config.swing_len
    if n < 2 * L + 1 or L < 1:
        return []

    atr = _atr(ordered, config.atr_period)
    zones: list[Zone] = []
    last_idx: dict[str, Optional[int]] = {"demand": None, "supply": None}

    for i in range(L, n - L):
        window = ordered[i - L:i + L + 1]
        bar = ordered[i]
        is_low = all(bar.low <= b.low for b in window)
        is_high = all(bar.high >= b.high for b in window)

        if is_low:
            kind = "demand"
            distal = bar.low
            proximal = max(bar.open, bar.close)
        elif is_high:
            kind = "supply"
            distal = bar.high
            proximal = min(bar.open, bar.close)
        else:
            continue

        height = abs(proximal - distal)
        a = atr[i]
        if a is not None and a > 0:
            if config.max_zone_atr > 0 and height > config.max_zone_atr * a:
                continue
            if config.min_zone_atr > 0 and height < config.min_zone_atr * a:
                continue

        li = last_idx[kind]
        if li is not None and (i - li) < config.min_separation_bars:
            continue

        confirmed = ordered[i + L].time + timedelta(minutes=config.bar_minutes)
        zones.append(Zone(
            kind=kind,
            pivot_index=i,
            pivot_time=bar.time,
            confirmed_time=confirmed,
            proximal=proximal,
            distal=distal,
            mid=(proximal + distal) / 2.0,
        ))
        last_idx[kind] = i

    return zones


def _zone_broken_before(zone: Zone, bars: list[Bar], cutoff_time: datetime) -> bool:
    """True if price closed beyond the zone's far edge after it formed but before cutoff."""
    for b in bars:
        if b.time < zone.confirmed_time:
            continue
        if b.time >= cutoff_time:
            break
        if zone.kind == "supply" and b.close > zone.distal:
            return True
        if zone.kind == "demand" and b.close < zone.distal:
            return True
    return False


def _pick_target(
        zone: Zone,
        opposite: list[Zone],
        bars: list[Bar],
        config: ZoneConfig,
        atr_at_zone: float,
) -> Optional[Zone]:
    """Nearest opposite zone that is already confirmed, on the right side,
    unbroken, and within the ATR distance band, as of `zone.confirmed_time`."""
    best: tuple[float, Zone] | None = None
    for t in opposite:
        if t.confirmed_time > zone.confirmed_time:
            continue  # not yet knowable: would be look-ahead
        if zone.kind == "demand":
            if t.proximal <= zone.proximal:
                continue
            dist = t.proximal - zone.proximal
        else:
            if t.proximal >= zone.proximal:
                continue
            dist = zone.proximal - t.proximal
        if dist < config.min_target_atr * atr_at_zone:
            continue
        if dist > config.max_target_atr * atr_at_zone:
            continue
        if _zone_broken_before(t, bars, zone.confirmed_time):
            continue
        if best is None or dist < best[0]:
            best = (dist, t)
    return best[1] if best else None


def generate_zone_signals(
        bars: Iterable[Bar],
        config: ZoneConfig = ZoneConfig(),
) -> list[ZoneSignal]:
    """Resting-limit signals: BUY at demand / SELL at supply, stop beyond the
    far edge, TP laddered across the nearest opposite zone."""
    ordered = sorted(bars, key=lambda b: b.time)
    zones = detect_zones(ordered, config)
    atr = _atr(ordered, config.atr_period)
    demands = [z for z in zones if z.kind == "demand"]
    supplies = [z for z in zones if z.kind == "supply"]

    out: list[ZoneSignal] = []
    for z in zones:
        a = atr[z.pivot_index]
        if a is None or a <= 0:
            continue  # ATR drives the target distance gate

        side = "BUY" if z.kind == "demand" else "SELL"
        r1, r2 = z.proximal, z.distal  # entry band; r1 = first-touched edge

        # Stop sits sl_buffer beyond the far edge, but never closer to the fill
        # (r1) than the broker's 0.40 minimum, or the order is unplaceable.
        if z.kind == "demand":
            sl = min(z.distal - config.sl_buffer, r1 - config.broker_stops_level)
        else:
            sl = max(z.distal + config.sl_buffer, r1 + config.broker_stops_level)

        target = _pick_target(z, supplies if side == "BUY" else demands, ordered, config, a)
        if target is None:
            continue

        # Ladder across the opposite zone: near edge -> mid -> far edge.
        tp1, tp2, tp3 = target.proximal, target.mid, target.distal

        out.append(ZoneSignal(
            signal_time_chart=z.confirmed_time,
            side=side,
            r1=_round(r1, config.price_digits),
            r2=_round(r2, config.price_digits),
            sl=_round(sl, config.price_digits),
            tp1=_round(tp1, config.price_digits),
            tp2=_round(tp2, config.price_digits),
            tp3=_round(tp3, config.price_digits),
            zone_kind=z.kind,
            proximal=_round(z.proximal, config.price_digits),
            distal=_round(z.distal, config.price_digits),
            target_proximal=_round(target.proximal, config.price_digits),
            reason=(
                f"{z.kind} pivot {z.pivot_time:%Y-%m-%d %H:%M} -> "
                f"target {target.kind} {target.pivot_time:%Y-%m-%d %H:%M}"
            ),
        ))

    out.sort(key=lambda s: (s.signal_time_chart, s.side))
    return out