"""Decision engine.

`decide(...)` is the single entry point. Given a new signal, the chart up
to "now", current open positions, and current equity, it returns a
`Recommendation` describing:

  - the action plan for the new signal (FOLLOW with a concrete order
    placement plan -- possibly partial when backtest replay shows some
    entries have already played out; SKIP_EXPIRED when the pending
    window has already closed; or SKIP_INVALIDATED when backtest replay
    shows EVERY entry has already played out), and
  - the current state of every existing open position (stage, effective
    stop, floating P&L, time-exit countdown).

`render_report(...)` formats a Recommendation as human-readable text
suitable for console / Telegram.

The engine deliberately adds no overlay logic (no skip / switch / hedge /
take-profit-early on existing positions). Those decisions belong to the
strategy itself: SL, SP, TP, and time exit. Adding cross-signal rules
without re-validating against the backtest would risk degrading the
validated result. Such overlays can be layered on later if backtested.

SKIP_EXPIRED, SKIP_INVALIDATED, and the new partial-FOLLOW mode are NOT
cross-signal overlays -- they're per-signal divergence-correction guards.
None changes which signals the strategy would have filled in backtest:
  - SKIP_EXPIRED refuses placement after the 4-hour pending window so we
    don't send limits that would just be cancelled.
  - SKIP_INVALIDATED refuses placement only when EVERY entry in the
    backtest replay has reached a terminal status (SL, LOCK_TP1, TP*,
    TIME_EXIT, NO_FILL) -- so there's literally nothing left to do.
  - Partial FOLLOW handles the in-between case: when SOME entries have
    played out in replay but others are still PENDING or OPEN, only the
    placeable entries are sent to MT5. This matches the backtest path
    exactly -- terminal entries are entries the backtest also wouldn't
    have any further action on.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .adapters import ChartSource, PositionSource
from .config import CONTRACT_SIZE_OZ, DEFAULT_CONFIG, StrategyConfig
from .positions import (
    Entry, Position, TERMINAL, advance_bars, compute_lot, open_position,
)
from .signal import Signal, compute_entries


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
    """What to do about the new signal.

    `action` is one of:
      - "FOLLOW"           -- place the orders in `orders`. May be a strict
        subset of all entry slots when the backtest replay shows some
        entries have already played out (partial FOLLOW). Inspect
        `replay_position` (when set) to see which entry indices were
        filtered out and why.
      - "SKIP_EXPIRED"     -- pending window already closed; do nothing.
      - "SKIP_INVALIDATED" -- every entry has reached a terminal status
        in the backtest replay (no placeable entries remain). The orders
        would diverge from the backtest path if placed now.
        `replay_position` carries the replayed Position for rendering the
        per-entry rationale.
    """
    signal: Signal
    action: str
    rationale: str
    orders: list[PlannedOrder]
    pending_expires_at: datetime
    final_target_label: str
    final_target_price: float
    total_initial_risk_dollars: float
    # Replayed Position from activation_time to now. Set whenever the engine
    # ran the validity check (i.e. `chart` was provided to decide). None for
    # backwards compat with callers that didn't pass a chart. When the action
    # is SKIP_INVALIDATED or FOLLOW with a filtered orders list, render_report
    # uses this to produce the per-entry outcome breakdown shown to the user.
    replay_position: Optional[Position] = None


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
    # Wall-clock placement time (chart tz) if the caller recorded one.
    # When present, render_report shows "Executed: ... (X min late)" so the
    # user can see how their human lag deviates from the strategy's ideal
    # (instant-at-signal-time) execution assumption.
    executed_at: Optional[datetime] = None


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
    # The chart is passed in so the validity gate can replay the signal
    # from activation_time to now and detect already-played-out signals.
    plan = _build_new_signal_plan(
        signal, positions.equity(), config, contract_size,
        now=now, chart=chart,
    )

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
        now: Optional[datetime] = None,
        chart: Optional[ChartSource] = None,
) -> NewSignalPlan:
    """Build the order placement plan for one signal, with two pre-flight gates.

    Gate order:
      1. SKIP_EXPIRED       -- now >= pending_expires_at. Cheap, runs first.
      2. Replay-based filtering (chart must be provided):
           - All entries terminal in replay        -> SKIP_INVALIDATED
           - Some entries terminal, others not     -> FOLLOW (partial: only
                                                      the still-PENDING/OPEN
                                                      entries are placed)
           - All entries still PENDING / OPEN      -> FOLLOW (standard)
      3. FOLLOW (no replay performed; chart was None) -- legacy path.

    The replay-based filtering is what the user requested with the rule
    "always execute signals if the status of the entry is still OPEN and
    Pending in backtest results". The replay's per-entry status is the
    authoritative source for which entries should reach MT5.
    """
    lot, base_stop_distance = compute_lot(equity, signal, config, contract_size)
    orders: list[PlannedOrder] = []
    side = signal.side
    for idx, ep in enumerate(compute_entries(signal, config)):
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

    # ---- Gate 1: SKIP_EXPIRED ----
    # Pending window already closed by the time we're deciding. Don't
    # bother placing -- the orders would just be cancelled on the next
    # manage cycle. Cheap to check, runs before the replay.
    if now is not None and now >= expires:
        minutes_past = (now - expires).total_seconds() / 60.0
        return NewSignalPlan(
            signal=signal, action="SKIP_EXPIRED",
            rationale=(
                f"Pending window already closed {minutes_past:.0f} min ago "
                f"(expired {expires:%Y-%m-%d %H:%M} GMT+3, "
                f"now {now:%Y-%m-%d %H:%M} GMT+3). "
                f"No orders will be placed -- they would only be cancelled "
                f"immediately on the next manage cycle."
            ),
            orders=orders, pending_expires_at=expires,
            final_target_label=final_target, final_target_price=target_price,
            total_initial_risk_dollars=total_risk,
            replay_position=None,
        )

    # ---- Gate 2: replay-based per-entry filtering ----
    # Replay the signal from activation_time to now. Per-entry status
    # determines which orders make it to MT5:
    #   PENDING / OPEN      -> placeable (entry still in play in backtest)
    #   terminal {SL, LOCK_TP1, TP1, TP2, TP3, TIME_EXIT, NO_FILL}
    #                       -> filter out (entry already played out)
    #
    # Skipped when chart is unavailable (legacy callers) so the old all-or-
    # nothing behavior is preserved.
    replay_pos: Optional[Position] = None
    if chart is not None and now is not None:
        replay_pos = open_position(signal, equity, config, contract_size)
        # If activation is still in the future, no bars to advance through;
        # all entries stay PENDING -> all placeable -> standard FOLLOW.
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
            return NewSignalPlan(
                signal=signal, action="SKIP_INVALIDATED",
                rationale=(
                    "Backtest replay from signal time to now shows every "
                    "entry has already played out -- nothing to place. "
                    "See per-entry breakdown below."
                ),
                orders=orders, pending_expires_at=expires,
                final_target_label=final_target, final_target_price=target_price,
                total_initial_risk_dollars=total_risk,
                replay_position=replay_pos,
            )
        if len(placeable_indices) < len(orders):
            filtered = [o for o in orders if o.entry_index in placeable_indices]
            filtered_risk = sum(o.risk_dollars for o in filtered)
            skipped_count = len(orders) - len(filtered)
            placed_ids = ", ".join(f"#{o.entry_index}" for o in filtered)
            return NewSignalPlan(
                signal=signal, action="FOLLOW",
                rationale=(
                    f"Partial placement: {len(filtered)} of {len(orders)} "
                    f"entries placeable ({placed_ids}). The other "
                    f"{skipped_count} entr"
                    f"{'y has' if skipped_count == 1 else 'ies have'} "
                    f"already played out in the backtest replay; "
                    f"only entries whose replay status is still PENDING "
                    f"or OPEN are sent to MT5. See per-entry breakdown below."
                ),
                orders=filtered, pending_expires_at=expires,
                final_target_label=final_target, final_target_price=target_price,
                total_initial_risk_dollars=filtered_risk,
                replay_position=replay_pos,
            )
        # All entries placeable -- fall through to standard FOLLOW below.

    # ---- Gate 3: FOLLOW ----
    return NewSignalPlan(
        signal=signal, action="FOLLOW",
        rationale=(
            f"Strategy follows every signal. "
            f"{config.entry_count} limits @ {', '.join(f'{o.entry_price:g}' for o in orders)}, "
            f"effective SL distance ${base_stop_distance:.2f}, "
            f"final target {final_target} = {target_price:g}, "
            f"lock to TP1 after first TP1 touch, "
            f"max hold {config.max_hold_minutes} min from first fill."
        ),
        orders=orders, pending_expires_at=expires,
        final_target_label=final_target, final_target_price=target_price,
        total_initial_risk_dollars=total_risk,
        replay_position=replay_pos,
    )


def format_replay_outcome(replay: Position, indent: str = "    ") -> list[str]:
    """Render a per-entry outcome breakdown for a replayed signal.

    Used by render_report (SKIP_INVALIDATED case AND partial-FOLLOW case)
    and by the CLI auto/decide EXECUTION log. Returns just the per-entry
    lines plus a "Backtest realized so far" summary -- callers add their
    own context about what will/won't be placed, since that depends on
    the surrounding gate logic.

    Output looks like:
        #0 (4702.00): filled 11:22, SL at 12:18, pnl -$56.70
        #1 (4703.50): filled 11:31, SL at 12:18, pnl -$45.36
        #2 (4705.00): NO_FILL (pending expired)
        Backtest realized so far: -$102.06
    """
    lines: list[str] = []
    for e in replay.entries:
        if e.status == "PENDING":
            lines.append(f"{indent}#{e.entry_index} ({e.entry_price:g}): still PENDING")
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
        else:  # terminal exit (SL, LOCK_TP1, TP1/2/3, TIME_EXIT)
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


def _stage_label(position: Position, config: StrategyConfig) -> str:
    if not position.filled_entries():
        return "Pending -- no fills yet"
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
    """Return a 'on time' / 'X min late' / 'X min early' annotation."""
    delta_min = (executed_at - signal_time).total_seconds() / 60.0
    if delta_min >= 0.5:
        return f"({delta_min:.1f} min late)"
    if delta_min <= -0.5:
        return f"({-delta_min:.1f} min early)"
    return "(on time)"


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

    if rec.new_signal.action == "FOLLOW":
        # Order plan -- only meaningful when we'd actually place.
        lines.append("")
        lines.append("  Orders to place:")
        for o in rec.new_signal.orders:
            lines.append(
                f"    #{o.entry_index} {o.side} LIMIT {o.entry_price:g}   "
                f"SL {o.initial_sl:.2f}   lot {o.lot:.2f}   "
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
        # If partial, show the per-entry replay so the user can see what
        # was filtered out and why.
        rp = rec.new_signal.replay_position
        if rp is not None and len(rec.new_signal.orders) < len(rp.entries):
            lines.append("")
            lines.append("  Backtest replay outcome (full signal):")
            lines.extend(format_replay_outcome(rp, indent="    "))
    elif rec.new_signal.action == "SKIP_EXPIRED":
        lines.append(
            f"  Pending window closed at: "
            f"{_fmt_time(rec.new_signal.pending_expires_at)} GMT+3"
        )
    elif rec.new_signal.action == "SKIP_INVALIDATED":
        # Per-entry replay outcome breakdown. The replay_position is set
        # whenever SKIP_INVALIDATED fires, so this block is safe to assume.
        rp = rec.new_signal.replay_position
        if rp is not None:
            lines.append("")
            lines.append("  Backtest replay outcome:")
            lines.extend(format_replay_outcome(rp, indent="    "))

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
        # Show actual placement time when we recorded one (decide --execute
        # captures it; legacy registry entries without it just skip this line).
        if p.executed_at is not None:
            lines.append(
                f"    Executed: {_fmt_time(p.executed_at)} GMT+3 "
                f"{_fmt_lateness(p.executed_at, s.signal_time_chart)}"
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
                f"stop @ {es.effective_stop:g}" if es.effective_stop is not None else "--"
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
            lines.append(f"      - {n}")

    # Summary -----------------------------------------------------------
    lines.append("")
    lines.append("SUMMARY")
    lines.append("-" * 70)
    total_realized = sum(p.realized_pnl for p in rec.open_positions)
    total_floating = sum(p.floating_pnl for p in rec.open_positions)
    if rec.new_signal.action == "FOLLOW":
        new_signal_line = (
            f"  New signal:             {rec.new_signal.action}  "
            f"({len(rec.new_signal.orders)} orders, "
            f"max risk {_fmt_money(-rec.new_signal.total_initial_risk_dollars)})"
        )
    else:
        new_signal_line = (
            f"  New signal:             {rec.new_signal.action}  "
            f"(no orders placed)"
        )
    lines.append(new_signal_line)
    lines.append(
        f"  Existing positions:     {len(rec.open_positions)}  "
        f"realized {_fmt_money(total_realized)}  "
        f"floating {_fmt_money(total_floating)}"
    )
    lines.append(f"  Equity:                 ${rec.equity:,.2f}")
    lines.append("=" * 70)
    return "\n".join(lines)