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

from collections import Counter
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from trading.engine import CONTRACT_SIZE_OZ, DEFAULT_CONFIG, StrategyConfig
from trading.engine import CsvChartSource
from trading.engine import Position, advance_bars, open_position
from trading.engine import Signal
from trading.engine import iter_bars, slice_bars
from trading.engine.core.trend_runner import prewarm_indicators_from_dataframe


# ---------------------------------------------------------------------------
# signal risk:reward policy (filter / rewrite-targets; default no-op)
# ---------------------------------------------------------------------------
def _atr_lookup(chart_df, period: int):
    """Return ``at(time) -> ATR`` (Wilder-style rolling mean of true range) using
    only the last CLOSED bar at-or-before ``time`` (no lookahead), or None."""
    import numpy as np
    h, l, c = chart_df["high"], chart_df["low"], chart_df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.rolling(int(period), min_periods=int(period)).mean().values
    times = chart_df["time"].values  # datetime64[ns]

    def at(t):
        idx = int(np.searchsorted(times, np.datetime64(t), side="right")) - 1
        if idx < 0:
            return None
        v = atr[idx]
        return None if v != v else float(v)  # NaN -> None
    return at


def apply_signal_rr_policy(sig, config: StrategyConfig, atr_value: float | None = None):
    """Filter / rewrite a signal per the config's R:R + SL-source policy.

    Returns the (possibly SL/TP-rewritten) signal, or ``None`` if filtered out by
    ``signal_min_rr``. No-op by default (signal SL source, ``signal_min_rr`` 0,
    ``rewrite_tp3_rr`` 0) so parity / DEFAULT_CONFIG behavior is unchanged.

    entry_edge = range_high (BUY) / range_low (SELL). Raw risk is |entry_edge -
    sl| ("signal" SL source) or ``atr_value * atr_sl_mult`` ("atr" -- which also
    REPLACES the signal's SL with entry_edge -/+ that), then scaled by
    ``sl_multiplier`` for the R:R reference when ``signal_rr_reference`` is
    "effective".
    """
    min_rr = float(getattr(config, "signal_min_rr", 0.0) or 0.0)
    rw3 = float(getattr(config, "rewrite_tp3_rr", 0.0) or 0.0)
    use_atr = (getattr(config, "sl_source", "signal") == "atr"
               and atr_value is not None and atr_value > 0.0)
    if min_rr <= 0.0 and rw3 <= 0.0 and not use_atr:
        return sig
    entry = sig.range_high if sig.side == "BUY" else sig.range_low
    if use_atr:
        risk0 = atr_value * float(config.atr_sl_mult)
        new_sl = entry - risk0 if sig.side == "BUY" else entry + risk0
        sig = replace(sig, sl=new_sl)
    else:
        risk0 = abs(entry - sig.sl)
    if risk0 <= 0.0:
        return sig
    effective = getattr(config, "signal_rr_reference", "nominal") == "effective"
    risk = risk0 * (config.sl_multiplier if effective else 1.0)
    if min_rr > 0.0 and abs(sig.tp1 - entry) / risk < min_rr:
        return None
    if rw3 > 0.0:
        r1, r2, r3 = (config.rewrite_tp1_rr, config.rewrite_tp2_rr,
                      config.rewrite_tp3_rr)
        if sig.side == "BUY":
            sig = replace(sig, tp1=entry + r1 * risk, tp2=entry + r2 * risk,
                          tp3=entry + r3 * risk)
        else:
            sig = replace(sig, tp1=entry - r1 * risk, tp2=entry - r2 * risk,
                          tp3=entry - r3 * risk)
    return sig


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


# Entry exit statuses in display order (for per-day / summary outcome columns).
ENTRY_STATUS_ORDER = [
    "NO_FILL", "SL", "BEP", "LOCK_HALF_TP1", "LOCK_TP1", "LOCK_TP2",
    "TP1", "TP2", "TP3", "TIME_EXIT", "TRAILING_STOP", "PENDING", "OPEN",
]


def _realized_rr(side: str, entry_price: float, sl: float,
                 exit_price: float | None, *, filled: bool) -> float | None:
    """Realized R-multiple of one entry: favourable move / risk distance to SL.

    Side-aware (a winning SELL is +R). Returns None for an entry that never
    filled or hasn't closed, or when the risk distance is zero.
    """
    if not filled or exit_price is None or entry_price is None or sl is None:
        return None
    risk = abs(entry_price - sl)
    if risk <= 0:
        return None
    favourable = (exit_price - entry_price) if side == "BUY" else (entry_price - exit_price)
    return favourable / risk


def _planned_rr(entry_price: float | None, sl: float | None,
                target: float | None) -> float | None:
    """Planned setup R:R = reward (entry->target) / risk (entry->SL). Positive.

    A setup property (defined even for an entry that never filled). Returned as
    the reward multiple N, displayed as 1:N.
    """
    if entry_price is None or sl is None or target is None:
        return None
    risk = abs(entry_price - sl)
    if risk <= 0:
        return None
    return abs(target - entry_price) / risk


def _payoff_ratio(win_pnls: list[float], loss_pnls: list[float]) -> float | None:
    """Realized payoff: average win $ / average loss $ (positive number)."""
    if not win_pnls or not loss_pnls:
        return None
    avg_win = sum(win_pnls) / len(win_pnls)
    avg_loss = abs(sum(loss_pnls) / len(loss_pnls))
    return avg_win / avg_loss if avg_loss > 0 else None


# ---------------------------------------------------------------------------
# full backtest
# ---------------------------------------------------------------------------
_STATUS_TO_KEY = {"WIN": "wins", "LOSS": "losses", "BREAKEVEN": "breakevens",
                  "NO_FILL": "no_fills", "OPEN": "open"}


def _classify_month_regimes(chart_df: pd.DataFrame,
                            month_keys: list[str]) -> dict[str, str]:
    """Volatility regime (R1quiet/R2bull/R3strong/R4parab) for each YYYY-MM month,
    read from that month's own M1 bars -- so the Summary shows how XAUUSD behaved
    each month. Empty string when a month has too few bars to classify."""
    from trading.engine.strategy.regime import read_current_regime
    cols = ["time", "open", "high", "low", "close"]
    if not all(c in chart_df.columns for c in cols):
        return {}
    df = chart_df[cols].set_index("time")
    period = df.index.to_period("M").astype(str)
    out: dict[str, str] = {}
    for mk in month_keys:
        sub = df[period == mk]
        if len(sub) < 60:           # < ~1h of M1: not enough to read a regime
            out[mk] = ""
            continue
        try:
            out[mk] = read_current_regime(sub).regime
        except Exception:
            out[mk] = ""
    return out


def _new_bucket(key_name: str, key_value: str, equity_start: float) -> dict:
    return {
        key_name: key_value, "signals": 0, "wins": 0, "losses": 0,
        "breakevens": 0, "no_fills": 0, "open": 0,
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


def screen_signal(sig, config, chart_start, chart_end, *, atr_at=None,
                  exclude_structural_anomalies=False, config_resolver=None):
    """Apply run_backtest's per-signal gates (chart bounds, structural anomaly,
    signal R:R / ATR policy). Returns ((transformed_sig, sig_config), None) to
    replay, or (None, reason) to exclude. Mirrors the run_backtest loop head so
    the hybrid backtest screens signals identically (parity)."""
    if chart_start is None or sig.signal_time_chart < chart_start:
        return None, "before chart start"
    if chart_end is None or sig.signal_time_chart > chart_end:
        return None, "after chart end"
    if exclude_structural_anomalies and sig.structural_anomaly:
        return None, "structural anomaly"
    sig_config = config_resolver(sig) if config_resolver is not None else config
    transformed = apply_signal_rr_policy(
        sig, sig_config,
        atr_value=atr_at(sig.signal_time_chart) if atr_at is not None else None)
    if transformed is None:
        return None, "below min R:R"
    return (transformed, sig_config), None


def replay_signal_rows(sig, chart_df, equity, sig_config, base_config,
                       contract_size=CONTRACT_SIZE_OZ):
    """Replay ONE already-screened/transformed signal on M1 bars and build its
    per-signal + per-entry report rows. Extracted VERBATIM from run_backtest's
    loop body so the hybrid tick/M1 backtest reuses the EXACT M1 row construction
    (parity). ``sig_config`` is the per-signal (resolver) replay config; ``base_config``
    is the run base used for the per-entry bonus + final-target label (matching
    run_backtest's original config/sig_config split). Returns a dict with keys
    row, entry_rows, status, equity_after."""
    pos = replay_signal(sig, chart_df, equity, sig_config, contract_size)
    status, trading_pnl = position_status(pos)
    closed_lots = 0.0 if status == "OPEN" else _entry_closed_lots(pos)
    bonus = 0.0 if status == "OPEN" else _bonus_for_position(pos, sig_config)
    total_pnl = trading_pnl + bonus if status != "OPEN" else None
    equity_after = equity if status == "OPEN" else equity + float(total_pnl or 0.0)
    row = {
        "global_id": sig.global_id, "signal_key": sig.signal_key,
        "signal_time_chart": sig.signal_time_chart,
        # The signal's own feed-zone (source) clock -- e.g. GMT+7 for the
        # Victor/SQZ6 feeds. The Daily/Monthly breakdowns group by THIS, so a
        # report day lines up with the signal codes (SQZ6-0623) the same way
        # --start-date/--end-date do, not the chart (EET/EEST) day.
        "signal_time_source": sig.signal_time_source,
        "side": sig.side,
        "status": status,
        "pnl": total_pnl,
        "trading_pnl": trading_pnl if status != "OPEN" else None,
        "bonus": bonus if status != "OPEN" else None,
        "closed_lots": closed_lots,
        "equity_before": equity, "equity_after": equity_after,
    }

    tz_label = (f"GMT+{sig.source_tz_offset}" if sig.source_tz_offset >= 0
                else f"GMT{sig.source_tz_offset}")
    entry_rows = []
    for e in pos.entries:
        entry_closed_lots = float(e.lot or 0.0) if e.fill_time is not None and e.exit_time is not None and status != "OPEN" else 0.0
        entry_bonus = entry_closed_lots * float(getattr(base_config, "bonus_per_closed_lot", 0.0) or 0.0)
        entry_trading_pnl = e.pnl
        entry_total_pnl = (entry_trading_pnl + entry_bonus) if entry_trading_pnl is not None and status != "OPEN" else entry_trading_pnl
        # Realized R-multiple = favourable price move / risk distance to the
        # executed SL. Side-aware so a winning SELL is +R. None when an entry
        # never filled/closed or has no risk distance.
        entry_rr = _realized_rr(sig.side, e.entry_price, e.initial_sl, e.exit_price,
                                filled=e.fill_time is not None)
        # Per-entry-target mode gives each leg its own target; else the single
        # position target. R:R is to whichever target this leg aims at.
        entry_target_price = e.target_price if e.target_price is not None else pos.target_level
        entry_target_label = e.target_label or base_config.final_target.upper()
        entry_rr_planned = _planned_rr(e.entry_price, e.initial_sl, entry_target_price)
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
            "final_target_label": entry_target_label,
            "final_target_price": entry_target_price,
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
            "rr": entry_rr,
            "rr_planned": entry_rr_planned,
            "first_fill_time": pos.first_fill_time,
            "time_exit_deadline": pos.time_exit_deadline,
            "signal_status": status,
            "equity_before": equity,
            "equity_after": equity_after,
        })
    return {"row": row, "entry_rows": entry_rows, "status": status,
            "equity_after": equity_after}


def aggregate_backtest_result(rows, entry_rows, excluded, config, chart_df,
                              chart_start, chart_end, equity, signals_parsed):
    """Aggregate replayed rows into the backtest result dict (summary, monthly,
    daily breakdowns, drawdown, entry stats). Extracted VERBATIM from run_backtest
    so the hybrid backtest emits a byte-identical result shape (parity)."""
    wins = sum(1 for r in rows if r["status"] == "WIN")
    losses = sum(1 for r in rows if r["status"] == "LOSS")
    breakevens = sum(1 for r in rows if r["status"] == "BREAKEVEN")
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
        # Group by the signal's feed-zone month (see signal_time_source above),
        # so the Monthly Breakdown matches the signal-code dates.
        mk = r["signal_time_source"].strftime("%Y-%m")
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
    # Label each month with the volatility regime read from its own M1 bars.
    month_regimes = _classify_month_regimes(
        chart_df, [b["month"] for b in monthly_rows])
    for b in monthly_rows:
        b["regime"] = month_regimes.get(b["month"], "")

    # Per-entry aggregation (status counts + realized R) keyed by day, used by
    # both the Daily Breakdown and the Summary.
    daily_entry: dict[str, dict] = {}
    for er in entry_rows:
        # signal_date is the feed-zone (source) date string already (YYYY-MM-DD),
        # so the per-entry day buckets match the signal-grouped day buckets below.
        dk = er["signal_date"]
        de = daily_entry.setdefault(dk, {"statuses": Counter(), "rr": [], "rrp": [], "entries": 0})
        de["entries"] += 1
        de["statuses"][er["entry_status"]] += 1
        if er.get("rr") is not None:
            de["rr"].append(er["rr"])
        if er.get("rr_planned") is not None:
            de["rrp"].append(er["rr_planned"])

    daily_by_key: dict[str, dict] = {}
    for r in rows:
        dk = r["signal_time_source"].strftime("%Y-%m-%d")
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

    def _attach_entry_detail(bucket: dict, dk: str) -> None:
        de = daily_entry.get(dk)
        bucket["entry_total"] = de["entries"] if de else 0
        bucket["entry_status_counts"] = dict(de["statuses"]) if de else {}
        rr_list = de["rr"] if de else []
        bucket["entry_rr_avg"] = sum(rr_list) / len(rr_list) if rr_list else None
        rrp_list = de["rrp"] if de else []
        bucket["entry_rrp_avg"] = sum(rrp_list) / len(rrp_list) if rrp_list else None

    # Daily rows span only the traded window [first signal day, last signal day],
    # so pre-start padding (e.g. 2024 days when the run starts 2025) is excluded.
    daily_rows: list[dict] = []
    if rows:
        first_day: date = min(r["signal_time_source"].date() for r in rows)
        last_day: date = max(r["signal_time_source"].date() for r in rows)
        cur = first_day
        running_equity = config.initial_capital
        while cur <= last_day:
            dk = cur.strftime("%Y-%m-%d")
            if dk in daily_by_key:
                b = daily_by_key[dk]
                running_equity = b["equity_end"]
            else:
                b = _new_bucket("date", dk, running_equity)
            _finalize_bucket(b)
            _attach_entry_detail(b, dk)
            daily_rows.append(b)
            cur += timedelta(days=1)

    # Summary-level entry outcomes + realized risk:reward.
    entry_status_counts = Counter(er["entry_status"] for er in entry_rows)
    statuses_present = (
        [s for s in ENTRY_STATUS_ORDER if entry_status_counts.get(s)]
        + [s for s in entry_status_counts if s not in ENTRY_STATUS_ORDER]
    )
    rr_values = [er["rr"] for er in entry_rows if er.get("rr") is not None]
    rrp_values = [er["rr_planned"] for er in entry_rows if er.get("rr_planned") is not None]
    filled_pnls = [
        er["trading_pnl"] for er in entry_rows
        if er["fill_time"] is not None and er["exit_time"] is not None and er.get("trading_pnl") is not None
    ]
    win_pnls = [p for p in filled_pnls if p > 0]
    loss_pnls = [p for p in filled_pnls if p < 0]

    return {
        "config": asdict(config),
        "chart_start": chart_start.isoformat(sep=" ") if chart_start else None,
        "chart_end": chart_end.isoformat(sep=" ") if chart_end else None,
        "signals_parsed": signals_parsed,
        "signals_included": len(rows),
        "signals_excluded": len(excluded),
        "final_equity": equity,
        "net_profit": equity - config.initial_capital,
        "realized_pnl": total_realized,
        "trading_pnl": trading_realized,
        "bonus": total_bonus,
        "closed_lots": total_closed_lots,
        "wins": wins, "losses": losses, "breakevens": breakevens,
        "no_fills": no_fills, "open": open_count,
        "win_rate_pct": wins / (wins + losses) * 100.0 if (wins + losses) else 0.0,
        "max_drawdown_pct": max_dd_pct,
        # Entry-level outcome breakdown + realized R:R.
        "entry_total": len(entry_rows),
        "entry_status_counts": dict(entry_status_counts),
        "entry_statuses_present": statuses_present,
        "entry_filled": sum(1 for er in entry_rows if er["fill_time"] is not None),
        "entry_no_fill": entry_status_counts.get("NO_FILL", 0),
        "entry_win_count": len(win_pnls),
        "entry_loss_count": len(loss_pnls),
        "entry_rr_avg": sum(rr_values) / len(rr_values) if rr_values else None,
        "entry_rrp_avg": sum(rrp_values) / len(rrp_values) if rrp_values else None,
        "entry_payoff_ratio": _payoff_ratio(win_pnls, loss_pnls),
        "rows": rows,
        "entry_rows": entry_rows,
        "monthly": monthly_rows,
        "daily": daily_rows,
    }


def run_backtest(
        signals: list[Signal], chart: CsvChartSource,
        config: StrategyConfig = DEFAULT_CONFIG,
        *,
        exclude_structural_anomalies: bool = False,
        contract_size: float = CONTRACT_SIZE_OZ,
        config_resolver=None,
) -> dict:
    """Replay every signal and aggregate.

    ``config_resolver``: optional ``callable(signal) -> StrategyConfig``. When
    given (regime-adaptive backtest), each signal is replayed under the config it
    returns instead of the fixed ``config`` -- so the backtest mirrors the live
    ``auto --adaptive`` switch. ``config`` is still the base for capital/reporting.
    """
    chart_df = chart.dataframe
    chart_start = chart.first_time()
    chart_end = chart.last_time()

    equity = config.initial_capital
    rows: list[dict] = []
    entry_rows: list[dict] = []
    excluded: list[dict] = []

    # ATR lookup only when the base config sources its SL from ATR (cheap; built
    # once). Per-signal config_resolver SL sources beyond the base are not ATR
    # here -- the Victor/self sweeps use a single fixed config per run.
    atr_at = (_atr_lookup(chart_df, config.atr_period)
              if getattr(config, "sl_source", "signal") == "atr" else None)

    for sig in signals:
        screened, reason = screen_signal(
            sig, config, chart_start, chart_end, atr_at=atr_at,
            exclude_structural_anomalies=exclude_structural_anomalies,
            config_resolver=config_resolver)
        if screened is None:
            excluded.append({"signal_key": sig.signal_key, "reason": reason})
            continue
        sig, sig_config = screened
        built = replay_signal_rows(sig, chart_df, equity, sig_config, config,
                                   contract_size=contract_size)
        rows.append(built["row"])
        entry_rows.extend(built["entry_rows"])
        if built["status"] != "OPEN":
            equity = built["equity_after"]
        if equity <= 0:
            break

    return aggregate_backtest_result(
        rows, entry_rows, excluded, config, chart_df, chart_start, chart_end,
        equity, len(signals))


def _dot_free_stem(stem: str) -> str:
    """Generated artifact names carry no dots outside the file extension.

    A run name like ``BEST_slm2.1_gap0.5_2025`` would otherwise make
    ``Path.with_suffix()`` treat ``.5_2025`` as the extension and silently
    truncate the workbook to ``BEST_slm2.1_gap0.xlsx``. Parameter values are
    rendered dot-free instead (``slm21``, ``gap05``) — see the naming
    convention in CLAUDE.md.
    """
    return stem.replace(".", "")


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
    preventing repeated reports/<run>/backtest_results.xlsx files. The final
    component is always sanitized dot-free (a dotted run name would otherwise be
    truncated at its last dot), so a `--output-dir reports/BEST_slm2.1_gap0.5`
    still writes the canonical `reports/BEST_slm21_gap05.xlsx`.
    """
    output_dir = Path(output_dir)
    default_name = "backtest_results.xlsx"

    name = output_dir.name
    if name.lower().endswith(".xlsx"):
        base = output_dir.with_name(_dot_free_stem(name[: -len(".xlsx")]) + ".xlsx")
    elif name.lower() == "reports" and output_dir.parent in {Path("."), Path("")}:
        base = output_dir / default_name
    else:
        base = output_dir.with_name(_dot_free_stem(name) + ".xlsx")

    if filename == default_name:
        return base

    suffix = Path(filename).stem
    if suffix.startswith("backtest_results_"):
        suffix = suffix[len("backtest_results_"):]
    return base.with_name(f"{base.stem}_{_dot_free_stem(suffix)}.xlsx")


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