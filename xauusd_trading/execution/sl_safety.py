"""Broker stop-level safety for SLTP modifications.

Backtest assumes every stop move is accepted. Live MT5 can reject SLTP modifies
when the requested SL is too close to current Bid/Ask. This helper clamps every
requested SL to the nearest legal broker level before order_send and emits both
action-log and forensic evidence whenever it changes or skips a stop.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from xauusd_trading.core.config import POINT_VALUE


@dataclass(frozen=True)
class SafeStopRequest:
    request: dict
    requested_sl: float
    clamped_sl: float
    changed: bool
    stops_level_points: int
    freeze_level_points: int
    min_distance: float
    bid: float
    ask: float


def _digits_for(sym) -> int:
    return int(getattr(sym, "digits", 2) if sym is not None else 2)


def _level_points(sym, name: str) -> int:
    if sym is None:
        return 0
    return max(0, int(getattr(sym, name, 0) or 0))


def _freeze_level_points(sym) -> int:
    # MT5 Python exposes trade_freeze_level; some fakes/tests use freeze_level.
    return max(_level_points(sym, "trade_freeze_level"), _level_points(sym, "freeze_level"))


def _round_buy_sl(value: float, digits: int) -> float:
    factor = 10 ** digits
    return round(math.floor(value * factor + 1e-9) / factor, digits)


def _round_sell_sl(value: float, digits: int) -> float:
    factor = 10 ** digits
    return round(math.ceil(value * factor - 1e-9) / factor, digits)


def _emit_forensic(executor, *, signal_key: str, action_name: str, ticket: int,
                   requested_sl: float, clamped_sl: float | None,
                   stops_level_points: int, freeze_level_points: int,
                   min_distance: float, bid: float | None, ask: float | None,
                   result: str, reason: str = "") -> None:
    forensic = getattr(executor, "forensic", None)
    emit = getattr(forensic, "_emit", None)
    if callable(emit):
        emit(
            "sltp_clamp",
            signal_key=signal_key,
            action=action_name,
            ticket=int(ticket),
            requested_sl=float(requested_sl),
            clamped_sl=(float(clamped_sl) if clamped_sl is not None else None),
            stops_level_points=int(stops_level_points),
            freeze_level_points=int(freeze_level_points),
            min_distance=float(min_distance),
            bid=(float(bid) if bid is not None else None),
            ask=(float(ask) if ask is not None else None),
            result=result,
            reason=reason,
        )


def prepare_sltp_modify_request(executor, p, requested_sl: float, signal_key: str,
                                action_name: str, label: str,
                                log: Any) -> SafeStopRequest | None:
    """Return a broker-legal SLTP request, or None when it must be skipped."""
    mt5 = executor.mt5
    sym = getattr(executor, "_sym_info", None) or mt5.symbol_info(executor.symbol)
    digits = _digits_for(sym)
    stops_points = _level_points(sym, "trade_stops_level")
    freeze_points = _freeze_level_points(sym)
    min_points = max(stops_points, freeze_points)
    min_distance = min_points * POINT_VALUE

    tick = mt5.symbol_info_tick(executor.symbol)
    if tick is None or getattr(tick, "bid", 0) <= 0 or getattr(tick, "ask", 0) <= 0:
        reason = "no live bid/ask tick available for SLTP clamp"
        msg = f"  {label} SL-lock on #{p.ticket}: {reason}; requested SL {requested_sl:g} skipped"
        log.warnings.append(msg)
        _emit_forensic(
            executor,
            signal_key=signal_key,
            action_name=action_name,
            ticket=p.ticket,
            requested_sl=requested_sl,
            clamped_sl=None,
            stops_level_points=stops_points,
            freeze_level_points=freeze_points,
            min_distance=min_distance,
            bid=None,
            ask=None,
            result="skipped",
            reason=reason,
        )
        return None

    bid = float(tick.bid)
    ask = float(tick.ask)
    requested = float(requested_sl)

    if p.type == mt5.POSITION_TYPE_BUY:
        legal_max = bid - min_distance
        clamped = _round_buy_sl(min(requested, legal_max), digits)
        if clamped >= bid:
            reason = f"clamped BUY SL {clamped:g} is not below Bid {bid:g}"
            log.warnings.append(f"  {label} SL-lock on #{p.ticket}: {reason}; skipped")
            _emit_forensic(
                executor,
                signal_key=signal_key,
                action_name=action_name,
                ticket=p.ticket,
                requested_sl=requested,
                clamped_sl=clamped,
                stops_level_points=stops_points,
                freeze_level_points=freeze_points,
                min_distance=min_distance,
                bid=bid,
                ask=ask,
                result="skipped",
                reason=reason,
            )
            return None
    else:
        legal_min = ask + min_distance
        clamped = _round_sell_sl(max(requested, legal_min), digits)
        if clamped <= ask:
            reason = f"clamped SELL SL {clamped:g} is not above Ask {ask:g}"
            log.warnings.append(f"  {label} SL-lock on #{p.ticket}: {reason}; skipped")
            _emit_forensic(
                executor,
                signal_key=signal_key,
                action_name=action_name,
                ticket=p.ticket,
                requested_sl=requested,
                clamped_sl=clamped,
                stops_level_points=stops_points,
                freeze_level_points=freeze_points,
                min_distance=min_distance,
                bid=bid,
                ask=ask,
                result="skipped",
                reason=reason,
            )
            return None

    changed = abs(clamped - round(requested, digits)) > 10 ** (-digits)
    if changed:
        msg = (
            f"  Clamped {label} SL on #{p.ticket}: requested {requested:g} -> "
            f"{clamped:g} (Bid={bid:g} Ask={ask:g}, stops={stops_points} pts, "
            f"freeze={freeze_points} pts, min={min_distance:g})"
        )
        log.actions.append(msg)
        _emit_forensic(
            executor,
            signal_key=signal_key,
            action_name=action_name,
            ticket=p.ticket,
            requested_sl=requested,
            clamped_sl=clamped,
            stops_level_points=stops_points,
            freeze_level_points=freeze_points,
            min_distance=min_distance,
            bid=bid,
            ask=ask,
            result="clamped",
        )

    return SafeStopRequest(
        request={
            "action": mt5.TRADE_ACTION_SLTP,
            "position": p.ticket,
            "sl": clamped,
            "tp": p.tp,
        },
        requested_sl=requested,
        clamped_sl=clamped,
        changed=changed,
        stops_level_points=stops_points,
        freeze_level_points=freeze_points,
        min_distance=min_distance,
        bid=bid,
        ask=ask,
    )
