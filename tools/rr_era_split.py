"""R:R-era split — per-era edge (fixed-lot, in R) and concurrent DD (risk-sized).

READ-ONLY diagnostic. Places no orders, changes no engine state, touches no
DEFAULT_CONFIG. It answers one question the backtest-window choice depends on:
does the per-trade edge measured on 2024 data transfer to 2026, given that the
provider widened his target ladder in roughly three steps?

The eras are pure DATE WINDOWS — backtest_explicit.py / backtest_portfolio_dd.py
already accept --start-date/--end-date, so running them by hand would give the
same numbers. The only things this file adds are (a) an era-fair edge metric and
(b) one table instead of six manual runs.

Why R and not dollars: the provider's SL/TP point-distances roughly doubled in
2026 purely because gold's price doubled, so fixed-lot $ P&L is NOT comparable
across eras. Expectancy in R (realized price move / intended price risk) is
scale-free — a +2R win in 2024 and a +2R win in 2026 mean the same thing. 1R is
the engine's OWN intended risk: entry_count * base_stop_distance, where
base_stop_distance = |entry0 - SL| * sl_multiplier. We read base_stop straight
from core.positions.compute_lot so the R denominator can never drift from what
the engine sizes against.

Two views, per project doctrine:
  EDGE -> fixed-lot pass: fill/win rates + mean/median R (capital-independent).
  DD   -> risk-sized pass: TRUE concurrent mark-to-market DD via
          backtest_portfolio_dd.mtm_drawdown (reused verbatim, no new DD logic),
          gated against --max-drawdown-limit-pct.

Default eras (chart time GMT+3), from the structural analysis of signals.txt:
  A  start .. 2025-03-31      TP3 ~ 3.5R   (folds in the 2024-04/05 settling)
  B  2025-04-01 .. 2026-01-31 TP3 ~ 3.2R
  C  2026-02-01 .. end        TP3 ~ 4.5-6.5R

The fixed-lot and risk-sized passes share fill/exit PRICES (fills are price-
driven, independent of lot), so R is identical either way; it is computed from
the fixed-lot pass for a clean capital-free reading.

Example (TRAILING-0.5 flag block shown; swap in the DD40 block to test that):
  python tools/rr_era_split.py `
    --signals generated/live_provider_high_growth_hour_side.txt `
    --charts data/XAUUSD_M1_*_ELEV8.csv `
    --output-dir reports/rr_era_split `
    --max-drawdown-limit-pct 40 --progress-interval-seconds 30 `
    --initial-capital 5000 --sizing-mode risk --lot 0.01 --risk 0.022 `
    --minimum-lot 0.01 --lot-step 0.01 --bonus-per-closed-lot 3 `
    --entries 1 --entry-ladder range_uniform --entry-sl-gap 2 `
    --activation-delay 0 --pending-expiry 630 --max-hold 15 --sl-multiplier 0.85 `
    --final-target TP1 --lock-after-tp1 true --lock-after-tp2 true `
    --tp1-lock-delay-minutes 0 --tp2-lock-delay-minutes 0 `
    --profit-lock-mode tp_levels --bep-trigger-distance 3 --tp1-lock-fraction 0.5 `
    --tp2-lock-target TP1 --runner-after-tp3 false --tp3-lock-target TP2 `
    --trailing-open-distance 0.5 --trailing-close-distance 0.5
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from xauusd_trading import CsvChartSource, parse_signals_file, run_backtest  # noqa: E402
from xauusd_trading.core.positions import compute_entries, compute_lot  # noqa: E402

# Reuse the explicit (no-hidden-defaults) flag block and the validated concurrent
# DD function verbatim -- no fork of the contract surface or the DD measurement.
from backtest_explicit import (  # noqa: E402
    add_required_strategy_args,
    config_from_args,
    _expand_chart_paths,
    filter_signals_by_date,
)
from backtest_portfolio_dd import mtm_drawdown, _entries_from_result  # noqa: E402


DEFAULT_ERAS: list[tuple[str, str | None, str | None]] = [
    ("A_le_2025-03", None, "2025-03-31"),
    ("B_2025-04_2026-01", "2025-04-01", "2026-01-31"),
    ("C_ge_2026-02", "2026-02-01", None),
]


# ---------------------------------------------------------------------------
# pure helpers (testable without market data)
# ---------------------------------------------------------------------------
def parse_era_spec(spec: str) -> tuple[str, str | None, str | None]:
    """'label:START:END' -> (label, start_or_None, end_or_None); blank dates open the side."""
    parts = spec.split(":")
    if len(parts) != 3:
        raise SystemExit(f"--era must be label:START:END (got {spec!r}); leave a side blank to open it")
    label, start, end = (p.strip() for p in parts)
    return label, (start or None), (end or None)


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def signal_intended_risk_price(sig, config) -> float:
    """1R in PRICE units = total intended stop distance summed across the ladder.

    base_stop comes from compute_lot (computed before any sizing branch, so it is
    equity- and mode-independent); the engine gives every ladder entry the same
    base_stop, hence total = n_entries * base_stop.
    """
    entries = compute_entries(sig, config)
    if not entries:
        return 0.0
    _, base_stop = compute_lot(config.initial_capital, sig, config)
    return len(entries) * float(base_stop)


def _signed_move(side: str, entry_price: float, exit_price: float) -> float:
    return (exit_price - entry_price) if side == "BUY" else (entry_price - exit_price)


def realized_price_move(entry_rows_for_signal: list[dict]) -> float:
    """Sum of per-entry signed price moves over CLOSED entries; NO_FILL contributes 0.

    Ladder entries carry equal lot in the engine, so price moves add without lot
    weighting -- dividing this by the signal's intended risk gives R directly.
    """
    total = 0.0
    for e in entry_rows_for_signal:
        if e.get("fill_time") is None or e.get("exit_time") is None or e.get("exit_price") is None:
            continue
        total += _signed_move(e["side"], float(e["entry_price"]), float(e["exit_price"]))
    return total


def summarize_edge(result: dict, risk_by_key: dict[str, float]) -> dict:
    """Fill/win rates + R expectancy for one era's fixed-lot pass."""
    entries_by_key: dict[str, list[dict]] = {}
    for e in result["entry_rows"]:
        entries_by_key.setdefault(e["signal_key"], []).append(e)

    r_all: list[float] = []
    r_filled: list[float] = []
    filled = 0
    for row in result["rows"]:
        key = row["signal_key"]
        ents = entries_by_key.get(key, [])
        risk = risk_by_key.get(key, 0.0)
        move = realized_price_move(ents)
        r = (move / risk) if risk > 0 else 0.0
        r_all.append(r)
        if any(e.get("fill_time") is not None for e in ents):
            filled += 1
            r_filled.append(r)

    n = len(result["rows"])
    return {
        "signals": n,
        "fill_%": (filled / n * 100.0) if n else 0.0,
        "win_%": result["win_rate_pct"],
        "no_fill": result["no_fills"],
        "fixed_net_no_bonus": result["trading_pnl"],
        "meanR_all": _mean(r_all),
        "meanR_filled": _mean(r_filled),
        "medR_filled": _median(r_filled),
    }


def measure_concurrent_dd(result: dict, times, closes, spreads, tindex, initial: float) -> dict:
    """True concurrent mark-to-market DD for one era's risk-sized pass."""
    entries, skipped = _entries_from_result(result, tindex)
    mtm_dd, peak_conc, _ = mtm_drawdown(entries, times, closes, spreads, initial)
    return {
        "filled_positions": len(entries),
        "skipped": skipped,
        "true_dd_%": mtm_dd,
        "peak_conc": peak_conc,
    }


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def print_tables(rows: list[dict], limit: float) -> None:
    edge_cols = ["era", "signals", "fill_%", "win_%", "no_fill",
                 "meanR_all", "meanR_filled", "medR_filled", "fixed_net_no_bonus"]
    dd_cols = ["era", "filled_positions", "peak_conc", "true_dd_%", f"dd<=" + f"{limit:g}%"]

    def render(title, cols, data):
        w = {c: max(len(c), max((len(_fmt(r.get(c))) for r in data), default=0)) for c in cols}
        print(f"\n{title}")
        print("  ".join(f"{c:>{w[c]}}" for c in cols))
        for r in data:
            print("  ".join(f"{_fmt(r.get(c)):>{w[c]}}" for c in cols))

    render("EDGE  (fixed-lot; R = realized price move / intended risk; no-fill counts as 0R)",
           edge_cols, rows)
    render("DD  (risk-sized; true concurrent mark-to-market; equity restarts per era)",
           dd_cols, rows)


def write_xlsx(path: Path, rows: list[dict]) -> Path:
    import openpyxl
    import pandas as pd
    from openpyxl.styles import Font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(rows).to_excel(xw, sheet_name="rr_era_split", index=False)
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    ws.freeze_panes = "A2"
    for cell in ws[1]:
        cell.font = Font(bold=True)
    wb.save(path)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rr_era_split",
        description="Per-era edge (R) and concurrent DD across the provider's R:R-template eras.",
    )
    p.add_argument("--signals", required=True, help="Executable/filtered signal file to backtest.")
    p.add_argument("--charts", required=True, nargs="+")
    p.add_argument("--era", action="append", default=None, metavar="LABEL:START:END",
                   help="Repeatable. Dates YYYY-MM-DD (chart time GMT+3), blank side = open. "
                        "Omit to use the three default R:R eras.")
    p.add_argument("--output-dir", default=None, help="Optional directory for the .xlsx report.")
    p.add_argument("--exclude-structural-anomalies", action="store_true")
    p.add_argument("--max-drawdown-limit-pct", type=float, required=True)
    p.add_argument("--progress-interval-seconds", type=float, default=0.0,
                   help="If > 0, print a per-era heartbeat to stderr.")
    add_required_strategy_args(p)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_config = config_from_args(args)
    chatty = args.progress_interval_seconds > 0

    chart = CsvChartSource(_expand_chart_paths(args.charts))
    df = chart.dataframe
    times = [t.to_pydatetime() for t in df["time"]]
    closes = df["close"].to_numpy()
    spreads = df["spread_price"].to_numpy()
    tindex = {t: i for i, t in enumerate(times)}

    all_signals = parse_signals_file(Path(args.signals))
    eras = [parse_era_spec(s) for s in args.era] if args.era else DEFAULT_ERAS

    rows: list[dict] = []
    for label, start, end in eras:
        era_signals = filter_signals_by_date(all_signals, start, end)
        if chatty:
            print(f"era {label}: {len(era_signals)} signals ({start or 'start'}..{end or 'end'})",
                  file=sys.stderr)
        if not era_signals:
            rows.append({"era": label, "signals": 0})
            continue

        risk_by_key = {s.signal_key: signal_intended_risk_price(s, base_config) for s in era_signals}

        edge_res = run_backtest(
            era_signals, chart, replace(base_config, sizing_mode="fixed"),
            exclude_structural_anomalies=args.exclude_structural_anomalies,
        )
        risk_res = run_backtest(
            era_signals, chart, replace(base_config, sizing_mode="risk"),
            exclude_structural_anomalies=args.exclude_structural_anomalies,
        )

        row = {"era": label}
        row.update(summarize_edge(edge_res, risk_by_key))
        dd = measure_concurrent_dd(risk_res, times, closes, spreads, tindex, base_config.initial_capital)
        row.update(dd)
        row["dd<=" + f"{args.max_drawdown_limit_pct:g}%"] = (
            "PASS" if abs(dd["true_dd_%"]) <= args.max_drawdown_limit_pct else "FAIL"
        )
        rows.append(row)

    print_tables(rows, args.max_drawdown_limit_pct)
    print("\nNOTE: R is scale-free and era-comparable; fixed_net_no_bonus is NOT (point-distances")
    print("      scale with gold's price). DD restarts equity per era and inherits run_backtest")
    print("      look-ahead lot sizing; live trailing-close adds broker clamp + slippage.")

    if args.output_dir:
        out = write_xlsx(Path(args.output_dir) / "rr_era_split.xlsx", rows)
        print(f"\nWrote Excel output to {out.resolve()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())