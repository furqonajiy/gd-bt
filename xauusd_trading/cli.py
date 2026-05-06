"""Command-line interface.

Two subcommands:

    xauusd backtest --signals SIGNALS_FILE --charts CSV [CSV ...] \\
                    [--output-dir DIR]

    xauusd decide   --signal "1. BUY XAUUSD ..." --signal-date 2026-05-06 \\
                    --signal-tz +7 \\
                    --charts CSV [CSV ...] \\
                    [--equity 1000] \\
                    [--positions-json positions.json]

The backtest subcommand reproduces the validated baseline.
The decide subcommand prints a human-readable decision report for one new
signal, given current chart and (optionally) currently-open positions.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from .adapters import CsvChartSource, ManualPositionSource
from .backtest import run_backtest, write_backtest_outputs
from .config import DEFAULT_CONFIG, StrategyConfig
from .engine import decide, render_report
from .signal import parse_one_signal, parse_signals_file


# ---------------------------------------------------------------------------
# subcommand: backtest
# ---------------------------------------------------------------------------

def cmd_backtest(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    signals = parse_signals_file(Path(args.signals))
    chart = CsvChartSource([Path(p) for p in args.charts])
    result = run_backtest(
        signals, chart, config,
        exclude_structural_anomalies=args.exclude_structural_anomalies,
    )
    summary = {k: v for k, v in result.items() if k != "rows"}
    print(json.dumps(summary, indent=2, default=str))
    if args.output_dir:
        write_backtest_outputs(result, Path(args.output_dir))
        print(f"\nWrote outputs to {Path(args.output_dir).resolve()}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# subcommand: decide
# ---------------------------------------------------------------------------

def cmd_decide(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    chart = CsvChartSource([Path(p) for p in args.charts])
    signal = parse_one_signal(args.signal, args.signal_date, args.signal_tz)

    now = None
    if args.now:
        from datetime import datetime as _dt
        now = _dt.fromisoformat(args.now)

    # Open positions can be supplied as a JSON list. Each entry is a signal
    # text + its date/tz. The runner replays them against the chart up to
    # "now" (the chart's last bar, or --now). This mirrors how a future MT5
    # adapter would synthesize live state from the broker.
    open_positions = []
    if args.positions_json:
        prior = json.loads(Path(args.positions_json).read_text(encoding="utf-8"))
        from .positions import open_position, advance_bars
        from .chart import iter_bars, slice_bars
        chart_df = chart.dataframe
        replay_end = now if now is not None else chart.last_time()
        for item in prior:
            psig = parse_one_signal(item["signal"], item["date"], int(item["tz"]))
            equity_at_open = float(item.get("equity_at_open", args.equity))
            pos = open_position(psig, equity_at_open, config)
            advance_bars(pos, iter_bars(slice_bars(chart_df, pos.activation_time, replay_end)), config)
            open_positions.append(pos)

    positions = ManualPositionSource(equity=args.equity, positions=open_positions)
    rec = decide(signal, chart, positions, config, now=now)
    print(render_report(rec))
    return 0


# ---------------------------------------------------------------------------
# argparse plumbing
# ---------------------------------------------------------------------------

def _add_strategy_overrides(p: argparse.ArgumentParser) -> None:
    p.add_argument("--initial-capital", type=float, default=DEFAULT_CONFIG.initial_capital)
    p.add_argument("--risk", type=float, default=DEFAULT_CONFIG.risk_per_signal,
                   help="Fraction of equity at risk per signal")
    p.add_argument("--entries", type=int, default=DEFAULT_CONFIG.entry_count, choices=[1, 2, 3])
    p.add_argument("--minimum-lot", type=float, default=DEFAULT_CONFIG.minimum_lot)
    p.add_argument("--lot-step", type=float, default=DEFAULT_CONFIG.lot_step)


def _config_from_args(args: argparse.Namespace) -> StrategyConfig:
    return StrategyConfig(
        initial_capital=getattr(args, "initial_capital", DEFAULT_CONFIG.initial_capital),
        risk_per_signal=getattr(args, "risk", DEFAULT_CONFIG.risk_per_signal),
        entry_count=getattr(args, "entries", DEFAULT_CONFIG.entry_count),
        minimum_lot=getattr(args, "minimum_lot", DEFAULT_CONFIG.minimum_lot),
        lot_step=getattr(args, "lot_step", DEFAULT_CONFIG.lot_step),
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xauusd",
                                description="XAUUSD validated-strategy backtest and live decision engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("backtest", help="Run historical backtest")
    pb.add_argument("--signals", required=True, help="Path to signals text file")
    pb.add_argument("--charts", required=True, nargs="+", help="MT5 M1 CSV files")
    pb.add_argument("--output-dir", default=None, help="Optional output directory for CSV/JSON files")
    pb.add_argument("--exclude-structural-anomalies", action="store_true")
    _add_strategy_overrides(pb)
    pb.set_defaults(func=cmd_backtest)

    pd_ = sub.add_parser("decide", help="Decide what to do with one new signal")
    pd_.add_argument("--signal", required=True,
                     help='Signal line, e.g. "1. BUY XAUUSD 4543 - 4541 SL 4536 TP1 4551 TP2 4561 TP3 4576 2:02 PM"')
    pd_.add_argument("--signal-date", required=True, help="ISO date of the signal, e.g. 2026-05-05")
    pd_.add_argument("--signal-tz", type=int, required=True, help="GMT offset of signal clock time, e.g. 7")
    pd_.add_argument("--charts", required=True, nargs="+", help="MT5 M1 CSV files (chart up to 'now')")
    pd_.add_argument("--equity", type=float, default=DEFAULT_CONFIG.initial_capital)
    pd_.add_argument("--positions-json", default=None,
                     help='Optional JSON file: list of {"signal": "...", "date": "YYYY-MM-DD", "tz": 7, "equity_at_open": 1000}')
    pd_.add_argument("--now", default=None,
                     help='Override "now" timestamp in chart timezone (GMT+3), e.g. "2026-05-05 18:00". Default: last chart bar.')
    _add_strategy_overrides(pd_)
    pd_.set_defaults(func=cmd_decide)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
