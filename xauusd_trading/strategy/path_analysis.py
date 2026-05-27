"""Signal path diagnostics for TP/SL strategy tuning.

This module intentionally answers a different question from the P&L backtest:
"what price path did each filled entry experience after fill?"  It counts
milestone sequences such as NEAR_TP1 -> SL, TP1 -> LOCK_TP1, TP1 -> TP2 -> TP3,
and mixed multi-entry signal outcomes.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from xauusd_trading import (
    CONTRACT_SIZE_OZ,
    DEFAULT_CONFIG,
    StrategyConfig,
    Signal,
    CsvChartSource,
    compute_entries,
    fill_trigger,
    initial_stop_for_entry,
    iter_bars,
    slice_bars,
    stop_trigger,
    target_trigger,
)


@dataclass
class EntryPath:
    """Diagnostic lifecycle for one entry slot."""

    entry_index: int
    entry_price: float
    initial_sl: float
    lot: float = 0.0
    status: str = "PENDING"
    fill_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    stop_at_exit: Optional[float] = None
    touched_tp1: bool = False
    touched_tp2: bool = False
    touched_tp3: bool = False
    near_tp1: bool = False
    near_tp1_time: Optional[datetime] = None
    max_favorable: float = 0.0
    max_adverse: float = 0.0
    events: list[str] = field(default_factory=list)
    armed_for_touch: bool = False

    def event_key(self) -> str:
        """Compact event sequence used for grouping."""
        meaningful = [
            e for e in self.events
            if not e.startswith("FILL@") and not e.startswith("NO_FILL@")
        ]
        if not meaningful:
            return self.status
        return ">".join(e.split("@", 1)[0] for e in meaningful)

    def milestone_key(self) -> str:
        """Highest favorable milestone reached before the terminal event."""
        if self.touched_tp3:
            return "TP3"
        if self.touched_tp2:
            return "TP2"
        if self.touched_tp1:
            return "TP1"
        if self.near_tp1:
            return "NEAR_TP1"
        if self.fill_time is not None:
            return "FILLED_NO_NEAR_TP1"
        return "NO_FILL"


def _pnl(side: str, entry: float, exit_price: float, lot: float, contract_size: float) -> float:
    if side == "BUY":
        return (exit_price - entry) * lot * contract_size
    return (entry - exit_price) * lot * contract_size


def _base_stop_distance(signal: Signal, entries: list[float], config: StrategyConfig) -> float:
    first = entries[0]
    raw = first - signal.sl if signal.side == "BUY" else signal.sl - first
    return raw * config.sl_multiplier


def _near_tp1_hit(side: str, high: float, low: float, tp1: float,
                  spread_price: float, near_tp1_dollars: float) -> bool:
    """True when the bar comes within N dollars of TP1 without necessarily touching TP1."""
    if near_tp1_dollars <= 0:
        return False
    if side == "BUY":
        return high >= tp1 - near_tp1_dollars
    # SELL target uses ask-side equivalent: bid low <= level - spread.
    return low <= tp1 - spread_price + near_tp1_dollars


def _favorable_move(side: str, entry: float, high: float, low: float, spread_price: float) -> float:
    """Best favorable excursion in exit-price dollars from entry for this bar."""
    if side == "BUY":
        return high - entry
    return entry - (low + spread_price)


def _adverse_move(side: str, entry: float, high: float, low: float, spread_price: float) -> float:
    """Worst adverse excursion in exit-price dollars from entry for this bar."""
    if side == "BUY":
        return entry - (low + spread_price)
    return high - entry


def _touch_arming_and_fill(signal: Signal, entry: EntryPath, bar) -> bool:
    """Replicate the strict-touch fill arming logic from core.positions."""
    side = signal.side
    sp = bar.spread_price

    if side == "BUY":
        opened_safe = (bar.open + sp) > entry.entry_price
        returned_safe = (bar.high + sp) > entry.entry_price
    else:
        opened_safe = bar.open < entry.entry_price
        returned_safe = bar.low < entry.entry_price

    if not entry.armed_for_touch:
        if opened_safe:
            entry.armed_for_touch = True
        elif returned_safe:
            entry.armed_for_touch = True
            return False
        else:
            return False

    return fill_trigger(side, bar.high, bar.low, entry.entry_price, sp)


def _current_stop(signal: Signal, entry: EntryPath, stage: int) -> tuple[float, str]:
    """Return stop level and terminal status if that stop is hit."""
    if stage >= 2:
        return signal.tp2, "LOCK_TP2"
    if stage >= 1:
        return signal.tp1, "LOCK_TP1"
    return entry.initial_sl, "SL"


def analyze_signal_path(
    signal: Signal,
    chart_df: pd.DataFrame,
    config: StrategyConfig = DEFAULT_CONFIG,
    *,
    near_tp1_dollars: float = 1.0,
    contract_size: float = CONTRACT_SIZE_OZ,
) -> dict:
    """Analyze TP/SL milestone sequence for one signal.

    This is spread-aware and uses the same strict-touch pending-fill rule as
    the execution engine. It adds diagnostic TP2 locking so that the path counts
    can answer: "after TP2, how often did price come back to TP2 vs continue to TP3?"
    """

    entries_prices = compute_entries(signal, config)
    base_stop_distance = _base_stop_distance(signal, entries_prices, config)
    lot = config.minimum_lot if config.minimum_lot > 0 else 0.0
    entries = [
        EntryPath(
            entry_index=i,
            entry_price=p,
            initial_sl=initial_stop_for_entry(signal.side, p, base_stop_distance),
            lot=lot,
        )
        for i, p in enumerate(entries_prices)
    ]

    activation_time = signal.signal_time_chart + timedelta(minutes=config.activation_delay_minutes)
    expiry_time = activation_time + timedelta(minutes=config.pending_expiry_minutes)
    chart_end = chart_df["time"].iloc[-1].to_pydatetime()
    replay_end = min(expiry_time + timedelta(minutes=config.max_hold_minutes + 5), chart_end)

    first_fill_time: Optional[datetime] = None
    time_exit_deadline: Optional[datetime] = None
    stage = 0

    for bar in iter_bars(slice_bars(chart_df, activation_time, replay_end)):
        if all(e.status not in {"PENDING", "OPEN"} for e in entries):
            break

        # 1) Fill pending entries during active TIF.
        if activation_time <= bar.time <= expiry_time:
            for e in entries:
                if e.status != "PENDING":
                    continue
                if _touch_arming_and_fill(signal, e, bar):
                    e.status = "OPEN"
                    e.fill_time = bar.time
                    e.events.append(f"FILL@{bar.time.isoformat(sep=' ')}")
                    if first_fill_time is None:
                        first_fill_time = bar.time
                        time_exit_deadline = bar.time + timedelta(minutes=config.max_hold_minutes)

        # 2) Expire unfilled entries.
        if bar.time > expiry_time:
            for e in entries:
                if e.status == "PENDING":
                    e.status = "NO_FILL"
                    e.events.append(f"NO_FILL@{bar.time.isoformat(sep=' ')}")

        open_entries = [e for e in entries if e.status == "OPEN"]
        if not open_entries:
            continue

        sp = bar.spread_price

        # 3) Check stage/target/stop for each open entry. Worst-case:
        # if the active stop and a target can both trigger in the same bar,
        # classify as stop first and do not give the target credit.
        for e in list(open_entries):
            e.max_favorable = max(
                e.max_favorable,
                _favorable_move(signal.side, e.entry_price, bar.high, bar.low, sp),
            )
            e.max_adverse = max(
                e.max_adverse,
                _adverse_move(signal.side, e.entry_price, bar.high, bar.low, sp),
            )

            stop_level, stop_status = _current_stop(signal, e, stage)
            stop_hit = stop_trigger(signal.side, bar.high, bar.low, stop_level, sp)
            tp1_hit = target_trigger(signal.side, bar.high, bar.low, signal.tp1, sp)
            tp2_hit = target_trigger(signal.side, bar.high, bar.low, signal.tp2, sp)
            tp3_hit = target_trigger(signal.side, bar.high, bar.low, signal.tp3, sp)
            any_target_hit = tp1_hit or tp2_hit or tp3_hit

            if (not e.touched_tp1 and not tp1_hit and
                    _near_tp1_hit(signal.side, bar.high, bar.low, signal.tp1, sp, near_tp1_dollars)):
                e.near_tp1 = True
                e.near_tp1_time = bar.time
                e.events.append(f"NEAR_TP1@{bar.time.isoformat(sep=' ')}")

            if stop_hit and any_target_hit:
                e.status = f"{stop_status}_SAME_BAR"
                e.exit_time = bar.time
                e.exit_price = stop_level
                e.stop_at_exit = stop_level
                e.events.append(f"{e.status}@{bar.time.isoformat(sep=' ')}")
                continue

            if stop_hit:
                e.status = stop_status
                e.exit_time = bar.time
                e.exit_price = stop_level
                e.stop_at_exit = stop_level
                e.events.append(f"{stop_status}@{bar.time.isoformat(sep=' ')}")
                continue

            if tp3_hit:
                if not e.touched_tp1:
                    e.touched_tp1 = True
                    e.events.append(f"TP1@{bar.time.isoformat(sep=' ')}")
                if not e.touched_tp2:
                    e.touched_tp2 = True
                    e.events.append(f"TP2@{bar.time.isoformat(sep=' ')}")
                e.touched_tp3 = True
                e.status = "TP3"
                e.exit_time = bar.time
                e.exit_price = signal.tp3
                e.events.append(f"TP3@{bar.time.isoformat(sep=' ')}")
                continue

            if tp2_hit:
                if not e.touched_tp1:
                    e.touched_tp1 = True
                    e.events.append(f"TP1@{bar.time.isoformat(sep=' ')}")
                if not e.touched_tp2:
                    e.touched_tp2 = True
                    e.events.append(f"TP2@{bar.time.isoformat(sep=' ')}")
                stage = max(stage, 2)
                continue

            if tp1_hit:
                if not e.touched_tp1:
                    e.touched_tp1 = True
                    e.events.append(f"TP1@{bar.time.isoformat(sep=' ')}")
                stage = max(stage, 1)

        # 4) Time exit.
        if time_exit_deadline is not None and bar.time >= time_exit_deadline:
            for e in entries:
                if e.status == "OPEN":
                    e.status = "TIME_EXIT"
                    e.exit_time = bar.time
                    e.exit_price = bar.close
                    e.events.append(f"TIME_EXIT@{bar.time.isoformat(sep=' ')}")

    for e in entries:
        if e.status == "OPEN":
            e.status = "OPEN"

    return {
        "signal": signal,
        "entries": entries,
        "activation_time": activation_time,
        "expiry_time": expiry_time,
        "first_fill_time": first_fill_time,
        "time_exit_deadline": time_exit_deadline,
    }


def _terminal_mix(statuses: Iterable[str]) -> str:
    statuses = list(statuses)
    if not statuses:
        return "NO_ENTRIES"
    filled = [s for s in statuses if s != "NO_FILL"]
    if not filled:
        return "ALL_NO_FILL"
    unique_filled = sorted(set(filled))
    if len(unique_filled) == 1:
        return f"ALL_{unique_filled[0]}"
    return "MIXED_" + "_".join(unique_filled)


def _signal_path_key(entries: list[EntryPath]) -> str:
    milestones = {e.milestone_key() for e in entries}
    if "TP3" in milestones:
        milestone = "HIT_TP3"
    elif "TP2" in milestones:
        milestone = "HIT_TP2_NOT_TP3"
    elif "TP1" in milestones:
        milestone = "HIT_TP1_NOT_TP2"
    elif "NEAR_TP1" in milestones:
        milestone = "NEAR_TP1_NOT_HIT"
    elif any(e.fill_time is not None for e in entries):
        milestone = "FILLED_NO_NEAR_TP1"
    else:
        milestone = "NO_FILL"

    near_to_sl = any(
        e.near_tp1 and not e.touched_tp1 and e.status in {"SL", "SL_SAME_BAR"}
        for e in entries
    )
    suffix = _terminal_mix(e.status for e in entries)
    if near_to_sl:
        return f"{milestone}->NEAR_TP1_THEN_SL->{suffix}"
    return f"{milestone}->{suffix}"


def run_path_analysis(
    signals: list[Signal],
    chart: CsvChartSource,
    config: StrategyConfig = DEFAULT_CONFIG,
    *,
    exclude_structural_anomalies: bool = False,
    near_tp1_dollars: float = 1.0,
    contract_size: float = CONTRACT_SIZE_OZ,
) -> dict:
    """Run signal path diagnostics for a full signal set."""

    chart_df = chart.dataframe
    chart_start = chart.first_time()
    chart_end = chart.last_time()

    signal_rows: list[dict] = []
    entry_rows: list[dict] = []
    excluded: list[dict] = []

    for sig in signals:
        if chart_start is None or sig.signal_time_chart < chart_start:
            excluded.append({"signal_key": sig.signal_key, "reason": "before chart start"})
            continue
        if chart_end is None or sig.signal_time_chart > chart_end:
            excluded.append({"signal_key": sig.signal_key, "reason": "after chart end"})
            continue
        if exclude_structural_anomalies and sig.structural_anomaly:
            excluded.append({"signal_key": sig.signal_key, "reason": "structural anomaly"})
            continue

        analysis = analyze_signal_path(
            sig, chart_df, config,
            near_tp1_dollars=near_tp1_dollars,
            contract_size=contract_size,
        )
        entries: list[EntryPath] = analysis["entries"]
        path_key = _signal_path_key(entries)
        statuses = [e.status for e in entries]
        filled_count = sum(1 for e in entries if e.fill_time is not None)
        tp1_count = sum(1 for e in entries if e.touched_tp1)
        tp2_count = sum(1 for e in entries if e.touched_tp2)
        tp3_count = sum(1 for e in entries if e.touched_tp3)
        sl_count = sum(1 for e in entries if e.status in {"SL", "SL_SAME_BAR"})
        lock_tp1_count = sum(1 for e in entries if e.status in {"LOCK_TP1", "LOCK_TP1_SAME_BAR"})
        lock_tp2_count = sum(1 for e in entries if e.status in {"LOCK_TP2", "LOCK_TP2_SAME_BAR"})
        near_then_sl = any(
            e.near_tp1 and not e.touched_tp1 and e.status in {"SL", "SL_SAME_BAR"}
            for e in entries
        )

        signal_rows.append({
            "global_id": sig.global_id,
            "signal_key": sig.signal_key,
            "signal_date": sig.source_date,
            "signal_time_source": sig.source_time_text,
            "signal_time_chart": sig.signal_time_chart,
            "side": sig.side,
            "range_low": sig.range_low,
            "range_high": sig.range_high,
            "SL": sig.sl,
            "TP1": sig.tp1,
            "TP2": sig.tp2,
            "TP3": sig.tp3,
            "activation_time": analysis["activation_time"],
            "expiry_time": analysis["expiry_time"],
            "filled_entries": filled_count,
            "tp1_entries": tp1_count,
            "tp2_entries": tp2_count,
            "tp3_entries": tp3_count,
            "sl_entries": sl_count,
            "lock_tp1_entries": lock_tp1_count,
            "lock_tp2_entries": lock_tp2_count,
            "no_fill_entries": sum(1 for s in statuses if s == "NO_FILL"),
            "near_tp1_entries": sum(1 for e in entries if e.near_tp1 and not e.touched_tp1),
            "near_tp1_then_sl": near_then_sl,
            "terminal_mix": _terminal_mix(statuses),
            "path_key": path_key,
            "entry_statuses": "|".join(statuses),
            "entry_paths": "|".join(e.event_key() for e in entries),
        })

        for e in entries:
            pnl = None
            if e.exit_price is not None:
                pnl = _pnl(sig.side, e.entry_price, e.exit_price, e.lot, contract_size)
            entry_rows.append({
                "global_id": sig.global_id,
                "signal_key": sig.signal_key,
                "signal_time_chart": sig.signal_time_chart,
                "side": sig.side,
                "entry_index": e.entry_index,
                "entry_price": e.entry_price,
                "initial_sl": e.initial_sl,
                "status": e.status,
                "fill_time": e.fill_time,
                "exit_time": e.exit_time,
                "exit_price": e.exit_price,
                "stop_at_exit": e.stop_at_exit,
                "pnl_at_min_lot": pnl,
                "touched_tp1": e.touched_tp1,
                "touched_tp2": e.touched_tp2,
                "touched_tp3": e.touched_tp3,
                "near_tp1": e.near_tp1 and not e.touched_tp1,
                "near_tp1_time": e.near_tp1_time,
                "max_favorable_dollars": e.max_favorable,
                "max_adverse_dollars": e.max_adverse,
                "milestone_key": e.milestone_key(),
                "event_key": e.event_key(),
                "events": " | ".join(e.events),
            })

    path_counter = Counter(r["path_key"] for r in signal_rows)
    entry_counter = Counter(r["event_key"] for r in entry_rows)
    status_counter = Counter(r["status"] for r in entry_rows)

    summary = {
        "signals_parsed": len(signals),
        "signals_included": len(signal_rows),
        "signals_excluded": len(excluded),
        "near_tp1_dollars": near_tp1_dollars,
        "signals_hit_tp1": sum(1 for r in signal_rows if r["tp1_entries"] > 0),
        "signals_hit_tp2": sum(1 for r in signal_rows if r["tp2_entries"] > 0),
        "signals_hit_tp3": sum(1 for r in signal_rows if r["tp3_entries"] > 0),
        "signals_hit_sl": sum(1 for r in signal_rows if r["sl_entries"] > 0),
        "signals_near_tp1_then_sl": sum(1 for r in signal_rows if r["near_tp1_then_sl"]),
        "entry_status_counts": dict(status_counter),
        "signal_path_counts": dict(path_counter.most_common()),
        "entry_path_counts": dict(entry_counter.most_common()),
    }

    return {
        "config": config,
        "summary": summary,
        "signals": signal_rows,
        "entries": entry_rows,
        "excluded": excluded,
    }


def write_path_analysis_outputs(result: dict, output_dir: Path) -> dict[str, Path]:
    """Write diagnostic CSV outputs and return their paths."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    paths = {
        "signals": output_dir / "signal_path_analysis.csv",
        "entries": output_dir / "entry_path_analysis.csv",
        "signal_path_counts": output_dir / "signal_path_counts.csv",
        "entry_path_counts": output_dir / "entry_path_counts.csv",
        "entry_status_counts": output_dir / "entry_status_counts.csv",
    }

    pd.DataFrame(result["signals"]).to_csv(paths["signals"], index=False)
    pd.DataFrame(result["entries"]).to_csv(paths["entries"], index=False)

    pd.DataFrame(
        [{"path_key": k, "count": v} for k, v in result["summary"]["signal_path_counts"].items()]
    ).to_csv(paths["signal_path_counts"], index=False)
    pd.DataFrame(
        [{"entry_path": k, "count": v} for k, v in result["summary"]["entry_path_counts"].items()]
    ).to_csv(paths["entry_path_counts"], index=False)
    pd.DataFrame(
        [{"status": k, "count": v} for k, v in result["summary"]["entry_status_counts"].items()]
    ).to_csv(paths["entry_status_counts"], index=False)

    return paths
