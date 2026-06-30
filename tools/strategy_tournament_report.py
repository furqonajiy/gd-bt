#!/usr/bin/env python3
"""Compare Victor and self-generated sweep winners under one scoreboard.

Inputs can be leaderboard CSVs, JSONL result files, champion/incumbent JSON
records, or directories containing those files. Each ``--source NAME=PATH``
emits the best DD/OOS-passing candidate found in that path, then the final
report ranks all sources on the same deploy objective:

  fixed-lot edge + $3/closed-lot bonus, tiebreak OOS, edge, walk-forward rate.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _truthy_not_false(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "n"}
    return bool(value)


def _metric(row: dict, *names: str) -> float:
    for name in names:
        if name in row and row.get(name) not in (None, ""):
            return _f(row.get(name))
    return 0.0


def edge(row: dict) -> float:
    return _metric(row, "edge", "fixed_no_bonus_profit")


def oos(row: dict) -> float:
    return _metric(row, "oos", "oos_fixed_no_bonus_profit")


def dd(row: dict) -> float | None:
    for name in ("dd", "concurrent_risk_max_dd_pct", "concurrent_dd_pct"):
        if name in row and row.get(name) not in (None, ""):
            return abs(_f(row.get(name)))
    return None


def bonus(row: dict) -> float:
    if "bonus_contribution" in row:
        return _f(row.get("bonus_contribution"))
    if "bonus" in row:
        return _f(row.get("bonus"))
    objective = deploy_objective(row)
    return max(0.0, objective - edge(row)) if objective else 0.0


def deploy_objective(row: dict) -> float:
    for name in ("deploy_objective", "fixed_with_bonus_profit", "edge_bonus"):
        if name in row and row.get(name) not in (None, ""):
            return _f(row.get(name))
    if "bonus_contribution" in row or "bonus" in row:
        return edge(row) + bonus(row)
    return edge(row)


def compounded(row: dict) -> float:
    return _metric(row, "net_bonus", "risk_net_profit_with_bonus", "net_profit_with_bonus")


def wf_fraction(row: dict) -> float | None:
    for name in ("walk_forward_positive_fraction", "wf_positive_fraction"):
        if name in row and row.get(name) not in (None, ""):
            return _f(row.get(name))
    return None


def row_passes(row: dict, *, dd_gate: float, require_oos: bool = True) -> bool:
    if row.get("error"):
        return False
    if not _truthy_not_false(row.get("passes_recommendation_gate", True)):
        return False
    if not _truthy_not_false(row.get("passes_walk_forward", True)):
        return False
    d = dd(row)
    if d is None or d > dd_gate:
        return False
    return (not require_oos) or oos(row) > 0.0


def rank_key(row: dict) -> tuple[float, float, float, float, float]:
    wf = wf_fraction(row)
    return (
        deploy_objective(row),
        oos(row),
        edge(row),
        wf if wf is not None else -1.0,
        compounded(row),
    )


def _config(row: dict) -> dict:
    cfg = row.get("config")
    if isinstance(cfg, dict):
        return cfg
    raw = row.get("config_json")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _cfg_brief(cfg: dict) -> str:
    if not cfg:
        return "-"
    parts = []
    for key, label in (
        ("entry_count", "e"),
        ("sl_multiplier", "slm"),
        ("entry_sl_gap", "gap"),
        ("tp1_lock_delay_minutes", "d"),
        ("max_hold_minutes", "hold"),
        ("final_target", "tgt"),
        ("risk_per_signal", "risk"),
    ):
        value = cfg.get(key)
        if value is not None:
            parts.append(f"{label}{value}")
    if cfg.get("sl_source") == "atr":
        parts.append(f"ATRsl{cfg.get('atr_sl_mult')}p{cfg.get('atr_period')}")
    if _f(cfg.get("signal_min_rr")) > 0:
        parts.append(f"minRR{cfg.get('signal_min_rr')}")
    return " ".join(parts[:10]) if parts else json.dumps(cfg, sort_keys=True)[:90]


@dataclass
class Candidate:
    source: str
    label: str
    path: str
    kind: str
    feed: str
    regime: str
    objective: float
    edge: float
    bonus: float
    oos: float
    dd: float | None
    wf_positive_fraction: float | None
    compounded: float
    config_brief: str
    config: dict

    def sort_key(self) -> tuple[float, float, float, float, float]:
        wf = self.wf_positive_fraction if self.wf_positive_fraction is not None else -1.0
        return (self.objective, self.oos, self.edge, wf, self.compounded)


def candidate_from_row(source: str, row: dict, path: Path, label: str, kind: str) -> Candidate:
    cfg = _config(row)
    return Candidate(
        source=source,
        label=label,
        path=str(path),
        kind=kind,
        feed=str(row.get("feed") or row.get("_feed") or row.get("cfg_feed") or "-"),
        regime=str(row.get("regime") or "-"),
        objective=deploy_objective(row),
        edge=edge(row),
        bonus=bonus(row),
        oos=oos(row),
        dd=dd(row),
        wf_positive_fraction=wf_fraction(row),
        compounded=compounded(row),
        config_brief=_cfg_brief(cfg),
        config=cfg,
    )


def _read_json(path: Path) -> dict | list:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def rows_from_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def rows_from_jsonl(path: Path) -> list[dict]:
    rows: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = str(row.get("candidate_id") or row.get("config_json") or len(rows))
        rows[key] = row
    return list(rows.values())


def candidate_from_many(source: str, rows: list[dict], path: Path,
                        *, dd_gate: float, label: str) -> Candidate | None:
    survivors = [row for row in rows if row_passes(row, dd_gate=dd_gate)]
    if not survivors:
        return None
    survivors.sort(key=rank_key, reverse=True)
    return candidate_from_row(source, survivors[0], path, label, "leaderboard")


def candidates_from_file(source: str, path: Path, *, dd_gate: float) -> list[Candidate]:
    suffix = path.suffix.lower()
    label = path.parent.name if path.name in {"leaderboard.csv", "results.jsonl"} else path.stem
    if suffix == ".csv":
        cand = candidate_from_many(source, rows_from_csv(path), path, dd_gate=dd_gate, label=label)
        return [cand] if cand else []
    if suffix == ".jsonl":
        cand = candidate_from_many(source, rows_from_jsonl(path), path, dd_gate=dd_gate, label=label)
        return [cand] if cand else []
    if suffix == ".json":
        data = _read_json(path)
        if isinstance(data, dict):
            if not row_passes(data, dd_gate=dd_gate):
                return []
            kind = "champion" if path.name.startswith("CHAMPION_") else "record"
            return [candidate_from_row(source, data, path, path.stem, kind)]
        if isinstance(data, list):
            cand = candidate_from_many(source, [r for r in data if isinstance(r, dict)], path,
                                       dd_gate=dd_gate, label=label)
            return [cand] if cand else []
    return []


def discover_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
        return
    patterns = [
        "leaderboard.csv",
        "results.jsonl",
        "CHAMPION_*.json",
        "INCUMBENT_*.json",
        "WINNER_*.json",
    ]
    seen: set[Path] = set()
    for pattern in patterns:
        for found in sorted(path.rglob(pattern)):
            if found not in seen:
                seen.add(found)
                yield found


def load_source(spec: str, *, dd_gate: float) -> list[Candidate]:
    if "=" not in spec:
        raise SystemExit("--source must be NAME=PATH")
    name, raw_path = spec.split("=", 1)
    name = name.strip()
    path = Path(raw_path.strip())
    if not name:
        raise SystemExit("--source name cannot be empty")
    if not path.exists():
        raise SystemExit(f"source path not found: {path}")
    out: list[Candidate] = []
    for file in discover_files(path):
        out.extend(candidates_from_file(name, file, dd_gate=dd_gate))
    return out


def _money(value: float) -> str:
    return f"${value:,.0f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _dd(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def render_markdown(candidates: list[Candidate], *, dd_gate: float) -> str:
    ranked = sorted(candidates, key=lambda c: c.sort_key(), reverse=True)
    lines = [
        "# Strategy tournament report",
        "",
        f"Ranked by fixed-lot edge + bonus, then OOS, edge, walk-forward rate, compounded net. DD gate <= {dd_gate:.0f}%.",
        "",
    ]
    if not ranked:
        lines.append("_No DD/OOS-passing candidates found._")
        return "\n".join(lines) + "\n"

    best_by_source: dict[str, Candidate] = {}
    for cand in ranked:
        best_by_source.setdefault(cand.source, cand)

    lines.append("## Best by Source")
    lines.append("")
    lines.append("| source | label | objective | edge | bonus | OOS | DD | WF +% | feed | config |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---|---|")
    for source in sorted(best_by_source):
        cand = best_by_source[source]
        lines.append(
            f"| {cand.source} | {cand.label} | {_money(cand.objective)} | "
            f"{_money(cand.edge)} | {_money(cand.bonus)} | {_money(cand.oos)} | "
            f"{_dd(cand.dd)} | {_pct(cand.wf_positive_fraction)} | `{cand.feed}` | "
            f"`{cand.config_brief}` |")

    lines.append("")
    lines.append("## Overall Ranking")
    lines.append("")
    lines.append("| # | source | label | objective | edge | bonus | OOS | DD | WF +% | compounded | source file |")
    lines.append("|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for idx, cand in enumerate(ranked, 1):
        lines.append(
            f"| {idx} | {cand.source} | {cand.label} | {_money(cand.objective)} | "
            f"{_money(cand.edge)} | {_money(cand.bonus)} | {_money(cand.oos)} | "
            f"{_dd(cand.dd)} | {_pct(cand.wf_positive_fraction)} | "
            f"{_money(cand.compounded)} | `{cand.path}` |")
    lines.append("")
    return "\n".join(lines)


def write_json(candidates: list[Candidate], path: Path) -> None:
    ranked = sorted(candidates, key=lambda c: c.sort_key(), reverse=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(c) for c in ranked], indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", action="append", default=[], metavar="NAME=PATH",
                   help="Repeatable. PATH can be a result directory, leaderboard.csv, results.jsonl, or champion JSON.")
    p.add_argument("--out", required=True, help="Markdown report path.")
    p.add_argument("--json-out", default=None, help="Optional normalized JSON output path.")
    p.add_argument("--dd-gate", type=float, default=40.0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.source:
        raise SystemExit("provide at least one --source NAME=PATH")
    candidates: list[Candidate] = []
    for spec in args.source:
        candidates.extend(load_source(spec, dd_gate=args.dd_gate))
    report = render_markdown(candidates, dd_gate=args.dd_gate)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    if args.json_out:
        write_json(candidates, Path(args.json_out))
    print(f"[strategy-tournament] candidates={len(candidates)} report={out}")
    if args.json_out:
        print(f"[strategy-tournament] json={args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
