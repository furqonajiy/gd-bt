#!/usr/bin/env python3
"""
XAUUSD optimized signal backtester.

Optimized default strategy from the analysis:
- Initial capital: $1,000
- Risk: 5% of current equity per signal
- Direction: follow original signal direction
- Entries: 3 range entries
- Activation delay: 0 minutes
- Pending expiry: 20 minutes after activation
- Max hold: 90 minutes after first fill
- Fill logic: strict touch-only, no stale/marketable auto-fill
- Initial SL: 1.25 x original first-entry-to-signal-SL distance
- Final target: TP2
- Stop lock: after TP1 is touched, move remaining open entries to TP1
- Spread-aware bid/ask trigger logic

MT5 chart input format, tab-separated:
<DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>

Chart prices are Bid. SPREAD is in points, where 1 point = $0.01.
Ask = Bid + spread. Exit price is always the level itself; spread is only used
for trigger logic.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd


# =========================
# Optimized strategy defaults
# =========================
DEFAULT_INITIAL_CAPITAL = 1000.0
DEFAULT_RISK_PER_SIGNAL = 0.05
DEFAULT_ENTRIES = 3
DEFAULT_ACTIVATION_DELAY_MINUTES = 0
DEFAULT_PENDING_EXPIRY_MINUTES = 20
DEFAULT_MAX_HOLD_MINUTES = 90
DEFAULT_SL_MULTIPLIER = 1.25
DEFAULT_FINAL_TARGET = "TP2"
DEFAULT_LOCK_AFTER_TP1 = True
DEFAULT_CONTRACT_SIZE_OZ = 100.0  # 1.0 lot XAUUSD = 100 oz, so 0.01 lot = $1 per $1 move
DEFAULT_POINT_VALUE = 0.01       # 1 spread point = $0.01
CHART_TIMEZONE_OFFSET = 3        # chart is GMT+3


DATE_LINE_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\s+GMT\s*(?P<sign>[+-])\s*(?P<offset>\d+)$", re.I)
SIGNAL_RE = re.compile(
    r"^\s*(?P<id>\d+)\.\s*"
    r"(?P<side>BUY|SELL)\s+XAUUSD\s+"
    r"(?P<r1>\d+(?:\.\d+)?)\s*-\s*(?P<r2>\d+(?:\.\d+)?)\s+"
    r"SL\s+(?P<sl>\d+(?:\.\d+)?)\s+"
    r"TP1\s+(?P<tp1>\d+(?:\.\d+)?)\s+"
    r"TP2\s+(?P<tp2>\d+(?:\.\d+)?)\s+"
    r"TP3\s+(?P<tp3>\d+(?:\.\d+)?)\s+"
    r"(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s*$",
    re.I,
)


@dataclass
class Signal:
    global_id: int
    day_id: int
    source_date: str
    source_tz_offset: int
    source_time_text: str
    signal_time_source: datetime
    signal_time_chart: datetime
    side: str
    r1: float
    r2: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    entries: list[float]
    anomalies: list[str] = field(default_factory=list)
    structural_anomaly: bool = False


@dataclass
class EntryState:
    entry_index: int
    entry: float
    planned_initial_sl: float
    lot: float
    status: str = "PENDING"
    fill_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    stop_at_exit: Optional[float] = None
    armed_for_touch: bool = False


def parse_gmt_offset(sign: str, offset: str) -> int:
    value = int(offset)
    return value if sign == "+" else -value


def to_chart_timezone(dt: datetime, source_offset: int, chart_offset: int = CHART_TIMEZONE_OFFSET) -> datetime:
    """Convert naive datetime from source GMT offset to chart GMT offset."""
    return dt + timedelta(hours=chart_offset - source_offset)


def normalize_entries(side: str, r1: float, r2: float) -> list[float]:
    """
    Use optimized 3-entry range logic.

    BUY range H/L -> entries H, H-1, L.
    SELL range L/H -> entries L, L+1, H.
    """
    high = max(r1, r2)
    low = min(r1, r2)
    if side == "BUY":
        return [high, high - 1.0, low]
    return [low, low + 1.0, high]


def validate_signal(side: str, r1: float, r2: float, sl: float, tp1: float, tp2: float, tp3: float, entries: list[float]) -> tuple[list[str], bool]:
    anomalies: list[str] = []
    structural = False

    if not math.isclose(abs(r1 - r2), 2.0, abs_tol=1e-9):
        anomalies.append(f"Range width is {abs(r1 - r2):.2f}, expected 2.00")

    if side == "BUY":
        if sl >= min(entries):
            anomalies.append("BUY SL is not below all entries")
            structural = True
        if not (tp1 < tp2 < tp3):
            anomalies.append("BUY TP order is inconsistent")
            structural = True
        if tp1 <= max(entries):
            anomalies.append("BUY TP1 is not above all entries")
            structural = True
    else:
        if sl <= max(entries):
            anomalies.append("SELL SL is not above all entries")
            structural = True
        if not (tp1 > tp2 > tp3):
            anomalies.append("SELL TP order is inconsistent")
            structural = True
        if tp1 >= min(entries):
            anomalies.append("SELL TP1 is not below all entries")
            structural = True

    return anomalies, structural


def parse_signals(signal_path: Path) -> list[Signal]:
    signals: list[Signal] = []
    current_date: Optional[str] = None
    current_offset: Optional[int] = None
    global_id = 0

    for raw_line in signal_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        date_match = DATE_LINE_RE.match(line)
        if date_match:
            current_date = date_match.group("date")
            current_offset = parse_gmt_offset(date_match.group("sign"), date_match.group("offset"))
            continue

        signal_match = SIGNAL_RE.match(line)
        if not signal_match:
            continue
        if current_date is None or current_offset is None:
            raise ValueError(f"Signal line found before date header: {line}")

        side = signal_match.group("side").upper()
        day_id = int(signal_match.group("id"))
        r1 = float(signal_match.group("r1"))
        r2 = float(signal_match.group("r2"))
        sl = float(signal_match.group("sl"))
        tp1 = float(signal_match.group("tp1"))
        tp2 = float(signal_match.group("tp2"))
        tp3 = float(signal_match.group("tp3"))
        source_time_text = signal_match.group("time").upper().replace(" ", "")
        signal_time_source = datetime.strptime(f"{current_date} {source_time_text}", "%Y-%m-%d %I:%M%p")
        signal_time_chart = to_chart_timezone(signal_time_source, current_offset)

        entries = normalize_entries(side, r1, r2)
        anomalies, structural = validate_signal(side, r1, r2, sl, tp1, tp2, tp3, entries)

        global_id += 1
        signals.append(
            Signal(
                global_id=global_id,
                day_id=day_id,
                source_date=current_date,
                source_tz_offset=current_offset,
                source_time_text=signal_match.group("time"),
                signal_time_source=signal_time_source,
                signal_time_chart=signal_time_chart,
                side=side,
                r1=r1,
                r2=r2,
                sl=sl,
                tp1=tp1,
                tp2=tp2,
                tp3=tp3,
                entries=entries,
                anomalies=anomalies,
                structural_anomaly=structural,
            )
        )

    return sorted(signals, key=lambda s: (s.signal_time_chart, s.global_id))


def read_chart_files(chart_paths: Iterable[Path], point_value: float = DEFAULT_POINT_VALUE) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in chart_paths:
        df = pd.read_csv(path, sep="\t")
        df.columns = [c.strip("<>").upper() for c in df.columns]
        required = {"DATE", "TIME", "OPEN", "HIGH", "LOW", "CLOSE", "SPREAD"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"Chart file {path} is missing required columns: {sorted(missing)}")

        df["time"] = pd.to_datetime(df["DATE"].astype(str) + " " + df["TIME"].astype(str), format="%Y.%m.%d %H:%M:%S")
        for col in ["OPEN", "HIGH", "LOW", "CLOSE", "SPREAD"]:
            df[col.lower()] = pd.to_numeric(df[col], errors="coerce")
        df["spread_price"] = df["spread"] * point_value
        frames.append(df[["time", "open", "high", "low", "close", "spread", "spread_price"]])

    if not frames:
        raise ValueError("At least one chart file is required")

    chart = pd.concat(frames, ignore_index=True)
    chart = chart.dropna(subset=["time", "open", "high", "low", "close", "spread_price"])
    chart = chart.drop_duplicates(subset=["time"], keep="last")
    chart = chart.sort_values("time").reset_index(drop=True)
    return chart


def side_target_trigger(side: str, high: float, low: float, level: float, spread_price: float) -> bool:
    if side == "BUY":
        return high >= level
    return low <= level - spread_price


def side_stop_trigger(side: str, high: float, low: float, level: float, spread_price: float) -> bool:
    if side == "BUY":
        return low <= level
    return high >= level - spread_price


def side_fill_trigger(side: str, high: float, low: float, entry: float, spread_price: float) -> bool:
    if side == "BUY":
        return low <= entry - spread_price
    return high >= entry


def pnl_for_entry(side: str, entry: float, exit_price: float, lot: float, contract_size: float) -> float:
    if side == "BUY":
        return (exit_price - entry) * lot * contract_size
    return (entry - exit_price) * lot * contract_size


def initial_stop_for_entry(side: str, entry: float, base_stop_distance: float) -> float:
    if side == "BUY":
        return entry - base_stop_distance
    return entry + base_stop_distance


def close_entry(
    entry_state: EntryState,
    status: str,
    exit_time: datetime,
    exit_price: float,
    side: str,
    contract_size: float,
    stop_at_exit: Optional[float] = None,
) -> None:
    entry_state.status = status
    entry_state.exit_time = exit_time
    entry_state.exit_price = exit_price
    entry_state.stop_at_exit = stop_at_exit
    entry_state.pnl = pnl_for_entry(side, entry_state.entry, exit_price, entry_state.lot, contract_size)


def compute_lot_per_entry(
    equity: float,
    risk_per_signal: float,
    entries: list[float],
    side: str,
    base_stop_distance: float,
    contract_size: float,
    minimum_lot: float = 0.0,
    lot_step: float = 0.0,
) -> float:
    """
    Size one equal lot for all planned entries so that total initial SL risk
    across all planned entries equals risk_per_signal x current equity.

    Example: equity=$1,000, risk=5%, total planned stop risk=$50.
    If only one or two entries fill, the realized loss can be less than 5%.
    """
    risk_amount = equity * risk_per_signal
    total_price_risk = 0.0
    for entry in entries:
        stop = initial_stop_for_entry(side, entry, base_stop_distance)
        total_price_risk += abs(entry - stop)

    if total_price_risk <= 0:
        return 0.0

    lot = risk_amount / (total_price_risk * contract_size)

    if lot_step and lot_step > 0:
        # Round down to avoid exceeding the intended risk.
        lot = math.floor(lot / lot_step) * lot_step
    if minimum_lot and lot < minimum_lot:
        lot = 0.0
    return lot


def backtest_signal(
    signal: Signal,
    chart: pd.DataFrame,
    equity_before: float,
    risk_per_signal: float = DEFAULT_RISK_PER_SIGNAL,
    activation_delay_minutes: int = DEFAULT_ACTIVATION_DELAY_MINUTES,
    pending_expiry_minutes: int = DEFAULT_PENDING_EXPIRY_MINUTES,
    max_hold_minutes: int = DEFAULT_MAX_HOLD_MINUTES,
    sl_multiplier: float = DEFAULT_SL_MULTIPLIER,
    final_target: str = DEFAULT_FINAL_TARGET,
    lock_after_tp1: bool = DEFAULT_LOCK_AFTER_TP1,
    entry_count: int = DEFAULT_ENTRIES,
    contract_size: float = DEFAULT_CONTRACT_SIZE_OZ,
    minimum_lot: float = 0.0,
    lot_step: float = 0.0,
) -> tuple[dict, list[dict]]:
    entries = signal.entries[:entry_count]
    if not entries:
        raise ValueError("entry_count must be at least 1")

    first_entry = entries[0]
    if signal.side == "BUY":
        raw_stop_distance = first_entry - signal.sl
    else:
        raw_stop_distance = signal.sl - first_entry
    base_stop_distance = raw_stop_distance * sl_multiplier

    lot = compute_lot_per_entry(
        equity=equity_before,
        risk_per_signal=risk_per_signal,
        entries=entries,
        side=signal.side,
        base_stop_distance=base_stop_distance,
        contract_size=contract_size,
        minimum_lot=minimum_lot,
        lot_step=lot_step,
    )

    entry_states = [
        EntryState(
            entry_index=i,
            entry=entry,
            planned_initial_sl=initial_stop_for_entry(signal.side, entry, base_stop_distance),
            lot=lot,
        )
        for i, entry in enumerate(entries)
    ]

    target_level = {"TP1": signal.tp1, "TP2": signal.tp2, "TP3": signal.tp3}[final_target.upper()]
    activation_time = signal.signal_time_chart + timedelta(minutes=activation_delay_minutes)
    expiry_time = activation_time + timedelta(minutes=pending_expiry_minutes)

    relevant_start = activation_time
    relevant_end = min(chart["time"].iloc[-1].to_pydatetime(), expiry_time + timedelta(minutes=max_hold_minutes + 5))
    bars = chart[(chart["time"] >= relevant_start) & (chart["time"] <= relevant_end)]

    if bars.empty:
        signal_result = {
            "global_id": signal.global_id,
            "signal_key": f"{signal.source_date}#{signal.day_id:02d}",
            "status": "NO_CHART",
            "pnl": 0.0,
            "equity_before": equity_before,
            "equity_after": equity_before,
            "first_fill_time": None,
            "last_exit_time": None,
            "entries_summary": "No chart bars in activation window",
        }
        return signal_result, []

    stage = 0  # 0 = before TP1 lock; 1 = TP1 touched/locked
    first_fill_time: Optional[datetime] = None
    last_exit_time: Optional[datetime] = None
    time_exit_deadline: Optional[datetime] = None

    for row in bars.itertuples(index=False):
        bar_time: datetime = row.time.to_pydatetime() if hasattr(row.time, "to_pydatetime") else row.time
        high = float(row.high)
        low = float(row.low)
        close = float(row.close)
        spread_price = float(row.spread_price)

        # Fill pending entries during active pending window.
        # Strict touch-only: do not fill a BUY that is already marketable
        # at/after activation (ask already <= entry), and do not fill a SELL
        # that is already marketable (bid already >= entry). The order must first
        # be on the non-marketable side, then genuinely retouch the level.
        if activation_time <= bar_time <= expiry_time:
            for entry_state in entry_states:
                if entry_state.status != "PENDING":
                    continue

                if signal.side == "BUY":
                    open_on_safe_side = (float(row.open) + spread_price) > entry_state.entry
                    returned_to_safe_side = (high + spread_price) > entry_state.entry
                else:
                    open_on_safe_side = float(row.open) < entry_state.entry
                    returned_to_safe_side = low < entry_state.entry

                if not entry_state.armed_for_touch:
                    if open_on_safe_side:
                        entry_state.armed_for_touch = True
                    elif returned_to_safe_side:
                        # The bar moved back to the safe side, but OHLC does not
                        # tell us whether this happened before or after the touch.
                        # Arm for the next bar to avoid stale/marketable fills.
                        entry_state.armed_for_touch = True
                        continue
                    else:
                        continue

                if side_fill_trigger(signal.side, high, low, entry_state.entry, spread_price):
                    entry_state.status = "OPEN"
                    entry_state.fill_time = bar_time
                    if first_fill_time is None:
                        first_fill_time = bar_time
                        time_exit_deadline = first_fill_time + timedelta(minutes=max_hold_minutes)

        # Expire pending entries after the pending window.
        if bar_time > expiry_time:
            for entry_state in entry_states:
                if entry_state.status == "PENDING":
                    entry_state.status = "NO_FILL"

        # Evaluate stop/target for open entries. Worst-case same-bar handling:
        # if a stop and target/lock can both trigger in the same 1-minute bar, stop wins.
        open_entries = [e for e in entry_states if e.status == "OPEN"]
        if open_entries:
            tp1_triggered = side_target_trigger(signal.side, high, low, signal.tp1, spread_price)
            final_target_triggered = side_target_trigger(signal.side, high, low, target_level, spread_price)

            for entry_state in list(open_entries):
                current_stop = signal.tp1 if (lock_after_tp1 and stage >= 1) else entry_state.planned_initial_sl
                stop_triggered = side_stop_trigger(signal.side, high, low, current_stop, spread_price)

                if stop_triggered:
                    if lock_after_tp1 and stage >= 1 and math.isclose(current_stop, signal.tp1, abs_tol=1e-9):
                        status = "LOCK_TP1"
                    else:
                        status = "SL"
                    close_entry(
                        entry_state=entry_state,
                        status=status,
                        exit_time=bar_time,
                        exit_price=current_stop,
                        side=signal.side,
                        contract_size=contract_size,
                        stop_at_exit=current_stop,
                    )
                    last_exit_time = bar_time
                elif final_target_triggered:
                    close_entry(
                        entry_state=entry_state,
                        status=final_target.upper(),
                        exit_time=bar_time,
                        exit_price=target_level,
                        side=signal.side,
                        contract_size=contract_size,
                    )
                    last_exit_time = bar_time

            if lock_after_tp1 and stage == 0 and tp1_triggered:
                # Lock remaining open entries and any future late fills to TP1.
                stage = 1

        # Time exit: close all remaining filled entries at the close of the first bar
        # at/after max-hold deadline.
        if time_exit_deadline is not None and bar_time >= time_exit_deadline:
            for entry_state in entry_states:
                if entry_state.status == "OPEN":
                    close_entry(
                        entry_state=entry_state,
                        status="TIME_EXIT",
                        exit_time=bar_time,
                        exit_price=close,
                        side=signal.side,
                        contract_size=contract_size,
                    )
                    last_exit_time = bar_time

        # Stop early once no more pending/open positions can change.
        if all(e.status in {"NO_FILL", "SL", "LOCK_TP1", "TP1", "TP2", "TP3", "TIME_EXIT"} for e in entry_states):
            break

    # Mark remaining states at chart end.
    chart_end = chart["time"].iloc[-1].to_pydatetime()
    for entry_state in entry_states:
        if entry_state.status == "PENDING":
            entry_state.status = "NO_FILL"
        elif entry_state.status == "OPEN":
            # If data ended before max hold could close the trade, keep it open.
            entry_state.pnl = None

    filled_entries = [e for e in entry_states if e.fill_time is not None]
    closed_entries = [e for e in entry_states if e.pnl is not None]
    open_entries = [e for e in entry_states if e.status == "OPEN"]
    total_pnl = sum(e.pnl for e in closed_entries if e.pnl is not None)

    if open_entries:
        status = "OPEN"
        equity_after = equity_before
    elif not filled_entries:
        status = "NO_FILL"
        equity_after = equity_before
    elif total_pnl > 0:
        status = "WIN"
        equity_after = equity_before + total_pnl
    elif total_pnl < 0:
        status = "LOSS"
        equity_after = equity_before + total_pnl
    else:
        status = "BREAKEVEN"
        equity_after = equity_before

    entry_summaries = []
    entry_rows = []
    for entry_state in entry_states:
        if entry_state.status == "NO_FILL":
            summary = f"#{entry_state.entry_index}({entry_state.entry:g}):NoFill"
        elif entry_state.status == "OPEN":
            summary = f"#{entry_state.entry_index}({entry_state.entry:g}):OPEN"
        else:
            summary = f"#{entry_state.entry_index}({entry_state.entry:g}):{entry_state.status}@{entry_state.exit_price:g}"
        entry_summaries.append(summary)

        entry_rows.append(
            {
                "global_id": signal.global_id,
                "signal_key": f"{signal.source_date}#{signal.day_id:02d}",
                "signal_time_chart": signal.signal_time_chart,
                "side": signal.side,
                "entry_index": entry_state.entry_index,
                "entry": entry_state.entry,
                "planned_initial_sl": entry_state.planned_initial_sl,
                "lot": entry_state.lot,
                "status": entry_state.status,
                "fill_time": entry_state.fill_time,
                "exit_time": entry_state.exit_time,
                "exit_price": entry_state.exit_price,
                "pnl": entry_state.pnl,
            }
        )

    signal_result = {
        "global_id": signal.global_id,
        "signal_key": f"{signal.source_date}#{signal.day_id:02d}",
        "source_date": signal.source_date,
        "source_time": signal.source_time_text,
        "source_tz": f"GMT{signal.source_tz_offset:+d}",
        "signal_time_chart": signal.signal_time_chart,
        "side": signal.side,
        "range": f"{signal.r1:g} - {signal.r2:g}",
        "entries": ", ".join(f"{e:g}" for e in entries),
        "sl": signal.sl,
        "tp1": signal.tp1,
        "tp2": signal.tp2,
        "tp3": signal.tp3,
        "activation_time": activation_time,
        "expiry_time": expiry_time,
        "first_fill_time": first_fill_time,
        "last_exit_time": last_exit_time,
        "lot_per_entry": lot,
        "equity_before": equity_before,
        "pnl": total_pnl if not open_entries else None,
        "equity_after": equity_after,
        "status": status,
        "entries_summary": " | ".join(entry_summaries),
        "anomalies": "; ".join(signal.anomalies),
        "structural_anomaly": signal.structural_anomaly,
        "chart_end": chart_end,
    }
    return signal_result, entry_rows


def summarize_by_period(results_df: pd.DataFrame, period: str) -> pd.DataFrame:
    if results_df.empty:
        return pd.DataFrame()

    df = results_df.copy()
    df["signal_time_chart"] = pd.to_datetime(df["signal_time_chart"])
    df["pnl_realized"] = df["pnl"].fillna(0.0)

    if period == "month":
        df["period"] = df["signal_time_chart"].dt.to_period("M").astype(str)
    elif period == "calendar_week":
        week_start = df["signal_time_chart"] - pd.to_timedelta(df["signal_time_chart"].dt.weekday, unit="D")
        week_end = week_start + pd.Timedelta(days=6)
        df["period"] = week_start.dt.strftime("%Y-%m-%d") + " to " + week_end.dt.strftime("%Y-%m-%d")
    else:
        raise ValueError("period must be 'month' or 'calendar_week'")

    rows = []
    for period_value, group in df.groupby("period", sort=True):
        start_equity = float(group.iloc[0]["equity_before"])
        end_equity = float(group.iloc[-1]["equity_after"])
        pnl = float(group["pnl_realized"].sum())
        rows.append(
            {
                "period": period_value,
                "signals": int(len(group)),
                "wins": int((group["status"] == "WIN").sum()),
                "losses": int((group["status"] == "LOSS").sum()),
                "no_fills": int((group["status"] == "NO_FILL").sum()),
                "open": int((group["status"] == "OPEN").sum()),
                "pnl": pnl,
                "start_equity": start_equity,
                "end_equity": end_equity,
                "return_pct": (pnl / start_equity * 100.0) if start_equity else 0.0,
            }
        )
    return pd.DataFrame(rows)


def compute_max_drawdown(equity_curve: pd.Series) -> float:
    if equity_curve.empty:
        return 0.0
    running_max = equity_curve.cummax()
    drawdown = (equity_curve / running_max) - 1.0
    return float(drawdown.min() * 100.0)


def run_backtest(
    signals: list[Signal],
    chart: pd.DataFrame,
    output_dir: Path,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
    risk_per_signal: float = DEFAULT_RISK_PER_SIGNAL,
    entry_count: int = DEFAULT_ENTRIES,
    exclude_structural_anomalies: bool = False,
    minimum_lot: float = 0.0,
    lot_step: float = 0.0,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)

    chart_start = chart["time"].iloc[0].to_pydatetime()
    chart_end = chart["time"].iloc[-1].to_pydatetime()

    equity = initial_capital
    signal_rows: list[dict] = []
    entry_rows: list[dict] = []
    excluded_rows: list[dict] = []

    for signal in signals:
        if signal.signal_time_chart < chart_start:
            excluded_rows.append(
                {
                    "global_id": signal.global_id,
                    "signal_key": f"{signal.source_date}#{signal.day_id:02d}",
                    "signal_time_chart": signal.signal_time_chart,
                    "reason": "Signal time before first available chart bar",
                }
            )
            continue
        if signal.signal_time_chart > chart_end:
            excluded_rows.append(
                {
                    "global_id": signal.global_id,
                    "signal_key": f"{signal.source_date}#{signal.day_id:02d}",
                    "signal_time_chart": signal.signal_time_chart,
                    "reason": "Signal time after last available chart bar",
                }
            )
            continue
        if exclude_structural_anomalies and signal.structural_anomaly:
            excluded_rows.append(
                {
                    "global_id": signal.global_id,
                    "signal_key": f"{signal.source_date}#{signal.day_id:02d}",
                    "signal_time_chart": signal.signal_time_chart,
                    "reason": "Structural signal anomaly",
                    "anomalies": "; ".join(signal.anomalies),
                }
            )
            continue

        signal_result, signal_entry_rows = backtest_signal(
            signal=signal,
            chart=chart,
            equity_before=equity,
            risk_per_signal=risk_per_signal,
            entry_count=entry_count,
            minimum_lot=minimum_lot,
            lot_step=lot_step,
        )
        signal_rows.append(signal_result)
        entry_rows.extend(signal_entry_rows)

        if signal_result["status"] != "OPEN":
            equity = float(signal_result["equity_after"])

        if equity <= 0:
            # Stop the compounded backtest if account is depleted.
            break

    signal_df = pd.DataFrame(signal_rows)
    entry_df = pd.DataFrame(entry_rows)
    excluded_df = pd.DataFrame(excluded_rows)

    if not signal_df.empty:
        signal_df["signal_time_chart"] = pd.to_datetime(signal_df["signal_time_chart"])
        signal_df["equity_after"] = pd.to_numeric(signal_df["equity_after"], errors="coerce")
        monthly_df = summarize_by_period(signal_df, "month")
        weekly_df = summarize_by_period(signal_df, "calendar_week")
        max_dd_pct = compute_max_drawdown(signal_df["equity_after"])
        realized_pnl = float(signal_df["pnl"].fillna(0.0).sum())
        final_equity = float(signal_df["equity_after"].dropna().iloc[-1])
        wins = int((signal_df["status"] == "WIN").sum())
        losses = int((signal_df["status"] == "LOSS").sum())
        no_fills = int((signal_df["status"] == "NO_FILL").sum())
        open_count = int((signal_df["status"] == "OPEN").sum())
        win_rate = wins / (wins + losses) * 100.0 if (wins + losses) else 0.0
    else:
        monthly_df = pd.DataFrame()
        weekly_df = pd.DataFrame()
        max_dd_pct = 0.0
        realized_pnl = 0.0
        final_equity = initial_capital
        wins = losses = no_fills = open_count = 0
        win_rate = 0.0

    anomalies_df = pd.DataFrame(
        [
            {
                "global_id": s.global_id,
                "signal_key": f"{s.source_date}#{s.day_id:02d}",
                "signal_time_chart": s.signal_time_chart,
                "side": s.side,
                "range": f"{s.r1:g} - {s.r2:g}",
                "entries": ", ".join(f"{e:g}" for e in s.entries[:entry_count]),
                "sl": s.sl,
                "tp1": s.tp1,
                "tp2": s.tp2,
                "tp3": s.tp3,
                "structural_anomaly": s.structural_anomaly,
                "anomalies": "; ".join(s.anomalies),
            }
            for s in signals
            if s.anomalies
        ]
    )

    summary = {
        "strategy": {
            "initial_capital": initial_capital,
            "risk_per_signal": risk_per_signal,
            "entry_count": entry_count,
            "activation_delay_minutes": DEFAULT_ACTIVATION_DELAY_MINUTES,
            "pending_expiry_minutes": DEFAULT_PENDING_EXPIRY_MINUTES,
            "max_hold_minutes": DEFAULT_MAX_HOLD_MINUTES,
            "sl_multiplier": DEFAULT_SL_MULTIPLIER,
            "final_target": DEFAULT_FINAL_TARGET,
            "lock_after_tp1": DEFAULT_LOCK_AFTER_TP1,
            "direction": "original signal direction",
            "fill_mode": "strict touch-only",
            "contract_size_oz_per_lot": DEFAULT_CONTRACT_SIZE_OZ,
            "minimum_lot": minimum_lot,
            "lot_step": lot_step,
        },
        "data": {
            "chart_start": chart_start.isoformat(sep=" "),
            "chart_end": chart_end.isoformat(sep=" "),
            "signals_parsed": len(signals),
            "signals_included": len(signal_df),
            "signals_excluded": len(excluded_df),
            "anomalies_flagged": len(anomalies_df),
        },
        "results": {
            "final_equity": final_equity,
            "net_profit": final_equity - initial_capital,
            "realized_pnl": realized_pnl,
            "max_drawdown_pct": max_dd_pct,
            "wins": wins,
            "losses": losses,
            "no_fills": no_fills,
            "open": open_count,
            "win_rate_pct": win_rate,
        },
    }

    signal_df.to_csv(output_dir / "signal_results.csv", index=False)
    entry_df.to_csv(output_dir / "entry_results.csv", index=False)
    monthly_df.to_csv(output_dir / "monthly_results.csv", index=False)
    weekly_df.to_csv(output_dir / "weekly_results.csv", index=False)
    anomalies_df.to_csv(output_dir / "anomalies.csv", index=False)
    excluded_df.to_csv(output_dir / "excluded_signals.csv", index=False)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backtest optimized XAUUSD signal strategy against MT5 M1 CSV files.")
    parser.add_argument("--signals", required=True, type=Path, help="Path to signal text file")
    parser.add_argument("--charts", required=True, nargs="+", type=Path, help="One or more MT5 tab-separated M1 CSV files")
    parser.add_argument("--output-dir", default=Path("backtest_output"), type=Path, help="Directory for output CSV/JSON files")
    parser.add_argument("--initial-capital", default=DEFAULT_INITIAL_CAPITAL, type=float, help="Initial equity, default 1000")
    parser.add_argument("--risk", default=DEFAULT_RISK_PER_SIGNAL, type=float, help="Risk per signal as decimal, default 0.05")
    parser.add_argument("--entries", default=DEFAULT_ENTRIES, type=int, choices=[1, 2, 3], help="Number of range entries to use, default 3")
    parser.add_argument("--minimum-lot", default=0.0, type=float, help="Optional broker minimum lot. 0 disables rounding/min check")
    parser.add_argument("--lot-step", default=0.0, type=float, help="Optional broker lot step, e.g. 0.01. 0 disables rounding")
    parser.add_argument(
        "--exclude-structural-anomalies",
        action="store_true",
        help="Exclude signals with invalid SL/TP structure. Default keeps them but flags them, matching the optimized analysis run.",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    signals = parse_signals(args.signals)
    chart = read_chart_files(args.charts)
    summary = run_backtest(
        signals=signals,
        chart=chart,
        output_dir=args.output_dir,
        initial_capital=args.initial_capital,
        risk_per_signal=args.risk,
        entry_count=args.entries,
        exclude_structural_anomalies=args.exclude_structural_anomalies,
        minimum_lot=args.minimum_lot,
        lot_step=args.lot_step,
    )

    print(json.dumps(summary, indent=2, default=str))
    print(f"\nOutput written to: {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
