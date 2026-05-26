"""Historical backtest runner.

Walks signals chronologically. For each signal, opens a Position and
advances it through bars from activation to expiry+max_hold. Equity
compounds with realized P&L; the next signal sees the updated equity.

`decide()` is not invoked here — the engine always returns FOLLOW with
the strategy's plan for backtest-eligible signals, so calling it would
be redundant. Both code paths share the same `core` modules.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from xauusd_trading import CONTRACT_SIZE_OZ, DEFAULT_CONFIG, StrategyConfig
from xauusd_trading import CsvChartSource
from xauusd_trading import Position, advance_bars, open_position
from xauusd_trading import Signal
from xauusd_trading import iter_bars, slice_bars


# ---------------------------------------------------------------------------
# single-signal replay
# ---------------------------------------------------------------------------

def replay_signal(
        signal: Signal, chart_df: pd.DataFrame, equity: float,
        config: StrategyConfig = DEFAULT_CONFIG,
        contract_size: float = CONTRACT_SIZE_OZ,
) -> Position:
    """Advance one signal through its lifetime and return the Position."""
    pos = open_position(signal, equity, config, contract_size)
    end = pos.expiry_time + timedelta(minutes=config.max_hold_minutes + 5)
    chart_end = chart_df["time"].iloc[-1].to_pydatetime()
    if end > chart_end:
        end = chart_end
    bars = iter_bars(slice_bars(chart_df, pos.activation_time, end))
    advance_bars(pos, bars, config, contract_size)
    return pos


def position_status(pos: Position) -> tuple[str, float]:
    """Classify a fully-replayed position.

    Returns (status, realized_pnl). status: WIN | LOSS | BREAKEVEN | NO_FILL | OPEN.
    """
    open_entries = pos.open_entries()
    if open_entries:
        return "OPEN", 0.0
    if not pos.filled_entries():
        return "NO_FILL", 0.0
    pnl = pos.realized_pnl()
    if pnl > 0:
        return "WIN", pnl
    if pnl < 0:
        return "LOSS", pnl
    return "BREAKEVEN", 0.0


# ---------------------------------------------------------------------------
# full backtest
# ---------------------------------------------------------------------------

_STATUS_TO_KEY = {"WIN": "wins", "LOSS": "losses",
                  "NO_FILL": "no_fills", "OPEN": "open"}


def _new_bucket(key_name: str, key_value: str, equity_start: float) -> dict:
    return {
        key_name: key_value, "signals": 0, "wins": 0, "losses": 0,
        "no_fills": 0, "open": 0, "pnl": 0.0,
        "equity_start": equity_start, "equity_end": equity_start,
    }


def _finalize_bucket(b: dict) -> None:
    """Compute derived percentages. Mutates in place."""
    wl = b["wins"] + b["losses"]
    b["win_rate_pct"] = b["wins"] / wl * 100.0 if wl else 0.0
    if b["equity_start"] and b["equity_start"] > 0:
        b["pnl_pct"] = b["pnl"] / b["equity_start"] * 100.0
    else:
        b["pnl_pct"] = 0.0


def run_backtest(
        signals: list[Signal], chart: CsvChartSource,
        config: StrategyConfig = DEFAULT_CONFIG,
        *,
        exclude_structural_anomalies: bool = False,
        contract_size: float = CONTRACT_SIZE_OZ,
) -> dict:
    chart_df = chart.dataframe
    chart_start = chart.first_time()
    chart_end = chart.last_time()

    equity = config.initial_capital
    rows: list[dict] = []
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

        pos = replay_signal(sig, chart_df, equity, config, contract_size)
        status, pnl = position_status(pos)
        equity_after = equity if status == "OPEN" else equity + pnl
        rows.append({
            "global_id": sig.global_id, "signal_key": sig.signal_key,
            "signal_time_chart": sig.signal_time_chart, "side": sig.side,
            "status": status, "pnl": pnl if status != "OPEN" else None,
            "equity_before": equity, "equity_after": equity_after,
        })

        tz_label = (f"GMT+{sig.source_tz_offset}" if sig.source_tz_offset >= 0
                    else f"GMT{sig.source_tz_offset}")
        for e in pos.entries:
            entry_rows.append({
                "global_id": sig.global_id,
                "signal_key": sig.signal_key,
                "signal_date": sig.source_date,
                "signal_time_source": sig.source_time_text,
                "source_tz": tz_label,
                "signal_time_chart": sig.signal_time_chart,
                "side": sig.side,
                "range_low": sig.range_low,
                "range_high": sig.range_high,
                "original_SL": sig.sl,
                "TP1": sig.tp1,
                "TP2": sig.tp2,
                "TP3": sig.tp3,
                "final_target_label": config.final_target.upper(),
                "final_target_price": pos.target_level,
                "entry_index": e.entry_index,
                "entry_price": e.entry_price,
                "effective_SL": e.initial_sl,
                "SL_distance": pos.base_stop_distance,
                "lot": e.lot,
                "entry_status": e.status,
                "fill_time": e.fill_time,
                "exit_time": e.exit_time,
                "exit_price": e.exit_price,
                "stop_at_exit": e.stop_at_exit,
                "pnl": e.pnl,
                "first_fill_time": pos.first_fill_time,
                "time_exit_deadline": pos.time_exit_deadline,
                "signal_status": status,
                "equity_before": equity,
                "equity_after": equity_after,
            })

        if status != "OPEN":
            equity = equity_after
        if equity <= 0:
            break

    wins = sum(1 for r in rows if r["status"] == "WIN")
    losses = sum(1 for r in rows if r["status"] == "LOSS")
    no_fills = sum(1 for r in rows if r["status"] == "NO_FILL")
    open_count = sum(1 for r in rows if r["status"] == "OPEN")
    realized = sum(r["pnl"] for r in rows if r["pnl"] is not None)

    # Max drawdown across the equity curve.
    max_dd_pct = 0.0
    peak = config.initial_capital
    for r in rows:
        eq = r["equity_after"]
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (eq - peak) / peak * 100.0
            if dd < max_dd_pct:
                max_dd_pct = dd

    # Monthly breakdown — bucket by signal_time_chart (GMT+3).
    monthly: dict[str, dict] = {}
    for r in rows:
        mk = r["signal_time_chart"].strftime("%Y-%m")
        if mk not in monthly:
            monthly[mk] = _new_bucket("month", mk, r["equity_before"])
        bucket = monthly[mk]
        bucket["signals"] += 1
        bucket[_STATUS_TO_KEY.get(r["status"], "no_fills")] += 1
        if r["pnl"] is not None:
            bucket["pnl"] += r["pnl"]
        bucket["equity_end"] = r["equity_after"]
    monthly_rows = sorted(monthly.values(), key=lambda b: b["month"])
    for b in monthly_rows:
        _finalize_bucket(b)

    # Daily breakdown — every calendar day in the chart range, carrying
    # equity forward across no-signal days.
    daily_by_key: dict[str, dict] = {}
    for r in rows:
        dk = r["signal_time_chart"].strftime("%Y-%m-%d")
        if dk not in daily_by_key:
            daily_by_key[dk] = _new_bucket("date", dk, r["equity_before"])
        bucket = daily_by_key[dk]
        bucket["signals"] += 1
        bucket[_STATUS_TO_KEY.get(r["status"], "no_fills")] += 1
        if r["pnl"] is not None:
            bucket["pnl"] += r["pnl"]
        bucket["equity_end"] = r["equity_after"]

    daily_rows: list[dict] = []
    if chart_start is not None and chart_end is not None:
        cur: date = chart_start.date()
        end_date: date = chart_end.date()
        running_equity = config.initial_capital
        while cur <= end_date:
            dk = cur.strftime("%Y-%m-%d")
            if dk in daily_by_key:
                b = daily_by_key[dk]
                running_equity = b["equity_end"]
            else:
                b = _new_bucket("date", dk, running_equity)
            _finalize_bucket(b)
            daily_rows.append(b)
            cur += timedelta(days=1)
    else:
        for b in sorted(daily_by_key.values(), key=lambda x: x["date"]):
            _finalize_bucket(b)
            daily_rows.append(b)

    return {
        "config": asdict(config),
        "chart_start": chart_start.isoformat(sep=" ") if chart_start else None,
        "chart_end": chart_end.isoformat(sep=" ") if chart_end else None,
        "signals_parsed": len(signals),
        "signals_included": len(rows),
        "signals_excluded": len(excluded),
        "final_equity": equity,
        "net_profit": equity - config.initial_capital,
        "realized_pnl": realized,
        "wins": wins, "losses": losses, "no_fills": no_fills, "open": open_count,
        "win_rate_pct": wins / (wins + losses) * 100.0 if (wins + losses) else 0.0,
        "max_drawdown_pct": max_dd_pct,
        "rows": rows,
        "entry_rows": entry_rows,
        "monthly": monthly_rows,
        "daily": daily_rows,
    }


def write_backtest_outputs(
        result: dict, output_dir: Path,
        filename: str = "backtest_results.xlsx",
) -> Path:
    """Write the backtest result as a styled .xlsx file.

    `filename` defaults to `backtest_results.xlsx`; scenario runs pass
    a distinguishing name like `backtest_results_1500_2026-05-19.xlsx`.
    Returns the path written. openpyxl is required.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    from ..reporting.excel_report import write_excel_report
    return write_excel_report(result, output_dir / filename)