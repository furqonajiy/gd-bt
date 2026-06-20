#!/usr/bin/env python3
"""Single live runner: self M15 signal generation + explicit MT5 execution.

Each watch pass regenerates the executable signal feed from latest CLOSED MT5 M1
bars, aggregates them to M15 with the same generator used by
``tools/generate_self_signals.py``, then hands that feed to the validated auto
execution pass. CSV files are only for archive/backtest seeding, never for live
signal freshness.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import auto_explicit  # noqa: E402
import generate_self_signals as selfgen  # noqa: E402

from trading.engine import (  # noqa: E402
    Mt5ChartSource,
    Mt5Connection,
    SignalRegistry,
    archive_m1_by_month,
    mt5_equity,
    parse_signals_file,
    render_archive_summary,
)
from trading.engine.cli import (  # noqa: E402
    ARCHIVE_DIR,
    ARCHIVE_MONTHS,
    AUTO_HEARTBEAT_SECONDS,
    _auto_pass,
    _print_auto_watch_heartbeat,
)

FEED_WINDOW_DAYS = 2


# ---------------------------------------------------------------------------
# feed construction
# ---------------------------------------------------------------------------

def _keyed_signals(signals):
    ordered = sorted(signals, key=lambda s: (s.signal_time_chart, s.side))
    out = []
    per_day: dict[str, int] = {}
    for signal in ordered:
        date_key = signal.signal_time_chart.date().isoformat()
        per_day[date_key] = per_day.get(date_key, 0) + 1
        out.append((signal, f"{date_key}#{per_day[date_key]:02d}"))
    return out


def _signal_line(signal, day_id: int, price_digits: int) -> str:
    digits = int(price_digits)

    def px(value: float) -> str:
        return f"{value:.{digits}f}"

    t = signal.signal_time_chart.strftime("%I:%M %p")
    return (
        f"{day_id}. {signal.side} XAUUSD {px(signal.r1)} - {px(signal.r2)} "
        f"SL {px(signal.sl)} TP1 {px(signal.tp1)} TP2 {px(signal.tp2)} TP3 {px(signal.tp3)} {t}"
    )


def _select_allowed_keys(keyed, placed_keys, *, cap, placed_count, block_new, min_new_time=None):
    allowed = {key for _signal, key in keyed if key in placed_keys}
    if block_new:
        return allowed
    remaining = max(0, int(cap) - int(placed_count))
    if remaining <= 0:
        return allowed
    used = 0
    for signal, key in sorted(keyed, key=lambda sk: sk[0].signal_time_chart, reverse=True):
        if key in placed_keys:
            continue
        if min_new_time is not None and signal.signal_time_chart <= min_new_time:
            continue
        allowed.add(key)
        used += 1
        if used >= remaining:
            break
    return allowed


def _render_feed(keyed, allowed_keys, *, source_tz_offset, price_digits):
    tz = f"GMT+{source_tz_offset}" if source_tz_offset >= 0 else f"GMT{source_tz_offset}"
    lines: list[str] = []
    last_date: str | None = None
    for signal, key in keyed:
        if key not in allowed_keys:
            continue
        date_key = signal.signal_time_chart.date().isoformat()
        day_id = int(key.split("#")[1])
        if date_key != last_date:
            if lines:
                lines.append("")
            lines.append(f"{date_key} {tz}")
            last_date = date_key
        lines.append(_signal_line(signal, day_id, price_digits))
    return ("\n".join(lines) + "\n") if lines else ""


def _atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# archive seeding/appending
# ---------------------------------------------------------------------------

def _expand_globs(patterns) -> list[Path]:
    out: list[Path] = []
    for pattern in patterns or []:
        if any(ch in pattern for ch in "*?["):
            matches = sorted(glob.glob(pattern))
            if not matches:
                continue
            out.extend(Path(match) for match in matches)
        else:
            path = Path(pattern)
            if path.exists():
                out.append(path)
    return out


def _seed_namespace(args) -> SimpleNamespace:
    m1_charts = args.seed_archive_m1_charts or args.seed_archive_charts
    return SimpleNamespace(
        m15_charts=args.seed_archive_m15_charts,
        m1_charts=m1_charts,
        charts=None,
        start_date=args.seed_archive_start_date,
        end_date=None,
        source_tz_offset=3,
        price_digits=args.price_digits,
        ema_fast=args.ema_fast,
        ema_slow=args.ema_slow,
        atr_period=args.atr_period,
        min_atr=args.min_atr,
        max_atr=args.max_atr,
        same_side_spacing_minutes=args.same_side_spacing_minutes,
        max_signals_per_day=args.max_signals_per_day,
        entry_offset=args.entry_offset,
        range_width=args.range_width,
        sl_gap_from_range=args.sl_gap_from_range,
        tp1_distance=args.tp1_distance,
        tp2_distance=args.tp2_distance,
        tp3_distance=args.tp3_distance,
    )


def _seed_archive_if_missing(args) -> None:
    if not args.backtest_archive:
        return
    if not (args.seed_archive_charts or args.seed_archive_m1_charts or args.seed_archive_m15_charts):
        return
    archive = Path(args.backtest_archive)
    if archive.exists() and archive.stat().st_size > 0:
        print(f"[auto_self] backtest archive {archive} exists; loop will extend it.")
        return

    seed_args = _seed_namespace(args)
    if not _expand_globs(seed_args.m1_charts) and not _expand_globs(seed_args.m15_charts):
        raise SystemExit("No seed archive chart files found.")
    signals = selfgen.generate_signals(seed_args)
    selfgen.write_signal_file(signals, archive, source_tz_offset=3, price_digits=args.price_digits)
    print(
        f"[auto_self] seeded backtest archive {archive}: {len(signals)} signals "
        f"from {args.seed_archive_start_date or 'start'}."
    )


def _append_to_archive(archive_path, keyed, price_digits: int) -> None:
    archive = Path(archive_path)
    existing_keys: set[str] = set()
    last_date: str | None = None
    file_has_content = archive.exists() and archive.stat().st_size > 0
    if file_has_content:
        try:
            parsed = parse_signals_file(archive)
        except Exception:
            return
        existing_keys = {signal.signal_key for signal in parsed}
        if parsed:
            last_date = parsed[-1].signal_time_chart.date().isoformat()

    new = [(signal, key) for signal, key in keyed if key not in existing_keys]
    if not new:
        return

    lines: list[str] = []
    for signal, key in new:
        date_key = signal.signal_time_chart.date().isoformat()
        day_id = int(key.split("#")[1])
        if date_key != last_date:
            if file_has_content or lines:
                lines.append("")
            lines.append(f"{date_key} GMT+3")
            last_date = date_key
        lines.append(_signal_line(signal, day_id, price_digits))

    archive.parent.mkdir(parents=True, exist_ok=True)
    with open(archive, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# guards
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


def _assert_account_mode(conn, expected: str) -> None:
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
    cur_spread = -1  # -1 = unknown (symbol_info unavailable); shown as 'n/a'
    if args.place_max_spread_points >= 0:
        si = conn.mt5.symbol_info(args.mt5_symbol)
        cur_spread = int(si.spread) if si is not None else 0
        if cur_spread > args.place_max_spread_points:
            spread_block = True

    return (halted or spread_block), halted, spread_block, cur_spread


# ---------------------------------------------------------------------------
# live generation
# ---------------------------------------------------------------------------

def _regenerate_feed(args, config, chart, chart_now, signals_path, registry_path, block_new) -> dict:
    bars_needed = max(int(args.mt5_history_bars), int(args.live_generation_bars))
    bars = chart.recent_closed_bars(bars_needed)
    bars_count = len(bars)
    signals = selfgen.generate_signals_from_m1_bars(bars, args)
    generated = len(signals)

    cutoff_date = (chart_now - timedelta(days=FEED_WINDOW_DAYS - 1)).date()
    signals = [signal for signal in signals if signal.signal_time_chart.date() >= cutoff_date]
    in_window = len(signals)

    keyed = _keyed_signals(signals)
    entries = SignalRegistry(registry_path).load()
    placed_keys = {item.get("signal_key") for item in entries}
    placed_count = len(entries)
    min_new_time = chart_now - timedelta(minutes=config.pending_expiry_minutes + 5)

    allowed = _select_allowed_keys(
        keyed, placed_keys,
        cap=int(args.max_concurrent_positions),
        placed_count=placed_count,
        block_new=block_new,
        min_new_time=min_new_time,
    )
    _atomic_write(
        signals_path,
        _render_feed(keyed, allowed, source_tz_offset=3, price_digits=args.price_digits),
    )

    if args.backtest_archive:
        _append_to_archive(args.backtest_archive, keyed, args.price_digits)

    return {
        "bars": bars_count, "generated": generated, "in_window": in_window,
        "allowed": len(allowed), "placed": placed_count,
        "cap": int(args.max_concurrent_positions),
    }


def _format_gen_diag(stats: dict, *, halted: bool, spread_block: bool,
                     cur_spread: int, block_new: bool) -> tuple[str, str]:
    """Per-cycle feed diagnostic: a console line plus a stable dedup signature.

    The dedup signature deliberately omits the raw spread (it flaps every tick)
    but keeps the derived block flags, so the line reprints only on a meaningful
    transition -- a signal entering/leaving the window, a placement, or the
    spread crossing the place threshold -- not on every watch interval.
    """
    if block_new:
        reason = "halt+spread" if (halted and spread_block) else ("halt" if halted else "spread")
    else:
        reason = "no"
    spread_txt = cur_spread if cur_spread >= 0 else "n/a"
    line = (
        f"[gen] bars={stats['bars']} generated={stats['generated']} "
        f"in-window={stats['in_window']} allowed={stats['allowed']} "
        f"placed={stats['placed']}/{stats['cap']} spread={spread_txt} block_new={reason}"
    )
    sig = (
        f"{stats['bars'] > 0}|{stats['generated']}|{stats['in_window']}|"
        f"{stats['allowed']}|{stats['placed']}|{stats['cap']}|{halted}|{spread_block}"
    )
    return line, sig


# ---------------------------------------------------------------------------
# watch loop
# ---------------------------------------------------------------------------

def _run_self_watch(args, config, conn, chart, signals_path, registry_path, day_guard_path) -> int:
    interval = float(args.watch_interval)
    iteration = 0
    candidate_console_state: dict[str, str] = {}
    notified_keys: dict[str, set] = {"detected": set(), "skipped": set()}
    last_heartbeat = time.monotonic()
    last_gen_diag: str | None = None  # dedups the [gen] line to state transitions
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

            block_new, halted, spread_block, cur_spread = _evaluate_guards(
                args, conn, equity, chart_now, day_guard_path)
            feed_stats = _regenerate_feed(
                args, config, chart, chart_now, signals_path, registry_path, block_new)

            diag_line, diag_sig = _format_gen_diag(
                feed_stats, halted=halted, spread_block=spread_block,
                cur_spread=cur_spread, block_new=block_new)
            if diag_sig != last_gen_diag:
                print(diag_line)
                last_gen_diag = diag_sig

            exit_code = _auto_pass(
                args, config, conn, chart, signals_path,
                iteration=iteration,
                candidate_console_state=candidate_console_state,
                notified_keys=notified_keys,
            )
            if exit_code != 0:
                print(f"[auto_self] pass returned {exit_code}; retrying next cycle.", file=sys.stderr)

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
        "Generate self M15 trend-pullback signals from closed MT5 M1 bars and "
        "execute them live with the explicit strategy contract."
    )

    gen = p.add_argument_group("self-signal generation")
    selfgen.add_generation_args(gen)
    gen.add_argument("--price-digits", type=int, default=2)
    gen.add_argument("--live-generation-bars", type=int, default=5000,
                     help="Closed MT5 M1 bars used to build live M15 indicators.")

    arch = p.add_argument_group("backtest archive (optional, cumulative full history)")
    arch.add_argument("--backtest-archive", default=None)
    arch.add_argument("--seed-archive-charts", nargs="+", default=None,
                      help="Legacy alias for --seed-archive-m1-charts.")
    arch.add_argument("--seed-archive-m1-charts", nargs="+", default=None)
    arch.add_argument("--seed-archive-m15-charts", nargs="+", default=None)
    arch.add_argument("--seed-archive-start-date", default=None, metavar="YYYY-MM-DD")

    safety = p.add_argument_group("live safety (required)")
    safety.add_argument("--account", choices=["demo", "live"], required=True)
    safety.add_argument("--max-concurrent-positions", type=int, required=True)
    safety.add_argument("--max-daily-loss-pct", type=float, required=True)
    safety.add_argument("--place-max-spread-points", type=_nonneg_int, required=True)
    safety.add_argument("--day-guard-json", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.max_concurrent_positions < 1:
        raise SystemExit("--max-concurrent-positions must be >= 1")
    if args.max_daily_loss_pct < 0:
        raise SystemExit("--max-daily-loss-pct must be >= 0")
    if args.watch_interval < 1.0:
        raise SystemExit("--watch-interval must be >= 1.0")
    if args.live_generation_bars < 2000:
        raise SystemExit("--live-generation-bars must be >= 2000")
    if (args.seed_archive_charts or args.seed_archive_m1_charts or args.seed_archive_m15_charts) and not args.backtest_archive:
        raise SystemExit("seed archive chart flags require --backtest-archive")

    config = auto_explicit.config_from_args(args)

    signals_path = Path(args.signals)
    signals_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path = Path(args.positions_json)
    day_guard_path = (
        Path(args.day_guard_json) if args.day_guard_json
        else registry_path.with_name(registry_path.stem + ".dayguard.json")
    )

    conn = Mt5Connection(
        path=args.mt5_path,
        login=args.mt5_login,
        password=args.mt5_password,
        server=args.mt5_server,
    )
    conn.initialize()
    try:
        _assert_account_mode(conn, args.account)

        try:
            summary = archive_m1_by_month(
                conn,
                args.mt5_symbol,
                ARCHIVE_DIR,
                months_back=ARCHIVE_MONTHS,
                server_offset_hours=args.mt5_server_offset,
                overwrite=False,
            )
            print(render_archive_summary(summary))
            print()
        except Exception as e:
            print(f"[mt5] archive failed (continuing): {e}", file=sys.stderr)

        _seed_archive_if_missing(args)

        chart = Mt5ChartSource(
            conn,
            symbol=args.mt5_symbol,
            server_offset_hours=args.mt5_server_offset,
            history_bars=args.mt5_history_bars,
        )

        print(
            f"[auto_self] account={args.account} cap={args.max_concurrent_positions} "
            f"daily_loss_pct={args.max_daily_loss_pct} "
            f"place_max_spread={args.place_max_spread_points}"
        )
        print(
            f"[auto_self] live_generation_bars={args.live_generation_bars} "
            f"feed={signals_path} archive={args.backtest_archive} "
            f"registry={registry_path} day_guard={day_guard_path}"
        )

        return _run_self_watch(args, config, conn, chart, signals_path, registry_path, day_guard_path)
    finally:
        conn.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())