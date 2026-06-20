"""Phase 1 — measure whether market regime predicts signal behaviour.

This is a READ-ONLY diagnostic. It places no orders and changes no engine
state. For every provider signal it:

  1. Labels the market regime at signal time using a trend-strength score
     (|EMA_fast - EMA_slow| / ATR) read from the last bar STRICTLY BEFORE the
     signal — causal, no look-ahead.
  2. Reuses ``run_path_analysis`` (spread-aware, strict-touch, same fill rule as
     the engine) to get each signal's price-path outcome: fill, TP1/2/3 touch,
     SL, and MFE/MAE (max favourable / adverse excursion in dollars).
  3. Buckets the signals and reports the path stats per bucket, so we can see
     whether trend-regime signals run further (higher MFE) than range-regime
     signals before committing any code to a branched strategy.

Headline split is TERCILES of trend-strength: thirds need no hand-picked
threshold, so the measurement itself cannot be tuned to manufacture a split.
A fixed binary cut and a with-trend / counter-trend view are shown alongside.

Example:

    python tools/regime_split_analysis.py \
      --signals signals.txt \
      --charts data/XAUUSD_M1_*_ELEV8.csv \
      --output-dir reports/regime_split
"""
from __future__ import annotations

import argparse
import bisect
import glob
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

# Allow running as `python tools/regime_split_analysis.py` from repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading.engine import (
    DEFAULT_CONFIG,
    StrategyConfig,
    CsvChartSource,
    parse_signals_file,
    iter_bars,
)
from trading.engine.strategy.path_analysis import run_path_analysis


# ---------------------------------------------------------------------------
# regime classifier (pure, testable)
# ---------------------------------------------------------------------------
def _ema(prev: float | None, value: float, period: int) -> float:
    # Mirrors trading.engine.core.trend_runner._ema exactly so regime labels use
    # the same EMA definition the engine's trend-runner uses (definition parity).
    if prev is None:
        return float(value)
    alpha = 2.0 / (max(1, int(period)) + 1.0)
    return prev + alpha * (float(value) - prev)


def compute_indicator_series(
        chart_df: pd.DataFrame, ema_fast_period: int, ema_slow_period: int,
        atr_period: int,
) -> dict:
    """One causal pass over the chart: per-bar EMA_fast, EMA_slow, ATR.

    ATR is an EMA of true range, matching trend_runner.update_indicators. Each
    value at bar t depends only on bars <= t, so sampling it later is look-ahead
    safe. Returns parallel lists keyed by time (chronological).
    """
    times: list = []
    ema_fast: list[float] = []
    ema_slow: list[float] = []
    atr: list[float] = []

    ef = es = a = None
    prev_close = None
    for bar in iter_bars(chart_df):
        tr = bar.high - bar.low
        if prev_close is not None:
            tr = max(tr, abs(bar.high - prev_close), abs(bar.low - prev_close))
        ef = _ema(ef, bar.close, ema_fast_period)
        es = _ema(es, bar.close, ema_slow_period)
        a = _ema(a, tr, atr_period)
        prev_close = bar.close
        times.append(bar.time)
        ema_fast.append(ef)
        ema_slow.append(es)
        atr.append(a)

    return {"time": times, "ema_fast": ema_fast, "ema_slow": ema_slow, "atr": atr}


def label_signal_regime(series: dict, signal_time, side: str, warmup: int) -> dict:
    """Classify regime at signal_time from the last bar strictly before it."""
    times = series["time"]
    idx = bisect.bisect_left(times, signal_time) - 1  # last bar with time < signal_time
    if idx < 0:
        return {"classified": False, "reason": "no_prior_bar"}
    if idx < warmup:
        return {"classified": False, "reason": "unwarmed"}
    ef = series["ema_fast"][idx]
    es = series["ema_slow"][idx]
    a = series["atr"][idx]
    if a is None or a <= 0:
        return {"classified": False, "reason": "atr_nonpositive"}
    strength = abs(ef - es) / a
    direction = "UP" if ef > es else "DOWN"
    with_trend = (direction == "UP" and side == "BUY") or (direction == "DOWN" and side == "SELL")
    return {
        "classified": True,
        "trend_strength": strength,
        "direction": direction,
        "with_trend": with_trend,
        "ref_bar_time": times[idx],
    }


# ---------------------------------------------------------------------------
# aggregation (pure, testable)
# ---------------------------------------------------------------------------
def _mean(xs: list[float]):
    return sum(xs) / len(xs) if xs else None


def _median(xs: list[float]):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def summarize(rows: list[dict]) -> dict:
    """Path stats for one bucket. Touch/SL rates are over FILLED signals only;
    MFE/MAE are over filled signals (excursions are undefined without a fill)."""
    n = len(rows)
    filled = [r for r in rows if r["filled"]]
    nf = len(filled)

    def rate(key: str):
        return (sum(1 for r in filled if r[key]) / nf) if nf else None

    mfe = [r["mfe"] for r in filled if r["mfe"] is not None]
    mae = [r["mae"] for r in filled if r["mae"] is not None]
    med_mfe, med_mae = _median(mfe), _median(mae)
    return {
        "signals": n,
        "fill_rate": (nf / n) if n else None,
        "tp1_rate": rate("tp1"),
        "tp2_rate": rate("tp2"),
        "tp3_rate": rate("tp3"),
        "sl_rate": rate("sl"),
        "near_tp1_then_sl_rate": rate("near_then_sl"),
        "mfe_mean": _mean(mfe),
        "mfe_median": med_mfe,
        "mae_mean": _mean(mae),
        "mae_median": med_mae,
        "mfe_to_mae_median": (med_mfe / med_mae) if (med_mae and med_mae > 0) else None,
    }


def tercile_buckets(classified_rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Split classified signals into low/mid/high trend-strength thirds."""
    rows = sorted(classified_rows, key=lambda r: r["trend_strength"])
    n = len(rows)
    if n < 3:
        return [("all", rows)]
    a, b = n // 3, 2 * n // 3
    return [("T1_low_trend", rows[:a]), ("T2_mid", rows[a:b]), ("T3_high_trend", rows[b:])]


# ---------------------------------------------------------------------------
# glue
# ---------------------------------------------------------------------------
def _entries_by_signal(entry_rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in entry_rows:
        out.setdefault(e["signal_key"], []).append(e)
    return out


def build_signal_rows(path_result: dict, series: dict, warmup: int) -> list[dict]:
    """Join the regime label and aggregated MFE/MAE onto each path-analysis signal."""
    entries_by_key = _entries_by_signal(path_result["entries"])
    rows: list[dict] = []
    for s in path_result["signals"]:
        ents = entries_by_key.get(s["signal_key"], [])
        filled_ents = [e for e in ents if e["fill_time"] is not None]
        filled = s["filled_entries"] > 0
        mfe = max((e["max_favorable_dollars"] for e in filled_ents), default=None) if filled_ents else None
        mae = max((e["max_adverse_dollars"] for e in filled_ents), default=None) if filled_ents else None

        label = label_signal_regime(series, s["signal_time_chart"], s["side"], warmup)
        rows.append({
            "signal_key": s["signal_key"],
            "signal_time_chart": s["signal_time_chart"],
            "side": s["side"],
            "classified": label["classified"],
            "reason": label.get("reason"),
            "trend_strength": label.get("trend_strength"),
            "direction": label.get("direction"),
            "with_trend": label.get("with_trend"),
            "filled": filled,
            "mfe": mfe,
            "mae": mae,
            "tp1": s["tp1_entries"] > 0,
            "tp2": s["tp2_entries"] > 0,
            "tp3": s["tp3_entries"] > 0,
            "sl": s["sl_entries"] > 0,
            "near_then_sl": bool(s["near_tp1_then_sl"]),
            "path_key": s["path_key"],
        })
    return rows


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def print_table(title: str, summary: dict) -> None:
    if not summary:
        return
    buckets = list(summary.keys())
    metrics = list(next(iter(summary.values())).keys())
    w = max(len(m) for m in metrics) + 1
    print(f"\n{title}")
    print(f"{'metric':<{w}}  " + "  ".join(f"{b:>16}" for b in buckets))
    for m in metrics:
        print(f"{m:<{w}}  " + "  ".join(f"{_fmt(summary[b][m]):>16}" for b in buckets))


def _summary_df(summary: dict) -> pd.DataFrame:
    df = pd.DataFrame(summary)  # columns = bucket labels, index = metric names
    df.index.name = "metric"
    return df.reset_index()


def write_xlsx(path: Path, signals_df: pd.DataFrame, tercile: dict,
               binary: dict, withtrend: dict) -> Path:
    import openpyxl
    from openpyxl.styles import Font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        _summary_df(tercile).to_excel(xw, sheet_name="terciles", index=False)
        _summary_df(binary).to_excel(xw, sheet_name="trend_vs_range", index=False)
        _summary_df(withtrend).to_excel(xw, sheet_name="with_vs_counter", index=False)
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


def _config_from_args(args: argparse.Namespace) -> StrategyConfig:
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
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="regime_split_analysis",
        description="Measure whether trend-strength regime at signal time predicts "
                    "signal behaviour (MFE/MAE/TP path), before building any branched strategy.",
    )
    p.add_argument("--signals", required=True, help="Signal text file.")
    p.add_argument("--charts", required=True, nargs="+", help="One or more MT5 M1 chart CSV files.")
    p.add_argument("--output-dir", default=None, help="Directory for the .xlsx report (optional).")
    p.add_argument("--exclude-structural-anomalies", action="store_true")
    p.add_argument("--near-tp1-dollars", type=float, default=1.0)

    # Classifier (fixed defaults; do not tune to manufacture a split).
    p.add_argument("--ema-fast", type=int, default=DEFAULT_CONFIG.trend_runner_ema_fast)
    p.add_argument("--ema-slow", type=int, default=DEFAULT_CONFIG.trend_runner_ema_slow)
    p.add_argument("--atr-period", type=int, default=DEFAULT_CONFIG.trend_runner_atr_period)
    p.add_argument("--trend-cutoff", type=float, default=1.0,
                   help="Binary readability cut: trend_strength >= cutoff = TREND, else RANGE.")
    p.add_argument("--warmup", type=int, default=None,
                   help="Bars required before a signal is classified (default 5*ema_slow).")

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
    config = _config_from_args(args)
    warmup = args.warmup if args.warmup is not None else 5 * args.ema_slow

    signals = parse_signals_file(Path(args.signals))
    chart = CsvChartSource(_expand_chart_paths(args.charts))

    series = compute_indicator_series(chart.dataframe, args.ema_fast, args.ema_slow, args.atr_period)
    path_result = run_path_analysis(
        signals, chart, config,
        exclude_structural_anomalies=args.exclude_structural_anomalies,
        near_tp1_dollars=args.near_tp1_dollars,
    )

    rows = build_signal_rows(path_result, series, warmup)
    classified = [r for r in rows if r["classified"]]
    unclassified = len(rows) - len(classified)

    # Headline: trend-strength terciles (no threshold to tune).
    tercile = {name: summarize(bucket) for name, bucket in tercile_buckets(classified)}

    # Binary readability cut at a fixed cutoff.
    cutoff = args.trend_cutoff
    binary = {
        f"RANGE(<{cutoff:g})": summarize([r for r in classified if r["trend_strength"] < cutoff]),
        f"TREND(>={cutoff:g})": summarize([r for r in classified if r["trend_strength"] >= cutoff]),
    }

    # Direction agreement: does following the signal's side align with EMA trend?
    withtrend = {
        "counter_trend": summarize([r for r in classified if not r["with_trend"]]),
        "with_trend": summarize([r for r in classified if r["with_trend"]]),
    }

    print(f"signals_parsed={len(signals)}  in_chart={len(rows)}  "
          f"classified={len(classified)}  unclassified={unclassified}")
    print(f"classifier: EMA({args.ema_fast}/{args.ema_slow})/ATR({args.atr_period}), "
          f"warmup={warmup} bars, binary cutoff={cutoff:g}")

    print_table("Trend-strength terciles (headline)", tercile)
    print_table(f"Binary trend vs range (cutoff {cutoff:g})", binary)
    print_table("With-trend vs counter-trend", withtrend)

    # Neutral premise check: does median MFE rise low -> high trend-strength?
    med = [tercile.get(k, {}).get("mfe_median") for k in ("T1_low_trend", "T2_mid", "T3_high_trend")]
    if all(v is not None for v in med):
        monotonic = med[0] <= med[1] <= med[2]
        print(f"\nPREMISE CHECK  median MFE by tercile (T1,T2,T3) = "
              f"{med[0]:.2f}, {med[1]:.2f}, {med[2]:.2f}  -> monotonic increase: "
              f"{'YES' if monotonic else 'NO'}")

    if args.output_dir:
        out = Path(args.output_dir)
        out = out if out.suffix.lower() == ".xlsx" else out / "regime_split.xlsx"
        written = write_xlsx(out, pd.DataFrame(rows), tercile, binary, withtrend)
        print(f"\nWrote: {written.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())