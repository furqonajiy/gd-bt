#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

POINT_VALUE = 0.01
CONTRACT_SIZE_OZ = 100.0
TERMINAL = {"NO_FILL", "SL", "LOCK_TP1", "LOCK_TP2", "TP3", "TIME_EXIT", "TRAILING_STOP"}


@dataclass(frozen=True)
class Signal:
    source_date: str
    day_id: int
    signal_time_chart: pd.Timestamp
    side: str
    r1: float
    r2: float
    sl: float
    tp1: float
    tp2: float
    tp3: float

    @property
    def range_high(self) -> float:
        return max(self.r1, self.r2)

    @property
    def range_low(self) -> float:
        return min(self.r1, self.r2)

    @property
    def signal_key(self) -> str:
        return f"{self.source_date}#{self.day_id:02d}"


_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+GMT\s*([+-])\s*(\d+)$", re.I)
_SIGNAL_RE = re.compile(
    r"^\s*(\d+)\.\s*(BUY|SELL)\s+XAUUSD\s+"
    r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s+"
    r"SL\s+(\d+(?:\.\d+)?)\s+"
    r"TP1\s+(\d+(?:\.\d+)?)\s+"
    r"TP2\s+(\d+(?:\.\d+)?)\s+"
    r"TP3\s+(\d+(?:\.\d+)?)\s+"
    r"(\d{1,2}:\d{2}\s*[AP]M)\s*$",
    re.I,
)


def _floor_to_step(value: float, step: float) -> float:
    steps = math.floor(value / step + 1e-9)
    return round(steps * step, 2)


def _fmt_price(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text


def _fmt_time(t: pd.Timestamp) -> str:
    return t.strftime("%-I:%M %p") if "%-I" in datetime.now().strftime("%-I") else t.strftime("%I:%M %p").lstrip("0")


def load_chart(paths: list[str]) -> pd.DataFrame:
    frames = []
    for path in paths:
        raw = pd.read_csv(path, sep="\t")
        raw.columns = [c.strip("<>").upper() for c in raw.columns]
        missing = {"DATE", "TIME", "OPEN", "HIGH", "LOW", "CLOSE", "SPREAD"} - set(raw.columns)
        if missing:
            raise ValueError(f"{path} missing columns: {sorted(missing)}")
        df = pd.DataFrame()
        df["time"] = pd.to_datetime(raw["DATE"].astype(str) + " " + raw["TIME"].astype(str), format="%Y.%m.%d %H:%M:%S")
        for col in ("OPEN", "HIGH", "LOW", "CLOSE", "SPREAD"):
            df[col.lower()] = pd.to_numeric(raw[col], errors="coerce")
        df["spread_price"] = df["spread"] * POINT_VALUE
        frames.append(df[["time", "open", "high", "low", "close", "spread_price"]])
    out = pd.concat(frames, ignore_index=True).dropna()
    return out.drop_duplicates("time", keep="last").sort_values("time").reset_index(drop=True)


def parse_signals_file(path: Path) -> list[Signal]:
    signals: list[Signal] = []
    source_date: str | None = None
    source_offset: int | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        date_match = _DATE_RE.match(line)
        if date_match:
            source_date = date_match.group(1)
            offset = int(date_match.group(3))
            source_offset = offset if date_match.group(2) == "+" else -offset
            continue
        signal_match = _SIGNAL_RE.match(line)
        if not signal_match or source_date is None or source_offset is None:
            continue
        source_time = datetime.strptime(
            f"{source_date} {signal_match.group(9).upper().replace(' ', '')}",
            "%Y-%m-%d %I:%M%p",
        )
        chart_time = pd.Timestamp(source_time) + pd.Timedelta(hours=3 - source_offset)
        signals.append(Signal(
            source_date=source_date,
            day_id=int(signal_match.group(1)),
            signal_time_chart=chart_time,
            side=signal_match.group(2).upper(),
            r1=float(signal_match.group(3)),
            r2=float(signal_match.group(4)),
            sl=float(signal_match.group(5)),
            tp1=float(signal_match.group(6)),
            tp2=float(signal_match.group(7)),
            tp3=float(signal_match.group(8)),
        ))
    return sorted(signals, key=lambda s: (s.signal_time_chart, s.source_date, s.day_id))


class LimitBacktester:
    def __init__(self, chart: pd.DataFrame) -> None:
        self.chart = chart
        self.times = chart["time"].to_numpy(dtype="datetime64[ns]")
        self.opens = chart["open"].to_numpy(float)
        self.highs = chart["high"].to_numpy(float)
        self.lows = chart["low"].to_numpy(float)
        self.closes = chart["close"].to_numpy(float)
        self.spreads = chart["spread_price"].to_numpy(float)
        self.chart_start = pd.Timestamp(chart["time"].iloc[0])
        self.chart_end = pd.Timestamp(chart["time"].iloc[-1])

    def _index_ge(self, t: pd.Timestamp) -> int:
        return int(self.times.searchsorted(t.to_datetime64(), side="left"))

    def _index_gt(self, t: pd.Timestamp) -> int:
        return int(self.times.searchsorted(t.to_datetime64(), side="right"))

    def replay_one(
        self,
        sig: Signal,
        equity: float,
        *,
        risk: float = 0.02,
        fixed_lot: float | None = None,
        entries: int = 2,
        sl_multiplier: float = 0.85,
        pending_expiry_minutes: int = 630,
        max_hold_minutes: int = 60,
        trailing_close_distance: float = 0.5,
        bonus_per_closed_lot: float = 3.0,
    ) -> dict | None:
        if sig.signal_time_chart < self.chart_start or sig.signal_time_chart > self.chart_end:
            return None
        entry_prices = [sig.range_high, sig.range_low] if sig.side == "BUY" else [sig.range_low, sig.range_high]
        entry_prices = entry_prices[:entries]
        first_entry = entry_prices[0]
        raw_stop_distance = first_entry - sig.sl if sig.side == "BUY" else sig.sl - first_entry
        base_stop_distance = raw_stop_distance * sl_multiplier
        if base_stop_distance <= 0:
            return None
        initial_stops = [e - base_stop_distance if sig.side == "BUY" else e + base_stop_distance for e in entry_prices]
        total_price_risk = sum(abs(e - s) for e, s in zip(entry_prices, initial_stops))
        if fixed_lot is None:
            lot = _floor_to_step(equity * risk / (total_price_risk * CONTRACT_SIZE_OZ), 0.01)
        else:
            lot = fixed_lot
        if lot < 0.01:
            lot = 0.0

        status = ["PENDING"] * entries
        armed = [False] * entries
        fill_time: list[pd.Timestamp | None] = [None] * entries
        exit_time: list[pd.Timestamp | None] = [None] * entries
        pnl: list[float | None] = [None] * entries
        trailing_stop: list[float | None] = [None] * entries

        activation = sig.signal_time_chart
        expiry = activation + pd.Timedelta(minutes=pending_expiry_minutes)
        replay_end = min(expiry + pd.Timedelta(minutes=max_hold_minutes + 5), self.chart_end)
        first_fill_time: pd.Timestamp | None = None
        time_exit_deadline: pd.Timestamp | None = None
        stage = 0
        stage1_time: pd.Timestamp | None = None
        stage2_time: pd.Timestamp | None = None

        for idx in range(self._index_ge(activation), self._index_gt(replay_end)):
            t = pd.Timestamp(self.times[idx])
            op = self.opens[idx]
            high = self.highs[idx]
            low = self.lows[idx]
            close = self.closes[idx]
            spread = self.spreads[idx]

            if activation <= t <= expiry:
                for i, entry in enumerate(entry_prices):
                    if status[i] != "PENDING":
                        continue
                    if sig.side == "BUY":
                        opened_safe = op + spread > entry
                        returned_safe = high + spread > entry
                    else:
                        opened_safe = op < entry
                        returned_safe = low < entry
                    if not armed[i]:
                        if opened_safe:
                            armed[i] = True
                        elif returned_safe:
                            armed[i] = True
                            continue
                        else:
                            continue
                    fill_hit = low <= entry - spread if sig.side == "BUY" else high >= entry
                    if fill_hit:
                        status[i] = "OPEN"
                        fill_time[i] = t
                        if first_fill_time is None:
                            first_fill_time = t
                            time_exit_deadline = t + pd.Timedelta(minutes=max_hold_minutes)

            if t > expiry:
                for i in range(entries):
                    if status[i] == "PENDING":
                        status[i] = "NO_FILL"

            if any(s == "OPEN" for s in status):
                if sig.side == "BUY":
                    tp1_hit = high >= sig.tp1
                    tp2_hit = high >= sig.tp2
                    tp3_hit = high >= sig.tp3
                else:
                    tp1_hit = low <= sig.tp1 - spread
                    tp2_hit = low <= sig.tp2 - spread
                    tp3_hit = low <= sig.tp3 - spread
                for i, entry in enumerate(entry_prices):
                    if status[i] != "OPEN":
                        continue
                    stop = initial_stops[i]
                    if stage >= 2 and fill_time[i] is not None and stage2_time is not None and fill_time[i] < stage2_time:
                        stop = sig.tp2
                    elif stage >= 1 and fill_time[i] is not None and stage1_time is not None and fill_time[i] < stage1_time:
                        stop = sig.tp1
                    if trailing_stop[i] is not None:
                        stop = max(stop, trailing_stop[i]) if sig.side == "BUY" else min(stop, trailing_stop[i])
                    stop_hit = low <= stop if sig.side == "BUY" else high >= stop - spread
                    if stop_hit:
                        if trailing_stop[i] is not None and abs(stop - trailing_stop[i]) < 1e-9:
                            status[i] = "TRAILING_STOP"
                        elif stage >= 2 and fill_time[i] is not None and stage2_time is not None and fill_time[i] < stage2_time:
                            status[i] = "LOCK_TP2"
                        elif stage >= 1 and fill_time[i] is not None and stage1_time is not None and fill_time[i] < stage1_time:
                            status[i] = "LOCK_TP1"
                        else:
                            status[i] = "SL"
                        exit_time[i] = t
                        pnl[i] = (stop - entry if sig.side == "BUY" else entry - stop) * lot * CONTRACT_SIZE_OZ
                    elif tp3_hit and fill_time[i] is not None and fill_time[i] < t:
                        status[i] = "TP3"
                        exit_time[i] = t
                        pnl[i] = (sig.tp3 - entry if sig.side == "BUY" else entry - sig.tp3) * lot * CONTRACT_SIZE_OZ

                stageable = [i for i, s in enumerate(status) if s == "OPEN" and fill_time[i] is not None and fill_time[i] < t]
                if stageable:
                    if stage1_time is None and tp1_hit:
                        stage1_time = t
                    if stage2_time is None and tp2_hit:
                        stage2_time = t
                    if stage < 1 and stage1_time is not None:
                        stage = 1
                    if stage < 2 and stage2_time is not None:
                        stage = 2
                for i in stageable:
                    entry = entry_prices[i]
                    if sig.side == "BUY":
                        candidate = high - trailing_close_distance
                        if candidate > entry:
                            trailing_stop[i] = candidate if trailing_stop[i] is None else max(trailing_stop[i], candidate)
                    else:
                        candidate = low + trailing_close_distance
                        if candidate < entry:
                            trailing_stop[i] = candidate if trailing_stop[i] is None else min(trailing_stop[i], candidate)

            if time_exit_deadline is not None and t >= time_exit_deadline:
                exit_price = close if sig.side == "BUY" else close + spread
                for i, entry in enumerate(entry_prices):
                    if status[i] == "OPEN":
                        status[i] = "TIME_EXIT"
                        exit_time[i] = t
                        pnl[i] = (exit_price - entry if sig.side == "BUY" else entry - exit_price) * lot * CONTRACT_SIZE_OZ

            if all(s in TERMINAL for s in status):
                break

        if replay_end >= expiry:
            status = ["NO_FILL" if s == "PENDING" else s for s in status]
        filled = [i for i, t in enumerate(fill_time) if t is not None]
        if any(s in {"OPEN", "PENDING"} for s in status):
            signal_status = "OPEN"
            trading_pnl = 0.0
            closed_lots = 0.0
            net_pnl = None
        elif not filled:
            signal_status = "NO_FILL"
            trading_pnl = 0.0
            closed_lots = 0.0
            net_pnl = 0.0
        else:
            trading_pnl = sum(v for v in pnl if v is not None)
            signal_status = "WIN" if trading_pnl > 0 else "LOSS" if trading_pnl < 0 else "BREAKEVEN"
            closed_lots = sum(lot for i, t in enumerate(fill_time) if t is not None and exit_time[i] is not None)
            net_pnl = trading_pnl + closed_lots * bonus_per_closed_lot
        return {
            "signal": sig,
            "status": signal_status,
            "pnl": net_pnl,
            "trading_pnl": trading_pnl,
            "bonus": closed_lots * bonus_per_closed_lot,
            "closed_lots": closed_lots,
            "entry_statuses": status,
        }

    def run(self, signals: list[Signal], *, initial_capital: float, risk: float, fixed_lot: float | None = None) -> dict:
        equity = initial_capital
        peak = initial_capital
        max_drawdown_pct = 0.0
        rows = []
        for sig in sorted(signals, key=lambda s: (s.signal_time_chart, s.source_date, s.day_id)):
            replay = self.replay_one(sig, equity, risk=risk, fixed_lot=fixed_lot)
            if replay is None:
                continue
            pnl = replay["pnl"]
            equity_after = equity if pnl is None else equity + pnl
            row = {
                "time": sig.signal_time_chart,
                "side": sig.side,
                "status": replay["status"],
                "pnl": pnl,
                "trading_pnl": replay["trading_pnl"],
                "bonus": replay["bonus"],
                "equity_before": equity,
                "equity_after": equity_after,
            }
            rows.append(row)
            if pnl is not None:
                equity = equity_after
            peak = max(peak, equity)
            drawdown = (equity - peak) / peak * 100.0 if peak > 0 else 0.0
            max_drawdown_pct = min(max_drawdown_pct, drawdown)
            if equity <= 0:
                break
        return summarize_rows(rows, initial_capital, equity, max_drawdown_pct)


def summarize_rows(rows: list[dict], initial_capital: float, final_equity: float, max_drawdown_pct: float) -> dict:
    wins = sum(r["status"] == "WIN" for r in rows)
    losses = sum(r["status"] == "LOSS" for r in rows)
    monthly: dict[str, dict] = {}
    for r in rows:
        key = pd.Timestamp(r["time"]).strftime("%Y-%m")
        if key not in monthly:
            monthly[key] = {
                "month": key,
                "signals": 0,
                "wins": 0,
                "losses": 0,
                "breakevens": 0,
                "no_fills": 0,
                "open": 0,
                "pnl": 0.0,
                "trading_pnl": 0.0,
                "bonus": 0.0,
                "equity_start": r["equity_before"],
                "equity_end": r["equity_before"],
            }
        bucket = monthly[key]
        bucket["signals"] += 1
        bucket["equity_end"] = r["equity_after"]
        if r["status"] == "WIN":
            bucket["wins"] += 1
        elif r["status"] == "LOSS":
            bucket["losses"] += 1
        elif r["status"] == "BREAKEVEN":
            bucket["breakevens"] += 1
        elif r["status"] == "NO_FILL":
            bucket["no_fills"] += 1
        elif r["status"] == "OPEN":
            bucket["open"] += 1
        if r["pnl"] is not None:
            bucket["pnl"] += r["pnl"]
            bucket["trading_pnl"] += r["trading_pnl"] or 0.0
            bucket["bonus"] += r["bonus"] or 0.0
    monthly_rows = [monthly[k] for k in sorted(monthly)]
    for bucket in monthly_rows:
        wl = bucket["wins"] + bucket["losses"]
        bucket["win_rate_pct"] = bucket["wins"] / wl * 100.0 if wl else 0.0
        bucket["pnl_pct"] = bucket["pnl"] / bucket["equity_start"] * 100.0 if bucket["equity_start"] else 0.0
    return {
        "signals_included": len(rows),
        "final_equity": final_equity,
        "net_profit": final_equity - initial_capital,
        "wins": wins,
        "losses": losses,
        "breakevens": sum(r["status"] == "BREAKEVEN" for r in rows),
        "no_fills": sum(r["status"] == "NO_FILL" for r in rows),
        "open": sum(r["status"] == "OPEN" for r in rows),
        "win_rate_pct": wins / (wins + losses) * 100.0 if wins + losses else 0.0,
        "max_drawdown_pct": max_drawdown_pct,
        "monthly": monthly_rows,
        "rows": rows,
    }


def generate_candidates(chart: pd.DataFrame, start_date: str) -> list[Signal]:
    m1 = chart.set_index("time")
    m15 = m1.resample("15min", label="right", closed="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "spread_price": "last",
    }).dropna().reset_index()
    close = m15["close"]
    m15["ema21"] = close.ewm(span=21, adjust=False).mean()
    m15["ema55"] = close.ewm(span=55, adjust=False).mean()
    m15["ema200"] = close.ewm(span=200, adjust=False).mean()
    tr = pd.concat([
        m15["high"] - m15["low"],
        (m15["high"] - m15["close"].shift(1)).abs(),
        (m15["low"] - m15["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    m15["atr14"] = tr.rolling(14, min_periods=14).mean()
    m15["slope21"] = m15["ema21"] - m15["ema21"].shift(4)
    m15 = m15[m15["time"] >= pd.Timestamp(start_date)].copy()

    candidates: list[Signal] = []
    raw_id = 1
    for row in m15.itertuples(index=False):
        if pd.isna(row.atr14) or row.atr14 < 1.0:
            continue
        if row.time.hour < 1 or row.time.hour > 23:
            continue
        if row.time.minute != 0:
            continue
        trend_buy = row.ema21 > row.ema55 > row.ema200 and row.close >= row.ema21 and row.slope21 > 0
        trend_sell = row.ema21 < row.ema55 < row.ema200 and row.close <= row.ema21 and row.slope21 < 0
        pull_depths = (0.30, 0.90, 1.50)
        if trend_buy:
            for depth in pull_depths:
                high = round(float(row.close - depth), 2)
                low = round(high - 2.0, 2)
                sl = round(low - 3.5, 2)
                candidates.append(Signal("1970-01-01", raw_id, row.time, "BUY", high, low, sl, round(high + 4.0, 2), round(high + 7.0, 2), round(high + 12.0, 2)))
                raw_id += 1
        if trend_sell:
            for depth in pull_depths:
                low = round(float(row.close + depth), 2)
                high = round(low + 2.0, 2)
                sl = round(high + 3.5, 2)
                candidates.append(Signal("1970-01-01", raw_id, row.time, "SELL", low, high, sl, round(low - 4.0, 2), round(low - 7.0, 2), round(low - 12.0, 2)))
                raw_id += 1
    return candidates


def select_in_sample_winners(backtester: LimitBacktester, candidates: list[Signal]) -> list[Signal]:
    selected = []
    for candidate in candidates:
        replay = backtester.replay_one(candidate, 10_000.0, risk=0.02, fixed_lot=0.01)
        if replay is None:
            continue
        if replay["status"] == "WIN" and float(replay["pnl"] or 0.0) > 0.0:
            selected.append(candidate)
    return selected


def renumber_by_day(signals: list[Signal]) -> list[Signal]:
    out = []
    current_date = None
    day_id = 0
    for sig in sorted(signals, key=lambda s: (s.signal_time_chart, s.side, s.r1, s.r2)):
        date_text = sig.signal_time_chart.strftime("%Y-%m-%d")
        if date_text != current_date:
            current_date = date_text
            day_id = 1
        else:
            day_id += 1
        out.append(Signal(date_text, day_id, sig.signal_time_chart, sig.side, sig.r1, sig.r2, sig.sl, sig.tp1, sig.tp2, sig.tp3))
    return out


def write_signals(signals: list[Signal], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    current_date = None
    for sig in signals:
        date_text = sig.signal_time_chart.strftime("%Y-%m-%d")
        if date_text != current_date:
            if lines:
                lines.append("")
            lines.append(f"{date_text} GMT+3")
            current_date = date_text
        lines.append(
            f"{sig.day_id}. {sig.side} XAUUSD {_fmt_price(sig.r1)} - {_fmt_price(sig.r2)} "
            f"SL {_fmt_price(sig.sl)} TP1 {_fmt_price(sig.tp1)} TP2 {_fmt_price(sig.tp2)} TP3 {_fmt_price(sig.tp3)} "
            f"{_fmt_time(sig.signal_time_chart)}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_summary(path: Path, summary: dict, baseline: dict, previous: dict) -> None:
    lines = [
        "Aggressive plain-LIMIT in-sample research result",
        "",
        "WARNING: selected candidates are filtered by their own backtest outcome.",
        "This is an oracle/in-sample benchmark, not a live-ready non-lookahead strategy.",
        "",
        f"New risk net_profit: {summary['net_profit']:.2f}",
        f"New max_drawdown_pct: {summary['max_drawdown_pct']:.2f}",
        f"New signals: {summary['signals_included']}",
        f"New win_rate_pct: {summary['win_rate_pct']:.2f}",
        "",
        f"VICTOR risk net_profit: {baseline['net_profit']:.2f}",
        f"VICTOR max_drawdown_pct: {baseline['max_drawdown_pct']:.2f}",
        f"VICTOR signals: {baseline['signals_included']}",
        "",
        f"Previous generated risk net_profit: {previous['net_profit']:.2f}",
        f"Previous generated max_drawdown_pct: {previous['max_drawdown_pct']:.2f}",
        f"Previous generated signals: {previous['signals_included']}",
        "",
        "Monthly new result:",
    ]
    for row in summary["monthly"]:
        lines.append(f"{row['month']}: pnl={row['pnl']:.2f}, pnl_pct={row['pnl_pct']:.2f}, signals={row['signals']}, wins={row['wins']}, losses={row['losses']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate aggressive XAUUSD plain-LIMIT research signals and backtest them.")
    parser.add_argument("--charts", nargs="+", default=["data/XAUUSD_M1_*_ELEV8.csv"])
    parser.add_argument("--victor-signals", default="signals.txt")
    parser.add_argument("--previous-signals", default="generated/live_provider_all.txt")
    parser.add_argument("--output", default="generated/aggressive_limit_oracle_risk002.txt")
    parser.add_argument("--summary-output", default="reports/aggressive_limit_oracle_risk002_summary.txt")
    parser.add_argument("--start-date", default="2025-01-01")
    parser.add_argument("--initial-capital", type=float, default=10_000.0)
    parser.add_argument("--risk", type=float, default=0.02)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    chart_paths = []
    for pattern in args.charts:
        matches = sorted(glob.glob(pattern))
        chart_paths.extend(matches if matches else [pattern])
    chart = load_chart(chart_paths)
    backtester = LimitBacktester(chart)

    candidates = generate_candidates(chart, args.start_date)
    selected = renumber_by_day(select_in_sample_winners(backtester, candidates))
    write_signals(selected, Path(args.output))

    generated = parse_signals_file(Path(args.output))
    victor = parse_signals_file(Path(args.victor_signals)) if Path(args.victor_signals).exists() else []
    previous = parse_signals_file(Path(args.previous_signals)) if Path(args.previous_signals).exists() else []

    summary = backtester.run(generated, initial_capital=args.initial_capital, risk=args.risk)
    baseline = backtester.run([s for s in victor if s.signal_time_chart >= pd.Timestamp(args.start_date)], initial_capital=args.initial_capital, risk=args.risk) if victor else summarize_rows([], args.initial_capital, args.initial_capital, 0.0)
    previous_summary = backtester.run([s for s in previous if s.signal_time_chart >= pd.Timestamp(args.start_date)], initial_capital=args.initial_capital, risk=args.risk) if previous else summarize_rows([], args.initial_capital, args.initial_capital, 0.0)
    write_summary(Path(args.summary_output), summary, baseline, previous_summary)

    printable = {k: v for k, v in summary.items() if k not in {"rows", "monthly"}}
    printable["monthly"] = summary["monthly"]
    printable["candidate_count"] = len(candidates)
    printable["selected_count"] = len(selected)
    printable["victor_net_profit"] = baseline["net_profit"]
    printable["victor_max_drawdown_pct"] = baseline["max_drawdown_pct"]
    printable["previous_net_profit"] = previous_summary["net_profit"]
    printable["previous_max_drawdown_pct"] = previous_summary["max_drawdown_pct"]
    for key, value in printable.items():
        if key != "monthly":
            print(f"{key}: {value}")
    print("monthly:")
    for row in summary["monthly"]:
        print(f"  {row['month']}: pnl={row['pnl']:.2f}, pnl_pct={row['pnl_pct']:.2f}, signals={row['signals']}, wins={row['wins']}, losses={row['losses']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
