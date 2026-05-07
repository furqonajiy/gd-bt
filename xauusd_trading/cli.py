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
import glob
import json
import sys
from pathlib import Path

from .adapters import CsvChartSource, ManualPositionSource
from .backtest import run_backtest, write_backtest_outputs
from .config import DEFAULT_CONFIG, StrategyConfig
from .engine import decide, render_report
from .signal import parse_one_signal, parse_signals_file


def _expand_chart_paths(patterns: list[str]) -> list[Path]:
    """Expand each pattern via glob (so wildcards work on every shell,
    including PowerShell, which does NOT auto-expand globs for external
    commands). Plain paths pass through unchanged. Raises if a pattern
    matches nothing.
    """
    if not patterns:
        return []
    out: list[Path] = []
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            matches = sorted(glob.glob(pat))
            if not matches:
                raise SystemExit(f"No files match pattern: {pat}")
            out.extend(Path(m) for m in matches)
        else:
            p = Path(pat)
            if not p.exists():
                raise SystemExit(f"Chart file not found: {pat}")
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# subcommand: backtest
# ---------------------------------------------------------------------------

def cmd_backtest(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    signals = parse_signals_file(Path(args.signals))
    chart = CsvChartSource(_expand_chart_paths(args.charts))
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

    # Source selection -------------------------------------------------
    use_mt5 = bool(args.mt5)
    if use_mt5:
        from .mt5_adapter import (
            Mt5ChartSource, Mt5Connection, mt5_equity,
            archive_m1_by_month, render_archive_summary,
        )
        conn = Mt5Connection(
            path=args.mt5_path, login=args.mt5_login,
            password=args.mt5_password, server=args.mt5_server,
        )
        conn.initialize()

        # Archive first so the saved files are always up to date even if
        # the decision step later fails for some reason.
        if not args.no_archive:
            summary = archive_m1_by_month(
                conn, args.mt5_symbol, args.archive_dir,
                months_back=args.archive_months,
                server_offset_hours=args.mt5_server_offset,
                overwrite=args.archive_overwrite,
            )
            print(render_archive_summary(summary))
            print()

        chart = Mt5ChartSource(
            conn, symbol=args.mt5_symbol,
            server_offset_hours=args.mt5_server_offset,
            history_bars=args.mt5_history_bars,
        )
        equity = mt5_equity(conn) if args.equity_from_mt5 else args.equity
    else:
        if not args.charts:
            raise SystemExit("Either --charts or --mt5 must be provided.")
        chart = CsvChartSource(_expand_chart_paths(args.charts))
        equity = args.equity

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
        replay_end = now if now is not None else chart.last_time()
        for item in prior:
            psig = parse_one_signal(item["signal"], item["date"], int(item["tz"]))
            equity_at_open = float(item.get("equity_at_open", equity))
            pos = open_position(psig, equity_at_open, config)
            advance_bars(pos, chart.bars_between(pos.activation_time, replay_end), config)
            open_positions.append(pos)

    positions = ManualPositionSource(equity=equity, positions=open_positions)
    rec = decide(signal, chart, positions, config, now=now)
    print(render_report(rec))

    if use_mt5:
        conn.shutdown()
    return 0


# ---------------------------------------------------------------------------
# subcommand: mt5-info (diagnostic)
# ---------------------------------------------------------------------------

def cmd_mt5_info(args: argparse.Namespace) -> int:
    """Print MT5 connection info, latest bar, account equity, and open
    positions/orders for the symbol. Use to verify the connection works.
    """
    from .mt5_adapter import (
        Mt5ChartSource, Mt5Connection, mt5_equity, mt5_open_positions_summary,
    )
    with Mt5Connection(
        path=args.mt5_path, login=args.mt5_login,
        password=args.mt5_password, server=args.mt5_server,
    ) as conn:
        chart = Mt5ChartSource(
            conn, symbol=args.mt5_symbol,
            server_offset_hours=args.mt5_server_offset,
        )
        last = chart.latest()
        print(f"Symbol:           {args.mt5_symbol}")
        print(f"Server offset:    GMT+{args.mt5_server_offset}")
        print(f"Latest bar:       {last.time if last else '(none)'}  "
              f"close={last.close if last else '-'}  "
              f"spread={last.spread_points if last else '-'} pts")
        try:
            print(f"Account equity:   ${mt5_equity(conn):,.2f}")
        except Exception as e:
            print(f"Account equity:   <error: {e}>")
        print()
        print("Open MT5 positions / pending orders for the symbol:")
        rows = mt5_open_positions_summary(conn, args.mt5_symbol)
        if not rows:
            print("  (none)")
        for r in rows:
            print(f"  [{r['kind']}] #{r['ticket']}  {r['type']}  "
                  f"vol={r['volume']}  open={r['price_open']}  "
                  f"sl={r['sl']}  tp={r['tp']}  comment={r['comment']!r}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """Pull M1 from MT5 and save to per-month CSVs without running a decision.
    Useful for one-off bulk archiving or scheduled fetches.
    """
    from .mt5_adapter import (
        Mt5Connection, archive_m1_by_month, render_archive_summary,
    )
    with Mt5Connection(
        path=args.mt5_path, login=args.mt5_login,
        password=args.mt5_password, server=args.mt5_server,
    ) as conn:
        summary = archive_m1_by_month(
            conn, args.mt5_symbol, args.archive_dir,
            months_back=args.archive_months,
            server_offset_hours=args.mt5_server_offset,
            overwrite=args.archive_overwrite,
        )
        print(render_archive_summary(summary))
    return 0


# ---------------------------------------------------------------------------
# argparse plumbing
# ---------------------------------------------------------------------------

def _add_archive_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("M1 archive (per-month CSV files)")
    g.add_argument("--archive-dir", default="data",
                   help="Directory to save per-month M1 CSV files (default: data/)")
    g.add_argument("--archive-months", type=int, default=4,
                   help="How many calendar months back to fetch (default: 4)")
    g.add_argument("--archive-overwrite", action="store_true",
                   help="Overwrite each month file with the latest fetch (default: merge, "
                        "which is safer when MT5's history window only partially covers a month)")

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


def _add_mt5_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("MT5 connection")
    g.add_argument("--mt5-symbol", default="XAUUSD",
                   help="Symbol name in MT5 Market Watch (e.g. XAUUSD, XAUUSD.r, GOLD)")
    g.add_argument("--mt5-server-offset", type=int, default=3,
                   help="Broker server timezone offset from UTC. Most XAUUSD brokers use 3.")
    g.add_argument("--mt5-history-bars", type=int, default=5_000,
                   help="How many M1 bars of history to make available for replay")
    g.add_argument("--mt5-path", default=None,
                   help="Optional path to terminal64.exe (defaults to MT5 auto-detect)")
    g.add_argument("--mt5-login", type=int, default=None,
                   help="Optional login. If omitted, uses currently logged-in terminal session.")
    g.add_argument("--mt5-password", default=None)
    g.add_argument("--mt5-server", default=None)


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
    src = pd_.add_argument_group("Chart source (pick one)")
    src.add_argument("--charts", nargs="+", default=None, help="MT5 M1 CSV files")
    src.add_argument("--mt5", action="store_true", help="Use live MT5 chart instead of CSV")
    pd_.add_argument("--equity", type=float, default=DEFAULT_CONFIG.initial_capital)
    pd_.add_argument("--equity-from-mt5", action="store_true",
                     help="Use account equity from MT5 (overrides --equity). Requires --mt5.")
    pd_.add_argument("--positions-json", default=None,
                     help='Optional JSON file: list of {"signal": "...", "date": "YYYY-MM-DD", "tz": 7, "equity_at_open": 1000}')
    pd_.add_argument("--now", default=None,
                     help='Override "now" timestamp in chart timezone (GMT+3), e.g. "2026-05-05 18:00". Default: last chart bar.')
    pd_.add_argument("--no-archive", action="store_true",
                     help="Skip auto-archiving M1 to per-month CSVs when using --mt5")
    _add_strategy_overrides(pd_)
    _add_mt5_flags(pd_)
    _add_archive_flags(pd_)
    pd_.set_defaults(func=cmd_decide)

    pm = sub.add_parser("mt5-info", help="Test MT5 connection: print latest bar, equity, open positions")
    _add_mt5_flags(pm)
    pm.set_defaults(func=cmd_mt5_info)

    pf = sub.add_parser("fetch", help="Pull M1 from MT5 and save to per-month CSVs (no decision)")
    _add_mt5_flags(pf)
    _add_archive_flags(pf)
    pf.set_defaults(func=cmd_fetch)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
