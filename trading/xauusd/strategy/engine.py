"""Decision engine.

`decide(signal, chart, positions, config)` returns a Recommendation
describing what to do with the new signal and the current state of every
existing open position. `render_report(rec)` formats it for the console.

Gate order applied to the new signal:
  1. SKIP_EXPIRED       — pending window already closed.
  2. Replay-based filtering — entries terminal in backtest replay are
     filtered out; remaining placeable entries go to MT5.
       - all terminal  -> SKIP_INVALIDATED
       - mix           -> FOLLOW (partial)
       - all placeable -> FOLLOW (standard)

The engine adds no cross-signal overlay logic. The gates above are
per-signal divergence corrections — they don't change which signals the
backtest would fill, only which entries reach MT5 when decide runs late.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from trading.xauusd import ChartSource, PositionSource
from trading.xauusd import CONTRACT_SIZE_OZ, DEFAULT_CONFIG, StrategyConfig
from trading.xauusd import (
    Entry, Position, advance_bars, compute_lot, entry_stop_levels, open_position,
)
from trading.xauusd import Signal, compute_entries


# ---------------------------------------------------------------------------
# result data classes
# ---------------------------------------------------------------------------

@dataclass
class PlannedOrder:
    """One limit/order slot to place when following a new signal."""
    entry_index: int
    side: str
    entry_price: float
    initial_sl: float
    lot: float
    risk_dollars: float


@dataclass
class NewSignalPlan:
    """Plan for the new signal.

    action: FOLLOW | SKIP_EXPIRED | SKIP_INVALIDATED
    orders: the placeable entries (may be a strict subset on partial FOLLOW)
    pending_activates_at: wall-clock/chart-time activation gate for live placement
    pending_expires_at: pending-order expiry gate for backtest/live parity
    replay_position: backtest replay from activation_time to now; set whenever
        the chart was provided. render_report uses it for the per-entry
        breakdown on partial FOLLOW and SKIP_INVALIDATED.
    trailing_open_distance/trailing_close_distance: copied from StrategyConfig so
        the live executor can reproduce the same virtual trailing behavior as
        shared replay.
    """
    signal: Signal
    action: str
    rationale: str
    orders: list[PlannedOrder]
    pending_expires_at: datetime
    final_target_label: str
    final_target_price: float
    total_initial_risk_dollars: float
    replay_position: Optional[Position] = None
    pending_activates_at: Optional[datetime] = None
    trailing_open_distance: float = 0.0
    trailing_close_distance: float = 0.0


@dataclass
class EntryStatus:
    """Snapshot of one entry slot in an existing position."""
    entry_index: int
    entry_price: float
    status: str
    effective_stop: Optional[float]
    fill_time: Optional[datetime]
    exit_time: Optional[datetime]
    exit_price: Optional[float]
    realized_pnl: Optional[float]
    floating_pnl: Optional[float]


@dataclass
class PositionStatus:
    """Snapshot of one in-flight Position."""
    signal: Signal
    stage: int
    stage_label: str
    first_fill_time: Optional[datetime]
    time_exit_at: Optional[datetime]
    minutes_to_time_exit: Optional[float]
    entries: list[EntryStatus]
    realized_pnl: float
    floating_pnl: float
    action: str                          # HOLD | WATCH
    notes: list[str] = field(default_factory=list)
    executed_at: Optional[datetime] = None   # wall-clock placement; renders "X min late"


@dataclass
class Recommendation:
    generated_at: datetime               # chart timezone (GMT+3)
    equity: float
    new_signal: NewSignalPlan
    open_positions: list[PositionStatus]
    config: StrategyConfig


def _plan(
        signal: Signal,
        action: str,
        rationale: str,
        orders: list[PlannedOrder],
        expires: datetime,
        final_target: str,
        target_price: float,
        total_risk: float,
        config: StrategyConfig,
        *,
        replay_position: Optional[Position] = None,
        activation: Optional[datetime] = None,
) -> NewSignalPlan:
    return NewSignalPlan(
        signal=signal,
        action=action,
        rationale=rationale,
        orders=orders,
        pending_expires_at=expires,
        final_target_label=final_target,
        final_target_price=target_price,
        total_initial_risk_dollars=total_risk,
        replay_position=replay_position,
        pending_activates_at=activation,
        trailing_open_distance=float(getattr(config, "trailing_open_distance", 0.0) or 0.0),
        trailing_close_distance=float(getattr(config, "trailing_close_distance", 0.0) or 0.0),
    )


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------

def decide(
        signal: Signal,
        chart: ChartSource,
        positions: PositionSource,
        config: StrategyConfig = DEFAULT_CONFIG,
        *,
        now: Optional[datetime] = None,
        contract_size: float = CONTRACT_SIZE_OZ,
) -> Recommendation:
    """Produce a Recommendation for the given signal.

    `now` defaults to the timestamp of the latest bar in the chart source
    (or the signal time, if the chart has no later bar yet). All times
    are chart timezone (GMT+3).
    """
    if now is None:
        last_bar = chart.latest()
        now = last_bar.time if last_bar is not None else signal.signal_time_chart
    else:
        last_bar = chart.latest(at_or_before=now)

    # Advance every existing open position up to "now".
    open_positions = positions.open_positions()
    for pos in open_positions:
        start = pos.last_processed_time or pos.activation_time
        if start <= now:
            advance_window = chart.bars_between(start + timedelta(minutes=0), now)
            advance_bars(pos, advance_window, config, contract_size)
    still_open = [p for p in open_positions if not p.is_terminal()]

    plan = _build_new_signal_plan(
        signal, positions.equity(), config, contract_size,
        now=now, chart=chart,
    )

    last_bid = last_bar.close if last_bar is not None else None
    last_spread = last_bar.spread_price if last_bar is not None else 0.0
    statuses = [
        _snapshot_position(p, now, last_bid, last_spread, config, contract_size)
        for p in still_open
    ]

    return Recommendation(
        generated_at=now, equity=positions.equity(),
        new_signal=plan, open_positions=statuses, config=config,
    )


# ---------------------------------------------------------------------------
# plan builder
# ---------------------------------------------------------------------------

def _build_new_signal_plan(
        signal: Signal, equity: float, config: StrategyConfig, contract_size: float,
        now: Optional[datetime] = None,
        chart: Optional[ChartSource] = None,
) -> NewSignalPlan:
    lot, base_stop_distance = compute_lot(equity, signal, config, contract_size)
    orders: list[PlannedOrder] = []
    side = signal.side
    entry_prices = list(compute_entries(signal, config))
    # Shared-SL collapses every leg's stop to one level; otherwise per-entry.
    stops = entry_stop_levels(side, entry_prices, base_stop_distance, config)
    for idx, ep in enumerate(entry_prices):
        sl = stops[idx]
        risk_dollars = abs(ep - sl) * lot * contract_size
        orders.append(PlannedOrder(
            entry_index=idx, side=side, entry_price=ep,
            initial_sl=sl, lot=lot, risk_dollars=risk_dollars,
        ))
    final_target = config.final_target.upper()
    target_price = {"TP1": signal.tp1, "TP2": signal.tp2, "TP3": signal.tp3}[final_target]
    activation = signal.signal_time_chart + timedelta(minutes=config.activation_delay_minutes)
    expires = activation + timedelta(minutes=config.pending_expiry_minutes)
    total_risk = sum(o.risk_dollars for o in orders)

    # Gate 1: SKIP_EXPIRED
    if now is not None and now >= expires:
        minutes_past = (now - expires).total_seconds() / 60.0
        return _plan(
            signal,
            "SKIP_EXPIRED",
            (
                f"Pending window already closed {minutes_past:.0f} min ago "
                f"(expired {expires:%Y-%m-%d %H:%M} GMT+3, "
                f"now {now:%Y-%m-%d %H:%M} GMT+3). "
                f"No orders will be placed -- they would only be cancelled "
                f"immediately on the next manage cycle."
            ),
            orders,
            expires,
            final_target,
            target_price,
            total_risk,
            config,
            replay_position=None,
            activation=activation,
        )

    # Gate 2: per-entry replay filtering.
    replay_pos: Optional[Position] = None
    if chart is not None and now is not None:
        replay_pos = open_position(signal, equity, config, contract_size)
        if replay_pos.activation_time <= now:
            advance_bars(
                replay_pos,
                chart.bars_between(replay_pos.activation_time, now),
                config, contract_size,
            )
        placeable_indices = {
            e.entry_index for e in replay_pos.entries
            if e.status in ("PENDING", "OPEN")
        }
        if not placeable_indices:
            return _plan(
                signal,
                "SKIP_INVALIDATED",
                (
                    "Backtest replay from signal time to now shows every "
                    "entry has already played out -- nothing to place. "
                    "See per-entry breakdown below."
                ),
                orders,
                expires,
                final_target,
                target_price,
                total_risk,
                config,
                replay_position=replay_pos,
                activation=activation,
            )
        if len(placeable_indices) < len(orders):
            filtered = [o for o in orders if o.entry_index in placeable_indices]
            filtered_risk = sum(o.risk_dollars for o in filtered)
            skipped_count = len(orders) - len(filtered)
            placed_ids = ", ".join(f"#{o.entry_index}" for o in filtered)
            return _plan(
                signal,
                "FOLLOW",
                (
                    f"Partial placement: {len(filtered)} of {len(orders)} "
                    f"entries placeable ({placed_ids}). The other "
                    f"{skipped_count} entr"
                    f"{'y has' if skipped_count == 1 else 'ies have'} "
                    f"already played out in the backtest replay; "
                    f"only entries whose replay status is still PENDING "
                    f"or OPEN are sent to MT5. See per-entry breakdown below."
                ),
                filtered,
                expires,
                final_target,
                target_price,
                filtered_risk,
                config,
                replay_position=replay_pos,
                activation=activation,
            )

    lock_text = "lock to TP1/TP2" if config.lock_after_tp2 else "lock to TP1"
    trail_bits = []
    if getattr(config, "trailing_open_distance", 0.0) > 0:
        trail_bits.append(f"trailing-open ${config.trailing_open_distance:g}")
    if getattr(config, "trailing_close_distance", 0.0) > 0:
        trail_bits.append(f"trailing-close ${config.trailing_close_distance:g}")
    trail_text = (", " + ", ".join(trail_bits)) if trail_bits else ""
    return _plan(
        signal,
        "FOLLOW",
        (
            f"Strategy follows every signal. "
            f"{config.entry_count} limits @ {', '.join(f'{o.entry_price:g}' for o in orders)}, "
            f"effective SL distance ${base_stop_distance:.2f}, "
            f"final target {final_target} = {target_price:g}, "
            f"{lock_text} after target touches, "
            f"max hold {config.max_hold_minutes} min from first fill{trail_text}."
        ),
        orders,
        expires,
        final_target,
        target_price,
        total_risk,
        config,
        replay_position=replay_pos,
        activation=activation,
    )


def format_replay_outcome(replay: Position, indent: str = "    ") -> list[str]:
    """Render a per-entry outcome breakdown for a replayed signal."""
    lines: list[str] = []
    for e in replay.entries:
        if e.status == "PENDING":
            suffix = ""
            if e.trailing_open_extreme is not None:
                suffix = f", trailing extreme {e.trailing_open_extreme:g}"
            lines.append(f"{indent}#{e.entry_index} ({e.entry_price:g}): still PENDING{suffix}")
        elif e.status == "OPEN":
            fill_str = f"{e.fill_time:%H:%M}" if e.fill_time is not None else "?"
            lines.append(
                f"{indent}#{e.entry_index} ({e.entry_price:g}): filled "
                f"{fill_str}, currently OPEN in backtest"
            )
        elif e.status == "NO_FILL":
            lines.append(
                f"{indent}#{e.entry_index} ({e.entry_price:g}): NO_FILL "
                f"(pending expired)"
            )
        else:
            fill_str = f"{e.fill_time:%H:%M}" if e.fill_time is not None else "?"
            exit_str = f"{e.exit_time:%H:%M}" if e.exit_time is not None else "?"
            pnl_str = f"${e.pnl:+.2f}" if e.pnl is not None else "-"
            lines.append(
                f"{indent}#{e.entry_index} ({e.entry_price:g}): filled "
                f"{fill_str}, {e.status} at {exit_str}, pnl {pnl_str}"
            )
    realized = replay.realized_pnl()
    lines.append(f"{indent}Backtest realized so far: ${realized:+.2f}")
    return lines


# ---------------------------------------------------------------------------
# snapshot helpers
# ---------------------------------------------------------------------------

def _stage_label(position: Position, config: StrategyConfig) -> str:
    if not position.filled_entries():
        return "Pending -- no fills yet"
    if config.lock_after_tp2 and position.stage >= 2:
        return "Stage 3 (TP2 locked, stops at TP2)"
    if config.lock_after_tp1 and position.stage >= 1:
        return "Stage 2 (TP1 locked, stops at TP1)"
    return "Stage 1 (initial SL active)"


def _action_for(position: Position, minutes_to_exit: Optional[float]) -> tuple[str, list[str]]:
    notes: list[str] = []
    if not position.filled_entries():
        return "HOLD", ["No fills; pending orders standing."]
    if minutes_to_exit is not None and minutes_to_exit <= 10:
        return "WATCH", [f"Time exit in {minutes_to_exit:.0f} min -- may close at bar close."]
    return "HOLD", notes


def _snapshot_entry(
        pos: Position, e: Entry, last_bid: Optional[float],
        last_spread: float, config: StrategyConfig, contract_size: float,
) -> EntryStatus:
    side = pos.signal.side
    effective_stop = (
        pos.effective_stop_for(e, config)
        if e.status == "OPEN" else None
    )
    floating: Optional[float] = None
    if e.status == "OPEN" and last_bid is not None:
        # Mark to market: BUY exits at Bid, SELL exits at Ask = Bid + spread.
        mark_price = last_bid if side == "BUY" else last_bid + last_spread
        if side == "BUY":
            floating = (mark_price - e.entry_price) * e.lot * contract_size
        else:
            floating = (e.entry_price - mark_price) * e.lot * contract_size
    return EntryStatus(
        entry_index=e.entry_index, entry_price=e.entry_price, status=e.status,
        effective_stop=effective_stop, fill_time=e.fill_time, exit_time=e.exit_time,
        exit_price=e.exit_price, realized_pnl=e.pnl, floating_pnl=floating,
    )


def _snapshot_position(
        pos: Position, now: datetime, last_bid: Optional[float],
        last_spread: float, config: StrategyConfig, contract_size: float,
) -> PositionStatus:
    minutes_to_exit: Optional[float] = None
    if pos.time_exit_deadline is not None:
        delta = (pos.time_exit_deadline - now).total_seconds() / 60.0
        minutes_to_exit = max(0.0, delta)
    entry_statuses = [
        _snapshot_entry(pos, e, last_bid, last_spread, config, contract_size)
        for e in pos.entries
    ]
    floating = sum((es.floating_pnl or 0.0) for es in entry_statuses)
    realized = pos.realized_pnl()
    action, notes = _action_for(pos, minutes_to_exit)
    return PositionStatus(
        signal=pos.signal, stage=pos.stage, stage_label=_stage_label(pos, config),
        first_fill_time=pos.first_fill_time, time_exit_at=pos.time_exit_deadline,
        minutes_to_time_exit=minutes_to_exit, entries=entry_statuses,
        realized_pnl=realized, floating_pnl=floating, action=action, notes=notes,
        executed_at=pos.executed_at,
    )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _fmt_time(t: Optional[datetime]) -> str:
    return "-" if t is None else t.strftime("%Y-%m-%d %H:%M")


def _fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "-"
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):.2f}"


def _fmt_lateness(executed_at: datetime, signal_time: datetime) -> str:
    delta_min = (executed_at - signal_time).total_seconds() / 60.0
    if delta_min >= 0.5:
        return f"({delta_min:.1f} min late)"
    if delta_min <= -0.5:
        return f"({-delta_min:.1f} min early)"
    return "(on time)"


def render_report(rec: Recommendation) -> str:
    lines = []
    cfg = rec.config
    ns = rec.new_signal
    s = ns.signal

    lines.append("=" * 72)
    lines.append("XAUUSD SIGNAL DECISION")
    lines.append("=" * 72)
    lines.append(f"Generated at: {rec.generated_at:%Y-%m-%d %H:%M}  GMT+3")
    lines.append(f"Equity used:  ${rec.equity:,.2f}")
    lines.append("")

    lines.append(f"Signal {s.signal_key}: {s.side} XAUUSD {s.r1:g}-{s.r2:g}  "
                 f"SL={s.sl:g} TP1={s.tp1:g} TP2={s.tp2:g} TP3={s.tp3:g}")
    lines.append(f"Source time: {s.source_date} {s.source_time_text} GMT{s.source_tz_offset:+d}  "
                 f"=> chart {s.signal_time_chart:%Y-%m-%d %H:%M} GMT+3")
    if s.anomalies:
        lines.append("Signal warnings: " + "; ".join(s.anomalies))
    lines.append("")

    lines.append("Recommendation: " + ns.action)
    lines.append("Reason: " + ns.rationale)
    lines.append(f"Pending expires: {ns.pending_expires_at:%Y-%m-%d %H:%M} GMT+3")
    lines.append(f"Final target: {ns.final_target_label} @ {ns.final_target_price:g}")
    lines.append(f"Total initial risk: ${ns.total_initial_risk_dollars:,.2f}")
    if ns.trailing_open_distance > 0 or ns.trailing_close_distance > 0:
        lines.append(
            f"Trailing: open={ns.trailing_open_distance:g}, "
            f"close={ns.trailing_close_distance:g}"
        )
    lines.append("")

    if ns.orders:
        lines.append("Orders to place:")
        for o in ns.orders:
            order_word = "TRAILING STOP" if ns.trailing_open_distance > 0 else "LIMIT"
            lines.append(
                f"  #{o.entry_index} {o.side} {order_word} seed {o.entry_price:g}  "
                f"SL={o.initial_sl:g}  TP={ns.final_target_price:g}  "
                f"lot={o.lot:.2f}  risk=${o.risk_dollars:,.2f}"
            )
    else:
        lines.append("Orders to place: none")

    if ns.replay_position is not None and ns.action in {"SKIP_INVALIDATED", "FOLLOW"}:
        if ns.action == "SKIP_INVALIDATED" or len(ns.orders) < len(ns.replay_position.entries):
            lines.append("")
            lines.append("Replay outcome up to now:")
            lines.extend(format_replay_outcome(ns.replay_position, indent="  "))

    lines.append("")
    lines.append("Open-position status:")
    if not rec.open_positions:
        lines.append("  (none)")
    else:
        for p in rec.open_positions:
            lines.append(f"  {p.signal.signal_key} {p.signal.side}  stage={p.stage_label}  action={p.action}")
            if p.executed_at is not None:
                lines.append(
                    f"    Executed: {p.executed_at:%Y-%m-%d %H:%M:%S} GMT+3 "
                    f"{_fmt_lateness(p.executed_at, p.signal.signal_time_chart)}"
                )
            if p.time_exit_at:
                lines.append(f"    Time exit: {_fmt_time(p.time_exit_at)}  ({p.minutes_to_time_exit:.0f} min)")
            for e in p.entries:
                fs = _fmt_time(e.fill_time)
                xs = _fmt_time(e.exit_time)
                eff = "-" if e.effective_stop is None else f"{e.effective_stop:g}"
                lines.append(
                    f"    #{e.entry_index} {e.status:10s} entry={e.entry_price:g} "
                    f"stop={eff} fill={fs} exit={xs} "
                    f"realized={_fmt_money(e.realized_pnl)} "
                    f"floating={_fmt_money(e.floating_pnl)}"
                )
            if p.notes:
                for n in p.notes:
                    lines.append(f"    NOTE: {n}")

    return "\n".join(lines)
