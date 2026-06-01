"""Historical backtest runner.

Walks signals chronologically. For each signal, opens a Position and
advances it through bars from activation to expiry+max_hold. Equity
compounds with realized P&L plus optional broker bonus/rebate; the next signal
sees the updated equity.

`decide()` is not invoked here — the engine always returns FOLLOW with
the strategy's plan for backtest-eligible signals, so calling it would
be redundant. Both code paths share the same `core` modules.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from xauusd_trading import CONTRACT_SIZE_OZ, DEFAULT_CONFIG, StrategyConfig
from xauusd_trading import CsvChartSource
from xauusd_trading import Position, advance_bars, open_position
from xauusd_trading import Signal
from xauusd_trading import iter_bars, slice_bars
from xauusd_trading.core.trend_runner import prewarm_indicators_from_dataframe


# ---------------------------------------------------------------------------
# single-signal replay
# ---------------------------------------------------------------------------
def _finalize_expired_pending_entries(pos: Position, replay_end: datetime) -> None:
    """Mark unfilled pending entries as NO_FILL once replay reaches expiry."""
    if replay_end < pos.expiry_time:
        return
    for entry in pos.entries:
        if entry.status == "PENDING":
            entry.status = "NO_FILL"


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
    prewarm_indicators_from_dataframe(pos, chart_df, config, replay_start=pos.activation_time)
    bars = iter_bars(slice_bars(chart_df, pos.activation_time, end))
    advance_bars(pos, bars, config, contract_size)
    _finalize_expired_pending_entries(pos, end)
    return pos


def position_status(pos: Position) -> tuple[str, float]:
    """Classify a fully-replayed position.

    Returns (status, realized_pnl). status: WIN | LOSS | BREAKEVEN | NO_FILL | OPEN.
    """
    open_entries = pos.open_entries()
    if open_entries:
        return "OPEN", 0.0
    if any(e.status == "PENDING" for e in pos.entries):
        return "OPEN", 0.0
    if not pos.filled_entries():
        return "NO_FILL", 0.0
    pnl = pos.realized_pnl()
    if pnl > 0:
        return "WIN", pnl
    if pnl < 0:
        return "LOSS", pnl
    return "BREAKEVEN", 0.0


def _entry_closed_lots(pos: Position) -> float:
    """Lots that closed during replay and therefore earn the broker bonus."""
    return sum(
        float(e.lot or 0.0)
        for e in pos.entries
        if e.fill_time is not None and e.exit_time is not None
    )


def _bonus_for_position(pos: Position, config: StrategyConfig) -> float:
    return _entry_closed_lots(pos) * float(getattr(config, "bonus_per_closed_lot", 0.0) or 0.0)


# ---------------------------------------------------------------------------
# full backtest
# ---------------------------------------------------------------------------
_STATUS_TO_KEY = {"WIN": "wins", "LOSS": "losses",
                  "NO_FILL": "no_fills", "OPEN": "open"}


def _new_bucket(key_name: str, key_value: str, equity_start: float) -> dict:
    return {
        key_name: key_value, "signals": 0, "wins": 0, "losses": 0,
        "no_fills": 0, "open": 0,
        "pnl": 0.0, "trading_pnl": 0.0, "bonus": 0.0, "closed_lots": 0.0,
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
        status, trading_pnl = position_status(pos)
        closed_lots = 0.0 if status == "OPEN" else _entry_closed_lots(pos)
        bonus = 0.0 if status == "OPEN" else _bonus_for_position(pos, config)
        total_pnl = trading_pnl + bonus if status != "OPEN" else None
        equity_after = equity if status == "OPEN" else equity + float(total_pnl or 0.0)
        rows.append({
            "global_id": sig.global_id, "signal_key": sig.signal_key,
            "signal_time_chart": sig.signal_time_chart, "side": sig.side,
            "status": status,
            "pnl": total_pnl,
            "trading_pnl": trading_pnl if status != "OPEN" else None,
            "bonus": bonus if status != "OPEN" else None,
            "closed_lots": closed_lots,
            "equity_before": equity, "equity_after": equity_after,
        })

        tz_label = (f"GMT+{sig.source_tz_offset}" if sig.source_tz_offset >= 0
                    else f"GMT{sig.source_tz_offset}")
        for e in pos.entries:
            entry_closed_lots = float(e.lot or 0.0) if e.fill_time is not None and e.exit_time is not None and status != "OPEN" else 0.0
            entry_bonus = entry_closed_lots * float(getattr(config, "bonus_per_closed_lot", 0.0) or 0.0)
            entry_trading_pnl = e.pnl
            entry_total_pnl = (entry_trading_pnl + entry_bonus) if entry_trading_pnl is not None and status != "OPEN" else entry_trading_pnl
            entry_rows.append({
                "global_id": sig.global_id,
                "signal_key": sig.signal_key,
                "entry_key": f"{sig.signal_key}.{e.entry_index + 1}",
                "entry_number": e.entry_index + 1,
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
                "trading_pnl": entry_trading_pnl,
                "closed_lots": entry_closed_lots,
                "bonus": entry_bonus,
                "pnl": entry_total_pnl,
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
    total_realized = sum(r["pnl"] for r in rows if r["pnl"] is not None)
    trading_realized = sum((r.get("trading_pnl") or 0.0) for r in rows if r["pnl"] is not None)
    total_bonus = sum((r.get("bonus") or 0.0) for r in rows if r["pnl"] is not None)
    total_closed_lots = sum((r.get("closed_lots") or 0.0) for r in rows)

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
            bucket["trading_pnl"] += r.get("trading_pnl") or 0.0
            bucket["bonus"] += r.get("bonus") or 0.0
            bucket["closed_lots"] += r.get("closed_lots") or 0.0
        bucket["equity_end"] = r["equity_after"]
    monthly_rows = sorted(monthly.values(), key=lambda b: b["month"])
    for b in monthly_rows:
        _finalize_bucket(b)

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
            bucket["trading_pnl"] += r.get("trading_pnl") or 0.0
            bucket["bonus"] += r.get("bonus") or 0.0
            bucket["closed_lots"] += r.get("closed_lots") or 0.0
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
        "realized_pnl": total_realized,
        "trading_pnl": trading_realized,
        "bonus": total_bonus,
        "closed_lots": total_closed_lots,
        "wins": wins, "losses": losses, "no_fills": no_fills, "open": open_count,
        "win_rate_pct": wins / (wins + losses) * 100.0 if (wins + losses) else 0.0,
        "max_drawdown_pct": max_dd_pct,
        "rows": rows,
        "entry_rows": entry_rows,
        "monthly": monthly_rows,
        "daily": daily_rows,
    }


def _backtest_output_path(output_dir: Path, filename: str = "backtest_results.xlsx") -> Path:
    """Resolve backtest output to a single Excel file path.

    Legacy/default:
      --output-dir reports -> reports/backtest_results.xlsx

    Named run:
      --output-dir reports/trailing_open_2_risk_0034
          -> reports/trailing_open_2_risk_0034.xlsx

    Scenario for named run:
      filename=backtest_results_5000_2025-01-06.xlsx
          -> reports/trailing_open_2_risk_0034_5000_2025-01-06.xlsx

    Important: only the exact reports directory is treated as a directory. Any
    deeper path is treated as a run-name stem even if an old folder exists there,
    preventing repeated reports/<run>/backtest_results.xlsx files.
    """
    output_dir = Path(output_dir)
    default_name = "backtest_results.xlsx"

    if output_dir.suffix.lower() == ".xlsx":
        base = output_dir
    elif output_dir.name.lower() == "reports" and output_dir.parent in {Path("."), Path("")}:
        base = output_dir / default_name
    else:
        base = output_dir.with_suffix(".xlsx")

    if filename == default_name:
        return base

    suffix = Path(filename).stem
    if suffix.startswith("backtest_results_"):
        suffix = suffix[len("backtest_results_"):]
    return base.with_name(f"{base.stem}_{suffix}.xlsx")


def write_backtest_outputs(
        result: dict, output_dir: Path,
        filename: str = "backtest_results.xlsx",
) -> Path:
    """Write the backtest result as a styled .xlsx file.

    ``output_dir`` accepts either the legacy reports directory or a named output
    stem/file path. This prevents repeated nested ``backtest_results.xlsx`` files
    when running many parameter sets.
    """
    output_path = _backtest_output_path(Path(output_dir), filename)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    from ..reporting.excel_report import write_excel_report
    return write_excel_report(result, output_path)
