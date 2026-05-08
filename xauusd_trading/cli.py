"""Command-line interface.

Subcommands:
    xauusd backtest   --signals SIGNALS_FILE --charts CSV [CSV ...] [--output-dir DIR]
                      (always fetches latest 2 months of M1 from MT5 first if available)

    xauusd decide     --signal "..." --signal-date YYYY-MM-DD --signal-tz N [--execute]
                      (default: print-only. With --execute: places + manages on MT5.)

    xauusd mt5-info   diagnostic
    xauusd fetch      pull M1 to per-month CSVs (no decision)
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

# Hardcoded archive policy (per project preference: minimal flags).
ARCHIVE_DIR = "data"
ARCHIVE_MONTHS = 2


def _expand_chart_paths(patterns: list[str]) -> list[Path]:
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


def _try_archive_from_mt5(symbol: str, server_offset: int) -> None:
    """Best-effort: pull last ARCHIVE_MONTHS from MT5 to ARCHIVE_DIR.
    Soft-fail (warn and continue) if MT5 isn't reachable.
    """
    try:
        from .mt5_adapter import (
            Mt5Connection, archive_m1_by_month, render_archive_summary,
        )
    except Exception as e:
        print(f"[mt5] skipped archive (import failed: {e})", file=sys.stderr)
        return
    try:
        with Mt5Connection() as conn:
            summary = archive_m1_by_month(
                conn, symbol, ARCHIVE_DIR,
                months_back=ARCHIVE_MONTHS,
                server_offset_hours=server_offset,
                overwrite=False,
            )
            print(render_archive_summary(summary))
            print()
    except Exception as e:
        print(f"[mt5] skipped archive ({e})", file=sys.stderr)


# ---------------------------------------------------------------------------
# subcommand: backtest
# ---------------------------------------------------------------------------

def cmd_backtest(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    _try_archive_from_mt5(args.mt5_symbol, args.mt5_server_offset)

    from .signal import parse_signals_file
    signals = parse_signals_file(Path(args.signals))
    chart = CsvChartSource(_expand_chart_paths(args.charts))
    result = run_backtest(
        signals, chart, config,
        exclude_structural_anomalies=args.exclude_structural_anomalies,
    )
    summary = {k: v for k, v in result.items() if k not in {"rows", "entry_rows"}}
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
    from .signal import parse_one_signal
    from .positions import open_position, advance_bars

    use_mt5 = bool(args.mt5) or bool(args.execute)
    conn = None

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
        # Always archive 2 months back.
        try:
            summary = archive_m1_by_month(
                conn, args.mt5_symbol, ARCHIVE_DIR,
                months_back=ARCHIVE_MONTHS,
                server_offset_hours=args.mt5_server_offset,
                overwrite=False,
            )
            print(render_archive_summary(summary))
            print()
        except Exception as e:
            print(f"[mt5] archive failed (continuing): {e}", file=sys.stderr)

        chart = Mt5ChartSource(
            conn, symbol=args.mt5_symbol,
            server_offset_hours=args.mt5_server_offset,
            history_bars=args.mt5_history_bars,
        )
        equity = mt5_equity(conn) if (args.equity_from_mt5 or args.execute) else args.equity
    else:
        if not args.charts:
            raise SystemExit("Either --charts or --mt5 (or --execute) must be provided.")
        chart = CsvChartSource(_expand_chart_paths(args.charts))
        equity = args.equity

    signal = parse_one_signal(args.signal, args.signal_date, args.signal_tz)

    now = None
    if args.now:
        from datetime import datetime as _dt
        now = _dt.fromisoformat(args.now)

    # Load tracked-signal registry; replay each against the chart up to "now".
    open_positions: list = []
    registry_path = Path(args.positions_json or "positions.json")
    prior_entries: list[dict] = []
    if registry_path.exists():
        try:
            prior_entries = json.loads(registry_path.read_text(encoding="utf-8"))
        except Exception:
            prior_entries = []

    replay_end = now if now is not None else chart.last_time()
    if prior_entries and replay_end is not None:
        for item in prior_entries:
            psig = parse_one_signal(item["signal"], item["date"], int(item["tz"]))
            equity_at_open = float(item.get("equity_at_open", equity))
            pos = open_position(psig, equity_at_open, config)
            advance_bars(pos, chart.bars_between(pos.activation_time, replay_end), config)
            open_positions.append(pos)

    positions = ManualPositionSource(equity=equity, positions=open_positions)
    rec = decide(signal, chart, positions, config, now=now)
    print(render_report(rec))

    # ---- execute on MT5 -----------------------------------------------
    if args.execute:
        from .mt5_executor import (
            Mt5Executor, SignalRegistry, signal_to_magic,
            render_execution_log, ExecutionLog,
        )
        executor = Mt5Executor(
            conn, args.mt5_symbol,
            min_lot=config.minimum_lot or 0.01,
            lot_step=config.lot_step or 0.01,
        )

        print()
        errors = executor.sanity_checks(expected_equity=equity)
        if errors:
            print("SANITY CHECKS FAILED -- aborting execution:")
            for e in errors:
                print(f"  ! {e}")
            conn.shutdown()
            return 2

        registry = SignalRegistry(registry_path)
        log = ExecutionLog()

        # Manage existing tracked positions.
        for pos in open_positions:
            mlog = executor.manage_position(pos, config, rec.generated_at)
            log.merge(mlog)

        # Check for unknown MT5 objects (warn-and-proceed per spec).
        known = {signal_to_magic(p.signal.signal_key) for p in open_positions}
        known.add(signal_to_magic(signal.signal_key))
        log.warnings.extend(executor.warn_on_unknown(known))

        # Place the new signal (skipped if it's already running).
        if any(p.signal.signal_key == signal.signal_key for p in open_positions):
            log.actions.append(
                f"Signal {signal.signal_key} is already tracked; managed above."
            )
        else:
            plog = executor.place_signal(signal, rec.new_signal)
            log.merge(plog)
            if plog.placed > 0:
                registry.add(signal, equity)

        # Auto-prune registry: drop entries whose magic has zero MT5 footprint.
        alive = executor.all_alive_magics()
        removed = registry.prune(alive)
        if removed:
            log.actions.append(f"Pruned {removed} closed signal(s) from {registry_path.name}")

        print(render_execution_log(log))

    if conn is not None:
        conn.shutdown()
    return 0


# ---------------------------------------------------------------------------
# subcommand: mt5-info
# ---------------------------------------------------------------------------

def cmd_mt5_info(args: argparse.Namespace) -> int:
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
    from .mt5_adapter import (
        Mt5Connection, archive_m1_by_month, render_archive_summary,
    )
    with Mt5Connection(
        path=args.mt5_path, login=args.mt5_login,
        password=args.mt5_password, server=args.mt5_server,
    ) as conn:
        summary = archive_m1_by_month(
            conn, args.mt5_symbol, ARCHIVE_DIR,
            months_back=ARCHIVE_MONTHS,
            server_offset_hours=args.mt5_server_offset,
            overwrite=False,
        )
        print(render_archive_summary(summary))
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def _add_strategy_overrides(p: argparse.ArgumentParser) -> None:
    p.add_argument("--initial-capital", type=float, default=DEFAULT_CONFIG.initial_capital)
    p.add_argument("--risk", type=float, default=DEFAULT_CONFIG.risk_per_signal)
    p.add_argument("--entries", type=int, default=DEFAULT_CONFIG.entry_count,
                   help="Number of entry slots per signal (>=1). Default uses validated config.")
    p.add_argument("--entry-ladder", default=DEFAULT_CONFIG.entry_ladder,
                   choices=["range_uniform", "range_to_sl"],
                   help="How to space entries: within the signal range or extended toward SL.")
    p.add_argument("--entry-sl-gap", type=float, default=DEFAULT_CONFIG.entry_sl_gap,
                   help="Dollars between deepest entry and signal SL (range_to_sl only).")


def _config_from_args(args: argparse.Namespace) -> StrategyConfig:
    return StrategyConfig(
        initial_capital=getattr(args, "initial_capital", DEFAULT_CONFIG.initial_capital),
        risk_per_signal=getattr(args, "risk", DEFAULT_CONFIG.risk_per_signal),
        entry_count=getattr(args, "entries", DEFAULT_CONFIG.entry_count),
        entry_ladder=getattr(args, "entry_ladder", DEFAULT_CONFIG.entry_ladder),
        entry_sl_gap=getattr(args, "entry_sl_gap", DEFAULT_CONFIG.entry_sl_gap),
    )


def _add_mt5_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("MT5 connection")
    g.add_argument("--mt5-symbol", default="XAUUSD")
    g.add_argument("--mt5-server-offset", type=int, default=3)
    g.add_argument("--mt5-history-bars", type=int, default=5_000)
    g.add_argument("--mt5-path", default=None)
    g.add_argument("--mt5-login", type=int, default=None)
    g.add_argument("--mt5-password", default=None)
    g.add_argument("--mt5-server", default=None)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xauusd")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("backtest", help="Run historical backtest (auto-fetches 2mo from MT5 first)")
    pb.add_argument("--signals", required=True)
    pb.add_argument("--charts", required=True, nargs="+")
    pb.add_argument("--output-dir", default=None)
    pb.add_argument("--exclude-structural-anomalies", action="store_true")
    _add_strategy_overrides(pb)
    _add_mt5_flags(pb)
    pb.set_defaults(func=cmd_backtest)

    pd_ = sub.add_parser("decide", help="Decide on one signal (use --execute to place orders on MT5)")
    pd_.add_argument("--signal", required=True)
    pd_.add_argument("--signal-date", required=True)
    pd_.add_argument("--signal-tz", type=int, required=True)
    src = pd_.add_argument_group("Chart source (only one needed)")
    src.add_argument("--charts", nargs="+", default=None)
    src.add_argument("--mt5", action="store_true")
    pd_.add_argument("--equity", type=float, default=DEFAULT_CONFIG.initial_capital)
    pd_.add_argument("--equity-from-mt5", action="store_true")
    pd_.add_argument("--positions-json", default=None,
                     help="Tracked-signal registry (default: positions.json, auto-managed when --execute is set)")
    pd_.add_argument("--now", default=None)
    pd_.add_argument("--execute", action="store_true",
                     help="Place orders on MT5 directly (no confirmation prompt). Implies --mt5.")
    _add_strategy_overrides(pd_)
    _add_mt5_flags(pd_)
    pd_.set_defaults(func=cmd_decide)

    pm = sub.add_parser("mt5-info", help="Diagnostic: latest bar, equity, open MT5 objects")
    _add_mt5_flags(pm)
    pm.set_defaults(func=cmd_mt5_info)

    pf = sub.add_parser("fetch", help="Pull last 2 months of M1 to data/")
    _add_mt5_flags(pf)
    pf.set_defaults(func=cmd_fetch)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
