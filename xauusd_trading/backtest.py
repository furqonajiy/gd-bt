"""Historical backtest runner.

Walks signals chronologically. For each signal, opens a Position and advances
it through bars from activation to expiry+max_hold (the same window the
original backtester used). Equity compounds with realized P&L.

Cross-signal interaction is unchanged from the original code: each signal is
processed in time order; equity for the next signal reflects the previous one.
The engine's `decide()` is not called inside the loop because we want a
faithful reproduction of the original backtest result. The engine produces
identical orders for each signal anyway (it always returns FOLLOW with the
strategy's plan), so calling it would be redundant. The same `core` modules
power both code paths.
"""
from __future__ import annotations
import json
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from .adapters import CsvChartSource
from .chart import iter_bars, slice_bars
from .config import CONTRACT_SIZE_OZ, DEFAULT_CONFIG, StrategyConfig
from .positions import Position, advance_bars, open_position
from .signal import Signal, parse_signals_file


# ---------------------------------------------------------------------------
# single-signal replay
# ---------------------------------------------------------------------------

def replay_signal(
    signal: Signal, chart_df: pd.DataFrame, equity: float,
    config: StrategyConfig = DEFAULT_CONFIG,
    contract_size: float = CONTRACT_SIZE_OZ,
) -> Position:
    """Advance one signal through its entire lifetime and return the Position."""
    pos = open_position(signal, equity, config, contract_size)
    end = pos.expiry_time + timedelta(minutes=config.max_hold_minutes + 5)
    chart_end = chart_df["time"].iloc[-1].to_pydatetime()
    if end > chart_end:
        end = chart_end
    bars = iter_bars(slice_bars(chart_df, pos.activation_time, end))
    advance_bars(pos, bars, config, contract_size)
    return pos


def position_status(pos: Position) -> tuple[str, float]:
    """Classify a fully-replayed position. Returns (status, realized_pnl).

    status: WIN | LOSS | BREAKEVEN | NO_FILL | OPEN
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

        # Per-entry detail rows. One row per Entry slot (3 per signal).
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

    # Monthly breakdown bucketed by signal_time_chart (GMT+3).
    monthly: dict[str, dict] = {}
    status_to_key = {"WIN": "wins", "LOSS": "losses",
                     "NO_FILL": "no_fills", "OPEN": "open"}
    for r in rows:
        mk = r["signal_time_chart"].strftime("%Y-%m")
        bucket = monthly.setdefault(mk, {
            "month": mk, "signals": 0, "wins": 0, "losses": 0,
            "no_fills": 0, "open": 0, "pnl": 0.0, "equity_end": None,
        })
        bucket["signals"] += 1
        bucket[status_to_key.get(r["status"], "no_fills")] += 1
        if r["pnl"] is not None:
            bucket["pnl"] += r["pnl"]
        bucket["equity_end"] = r["equity_after"]
    monthly_rows = sorted(monthly.values(), key=lambda b: b["month"])
    for b in monthly_rows:
        wl = b["wins"] + b["losses"]
        b["win_rate_pct"] = b["wins"] / wl * 100.0 if wl else 0.0

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
    }


def write_backtest_outputs(result: dict, output_dir: Path) -> None:
    """Write summary.json, signal_results.csv, entry_results.csv, and
    backtest_results.xlsx to the output directory.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Summary JSON (everything except the heavy row data).
    summary = {k: v for k, v in result.items()
               if k not in {"rows", "entry_rows"}}
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    # CSVs for tooling.
    pd.DataFrame(result["rows"]).to_csv(output_dir / "signal_results.csv", index=False)
    pd.DataFrame(result["entry_rows"]).to_csv(output_dir / "entry_results.csv", index=False)

    # Excel report — soft dependency.
    try:
        from .excel_report import write_excel_report
        write_excel_report(result, output_dir / "backtest_results.xlsx")
    except ImportError as e:
        # openpyxl not installed; xlsx is optional, CSVs always written.
        print(f"[warn] Excel output skipped: {e}. "
              f"Install with `pip install openpyxl` to enable.")
