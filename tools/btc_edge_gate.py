#!/usr/bin/env python3
"""BTC rejection-signal EDGE GATE -- does the signal predict direction at all?

This is the go/no-go test that runs BEFORE any live executor work. It generates
the BTC rejection signals through the exact blessed path the backtest/live runner
use (generate_rejection_signals + BTC_REJECTION_CONFIG), then measures, on the raw
ticks, how far price runs in the signal's favour versus against it -- net of the
real bid/ask spread -- and what a placeable (target, stop) trade would have won.

Why ticks and not M1: the question is intrabar -- which level price reaches first
-- and an M1 bar can't answer it. Why spread-aware: a BUY is entered at the ask and
closed at the bid, so every excursion starts ~one spread underwater; that handicap
is exactly what a real edge has to overcome, and it is baked in here by referencing
ask on entry and marking longs against bid (symmetric for sells).

Reading the output:
- If MFE and |MAE| are roughly symmetric and no (target, stop) pair beats its
  fair-coin breakeven win rate, the signal is directionless on BTC -> STOP. No
  amount of parameter tuning rescues a signal with no directional information.
- If MFE clearly exceeds |MAE| and pairs clear breakeven with margin, there is an
  edge worth building geometry around (step 2: full tick backtest).

Stops are floored at BTC's stops_level ($62) by default, because anything tighter
is unplaceable on ELEV8 regardless of what the numbers say.

PowerShell (conda env `trading`):
    python tools/btc_edge_gate.py `
        --charts "data/BTCUSD_M1_*_ELEV8.csv" `
        --ticks  "data/ticks/BTCUSD_TICK_*_ELEV8.csv" `
        --out reports/btc_edge_gate_signals.csv

Point --ticks at EITHER the full monthly files OR the H1/H2 parts, never both.
"""
from __future__ import annotations

import argparse
import csv
import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading import (  # noqa: E402
    CsvChartSource,
    generate_rejection_signals,
)
from btcusd_trading import (  # noqa: E402
    BTC_REJECTION_CONFIG,
    BTC_SPEC,
    assert_configured,
)
# Shared GMT+3 chart-time <-> MT5 epoch conversion, so a signal's timestamp lines
# up with the tick <TIME_MSC> the same way export_ticks wrote it (no drift).
from tools.export_ticks import _chart_to_mt5_epoch  # noqa: E402

_TICK_COLS = ["<TIME_MSC>", "<BID>", "<ASK>"]


# ----------------------------------------------------------------------------- #
# Pure measurement primitives (unit-tested; no I/O, no MT5).
# ----------------------------------------------------------------------------- #
def excursion(side: str, entry_ask: float, entry_bid: float,
              bid_win: np.ndarray, ask_win: np.ndarray) -> tuple[float, float]:
    """Spread-aware (MFE, MAE) in price for one signal over a tick window.

    A long enters at the ask and is marked against the bid; a short enters at the
    bid and is marked against the ask. MFE is favourable (>= the start drawdown),
    MAE is adverse (<= 0 once the spread is paid).
    """
    if bid_win.size == 0:  # signal sits in a tick gap shorter than this horizon
        return float("nan"), float("nan")
    if side == "BUY":
        return float(bid_win.max()) - entry_ask, float(bid_win.min()) - entry_ask
    return entry_bid - float(ask_win.min()), entry_bid - float(ask_win.max())


def first_touch(side: str, entry_ask: float, entry_bid: float,
                bid_win: np.ndarray, ask_win: np.ndarray,
                target: float, stop: float) -> str:
    """Which level is hit first, tick by tick: 'win', 'loss', or 'timeout'.

    At tick resolution each tick is a single price, so there is no same-bar
    target+stop ambiguity to resolve pessimistically -- whichever index comes
    first simply wins.
    """
    if bid_win.size == 0:
        return "timeout"
    if side == "BUY":
        win = bid_win >= entry_ask + target
        loss = bid_win <= entry_ask - stop
    else:
        win = ask_win <= entry_bid - target
        loss = ask_win >= entry_bid + stop
    wi = int(np.argmax(win)) if win.any() else None
    li = int(np.argmax(loss)) if loss.any() else None
    if wi is None and li is None:
        return "timeout"
    if li is None:
        return "win"
    if wi is None:
        return "loss"
    return "win" if wi < li else "loss"


def breakeven_winrate(target: float, stop: float) -> float:
    """Fair-coin win rate that breaks even at this reward:risk."""
    return stop / (stop + target)


# ----------------------------------------------------------------------------- #
# Tick loading.
# ----------------------------------------------------------------------------- #
def _load_ticks(patterns: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (msc, bid, ask) sorted by msc, with exact-duplicate rows dropped.

    Reads only the three needed columns and frees each dataframe immediately so
    peak memory is roughly the final arrays plus one month's read buffer.
    """
    files: list[str] = []
    for pat in patterns:
        files.extend(sorted(glob.glob(pat)) if any(c in pat for c in "*?[") else [pat])
    if not files:
        raise SystemExit("No tick files matched --ticks")

    msc_parts, bid_parts, ask_parts = [], [], []
    for path in files:
        df = pd.read_csv(path, sep="\t", usecols=_TICK_COLS)
        msc_parts.append(df["<TIME_MSC>"].to_numpy(dtype=np.int64))
        bid_parts.append(df["<BID>"].to_numpy(dtype=np.float32))
        ask_parts.append(df["<ASK>"].to_numpy(dtype=np.float32))
        del df

    msc = np.concatenate(msc_parts)
    bid = np.concatenate(bid_parts)
    ask = np.concatenate(ask_parts)

    order = np.argsort(msc, kind="stable")
    msc, bid, ask = msc[order], bid[order], ask[order]

    # Drop exact-duplicate consecutive ticks from overlapping files; keep distinct
    # ticks that merely share a millisecond.
    if len(msc) > 1:
        keep = np.ones(len(msc), dtype=bool)
        keep[1:] = ~((msc[1:] == msc[:-1]) & (bid[1:] == bid[:-1]) & (ask[1:] == ask[:-1]))
        msc, bid, ask = msc[keep], bid[keep], ask[keep]
    return msc, bid, ask


# ----------------------------------------------------------------------------- #
# Driver.
# ----------------------------------------------------------------------------- #
def _expand_charts(patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        matches = sorted(glob.glob(pat)) if any(c in pat for c in "*?[") else [pat]
        for m in matches:
            p = Path(m)
            if not p.exists():
                raise SystemExit(f"Chart file not found: {m}")
            out.append(p)
    if not out:
        raise SystemExit("No chart files matched --charts")
    return list({p.resolve(): p for p in out}.values())


def _parse_floats(text: str) -> list[float]:
    return [float(x) for x in text.split(",") if x.strip()]


def run(chart_patterns: list[str], tick_patterns: list[str], out_csv: str, *,
        horizons_min: list[int], stops: list[float], rrs: list[float],
        max_hold_min: int, server_offset: int, entry_tol_min: int) -> dict:
    assert_configured()  # never measure on placeholder BTC values

    chart = CsvChartSource(_expand_charts(chart_patterns), point_value=BTC_SPEC.point_value)
    bars = list(chart.bars_between(chart.first_time(), chart.last_time()))
    signals = generate_rejection_signals(bars, BTC_REJECTION_CONFIG)
    print(f"[signals] generated {len(signals)} rejection signal(s) from "
          f"{len(bars):,} M1 bars.")
    if not signals:
        raise SystemExit("No signals generated; nothing to measure.")

    print("[ticks] loading ...")
    msc, bid, ask = _load_ticks(tick_patterns)
    print(f"[ticks] {len(msc):,} ticks, "
          f"{msc[0]} .. {msc[-1]} (msc).")

    horizons_ms = {h: h * 60_000 for h in horizons_min}
    max_hold_ms = max_hold_min * 60_000
    entry_tol_ms = entry_tol_min * 60_000
    pairs = [(s, s * rr) for s in stops for rr in rrs]  # (stop, target)

    rows: list[dict] = []
    measured = 0
    no_cover = 0
    side_counts = {"BUY": 0, "SELL": 0}
    mfe_acc = {h: [] for h in horizons_min}
    mae_acc = {h: [] for h in horizons_min}
    ft_counts = {p: {"win": 0, "loss": 0, "timeout": 0} for p in pairs}

    for sig in signals:
        sig_msc = _chart_to_mt5_epoch(sig.signal_time_chart, server_offset) * 1000
        lo = int(np.searchsorted(msc, sig_msc, side="left"))
        # No tick at/after the signal, or the nearest one is beyond the tolerance
        # (a market gap, or the signal predates tick coverage): not enterable, skip.
        if lo >= len(msc) or int(msc[lo]) - sig_msc > entry_tol_ms:
            no_cover += 1
            continue
        measured += 1
        side_counts[sig.side] += 1
        entry_ask = float(ask[lo])
        entry_bid = float(bid[lo])

        row = {
            "side": sig.side,
            "signal_time": sig.signal_time_chart.isoformat(sep=" "),
            "entry_ask": round(entry_ask, 2),
            "entry_bid": round(entry_bid, 2),
        }

        for h in horizons_min:
            hi = int(np.searchsorted(msc, sig_msc + horizons_ms[h], side="right"))
            mfe, mae = excursion(sig.side, entry_ask, entry_bid, bid[lo:hi], ask[lo:hi])
            if np.isnan(mfe):
                row[f"mfe_{h}m"] = ""
                row[f"mae_{h}m"] = ""
                continue
            mfe_acc[h].append(mfe)
            mae_acc[h].append(mae)
            row[f"mfe_{h}m"] = round(mfe, 2)
            row[f"mae_{h}m"] = round(mae, 2)

        hi_hold = int(np.searchsorted(msc, sig_msc + max_hold_ms, side="right"))
        bw, aw = bid[lo:hi_hold], ask[lo:hi_hold]
        for (stop, target) in pairs:
            outcome = first_touch(sig.side, entry_ask, entry_bid, bw, aw, target, stop)
            ft_counts[(stop, target)][outcome] += 1
            row[f"ft_s{int(stop)}_t{int(target)}"] = outcome

        rows.append(row)

    _write_csv(Path(out_csv), rows)
    summary = _print_summary(measured, no_cover, side_counts, horizons_min,
                             mfe_acc, mae_acc, pairs, ft_counts, max_hold_min)
    print(f"[out] per-signal detail -> {out_csv}")
    return summary


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _print_summary(measured, no_cover, side_counts, horizons_min,
                   mfe_acc, mae_acc, pairs, ft_counts, max_hold_min) -> dict:
    print("\n================ EDGE GATE SUMMARY ================")
    print(f"signals measured: {measured}   (no tick coverage: {no_cover})")
    print(f"  BUY: {side_counts['BUY']}   SELL: {side_counts['SELL']}")

    print("\n-- spread-aware excursion (median, $) --")
    print(f"{'horizon':>9} | {'med MFE':>9} | {'med |MAE|':>9} | {'MFE/|MAE|':>9}")
    for h in horizons_min:
        med_mfe = float(np.median(mfe_acc[h])) if mfe_acc[h] else 0.0
        med_mae = abs(float(np.median(mae_acc[h]))) if mae_acc[h] else 0.0
        ratio = (med_mfe / med_mae) if med_mae > 0 else float("inf")
        print(f"{h:>7}m | {med_mfe:>9.2f} | {med_mae:>9.2f} | {ratio:>9.2f}")

    print(f"\n-- first-touch win rate over {max_hold_min}m (target before stop, net of spread) --")
    print(f"{'stop$':>6} {'target$':>8} {'RR':>5} | {'win%':>6} {'lose%':>6} {'timeout%':>8} | {'breakeven%':>10} {'edge':>6}")
    best_edge = -1.0
    best_line = None
    for (stop, target) in pairs:
        c = ft_counts[(stop, target)]
        resolved = c["win"] + c["loss"]
        total = resolved + c["timeout"]
        winp = 100.0 * c["win"] / resolved if resolved else 0.0
        losep = 100.0 * c["loss"] / resolved if resolved else 0.0
        timep = 100.0 * c["timeout"] / total if total else 0.0
        be = 100.0 * breakeven_winrate(target, stop)
        edge = winp - be
        if resolved and edge > best_edge:
            best_edge, best_line = edge, (stop, target, winp, be)
        print(f"{stop:>6.0f} {target:>8.0f} {target / stop:>5.2f} | "
              f"{winp:>6.1f} {losep:>6.1f} {timep:>8.1f} | {be:>10.1f} {edge:>+6.1f}")

    print("\n-- verdict --")
    if best_line is None or best_edge <= 0:
        print("  No (target, stop) pair beats its fair-coin breakeven. The rejection")
        print("  signal shows no tradable directional edge on BTC at placeable geometry.")
        print("  -> STOP. Do not build the executor; tuning cannot create absent edge.")
        verdict = "no_edge"
    else:
        stop, target, winp, be = best_line
        print(f"  Best: stop=${stop:.0f} target=${target:.0f}  ->  {winp:.1f}% vs "
              f"{be:.1f}% breakeven  (+{best_edge:.1f} pts).")
        print("  An edge is present at placeable geometry; proceed to step 2 (full")
        print("  tick backtest), then validate it out-of-sample before going live.")
        verdict = "edge_present"
    print("===================================================\n")
    return {"verdict": verdict, "measured": measured, "best_edge": best_edge}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="BTC rejection-signal edge gate (tick MFE/MAE + first-touch).")
    p.add_argument("--charts", nargs="+", required=True, help="BTC M1 CSV path(s)/glob(s) for signal generation.")
    p.add_argument("--ticks", nargs="+", required=True, help="BTC tick CSV path(s)/glob(s); fulls OR halves, not both.")
    p.add_argument("--out", default="reports/btc_edge_gate_signals.csv", help="Per-signal detail CSV.")
    p.add_argument("--horizons", default="15,60,240", help="MFE/MAE horizons in minutes, comma-separated.")
    p.add_argument("--stops", default="62,120,200", help="Stop distances in $, comma-separated (>= 62 = BTC floor).")
    p.add_argument("--rr", default="1.0,1.5,2.0", help="Reward:risk multipliers, comma-separated (target = stop*rr).")
    p.add_argument("--max-hold", type=int, default=240, help="First-touch horizon in minutes.")
    p.add_argument("--entry-tolerance-min", type=int, default=5,
                   help="Skip a signal if the nearest tick is more than this many minutes away.")
    p.add_argument("--mt5-server-offset", type=int, default=3)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run(
        args.charts, args.ticks, args.out,
        horizons_min=[int(x) for x in args.horizons.split(",") if x.strip()],
        stops=_parse_floats(args.stops),
        rrs=_parse_floats(args.rr),
        max_hold_min=args.max_hold,
        server_offset=args.mt5_server_offset,
        entry_tol_min=args.entry_tolerance_min,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())