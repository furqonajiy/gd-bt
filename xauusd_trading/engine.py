"""Decision engine.

`decide(...)` is the single entry point. Given a new signal, the chart up
to "now", current open positions, and current equity, it returns a
`Recommendation` describing:

  - the action plan for the new signal (always FOLLOW with a concrete order
    placement plan, since the validated strategy follows every signal), and
  - the current state of every existing open position (stage, effective
    stop, floating P&L, time-exit countdown).

`render_report(...)` formats a Recommendation as human-readable text
suitable for console / Telegram.

The engine deliberately adds no overlay logic (no skip / switch / hedge /
take-profit-early on existing positions). Those decisions belong to the
strategy itself: SL, SP, TP, and time exit. Adding cross-signal rules
without re-validating against the backtest would risk degrading the
61% / ~8.7x result. Such overlays can be layered on later if backtested.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .adapters import ChartSource, PositionSource
from .config import CONTRACT_SIZE_OZ, DEFAULT_CONFIG, StrategyConfig
from .positions import (
    Entry, Position, advance_bars, compute_lot, open_position,
)
from .signal import Signal


# ---------------------------------------------------------------------------
# result data classes
# ---------------------------------------------------------------------------

@dataclass
class PlannedOrder:
    """One limit order to place when following a new signal."""
    entry_index: int
    side: str
    entry_price: float
    initial_sl: float
    lot: float
    risk_dollars: float


@dataclass
class NewSignalPlan:
    """What to do about the new signal."""
    signal: Signal
    action: str                          # "FOLLOW" (placeholder for future overlays)
    rationale: str
    orders: list[PlannedOrder]
    pending_expires_at: datetime
    final_target_label: str
    final_target_price: float
    total_initial_risk_dollars: float


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
    stage_label: str                     # "Pending" / "Stage 1 (initial SL)" / "Stage 2 (TP1 locked)"
    first_fill_time: Optional[datetime]
    time_exit_at: Optional[datetime]
    minutes_to_time_exit: Optional[float]
    entries: list[EntryStatus]
    realized_pnl: float
    floating_pnl: float
    action: str                          # HOLD / WATCH (no overlay-driven actions yet)
    notes: list[str] = field(default_factory=list)


@dataclass
class Recommendation:
    generated_at: datetime               # in chart timezone (GMT+3)
    equity: float
    new_signal: NewSignalPlan
    open_positions: list[PositionStatus]
    config: StrategyConfig


# ---------------------------------------------------------------------------
# the decision function
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
    (or the signal time, if the chart has no later bar yet). All times are
    chart timezone (GMT+3).
    """
    # Resolve "now" first, then mark-to-market against the latest bar that
    # is actually at or before "now".
    if now is None:
        last_bar = chart.latest()
        now = last_bar.time if last_bar is not None else signal.signal_time_chart
    else:
        last_bar = chart.latest(at_or_before=now)

    # --- 1. Bring every existing open position up to "now" ---------------
    open_positions = positions.open_positions()
    for pos in open_positions:
        start = pos.last_processed_time or pos.activation_time
        if start <= now:
            advance_window = chart.bars_between(start + timedelta(minutes=0), now)
            advance_bars(pos, advance_window, config, contract_size)

    # Keep only those that are still in flight after advancement.
    still_open = [p for p in open_positions if not p.is_terminal()]

    # --- 2. Build the plan for the new signal ---------------------------
    plan = _build_new_signal_plan(signal, positions.equity(), config, contract_size)

    # --- 3. Snapshot existing positions ---------------------------------
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
# helpers
# ---------------------------------------------------------------------------

def _build_new_signal_plan(
    signal: Signal, equity: float, config: StrategyConfig, contract_size: float,
) -> NewSignalPlan:
    lot, base_stop_distance = compute_lot(equity, signal, config, contract_size)
    orders: list[PlannedOrder] = []
    side = signal.side
    for idx, ep in enumerate(signal.entries[:config.entry_count]):
        sl = ep - base_stop_distance if side == "BUY" else ep + base_stop_distance
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

    rationale = (
        f"Strategy follows every signal. "
        f"3 limits @ {', '.join(f'{o.entry_price:g}' for o in orders)}, "
        f"effective SL distance ${base_stop_distance:.2f}, "
        f"final target {final_target} = {target_price:g}, "
        f"lock to TP1 after first TP1 touch, "
        f"max hold {config.max_hold_minutes} min from first fill."
    )
    return NewSignalPlan(
        signal=signal, action="FOLLOW", rationale=rationale, orders=orders,
        pending_expires_at=expires, final_target_label=final_target,
        final_target_price=target_price, total_initial_risk_dollars=total_risk,
    )


def _stage_label(position: Position, config: StrategyConfig) -> str:
    if not position.filled_entries():
        return "Pending — no fills yet"
    if config.lock_after_tp1 and position.stage >= 1:
        return "Stage 2 (TP1 locked, stops at TP1)"
    return "Stage 1 (initial SL active)"


def _action_for(position: Position, minutes_to_exit: Optional[float]) -> tuple[str, list[str]]:
    notes: list[str] = []
    if not position.filled_entries():
        return "HOLD", ["No fills; pending orders standing."]
    if minutes_to_exit is not None and minutes_to_exit <= 10:
        return "WATCH", [f"Time exit in {minutes_to_exit:.0f} min — may close at bar close."]
    return "HOLD", notes


def _snapshot_entry(
    pos: Position, e: Entry, last_bid: Optional[float],
    last_spread: float, config: StrategyConfig, contract_size: float,
) -> EntryStatus:
    side = pos.signal.side
    effective_stop = (
        pos.effective_stop_for(e, config.lock_after_tp1) if e.status == "OPEN" else None
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


def render_report(rec: Recommendation) -> str:
    sig = rec.new_signal.signal
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("XAUUSD TRADING DECISION")
    lines.append(
        f"Generated:  {_fmt_time(rec.generated_at)} GMT+3 (chart time)   "
        f"Equity: ${rec.equity:,.2f}"
    )
    lines.append("=" * 70)

    # New signal --------------------------------------------------------
    lines.append("")
    lines.append("NEW SIGNAL")
    lines.append("-" * 70)
    lines.append(
        f"  {sig.side} XAUUSD {sig.r1:g} - {sig.r2:g}   "
        f"SL {sig.sl:g}   TP1 {sig.tp1:g}  TP2 {sig.tp2:g}  TP3 {sig.tp3:g}"
    )
    lines.append(
        f"  Issued {sig.source_time_text} GMT{sig.source_tz_offset:+d} "
        f"= {_fmt_time(sig.signal_time_chart)} GMT+3"
    )
    lines.append("")
    lines.append(f"  Action: {rec.new_signal.action}")
    lines.append(f"  Reason: {rec.new_signal.rationale}")
    lines.append("")
    lines.append("  Orders to place:")
    for o in rec.new_signal.orders:
        lines.append(
            f"    #{o.entry_index} {o.side} LIMIT {o.entry_price:g}   "
            f"SL {o.initial_sl:.2f}   lot {o.lot:.4f}   "
            f"risk {_fmt_money(-o.risk_dollars)}"
        )
    lines.append(
        f"  Pending expires:  {_fmt_time(rec.new_signal.pending_expires_at)} GMT+3 "
        f"({rec.config.pending_expiry_minutes} min after activation)"
    )
    lines.append(
        f"  Final target:     {rec.new_signal.final_target_label} "
        f"@ {rec.new_signal.final_target_price:g} "
        f"(lock to TP1 after TP1 touch)"
    )
    lines.append(f"  Max hold:         {rec.config.max_hold_minutes} min from first fill")
    lines.append(
        f"  Total initial risk if all fill: "
        f"{_fmt_money(-rec.new_signal.total_initial_risk_dollars)} "
        f"({rec.config.risk_per_signal * 100:.1f}% of equity)"
    )

    # Open positions ----------------------------------------------------
    lines.append("")
    lines.append(f"OPEN POSITIONS  ({len(rec.open_positions)})")
    lines.append("-" * 70)
    if not rec.open_positions:
        lines.append("  None.")
    for p in rec.open_positions:
        s = p.signal
        lines.append(
            f"  Signal {s.signal_key}  {s.side} {s.r1:g}-{s.r2:g}  "
            f"issued {_fmt_time(s.signal_time_chart)}"
        )
        lines.append(
            f"    Stage:  {p.stage_label}    "
            f"First fill: {_fmt_time(p.first_fill_time)}    "
            f"Time exit: {_fmt_time(p.time_exit_at)}"
            + (f"  ({p.minutes_to_time_exit:.0f} min left)"
               if p.minutes_to_time_exit is not None else "")
        )
        for es in p.entries:
            stop_str = (
                f"stop @ {es.effective_stop:g}" if es.effective_stop is not None else "—"
            )
            if es.status == "OPEN":
                lines.append(
                    f"      #{es.entry_index} ({es.entry_price:g})  OPEN   {stop_str}   "
                    f"floating { _fmt_money(es.floating_pnl) }"
                )
            elif es.status == "PENDING":
                lines.append(f"      #{es.entry_index} ({es.entry_price:g})  PENDING")
            elif es.status == "NO_FILL":
                lines.append(f"      #{es.entry_index} ({es.entry_price:g})  NoFill")
            else:
                lines.append(
                    f"      #{es.entry_index} ({es.entry_price:g})  "
                    f"{es.status}@{es.exit_price:g}  realized {_fmt_money(es.realized_pnl)}"
                )
        lines.append(
            f"    Realized: {_fmt_money(p.realized_pnl)}   "
            f"Floating: {_fmt_money(p.floating_pnl)}   "
            f"Action: {p.action}"
        )
        for n in p.notes:
            lines.append(f"      • {n}")

    # Summary -----------------------------------------------------------
    lines.append("")
    lines.append("SUMMARY")
    lines.append("-" * 70)
    total_realized = sum(p.realized_pnl for p in rec.open_positions)
    total_floating = sum(p.floating_pnl for p in rec.open_positions)
    lines.append(
        f"  New signal:             {rec.new_signal.action}  "
        f"({len(rec.new_signal.orders)} orders, "
        f"max risk {_fmt_money(-rec.new_signal.total_initial_risk_dollars)})"
    )
    lines.append(
        f"  Existing positions:     {len(rec.open_positions)}  "
        f"realized {_fmt_money(total_realized)}  "
        f"floating {_fmt_money(total_floating)}"
    )
    lines.append(f"  Equity:                 ${rec.equity:,.2f}")
    lines.append("=" * 70)
    return "\n".join(lines)
