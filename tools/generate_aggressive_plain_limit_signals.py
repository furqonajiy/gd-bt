#!/usr/bin/env python3
"""Generate aggressive plain-LIMIT XAUUSD signals from M1 ELEV8 charts.

IMPORTANT
---------
This is an in-sample research generator. It deliberately builds many historical
candidate LIMIT signals, replays each candidate on the same chart window, and
keeps only historical winners. That makes it useful for stress-testing the
plain LIMIT executor, but it is NOT a live-ready predictive strategy.

The generated file is deterministic for the same input chart files and reproduces
``generated/aggressive_plain_limit.txt`` from the research run.

Expected command:

    python tools/generate_aggressive_plain_limit_signals.py ^
      --charts data/XAUUSD_M1_*.csv ^
      --output generated/aggressive_plain_limit.txt ^
      --summary reports/aggressive_plain_limit_generator_summary.json

The output signal format is compatible with xauusd_trading.parse_signals_file.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

try:
    from numba import njit
except Exception:  # pragma: no cover - slow fallback for environments without numba
    def njit(*args, **kwargs):
        if args and callable(args[0]):
            return args[0]
        def decorate(fn):
            return fn
        return decorate

POINT_VALUE = 0.01
CONTRACT_SIZE_OZ = 100.0
INITIAL_CAPITAL = 50_000.0
RISK_PER_SIGNAL = 0.02
LOT_STEP = 0.01
MINIMUM_LOT = 0.01
BONUS_PER_CLOSED_LOT = 3.0
SL_MULTIPLIER = 0.85
ENTRY_COUNT = 2
PENDING_EXPIRY_MINUTES = 630
MAX_HOLD_MINUTES = 60
REPLAY_EXTRA_MINUTES = 5
KEEP_PER_DAY = 25
SIGNAL_START = "2025-01-01"
SIGNAL_END_CUTOFF = "2026-06-03T00:00:00"

# offset, TP1 distance, TP2 distance, TP3 distance, SL gap below/above 2-dollar range
PARAM_GRID = np.array([
    [1.0, 2.0, 4.0, 6.0, 2.0],
    [1.5, 2.5, 5.0, 7.5, 2.0],
    [2.0, 3.0, 6.0, 9.0, 2.5],
    [3.0, 4.0, 8.0, 12.0, 3.0],
    [4.0, 5.0, 10.0, 15.0, 3.0],
], dtype=float)


@dataclass(frozen=True)
class CandidateMeta:
    signal_time: datetime
    side: int  # 1 BUY, -1 SELL
    range_high: float
    range_low: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    entry0: float
    entry1: float


def _expand_paths(patterns: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in patterns:
        if any(ch in raw for ch in "*?["):
            matches = sorted(glob.glob(raw))
            if not matches:
                raise SystemExit(f"No chart files match: {raw}")
            paths.extend(Path(p) for p in matches)
        else:
            path = Path(raw)
            if not path.exists():
                raise SystemExit(f"Chart file not found: {raw}")
            paths.append(path)
    unique = sorted(set(paths))
    if not unique:
        raise SystemExit("No chart files provided")
    return unique


def _chart_source_priority(path: Path) -> int:
    stem = path.stem.upper()
    if stem.endswith("_ELEV8"):
        return 20
    if stem.endswith("_INTERNET"):
        return 10
    return 0


def load_chart(paths: Iterable[str]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for input_order, path in enumerate(_expand_paths(paths)):
        df = pd.read_csv(path, sep="\t")
        df.columns = [c.strip("<>").upper() for c in df.columns]
        required = {"DATE", "TIME", "OPEN", "HIGH", "LOW", "CLOSE", "SPREAD"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"{path} missing columns: {sorted(missing)}")
        df["time"] = pd.to_datetime(
            df["DATE"].astype(str) + " " + df["TIME"].astype(str),
            format="%Y.%m.%d %H:%M:%S",
            )
        for col in ("OPEN", "HIGH", "LOW", "CLOSE", "SPREAD"):
            df[col.lower()] = pd.to_numeric(df[col], errors="coerce")
        df["spread_price"] = df["spread"] * POINT_VALUE
        df["source_priority"] = _chart_source_priority(path)
        df["input_order"] = input_order
        frames.append(df[[
            "time", "open", "high", "low", "close", "spread", "spread_price",
            "source_priority", "input_order",
        ]])
    chart = pd.concat(frames, ignore_index=True).dropna(
        subset=["time", "open", "high", "low", "close", "spread_price"]
    )
    chart = chart.sort_values(["time", "source_priority", "input_order"])
    chart = chart.drop_duplicates("time", keep="last")
    return chart.sort_values("time").reset_index(drop=True)


@njit(cache=True)
def _eval_one(
        start_idx: int,
        end_idx: int,
        expiry_idx: int,
        side: int,
        entry0: float,
        entry1: float,
        signal_sl: float,
        tp1: float,
        tp2: float,
        tp3: float,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        spreads: np.ndarray,
):
    if side == 1:
        base_stop = (entry0 - signal_sl) * SL_MULTIPLIER
        initial_sl0 = entry0 - base_stop
        initial_sl1 = entry1 - base_stop
    else:
        base_stop = (signal_sl - entry0) * SL_MULTIPLIER
        initial_sl0 = entry0 + base_stop
        initial_sl1 = entry1 + base_stop

    status0 = 0  # pending=0, open=1, no-fill=2, closed=3
    status1 = 0
    armed0 = False
    armed1 = False
    fill0 = -1
    fill1 = -1
    pnl0 = 0.0
    pnl1 = 0.0
    first_fill = -1
    time_exit_idx = -1
    stage = 0
    stage1_idx = -1
    stage2_idx = -1
    closed_count = 0

    for bar_idx in range(start_idx, end_idx):
        spread = spreads[bar_idx]
        high = highs[bar_idx]
        low = lows[bar_idx]
        open_ = opens[bar_idx]
        close = closes[bar_idx]

        if bar_idx <= expiry_idx:
            if status0 == 0:
                if side == 1:
                    opened_safe = (open_ + spread) > entry0
                    returned_safe = (high + spread) > entry0
                    fill = low <= entry0 - spread
                else:
                    opened_safe = open_ < entry0
                    returned_safe = low < entry0
                    fill = high >= entry0
                if not armed0:
                    if opened_safe:
                        armed0 = True
                    elif returned_safe:
                        armed0 = True
                        fill = False
                    else:
                        fill = False
                if fill:
                    status0 = 1
                    fill0 = bar_idx
                    if first_fill < 0:
                        first_fill = bar_idx
                        time_exit_idx = bar_idx + MAX_HOLD_MINUTES

            if status1 == 0:
                if side == 1:
                    opened_safe = (open_ + spread) > entry1
                    returned_safe = (high + spread) > entry1
                    fill = low <= entry1 - spread
                else:
                    opened_safe = open_ < entry1
                    returned_safe = low < entry1
                    fill = high >= entry1
                if not armed1:
                    if opened_safe:
                        armed1 = True
                    elif returned_safe:
                        armed1 = True
                        fill = False
                    else:
                        fill = False
                if fill:
                    status1 = 1
                    fill1 = bar_idx
                    if first_fill < 0:
                        first_fill = bar_idx
                        time_exit_idx = bar_idx + MAX_HOLD_MINUTES

        if bar_idx > expiry_idx:
            if status0 == 0:
                status0 = 2
            if status1 == 0:
                status1 = 2

        if status0 == 1 or status1 == 1:
            if side == 1:
                tp1_hit = high >= tp1
                tp2_hit = high >= tp2
                tp3_hit = high >= tp3
            else:
                tp1_hit = low <= tp1 - spread
                tp2_hit = low <= tp2 - spread
                tp3_hit = low <= tp3 - spread

            if status0 == 1:
                lock_stage = 0
                if stage >= 2 and stage2_idx >= 0 and fill0 < stage2_idx:
                    lock_stage = 2
                elif stage >= 1 and stage1_idx >= 0 and fill0 < stage1_idx:
                    lock_stage = 1
                stop = initial_sl0
                if lock_stage >= 2:
                    stop = tp2
                elif lock_stage >= 1:
                    stop = tp1
                stop_hit = low <= stop if side == 1 else high >= stop - spread
                if stop_hit:
                    pnl0 = stop - entry0 if side == 1 else entry0 - stop
                    status0 = 3
                    closed_count += 1
                elif tp3_hit and fill0 < bar_idx:
                    pnl0 = tp3 - entry0 if side == 1 else entry0 - tp3
                    status0 = 3
                    closed_count += 1

            if status1 == 1:
                lock_stage = 0
                if stage >= 2 and stage2_idx >= 0 and fill1 < stage2_idx:
                    lock_stage = 2
                elif stage >= 1 and stage1_idx >= 0 and fill1 < stage1_idx:
                    lock_stage = 1
                stop = initial_sl1
                if lock_stage >= 2:
                    stop = tp2
                elif lock_stage >= 1:
                    stop = tp1
                stop_hit = low <= stop if side == 1 else high >= stop - spread
                if stop_hit:
                    pnl1 = stop - entry1 if side == 1 else entry1 - stop
                    status1 = 3
                    closed_count += 1
                elif tp3_hit and fill1 < bar_idx:
                    pnl1 = tp3 - entry1 if side == 1 else entry1 - tp3
                    status1 = 3
                    closed_count += 1

            stageable = (status0 == 1 and fill0 < bar_idx) or (status1 == 1 and fill1 < bar_idx)
            if stageable:
                if stage1_idx < 0 and tp1_hit:
                    stage1_idx = bar_idx
                if stage2_idx < 0 and tp2_hit:
                    stage2_idx = bar_idx
                if stage < 1 and stage1_idx >= 0:
                    stage = 1
                if stage < 2 and stage2_idx >= 0:
                    stage = 2

        if time_exit_idx >= 0 and bar_idx >= time_exit_idx:
            exit_price = close if side == 1 else close + spread
            if status0 == 1:
                pnl0 = exit_price - entry0 if side == 1 else entry0 - exit_price
                status0 = 3
                closed_count += 1
            if status1 == 1:
                pnl1 = exit_price - entry1 if side == 1 else entry1 - exit_price
                status1 = 3
                closed_count += 1

        if (status0 == 2 or status0 == 3) and (status1 == 2 or status1 == 3):
            break

    if status0 == 0:
        status0 = 2
    if status1 == 0:
        status1 = 2

    filled = fill0 >= 0 or fill1 >= 0
    fixed_001_trading_pnl = pnl0 + pnl1
    if not filled:
        return 0.0, 0, 0
    if fixed_001_trading_pnl > 1e-9:
        return fixed_001_trading_pnl, 1, closed_count
    if fixed_001_trading_pnl < -1e-9:
        return fixed_001_trading_pnl, -1, closed_count
    return fixed_001_trading_pnl, 0, closed_count


@njit(cache=True)
def _eval_batch(
        start_idxs: np.ndarray,
        end_idxs: np.ndarray,
        expiry_idxs: np.ndarray,
        sides: np.ndarray,
        entry0s: np.ndarray,
        entry1s: np.ndarray,
        sls: np.ndarray,
        tp1s: np.ndarray,
        tp2s: np.ndarray,
        tp3s: np.ndarray,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        spreads: np.ndarray,
):
    n = len(start_idxs)
    pnls = np.zeros(n, dtype=np.float64)
    statuses = np.zeros(n, dtype=np.int8)
    closed = np.zeros(n, dtype=np.int8)
    for i in range(n):
        pnl, status, closed_count = _eval_one(
            start_idxs[i], end_idxs[i], expiry_idxs[i], sides[i],
            entry0s[i], entry1s[i], sls[i], tp1s[i], tp2s[i], tp3s[i],
            opens, highs, lows, closes, spreads,
        )
        pnls[i] = pnl
        statuses[i] = status
        closed[i] = closed_count
    return pnls, statuses, closed


def _fmt_price(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_signals(selected: list[tuple[float, int, CandidateMeta]]) -> str:
    by_day: dict[str, list[CandidateMeta]] = {}
    for _, _, meta in selected:
        by_day.setdefault(meta.signal_time.strftime("%Y-%m-%d"), []).append(meta)

    lines: list[str] = []
    for day in sorted(by_day):
        lines.append(f"{day} GMT+3")
        day_items = sorted(by_day[day], key=lambda m: (m.signal_time, m.side, m.range_high, m.range_low))
        for day_id, meta in enumerate(day_items, start=1):
            side_text = "BUY" if meta.side == 1 else "SELL"
            time_text = meta.signal_time.strftime("%I:%M %p").lstrip("0")
            if meta.side == 1:
                r1, r2 = meta.range_high, meta.range_low
            else:
                r1, r2 = meta.range_low, meta.range_high
            lines.append(
                f"{day_id}. {side_text} XAUUSD {_fmt_price(r1)} - {_fmt_price(r2)} "
                f"SL {_fmt_price(meta.sl)} TP1 {_fmt_price(meta.tp1)} "
                f"TP2 {_fmt_price(meta.tp2)} TP3 {_fmt_price(meta.tp3)} {time_text}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_candidates(chart: pd.DataFrame):
    opens = chart["open"].to_numpy(np.float64)
    highs = chart["high"].to_numpy(np.float64)
    lows = chart["low"].to_numpy(np.float64)
    closes = chart["close"].to_numpy(np.float64)
    spreads = chart["spread_price"].to_numpy(np.float64)
    spread_points = chart["spread"].to_numpy()
    times = chart["time"].to_numpy("datetime64[ns]")
    time_int = times.astype("datetime64[ns]").astype(np.int64)
    minutes = chart["time"].dt.minute.to_numpy()

    ema21 = pd.Series(closes).ewm(span=21, adjust=False).mean().to_numpy()
    ema55 = pd.Series(closes).ewm(span=55, adjust=False).mean().to_numpy()
    ema144 = pd.Series(closes).ewm(span=144, adjust=False).mean().to_numpy()

    end_cut = np.datetime64(SIGNAL_END_CUTOFF).astype("datetime64[ns]").astype(np.int64)
    base_idxs = np.where((minutes % 15 == 0) & (time_int <= end_cut) & (spread_points <= 40))[0]
    base_idxs = base_idxs[base_idxs + 1 < len(chart)]
    base_idxs = base_idxs[time_int[base_idxs + 1] - time_int[base_idxs] == 60_000_000_000]

    start_idxs: list[int] = []
    end_idxs: list[int] = []
    expiry_idxs: list[int] = []
    sides: list[int] = []
    entry0s: list[float] = []
    entry1s: list[float] = []
    sls: list[float] = []
    tp1s: list[float] = []
    tp2s: list[float] = []
    tp3s: list[float] = []
    meta: list[CandidateMeta] = []

    for idx in base_idxs:
        close = float(closes[idx])
        side_pref: list[int] = []
        if ema21[idx] > ema55[idx] > ema144[idx]:
            side_pref.append(1)
        if ema21[idx] < ema55[idx] < ema144[idx]:
            side_pref.append(-1)
        if close - ema21[idx] > 8:
            side_pref.append(-1)
        if ema21[idx] - close > 8:
            side_pref.append(1)
        if not side_pref:
            continue

        signal_idx = idx + 1
        signal_ns = int(time_int[signal_idx])
        signal_time = pd.Timestamp(times[signal_idx]).to_pydatetime()
        expiry_idx = int(np.searchsorted(time_int, signal_ns + PENDING_EXPIRY_MINUTES * 60_000_000_000, side="right") - 1)
        end_idx = int(np.searchsorted(time_int, signal_ns + (PENDING_EXPIRY_MINUTES + MAX_HOLD_MINUTES + REPLAY_EXTRA_MINUTES) * 60_000_000_000, side="right"))

        for side in dict.fromkeys(side_pref):
            for offset, tp1_dist, tp2_dist, tp3_dist, sl_gap in PARAM_GRID:
                if side == 1:
                    range_high = round(close - offset, 2)
                    range_low = round(range_high - 2.0, 2)
                    sl = round(range_low - sl_gap, 2)
                    tp1 = round(range_high + tp1_dist, 2)
                    tp2 = round(range_high + tp2_dist, 2)
                    tp3 = round(range_high + tp3_dist, 2)
                    entry0 = range_high
                    entry1 = range_low
                else:
                    range_low = round(close + offset, 2)
                    range_high = round(range_low + 2.0, 2)
                    sl = round(range_high + sl_gap, 2)
                    tp1 = round(range_low - tp1_dist, 2)
                    tp2 = round(range_low - tp2_dist, 2)
                    tp3 = round(range_low - tp3_dist, 2)
                    entry0 = range_low
                    entry1 = range_high

                start_idxs.append(signal_idx)
                end_idxs.append(end_idx)
                expiry_idxs.append(expiry_idx)
                sides.append(side)
                entry0s.append(entry0)
                entry1s.append(entry1)
                sls.append(sl)
                tp1s.append(tp1)
                tp2s.append(tp2)
                tp3s.append(tp3)
                meta.append(CandidateMeta(signal_time, side, range_high, range_low, sl, tp1, tp2, tp3, entry0, entry1))

    arrays = (
        np.array(start_idxs, dtype=np.int64),
        np.array(end_idxs, dtype=np.int64),
        np.array(expiry_idxs, dtype=np.int64),
        np.array(sides, dtype=np.int8),
        np.array(entry0s, dtype=np.float64),
        np.array(entry1s, dtype=np.float64),
        np.array(sls, dtype=np.float64),
        np.array(tp1s, dtype=np.float64),
        np.array(tp2s, dtype=np.float64),
        np.array(tp3s, dtype=np.float64),
    )
    market_arrays = (opens, highs, lows, closes, spreads)
    return arrays, market_arrays, meta


def _select_winners(pnls: np.ndarray, statuses: np.ndarray, closed: np.ndarray, meta: list[CandidateMeta]):
    by_day: dict[str, list[tuple[float, int, CandidateMeta]]] = {}
    for idx in np.where((statuses == 1) & (pnls > 0.02))[0]:
        item = meta[int(idx)]
        by_day.setdefault(item.signal_time.strftime("%Y-%m-%d"), []).append((float(pnls[int(idx)]), int(closed[int(idx)]), item))

    selected: list[tuple[float, int, CandidateMeta]] = []
    for day, items in sorted(by_day.items()):
        items = sorted(items, key=lambda x: x[0], reverse=True)
        selected.extend(items[:KEEP_PER_DAY])
    return sorted(selected, key=lambda x: (x[2].signal_time, x[2].side, x[2].range_high, x[2].range_low))


def _floor_lot(raw_lot: float) -> float:
    lot = math.floor(raw_lot / LOT_STEP + 1e-9) * LOT_STEP
    lot = round(lot, 2)
    return lot if lot >= MINIMUM_LOT - 1e-9 else 0.0


def _summarize(selected: list[tuple[float, int, CandidateMeta]]) -> dict:
    equity = INITIAL_CAPITAL
    peak = INITIAL_CAPITAL
    max_dd = 0.0
    rows: list[dict] = []
    fixed_net = 0.0
    fixed_monthly: dict[str, dict] = {}
    monthly: dict[str, dict] = {}

    for fixed_001_trading_pnl, closed_count, meta in selected:
        raw_stop = meta.entry0 - meta.sl if meta.side == 1 else meta.sl - meta.entry0
        base_stop = raw_stop * SL_MULTIPLIER
        total_price_risk = ENTRY_COUNT * base_stop
        lot = _floor_lot((equity * RISK_PER_SIGNAL) / (total_price_risk * CONTRACT_SIZE_OZ)) if total_price_risk > 0 else 0.0
        trading_pnl = fixed_001_trading_pnl * (lot / 0.01)
        bonus = closed_count * lot * BONUS_PER_CLOSED_LOT
        pnl = trading_pnl + bonus
        before = equity
        equity += pnl
        if equity > peak:
            peak = equity
        drawdown = (equity - peak) / peak * 100.0 if peak > 0 else 0.0
        if drawdown < max_dd:
            max_dd = drawdown

        month = meta.signal_time.strftime("%Y-%m")
        bucket = monthly.setdefault(month, {
            "month": month, "signals": 0, "pnl": 0.0, "trading_pnl": 0.0,
            "bonus": 0.0, "closed_lots": 0.0, "wins": 0, "losses": 0,
            "breakevens": 0, "no_fills": 0,
        })
        bucket["signals"] += 1
        bucket["wins"] += 1
        bucket["pnl"] += pnl
        bucket["trading_pnl"] += trading_pnl
        bucket["bonus"] += bonus
        bucket["closed_lots"] += closed_count * lot

        fixed_bonus = closed_count * 0.01 * BONUS_PER_CLOSED_LOT
        fixed_total = fixed_001_trading_pnl + fixed_bonus
        fixed_net += fixed_total
        fb = fixed_monthly.setdefault(month, {"month": month, "signals": 0, "pnl": 0.0, "trading_pnl": 0.0, "bonus": 0.0})
        fb["signals"] += 1
        fb["pnl"] += fixed_total
        fb["trading_pnl"] += fixed_001_trading_pnl
        fb["bonus"] += fixed_bonus

        rows.append({
            "time": meta.signal_time.isoformat(sep=" "),
            "side": "BUY" if meta.side == 1 else "SELL",
            "lot": lot,
            "equity_before": before,
            "equity_after": equity,
            "pnl": pnl,
            "trading_pnl": trading_pnl,
            "bonus": bonus,
            "closed_lots": closed_count * lot,
            "status": "WIN",
        })

    return {
        "method": "in_sample_hindsight_filtered_plain_limit_research",
        "start_date": SIGNAL_START,
        "risk_per_signal": RISK_PER_SIGNAL,
        "trailing_open_distance": 0.0,
        "trailing_close_distance": 0.0,
        "signals": len(selected),
        "wins": len(selected),
        "losses": 0,
        "breakevens": 0,
        "no_fills": 0,
        "win_rate_pct": 100.0 if selected else 0.0,
        "risk_final_equity": equity,
        "risk_net_profit": equity - INITIAL_CAPITAL,
        "risk_max_drawdown_pct": max_dd,
        "fixed_lot_net_profit": fixed_net,
        "monthly": sorted(monthly.values(), key=lambda x: x["month"]),
        "fixed_monthly": sorted(fixed_monthly.values(), key=lambda x: x["month"]),
        "rows": rows,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate aggressive in-sample plain LIMIT XAUUSD signals.")
    parser.add_argument("--charts", nargs="+", required=True, help="Chart CSV paths/globs, e.g. data/XAUUSD_M1_*.csv")
    parser.add_argument("--output", default="generated/aggressive_plain_limit.txt")
    parser.add_argument("--summary", default="reports/aggressive_plain_limit_generator_summary.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    chart = load_chart(args.charts)
    chart = chart[chart["time"] >= pd.Timestamp(SIGNAL_START)].reset_index(drop=True)

    candidate_arrays, market_arrays, meta = _build_candidates(chart)
    pnls, statuses, closed = _eval_batch(*candidate_arrays, *market_arrays)
    selected = _select_winners(pnls, statuses, closed, meta)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_signals(selected), encoding="utf-8")

    summary = _summarize(selected)
    summary_path = Path(args.summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    compact = {k: summary[k] for k in [
        "signals", "risk_net_profit", "risk_final_equity", "risk_max_drawdown_pct",
        "wins", "losses", "breakevens", "no_fills", "win_rate_pct", "fixed_lot_net_profit",
    ]}
    print(json.dumps(compact, indent=2))
    print(f"Wrote signals to {output.resolve()}")
    print(f"Wrote summary to {summary_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())