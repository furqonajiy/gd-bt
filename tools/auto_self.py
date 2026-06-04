#!/usr/bin/env python3
"""Single live runner: self-rejection signal generation + execution, 24/7.

Each watch pass it (1) regenerates the executable signal feed from the latest
CLOSED M1 bars using the exact same `generate_rejection_signals` the backtest
uses, then (2) hands that feed to the validated auto execution pass
(`_auto_pass`). The executor and engine are NOT modified or forked: the feed
file is the seam, and `_auto_pass` already re-parses it every pass and places
any new signal.

All safety guards (account mode, max concurrent positions, daily-loss halt,
placement spread) are enforced by controlling WHICH new signals are written
into the executable feed this pass. Open positions keep being managed
regardless, because `_auto_pass` manages off the registry, not the feed.

The executable feed (`--signals`) is a rolling window, so it is NOT a good
backtest input. When `--backtest-archive` is given, every generated signal is
also appended to a cumulative full-history file (optionally seeded once from
CSV charts via `--seed-archive-charts`). Point `backtest_explicit.py` at THAT
file. The archive is a side output only and never affects live execution.

Strategy-contract args are reused verbatim from auto_explicit so the live
contract is single-sourced with the validated backtest contract.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import auto_explicit  # sibling tool; reuse its validated strategy-contract parser/config

from xauusd_trading import (  # noqa: E402
    CsvChartSource,
    Mt5ChartSource,
    Mt5Connection,
    RejectionSignalConfig,
    SignalRegistry,
    archive_m1_by_month,
    format_generated_signals,
    generate_rejection_signals,
    iter_bars,
    mt5_equity,
    parse_signals_file,
    render_archive_summary,
)
from xauusd_trading.cli import (  # noqa: E402
    ARCHIVE_DIR,
    ARCHIVE_MONTHS,
    AUTO_HEARTBEAT_SECONDS,
    _auto_pass,
    _print_auto_watch_heartbeat,
)

# Render the executable feed over a fixed trailing window of whole calendar
# days. Whole days (not a mid-day slice) keep per-day numbering stable across
# rewrites, so an already-placed signal's signal_key never shifts. Two days
# comfortably exceeds the actionable horizon (pending_expiry ~10.5h) yet stays
# inside the 5000-bar (~3.5 day) history window, so every rendered day is fully
# present. The same window bounds archive appends, so appended signals are also
# numbered from a full day (matching the seed's per-day numbering).
FEED_WINDOW_DAYS = 2


# ---------------------------------------------------------------------------
# feed construction (pure; unit-tested)
# ---------------------------------------------------------------------------

def _keyed_signals(signals):
    """[(GeneratedSignal, "YYYY-MM-DD#NN")] with the same per-day numbering
    `format_generated_signals` produces: sorted by (time, side), 1-based
    counter reset per calendar day. The signal_key is therefore deterministic
    from content and stable while a day's full signal set is reproduced.
    """
    ordered = sorted(signals, key=lambda s: (s.signal_time_chart, s.side))
    out = []
    per_day: dict[str, int] = {}
    for s in ordered:
        d = s.signal_time_chart.date().isoformat()
        per_day[d] = per_day.get(d, 0) + 1
        out.append((s, f"{d}#{per_day[d]:02d}"))
    return out


def _signal_line(s, day_id: int, price_digits: int) -> str:
    digits = int(price_digits)

    def px(v: float) -> str:
        return f"{v:.{digits}f}"

    t = s.signal_time_chart.strftime("%I:%M %p")
    return (
        f"{day_id}. {s.side} XAUUSD {px(s.r1)} - {px(s.r2)} "
        f"SL {px(s.sl)} TP1 {px(s.tp1)} TP2 {px(s.tp2)} TP3 {px(s.tp3)} {t}"
    )


def _select_allowed_keys(keyed, placed_keys, *, cap, placed_count,
                         block_new, min_new_time=None):
    """Decide which keys appear in the executable feed this pass.

    Already-placed keys are always kept (so numbering/keys stay matched to the
    registry). New keys are admitted NEWEST-first up to the remaining slot
    count: the freshest signal is the one most likely still PENDING/OPEN and
    actually placeable, so admitting oldest-first lets already-played-out
    signals consume the cap and starve fresh signals. A guard blocks all new
    entries, and signals older than the executor's acceptance window are never
    admitted.
    """
    allowed = {key for _s, key in keyed if key in placed_keys}
    if block_new:
        return allowed
    remaining = max(0, int(cap) - int(placed_count))
    if remaining <= 0:
        return allowed
    used = 0
    for s, key in sorted(keyed, key=lambda sk: sk[0].signal_time_chart, reverse=True):
        if key in placed_keys:
            continue
        if min_new_time is not None and s.signal_time_chart <= min_new_time:
            continue
        allowed.add(key)
        used += 1
        if used >= remaining:
            break
    return allowed


def _render_feed(keyed, allowed_keys, *, source_tz_offset, price_digits):
    """Render the allowed keys in the human signal-file format. Byte-compatible
    with `format_generated_signals` so the SAME file backtests identically; the
    printed line number is the stable per-day id (gaps from withheld signals
    are fine -- the parser reads the printed number, not line position).
    """
    tz = f"GMT+{source_tz_offset}" if source_tz_offset >= 0 else f"GMT{source_tz_offset}"
    lines: list[str] = []
    last_date: str | None = None
    for s, key in keyed:
        if key not in allowed_keys:
            continue
        d = s.signal_time_chart.date().isoformat()
        day_id = int(key.split("#")[1])
        if d != last_date:
            if lines:
                lines.append("")
            lines.append(f"{d} {tz}")
            last_date = d
        lines.append(_signal_line(s, day_id, price_digits))
    return ("\n".join(lines) + "\n") if lines else ""


def _atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# cumulative backtest archive (side output; never affects execution)
# ---------------------------------------------------------------------------

def _expand_globs(patterns) -> list[Path]:
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
    if not out:
        raise SystemExit("No chart files provided for --seed-archive-charts")
    return out


def _seed_archive_if_missing(args, rcfg) -> None:
    """Build the full-history archive once from CSV charts if it does not yet
    exist. This is the same batch generation tools/generate_self_signals.py
    does; the live loop then extends the archive going forward.
    """
    if not args.backtest_archive or not args.seed_archive_charts:
        return
    archive = Path(args.backtest_archive)
    if archive.exists() and archive.stat().st_size > 0:
        print(f"[auto_self] backtest archive {archive} exists; loop will extend it.")
        return
    chart = CsvChartSource(_expand_globs(args.seed_archive_charts))
    df = chart.dataframe
    if args.seed_archive_start_date:
        start = datetime.strptime(args.seed_archive_start_date, "%Y-%m-%d")
        df = df[df["time"] >= start]
    sigs = generate_rejection_signals(iter_bars(df), rcfg)
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(
        format_generated_signals(sigs, source_tz_offset=3, price_digits=rcfg.price_digits),
        encoding="utf-8",
    )
    print(f"[auto_self] seeded backtest archive {archive}: {len(sigs)} signals "
          f"from {args.seed_archive_start_date or 'start'}.")


def _append_to_archive(archive_path, keyed, price_digits: int) -> None:
    """Append generated signals whose key is not already in the archive,
    preserving per-day numbering: past days are frozen, today's new signals
    append in time order under the existing/created day header. `keyed` is the
    same whole-day-windowed list used for the executable feed, so numbering
    matches the seed. On any parse hiccup we skip rather than risk corrupting
    the archive (it is a backtest input, never the live feed).
    """
    archive = Path(archive_path)
    existing_keys: set[str] = set()
    last_date: str | None = None
    file_has_content = archive.exists() and archive.stat().st_size > 0
    if file_has_content:
        try:
            parsed = parse_signals_file(archive)
        except Exception:
            return
        existing_keys = {s.signal_key for s in parsed}
        if parsed:
            last_date = parsed[-1].signal_time_chart.date().isoformat()

    new = [(s, k) for s, k in keyed if k not in existing_keys]
    if not new:
        return

    lines: list[str] = []
    for s, key in new:  # ascending time
        d = s.signal_time_chart.date().isoformat()
        day_id = int(key.split("#")[1])
        if d != last_date:
            if file_has_content or lines:
                lines.append("")
            lines.append(f"{d} GMT+3")
            last_date = d
        lines.append(_signal_line(s, day_id, price_digits))

    archive.parent.mkdir(parents=True, exist_ok=True)
    with open(archive, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# day-loss circuit breaker state (persisted; survives restarts)
# ---------------------------------------------------------------------------

def _load_day_guard(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def _save_day_guard(path: Path, state: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(state), encoding="utf-8")


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

def _assert_account_mode(conn, expected: str) -> None:
    """Refuse to run if --account does not match the connected MT5 account.
    Prevents ever pointing a demo-tuned or live config at the wrong account.
    """
    info = conn.mt5.account_info()
    if info is None:
        raise SystemExit("account_info() returned None; cannot verify account mode.")
    real_mode = getattr(conn.mt5, "ACCOUNT_TRADE_MODE_REAL", 2)
    is_real = int(info.trade_mode) == int(real_mode)
    want_live = expected == "live"
    if is_real != want_live:
        actual = "live/real" if is_real else "demo/contest"
        raise SystemExit(
            f"ACCOUNT MODE MISMATCH: --account {expected} but MT5 account "
            f"#{getattr(info, 'login', '?')} on {getattr(info, 'server', '?')!r} "
            f"is {actual} (trade_mode={info.trade_mode}). Refusing to run."
        )


def _evaluate_guards(args, conn, equity, chart_now, day_guard_path):
    """Returns (block_new, halted, spread_block). The daily-loss halt is
    equity-based (includes floating P&L), trips once per day, and stays tripped
    until the chart date rolls over.
    """
    today = chart_now.date().isoformat()
    state = _load_day_guard(day_guard_path)
    if state.get("date") != today:
        state = {"date": today, "start_equity": float(equity), "halted": False}
        _save_day_guard(day_guard_path, state)

    halted = bool(state.get("halted"))
    if not halted and args.max_daily_loss_pct > 0:
        start_eq = float(state.get("start_equity") or 0.0)
        if start_eq > 0 and equity <= start_eq * (1.0 - args.max_daily_loss_pct / 100.0):
            halted = True
            state["halted"] = True
            _save_day_guard(day_guard_path, state)
            print(
                f"[guard] DAILY-LOSS HALT: equity {equity:.2f} <= day-start "
                f"{start_eq:.2f} - {args.max_daily_loss_pct:.2f}%. "
                f"No new entries for the rest of {today}."
            )

    spread_block = False
    if args.place_max_spread_points >= 0:
        si = conn.mt5.symbol_info(args.mt5_symbol)
        cur = int(si.spread) if si is not None else 0
        if cur > args.place_max_spread_points:
            spread_block = True

    return (halted or spread_block), halted, spread_block


# ---------------------------------------------------------------------------
# generation step
# ---------------------------------------------------------------------------

def _regenerate_feed(args, config, rcfg, chart, chart_now, signals_path,
                     registry_path, block_new) -> None:
    bars = chart.recent_closed_bars(args.mt5_history_bars)
    sigs = generate_rejection_signals(bars, rcfg)

    cutoff_date = (chart_now - timedelta(days=FEED_WINDOW_DAYS - 1)).date()
    sigs = [s for s in sigs if s.signal_time_chart.date() >= cutoff_date]

    keyed = _keyed_signals(sigs)
    entries = SignalRegistry(registry_path).load()
    placed_keys = {item.get("signal_key") for item in entries}
    placed_count = len(entries)

    # Mirror the executor's acceptance window so we don't expose new signals it
    # would only reject as expired.
    min_new_time = chart_now - timedelta(minutes=config.pending_expiry_minutes + 5)

    allowed = _select_allowed_keys(
        keyed, placed_keys,
        cap=int(args.max_concurrent_positions),
        placed_count=placed_count,
        block_new=block_new,
        min_new_time=min_new_time,
    )
    _atomic_write(signals_path, _render_feed(keyed, allowed, source_tz_offset=3,
                                             price_digits=rcfg.price_digits))

    # Cumulative backtest archive: append every generated signal (guards do NOT
    # apply here -- the archive must reflect the full signal strategy for parity
    # backtests; guards are a live-only overlay on the executable feed).
    if args.backtest_archive:
        _append_to_archive(args.backtest_archive, keyed, rcfg.price_digits)


# ---------------------------------------------------------------------------
# watch loop
# ---------------------------------------------------------------------------

def _run_self_watch(args, config, rcfg, conn, chart, signals_path,
                    registry_path, day_guard_path) -> int:
    interval = float(args.watch_interval)
    iteration = 0
    candidate_console_state: dict[str, str] = {}
    notified_keys: dict[str, set] = {"detected": set(), "skipped": set()}
    last_heartbeat = time.monotonic()
    try:
        while True:
            iteration += 1
            _assert_account_mode(conn, args.account)

            try:
                equity = mt5_equity(conn)
            except Exception as e:
                print(f"[mt5] account_info() failed: {e}", file=sys.stderr)
                time.sleep(interval)
                continue

            chart_now = chart.last_time()
            if chart_now is None:
                print("[mt5] no chart data available; skipping iteration")
                time.sleep(interval)
                continue

            block_new, _halted, _spread = _evaluate_guards(
                args, conn, equity, chart_now, day_guard_path
            )
            _regenerate_feed(
                args, config, rcfg, chart, chart_now,
                signals_path, registry_path, block_new,
            )

            exit_code = _auto_pass(
                args, config, conn, chart, signals_path,
                iteration=iteration,
                candidate_console_state=candidate_console_state,
                notified_keys=notified_keys,
            )
            # A non-zero pass is a transient MT5/sanity condition. Surface it
            # but keep the 24/7 loop alive; the next pass retries.
            if exit_code != 0:
                print(f"[auto_self] pass returned {exit_code}; retrying next cycle.",
                      file=sys.stderr)

            now_monotonic = time.monotonic()
            if now_monotonic - last_heartbeat >= AUTO_HEARTBEAT_SECONDS:
                _print_auto_watch_heartbeat(iteration)
                last_heartbeat = now_monotonic
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        print("Interrupted; exiting auto_self.")
        return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _nonneg_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = auto_explicit.build_parser()
    p.description = (
        "Generate self-rejection signals from closed MT5 bars and execute them "
        "live, 24/7, with safety guards. --signals is the rolling executable feed "
        "this tool (re)writes; use --backtest-archive for a cumulative full-history "
        "file to backtest."
    )

    gen = p.add_argument_group("self-signal generation (defaults match the validated backtest)")
    gen.add_argument("--lookback-bars", type=int, default=20)
    gen.add_argument("--min-wick", type=float, default=1.0)
    gen.add_argument("--min-bar-range", type=float, default=1.5)
    gen.add_argument("--wick-body-ratio", type=float, default=1.2)
    gen.add_argument("--zone-buffer", type=float, default=0.25)
    gen.add_argument("--zone-size", type=float, default=1.0)
    gen.add_argument("--cooldown-minutes", type=int, default=20)
    gen.add_argument("--same-zone-cooldown-minutes", type=int, default=120)
    gen.add_argument("--max-spread-points", type=int, default=35,
                     help="Signal-time spread filter on the rejection bar; -1 disables.")
    gen.add_argument("--session-start-hour", type=int, default=7, help="-1 disables.")
    gen.add_argument("--session-end-hour", type=int, default=22, help="-1 disables.")
    gen.add_argument("--entry-range-width", type=float, default=2.0)
    gen.add_argument("--sl-distance", type=float, default=5.0)
    gen.add_argument("--tp1-distance", type=float, default=10.0)
    gen.add_argument("--tp2-distance", type=float, default=20.0)
    gen.add_argument("--tp3-distance", type=float, default=40.0)
    gen.add_argument("--price-digits", type=int, default=2)

    arch = p.add_argument_group("backtest archive (optional, cumulative full history)")
    arch.add_argument("--backtest-archive", default=None,
                      help="Cumulative signal file that grows with every generated signal -- "
                           "point backtest_explicit at THIS. The live --signals feed stays a rolling window.")
    arch.add_argument("--seed-archive-charts", nargs="+", default=None,
                      help="If the archive is missing, build it once at startup from these M1 CSV(s); globs ok.")
    arch.add_argument("--seed-archive-start-date", default=None, metavar="YYYY-MM-DD")

    safety = p.add_argument_group("live safety (required)")
    safety.add_argument("--account", choices=["demo", "live"], required=True,
                        help="Checked against MT5 trade_mode at startup and every pass.")
    safety.add_argument("--max-concurrent-positions", type=int, required=True,
                        help="Hard cap on live signals/positions; new entries withheld at the cap.")
    safety.add_argument("--max-daily-loss-pct", type=float, required=True,
                        help="Intraday equity drawdown vs day-start that halts new entries; 0 disables.")
    safety.add_argument("--place-max-spread-points", type=_nonneg_int, required=True,
                        help="Skip new placements while current spread exceeds this (points).")
    safety.add_argument("--day-guard-json", default=None,
                        help="Path for the daily-loss state file; defaults next to --positions-json.")
    return p


def _build_rejection_config(args) -> RejectionSignalConfig:
    def or_none(v):
        return None if v < 0 else v
    return RejectionSignalConfig(
        lookback_bars=args.lookback_bars,
        min_wick=args.min_wick,
        min_bar_range=args.min_bar_range,
        wick_body_ratio=args.wick_body_ratio,
        zone_buffer=args.zone_buffer,
        zone_size=args.zone_size,
        cooldown_minutes=args.cooldown_minutes,
        same_zone_cooldown_minutes=args.same_zone_cooldown_minutes,
        max_spread_points=or_none(args.max_spread_points),
        session_start_hour=or_none(args.session_start_hour),
        session_end_hour=or_none(args.session_end_hour),
        entry_range_width=args.entry_range_width,
        sl_distance=args.sl_distance,
        tp1_distance=args.tp1_distance,
        tp2_distance=args.tp2_distance,
        tp3_distance=args.tp3_distance,
        price_digits=args.price_digits,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.max_concurrent_positions < 1:
        raise SystemExit("--max-concurrent-positions must be >= 1")
    if args.max_daily_loss_pct < 0:
        raise SystemExit("--max-daily-loss-pct must be >= 0")
    if args.watch_interval < 1.0:
        raise SystemExit("--watch-interval must be >= 1.0")
    if args.seed_archive_charts and not args.backtest_archive:
        raise SystemExit("--seed-archive-charts requires --backtest-archive")

    config = auto_explicit.config_from_args(args)
    rcfg = _build_rejection_config(args)

    signals_path = Path(args.signals)
    signals_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path = Path(args.positions_json)
    day_guard_path = (
        Path(args.day_guard_json) if args.day_guard_json
        else registry_path.with_name(registry_path.stem + ".dayguard.json")
    )

    conn = Mt5Connection(
        path=args.mt5_path, login=args.mt5_login,
        password=args.mt5_password, server=args.mt5_server,
    )
    conn.initialize()
    try:
        _assert_account_mode(conn, args.account)

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

        _seed_archive_if_missing(args, rcfg)

        chart = Mt5ChartSource(
            conn, symbol=args.mt5_symbol,
            server_offset_hours=args.mt5_server_offset,
            history_bars=args.mt5_history_bars,
        )

        print(
            f"[auto_self] account={args.account} cap={args.max_concurrent_positions} "
            f"daily_loss_pct={args.max_daily_loss_pct} "
            f"place_max_spread={args.place_max_spread_points}"
        )
        print(f"[auto_self] feed={signals_path} archive={args.backtest_archive} "
              f"registry={registry_path} day_guard={day_guard_path}")

        return _run_self_watch(
            args, config, rcfg, conn, chart,
            signals_path, registry_path, day_guard_path,
        )
    finally:
        conn.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())