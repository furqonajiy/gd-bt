#!/usr/bin/env python3
"""Champion/challenger deploy report for the feed x regime adaptive grid.

The grid sweep (`.github/workflows/self-regime-grid.yml`) is the CHALLENGER: it
fans out many adaptive feeds x regimes and finds the best DD<=40% config per
regime. The user keeps trading their INCUMBENT live config (scalper24:
``entry_count=6, sl_multiplier=2.1, tp1_lock_delay_minutes=24``) and only
SWITCHES when a challenger STRICTLY beats it. This module renders the single
file they pull to decide: ``CHAMPIONS.md`` at the repo root.

Three responsibilities, all pure-ish and unit-testable (no network, no MT5):

  1. ``best_challenger(rows, dd_gate)`` -- pick the best DD<=gate challenger for a
     regime (rank by OOS then edge), from the same result rows the aggregate
     already loads from each cell's ``results.jsonl``.

  2. MONOTONIC champion: ``update_champion(...)`` only replaces the committed
     ``CHAMPION_<regime>.json`` when a fresh challenger STRICTLY beats the STORED
     champion's oos (tiebreak edge) at DD<=gate. CHAMPIONS.md reflects the stored
     champion, so the published deploy target never regresses across passes.

  3. ``render_champions_md(...)`` -- the deploy table: per regime
     incumbent | published champion | VERDICT, the live regime flagged
     ``>>> RUN THIS NOW <<<``, and a directly-runnable CLI for every published
     champion (also dropped into ``self_cli_best_<regime>.txt``).

The runnable CLI reuses ``sweep2021.orchestrate.baseline_explicit_args`` (the
2021 orchestrator's config->args renderer) when importable so the deploy command
matches the project's blessed flag set, with charts/start-date/signals
overridden to this regime's slice; otherwise it falls back to a
``python -m xauusd_trading.cli backtest`` line per the workflow contract.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DD_GATE = 40.0
LIVE_REGIME = "R4parab"

# Same regime -> chart month-glob mapping the shard job and incumbent use.
REGIME_CHARTS: dict[str, str] = {
    "R1quiet": ("data/XAUUSD_M1_2021{11,12}_ELEV8.csv data/XAUUSD_M1_2022*_ELEV8.csv "
                "data/XAUUSD_M1_2023{01,02,03,04,05,06,07,08,09}_ELEV8.csv"),
    "R2bull": "data/XAUUSD_M1_2023{10,11,12}_ELEV8.csv data/XAUUSD_M1_2024*_ELEV8.csv",
    "R3strong": "data/XAUUSD_M1_2025*_ELEV8.csv",
    "R4parab": "data/XAUUSD_M1_2026*_ELEV8.csv",
}
REGIME_START: dict[str, str] = {
    "R1quiet": "2021-11-01",
    "R2bull": "2023-10-01",
    "R3strong": "2025-01-01",
    "R4parab": "2026-01-01",
}


def feed_signals(feed: str) -> str:
    """Resolve a matrix feed NAME (e.g. ``adE_farTP``) to its archive path.

    A challenger row's ``_feed`` is the matrix label; the runnable CLI needs the
    actual generated archive. If ``feed`` already looks like a path (contains
    ``/`` or ends in ``.txt``) it is returned unchanged, so a champion record that
    already stored the resolved file path round-trips.
    """
    if not feed:
        return feed
    if "/" in feed or feed.endswith(".txt"):
        return feed
    return f"generated/adaptive_{feed}.txt"


# --------------------------------------------------------------------------
# Metric helpers (all higher-is-better; None treated as 0.0 for ranking).
# --------------------------------------------------------------------------
def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _oos(d: dict) -> float:
    # Challenger rows store oos under oos_fixed_no_bonus_profit; champion/incumbent
    # JSONs store it flattened as "oos".
    if "oos" in d:
        return _f(d.get("oos"))
    return _f(d.get("oos_fixed_no_bonus_profit"))


def _edge(d: dict) -> float:
    if "edge" in d:
        return _f(d.get("edge"))
    return _f(d.get("fixed_no_bonus_profit"))


def _dd(d: dict):
    if "dd" in d:
        v = d.get("dd")
    else:
        v = d.get("concurrent_risk_max_dd_pct")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def strictly_beats(challenger: dict, incumbent: dict) -> bool:
    """challenger strictly beats incumbent: higher OOS, tiebreak higher edge."""
    co, io = _oos(challenger), _oos(incumbent)
    if co > io:
        return True
    if co == io:
        return _edge(challenger) > _edge(incumbent)
    return False


def best_challenger(rows: list[dict], dd_gate: float = DD_GATE) -> dict | None:
    """Best DD<=gate challenger row, ranked by OOS then edge (both desc)."""
    survivors = [r for r in rows if (_dd(r) is not None and _dd(r) <= dd_gate)]
    if not survivors:
        return None
    survivors.sort(key=lambda r: (_oos(r), _edge(r)), reverse=True)
    return survivors[0]


# --------------------------------------------------------------------------
# Runnable CLI for a published champion / challenger config.
# --------------------------------------------------------------------------
def render_champion_cli(cfg: dict, *, regime: str, feed: str) -> str:
    """A directly-runnable backtest command for ``cfg`` on this regime's slice.

    Prefers the 2021 orchestrator's ``baseline_explicit_args`` renderer (the
    blessed flag set), overriding charts/start-date/signals for the regime so the
    command actually targets this regime. Falls back to a plain
    ``python -m xauusd_trading.cli backtest`` line if the renderer is unavailable.
    """
    charts = REGIME_CHARTS.get(regime, "data/XAUUSD_M1_*_ELEV8.csv")
    start = REGIME_START.get(regime, "2021-11-01")
    signals = feed_signals(feed)  # resolve a matrix feed NAME to its archive path
    cont = " \\\n  "
    try:
        from sweep2021 import orchestrate as o  # noqa: E402

        args = list(o.baseline_explicit_args(
            cfg, signals=signals, output_dir=f"reports/CHAMPION_{regime}"))
        # Override the renderer's full-history charts/start with this regime's.
        # baseline_explicit_args returns a flat [flag, value, flag, value, ...]
        # list; pair each flag with its value onto one continued line so the
        # rendered command is readable and copy-pasteable.
        pairs: list[str] = []
        i = 0
        while i < len(args):
            flag = str(args[i])
            val = str(args[i + 1]) if i + 1 < len(args) else ""
            if flag == "--charts":
                val = charts
            elif flag == "--start-date":
                val = start
            pairs.append(f"{flag} {val}".rstrip())
            i += 2
        return "python tools/backtest_explicit.py" + cont + cont.join(pairs) + "\n"
    except Exception:
        # Minimal but runnable fallback per the workflow contract.
        def g(k, d):
            v = cfg.get(k)
            return d if v is None else v
        return (
            "python -m xauusd_trading.cli backtest" + cont
            + cont.join([
                f"--signals {signals}",
                f"--charts {charts}",
                f"--entries {int(g('entry_count', 6))}",
                f"--sl-multiplier {g('sl_multiplier', 2.1)}",
                f"--tp1-lock-delay-minutes {int(g('tp1_lock_delay_minutes', 24))}",
                f"--final-target {g('final_target', 'TP3')}",
                f"--risk {g('risk_per_signal', 0.01)}",
            ]) + "\n")


# --------------------------------------------------------------------------
# Monotonic champion store.
# --------------------------------------------------------------------------
def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def update_champion(out_dir: Path, regime: str, challenger: dict | None) -> dict | None:
    """Maybe replace CHAMPION_<regime>.json; return the published champion (or None).

    The challenger row uses sweep keys (oos_fixed_no_bonus_profit, _feed, config).
    The stored champion is a flattened record (oos/edge/dd/config/feed). A new
    challenger only replaces the stored champion when it STRICTLY beats the stored
    champion's oos (tiebreak edge) -- so the published deploy target is monotonic.
    """
    path = out_dir / f"CHAMPION_{regime}.json"
    stored = load_json(path)

    cand_record = None
    if challenger is not None:
        feed_name = challenger.get("_feed")
        cand_record = {
            "regime": regime,
            "kind": "champion",
            "feed": feed_name,
            "feed_file": feed_signals(feed_name or ""),
            "edge": _edge(challenger),
            "oos": _oos(challenger),
            "dd": _dd(challenger),
            "config": challenger.get("config") or {},
            "config_json": challenger.get(
                "config_json",
                json.dumps(challenger.get("config") or {}, sort_keys=True)),
        }

    if stored is None:
        if cand_record is not None:
            path.write_text(json.dumps(cand_record, indent=2, sort_keys=True) + "\n")
        return cand_record

    if cand_record is not None and strictly_beats(cand_record, stored):
        path.write_text(json.dumps(cand_record, indent=2, sort_keys=True) + "\n")
        return cand_record
    return stored


# --------------------------------------------------------------------------
# CHAMPIONS.md
# --------------------------------------------------------------------------
def _fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def render_champions_md(
        regimes: list[str],
        incumbents: dict[str, dict | None],
        champions: dict[str, dict | None],
        *,
        live_regime: str = LIVE_REGIME,
        dd_gate: float = DD_GATE,
        now: datetime | None = None) -> str:
    """Render the full CHAMPIONS.md deploy file."""
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%d %H:%M:%SZ")

    lines: list[str] = []
    lines.append(f"<!-- Last updated {ts}; live regime: {live_regime} -->")
    lines.append("# CHAMPIONS — champion/challenger deploy")
    lines.append("")
    lines.append(f"_Last updated {ts}; live regime: **{live_regime}**._")
    lines.append("")
    lines.append("**Legend.** edge = fixed-lot no-bonus full-period P&L; "
                 "oos = fixed-lot no-bonus held-out-tail P&L; "
                 f"dd = concurrent risk-sized max drawdown %. DD gate <= {dd_gate:.0f}%. "
                 "VERDICT is **SWITCH** only when the best DD-passing challenger's "
                 "**oos** strictly exceeds the incumbent's (tiebreak edge); else "
                 "**HOLD**. The published champion is **monotonic** — it is only "
                 "replaced when a challenger strictly beats the stored champion, so "
                 "this file never regresses.")
    lines.append("")
    lines.append("| regime | incumbent (edge / oos / dd) | best challenger "
                 "(feed: edge / oos / dd) | VERDICT |")
    lines.append("|---|---|---|---|")

    cli_blocks: list[str] = []

    for regime in regimes:
        inc = incumbents.get(regime)
        champ = champions.get(regime)
        is_live = regime == live_regime
        label = f"**{regime}**" + (" `>>> RUN THIS NOW <<<`" if is_live else "")

        inc_cell = (f"{_fmt(_edge(inc))} / {_fmt(_oos(inc))} / {_fmt(_dd(inc))}%"
                    if inc else "n/a")

        if champ:
            ch_cell = (f"`{champ.get('feed')}`: {_fmt(_edge(champ))} / "
                       f"{_fmt(_oos(champ))} / {_fmt(_dd(champ))}%")
        else:
            ch_cell = f"no DD<={dd_gate:.0f}% challenger yet"

        # VERDICT. The published champion is always DD<=gate (best_challenger
        # filters it). The incumbent only counts as a valid option if it ALSO
        # clears the DD gate -- DD<=40% is a hard constraint, so an incumbent that
        # exceeds it is disqualified and any compliant champion wins regardless of
        # raw OOS (a 72.5%-DD incumbent is not deployable under the rule).
        inc_dd = _dd(inc) if inc else None
        inc_compliant = inc is not None and inc_dd is not None and inc_dd <= dd_gate
        cli = ""
        if champ:
            cli = render_champion_cli(
                champ.get("config") or {}, regime=regime, feed=champ.get("feed") or "")
        if champ and inc is None:
            verdict = "**SWITCH** (no incumbent baseline) — see CLI below"
        elif champ and not inc_compliant:
            verdict = (f"**SWITCH** — incumbent DD {_fmt(inc_dd)}% exceeds "
                       f"{dd_gate:.0f}% gate; champion is compliant — see CLI below")
        elif champ and strictly_beats(champ, inc):
            verdict = "**SWITCH** — beats compliant incumbent — see CLI below"
        elif champ:  # compliant incumbent the champion doesn't beat
            cli = ""
            verdict = "HOLD — incumbent better at DD<=40%"
        else:
            verdict = "HOLD (keep incumbent)"

        lines.append(f"| {label} | {inc_cell} | {ch_cell} | {verdict} |")

        # Emit the runnable champion CLI block (also written to self_cli_best_*).
        if champ:
            flag = " >>> RUN THIS NOW <<<" if is_live else ""
            cli_for_block = cli or render_champion_cli(
                champ.get("config") or {}, regime=regime, feed=champ.get("feed") or "")
            cli_blocks.append(
                f"### {regime} published champion{flag}\n\n"
                f"feed=`{champ.get('feed')}` "
                f"edge={_fmt(_edge(champ))} oos={_fmt(_oos(champ))} "
                f"dd={_fmt(_dd(champ))}%\n\n```bash\n" + cli_for_block + "```\n")

    lines.append("")
    lines.append("## Runnable champion commands")
    lines.append("")
    if cli_blocks:
        lines.extend(cli_blocks)
    else:
        lines.append("_No published champion yet for any regime._")
        lines.append("")

    return "\n".join(lines) + "\n"
