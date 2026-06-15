"""Phase 2 — turn the with/counter-trend flag into P&L.

READ-ONLY. No engine or order changes. Compares three ways of acting on the
direction flag from Phase 1:

  incumbent    take every signal (current strategy)
  with_trend   take only signals whose side agrees with the EMA trend
  derate       take every signal, but size counter-trend signals down

Two views, per the project doctrine:
  * EDGE  -> fixed-lot net + bonus (capital-independent), sliced by year so we
            see whether any advantage is stable or just one good year.
  * DD    -> risk-sized run, full period, for the <= 40% gate.

The derate's DD needs a per-signal risk, which run_backtest does not take, so we
compose a thin compounding loop over the SAME replay_signal engine (no fork). A
parity test asserts that with derate-factor 1.0 it reproduces run_backtest.

Example:

    python tools/regime_pnl_compare.py \
      --signals signals.txt \
      --charts data/XAUUSD_M1_*_ELEV8.csv \
      --derate-factor 0.5 \
      --output-dir reports/regime_pnl
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xauusd_trading import (
    CONTRACT_SIZE_OZ,
    DEFAULT_CONFIG,
    StrategyConfig,
    CsvChartSource,
    parse_signals_file,
)
from xauusd_trading.strategy.backtest import run_backtest, replay_signal, position_status


def _load_classifier():
    # Single source of truth for the regime label: reuse Phase-1's classifier
    # instead of re-implementing it (no drift between the two tools).
    spec = importlib.util.spec_from_file_location(
        "regime_split_analysis", ROOT / "tools" / "regime_split_analysis.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_rs = _load_classifier()
compute_indicator_series = _rs.compute_indicator_series
label_signal_regime = _rs.label_signal_regime


# ---------------------------------------------------------------------------
# labeling
# ---------------------------------------------------------------------------
def label_signals(signals, series: dict, warmup: int) -> dict[str, dict]:
    """Map signal_key -> {classified, with_trend} using the Phase-1 classifier."""
    out: dict[str, dict] = {}
    for sig in signals:
        lab = label_signal_regime(series, sig.signal_time_chart, sig.side, warmup)
        out[sig.signal_key] = {
            "classified": lab["classified"],
            "with_trend": bool(lab.get("with_trend")) if lab["classified"] else False,
        }
    return out


# ---------------------------------------------------------------------------
# per-signal-risk compounding loop (reuses replay_signal; not an engine fork)
# ---------------------------------------------------------------------------
def _closed_lots(pos) -> float:
    return sum(
        float(e.lot or 0.0)
        for e in pos.entries
        if e.fill_time is not None and e.exit_time is not None
    )


def run_per_signal_risk(signals, chart_df, base_config: StrategyConfig,
                        counter_keys: set[str], derate_factor: float,
                        contract_size: float = CONTRACT_SIZE_OZ,
                        exclude_structural_anomalies: bool = False) -> dict:
    """Risk-sized compounding run where counter-trend signals use derated risk.

    Identical to run_backtest when every signal uses base_config (i.e. when
    counter_keys is empty or derate_factor == 1.0) — that equivalence is the
    parity guard in the tests.
    """
    full = base_config
    derated = replace(base_config, risk_per_signal=base_config.risk_per_signal * derate_factor)
    bonus_rate = float(getattr(base_config, "bonus_per_closed_lot", 0.0) or 0.0)

    equity = base_config.initial_capital
    chart_start = chart_df["time"].iloc[0].to_pydatetime() if len(chart_df) else None
    chart_end = chart_df["time"].iloc[-1].to_pydatetime() if len(chart_df) else None
    rows: list[dict] = []
    for sig in signals:
        if chart_start is None or sig.signal_time_chart < chart_start or sig.signal_time_chart > chart_end:
            continue
        if exclude_structural_anomalies and sig.structural_anomaly:
            continue
        cfg = derated if sig.signal_key in counter_keys else full
        pos = replay_signal(sig, chart_df, equity, cfg, contract_size)
        status, trading_pnl = position_status(pos)
        if status == "OPEN":
            pnl = None
            equity_after = equity
        else:
            closed = _closed_lots(pos)
            pnl = trading_pnl + closed * bonus_rate
            equity_after = equity + pnl
        rows.append({"signal_key": sig.signal_key, "status": status,
                     "pnl": pnl, "equity_after": equity_after})
        if status != "OPEN":
            equity = equity_after
        if equity <= 0:
            break

    max_dd = 0.0
    peak = base_config.initial_capital
    for r in rows:
        eq = r["equity_after"]
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (eq - peak) / peak * 100.0
            if dd < max_dd:
                max_dd = dd
    return {"net": equity - base_config.initial_capital, "max_dd_pct": max_dd,
            "final_equity": equity, "signals_run": len(rows)}


# ---------------------------------------------------------------------------
# fixed-lot edge (capital-independent; subset/scale arithmetic is exact)
# ---------------------------------------------------------------------------
def _fold(dt) -> str:
    return str(dt.year)


def fixed_lot_edge(fixed_rows: list[dict], labels: dict[str, dict],
                   derate_factor: float) -> dict:
    """Per-fold incumbent / with-trend / derate net from a single fixed-lot run.

    At a fixed lot each trade's P&L is independent of the others, so filtering
    is a subset sum and derating is a linear scale of the counter-trend subset:
      derate = incumbent - (1 - factor) * counter_pnl
    """
    folds: dict[str, dict] = {}
    for r in fixed_rows:
        if r["pnl"] is None:
            continue
        lab = labels.get(r["signal_key"], {"classified": False, "with_trend": False})
        is_counter = lab["classified"] and not lab["with_trend"]
        is_with = lab["classified"] and lab["with_trend"]
        for key in ("ALL", _fold(r["signal_time_chart"])):
            b = folds.setdefault(key, {
                "fold": key, "signals": 0, "with_trend_signals": 0, "counter_signals": 0,
                "incumbent_net": 0.0, "with_trend_net": 0.0, "counter_net": 0.0,
            })
            b["signals"] += 1
            b["incumbent_net"] += r["pnl"]
            if is_with:
                b["with_trend_signals"] += 1
                b["with_trend_net"] += r["pnl"]
            elif is_counter:
                b["counter_signals"] += 1
                b["counter_net"] += r["pnl"]

    rows = []
    for key in ["ALL"] + sorted(k for k in folds if k != "ALL"):
        b = folds[key]
        derate_net = b["incumbent_net"] - (1.0 - derate_factor) * b["counter_net"]
        n = b["signals"]
        rows.append({
            "fold": key,
            "signals": n,
            "with/counter": f'{b["with_trend_signals"]}/{b["counter_signals"]}',
            "incumbent_net": b["incumbent_net"],
            "with_trend_net": b["with_trend_net"],
            "derate_net": derate_net,
            "d_with_trend": b["with_trend_net"] - b["incumbent_net"],
            "d_derate": derate_net - b["incumbent_net"],
            "incumbent_net_per_sig": b["incumbent_net"] / n if n else None,
            "with_trend_net_per_sig": (b["with_trend_net"] / b["with_trend_signals"]
                                       if b["with_trend_signals"] else None),
        })
    return {"rows": rows}


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def print_edge(edge: dict) -> None:
    cols = ["fold", "signals", "with/counter", "incumbent_net", "with_trend_net",
            "derate_net", "d_with_trend", "d_derate"]
    widths = {c: max(len(c), max(len(_fmt(r[c])) for r in edge["rows"])) for c in cols}
    print("\nFixed-lot edge — net $ (capital-independent), by year")
    print("  ".join(f"{c:>{widths[c]}}" for c in cols))
    for r in edge["rows"]:
        print("  ".join(f"{_fmt(r[c]):>{widths[c]}}" for c in cols))


def print_dd(dd_rows: list[dict]) -> None:
    cols = ["variant", "signals_run", "net", "max_dd_pct", "final_equity", "dd_ok<=40%"]
    widths = {c: max(len(c), max(len(_fmt(r.get(c))) for r in dd_rows)) for c in cols}
    print("\nRisk-sized — DD gate, full period")
    print("  ".join(f"{c:>{widths[c]}}" for c in cols))
    for r in dd_rows:
        print("  ".join(f"{_fmt(r.get(c)):>{widths[c]}}" for c in cols))


def write_xlsx(path: Path, edge: dict, dd_rows: list[dict], signals_df: pd.DataFrame) -> Path:
    import openpyxl
    from openpyxl.styles import Font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(edge["rows"]).to_excel(xw, sheet_name="fixed_lot_edge", index=False)
        pd.DataFrame(dd_rows).to_excel(xw, sheet_name="risk_dd", index=False)
        signals_df.to_excel(xw, sheet_name="signals", index=False)
    wb = openpyxl.load_workbook(path)
    for ws in wb.worksheets:
        ws.freeze_panes = "A2"
        for cell in ws[1]:
            cell.font = Font(bold=True)
    wb.save(path)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _expand_chart_paths(patterns: list[str]) -> list[Path]:
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


def _base_config(args: argparse.Namespace) -> StrategyConfig:
    return replace(
        DEFAULT_CONFIG,
        entry_count=args.entries,
        entry_ladder=args.entry_ladder,
        entry_sl_gap=args.entry_sl_gap,
        activation_delay_minutes=args.activation_delay,
        pending_expiry_minutes=args.pending_expiry,
        max_hold_minutes=args.max_hold,
        sl_multiplier=args.sl_multiplier,
        final_target=args.final_target,
        risk_per_signal=args.risk,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="regime_pnl_compare",
        description="Compare incumbent vs with-trend filter vs counter-trend derate in P&L.",
    )
    p.add_argument("--signals", required=True)
    p.add_argument("--charts", required=True, nargs="+")
    p.add_argument("--output-dir", default=None)
    p.add_argument("--exclude-structural-anomalies", action="store_true")
    p.add_argument("--derate-factor", type=float, default=0.5,
                   help="Risk/size multiplier applied to counter-trend signals.")

    # Classifier (fixed; do not tune).
    p.add_argument("--ema-fast", type=int, default=DEFAULT_CONFIG.trend_runner_ema_fast)
    p.add_argument("--ema-slow", type=int, default=DEFAULT_CONFIG.trend_runner_ema_slow)
    p.add_argument("--atr-period", type=int, default=DEFAULT_CONFIG.trend_runner_atr_period)
    p.add_argument("--warmup", type=int, default=None)

    # Edge run uses a fixed lot and inflated capital so P&L is capital-independent
    # and the equity<=0 break never truncates the signal stream.
    p.add_argument("--fixed-lot", type=float, default=1.0)
    p.add_argument("--fixed-capital", type=float, default=10_000_000.0)
    p.add_argument("--risk", type=float, default=DEFAULT_CONFIG.risk_per_signal)

    # Path geometry (defaults track the DD40 contract).
    p.add_argument("--entries", type=int, default=DEFAULT_CONFIG.entry_count)
    p.add_argument("--entry-ladder", default=DEFAULT_CONFIG.entry_ladder,
                   choices=["signal_range_3", "range_uniform", "range_to_sl"])
    p.add_argument("--entry-sl-gap", type=float, default=DEFAULT_CONFIG.entry_sl_gap)
    p.add_argument("--activation-delay", type=int, default=DEFAULT_CONFIG.activation_delay_minutes)
    p.add_argument("--pending-expiry", type=int, default=DEFAULT_CONFIG.pending_expiry_minutes)
    p.add_argument("--max-hold", type=int, default=DEFAULT_CONFIG.max_hold_minutes)
    p.add_argument("--sl-multiplier", type=float, default=DEFAULT_CONFIG.sl_multiplier)
    p.add_argument("--final-target", default=DEFAULT_CONFIG.final_target, choices=["TP1", "TP2", "TP3"])
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = _base_config(args)
    warmup = args.warmup if args.warmup is not None else 5 * args.ema_slow
    factor = args.derate_factor

    signals = parse_signals_file(Path(args.signals))
    chart = CsvChartSource(_expand_chart_paths(args.charts))
    chart_df = chart.dataframe

    series = compute_indicator_series(chart_df, args.ema_fast, args.ema_slow, args.atr_period)
    labels = label_signals(signals, series, warmup)
    counter_keys = {k for k, v in labels.items() if v["classified"] and not v["with_trend"]}
    with_trend_keys = {k for k, v in labels.items() if v["classified"] and v["with_trend"]}
    with_trend_signals = [s for s in signals if s.signal_key in with_trend_keys]

    # --- EDGE: one fixed-lot run, then exact subset/scale arithmetic ---
    fixed_cfg = replace(base, sizing_mode="fixed", lot_per_entry=args.fixed_lot,
                        initial_capital=args.fixed_capital)
    fixed_res = run_backtest(signals, chart, fixed_cfg,
                             exclude_structural_anomalies=args.exclude_structural_anomalies)
    edge = fixed_lot_edge(fixed_res["rows"], labels, factor)

    # --- DD GATE: real risk-sized compounding runs, full period ---
    inc = run_backtest(signals, chart, base,
                       exclude_structural_anomalies=args.exclude_structural_anomalies)
    flt = run_backtest(with_trend_signals, chart, base,
                       exclude_structural_anomalies=args.exclude_structural_anomalies)
    der = run_per_signal_risk(signals, chart_df, base, counter_keys, factor,
                              exclude_structural_anomalies=args.exclude_structural_anomalies)

    dd_rows = [
        {"variant": "incumbent", "signals_run": inc["signals_included"], "net": inc["net_profit"],
         "max_dd_pct": inc["max_drawdown_pct"], "final_equity": inc["final_equity"],
         "dd_ok<=40%": inc["max_drawdown_pct"] >= -40.0},
        {"variant": "with_trend_filter", "signals_run": flt["signals_included"], "net": flt["net_profit"],
         "max_dd_pct": flt["max_drawdown_pct"], "final_equity": flt["final_equity"],
         "dd_ok<=40%": flt["max_drawdown_pct"] >= -40.0},
        {"variant": f"derate x{factor:g}", "signals_run": der["signals_run"], "net": der["net"],
         "max_dd_pct": der["max_dd_pct"], "final_equity": der["final_equity"],
         "dd_ok<=40%": der["max_dd_pct"] >= -40.0},
    ]

    n_class = sum(1 for v in labels.values() if v["classified"])
    print(f"signals_parsed={len(signals)}  in_chart={fixed_res['signals_included']}  "
          f"classified={n_class}  with_trend={len(with_trend_keys)}  counter={len(counter_keys)}")
    print(f"classifier EMA({args.ema_fast}/{args.ema_slow})/ATR({args.atr_period}) warmup={warmup}; "
          f"edge lot={args.fixed_lot:g} cap={args.fixed_capital:,.0f}; risk={args.risk:g} derate={factor:g}")

    print_edge(edge)
    print_dd(dd_rows)

    print("\nCaveats: fixed-lot net is the edge (risk equity is a compounding artifact); "
          "$3/lot bonus inflates vs live; sequential DD understates correlated-cluster DD; in-sample.")

    if args.output_dir:
        rows_out = []
        rmap = {r["signal_key"]: r for r in fixed_res["rows"]}
        for k, v in labels.items():
            r = rmap.get(k, {})
            rows_out.append({
                "signal_key": k,
                "signal_time_chart": r.get("signal_time_chart"),
                "side": r.get("side"),
                "classified": v["classified"],
                "with_trend": v["with_trend"],
                "status": r.get("status"),
                "fixed_lot_pnl": r.get("pnl"),
            })
        out = Path(args.output_dir)
        out = out if out.suffix.lower() == ".xlsx" else out / "regime_pnl.xlsx"
        written = write_xlsx(out, edge, dd_rows, pd.DataFrame(rows_out))
        print(f"\nWrote: {written.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())