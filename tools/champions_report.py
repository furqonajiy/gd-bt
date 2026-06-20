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
``python -m trading.engine.cli backtest`` line per the workflow contract.
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
# "Stretch" tier: a config that runs DD between the 40% deploy gate and 50% is
# only worth surfacing if it beats the DD<=40% champion's net+bonus by a wide
# margin (the user's "if DD 50% but a LOT more profit, consider it" rule).
STRETCH_DD_GATE = 50.0
STRETCH_MARGIN = 1.25   # >= +25% net+bonus over the DD<=40% champion
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
    return f"signals/adaptive_{feed}.txt"


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


def _net_bonus(d: dict) -> float:
    # The deploy objective: compounded net P&L + $3/closed-lot bonus. Challenger
    # rows store it as risk_net_profit_with_bonus; flattened champion/incumbent
    # records store it as net_bonus.
    if "net_bonus" in d:
        return _f(d.get("net_bonus"))
    return _f(d.get("risk_net_profit_with_bonus"))


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
    """challenger strictly beats incumbent on the deploy objective: higher
    net+bonus profit, tiebreak higher OOS (held-out tail) then edge."""
    cb, ib = _net_bonus(challenger), _net_bonus(incumbent)
    if cb != ib:
        return cb > ib
    co, io = _oos(challenger), _oos(incumbent)
    if co != io:
        return co > io
    return _edge(challenger) > _edge(incumbent)


def best_challenger(rows: list[dict], dd_gate: float = DD_GATE) -> dict | None:
    """Best DD<=gate challenger row, ranked by net+bonus profit (the deploy
    objective) then OOS then edge. OOS>0 is required as an overfit guard, so an
    in-sample-only blowup never wins."""
    survivors = [r for r in rows
                 if _dd(r) is not None and _dd(r) <= dd_gate and _oos(r) > 0.0]
    if not survivors:
        return None
    survivors.sort(key=lambda r: (_net_bonus(r), _oos(r), _edge(r)), reverse=True)
    return survivors[0]


def stretch_challenger(rows: list[dict], champ_40: dict | None,
                       *, dd_gate: float = STRETCH_DD_GATE,
                       margin: float = STRETCH_MARGIN) -> dict | None:
    """Best DD<=``dd_gate`` (40<DD<=50) challenger whose net+bonus beats the
    DD<=40% champion by >= ``margin``. Returns None unless such a config exists
    AND it actually runs ABOVE the 40% gate (else it's just the 40% champion)."""
    best = best_challenger(rows, dd_gate=dd_gate)
    if best is None or _dd(best) is None or _dd(best) <= DD_GATE:
        return None
    base = _net_bonus(champ_40) if champ_40 else 0.0
    if base > 0.0 and _net_bonus(best) < base * margin:
        return None
    return best


# --------------------------------------------------------------------------
# Runnable CLI for a published champion / challenger config.
# --------------------------------------------------------------------------
def render_champion_cli(cfg: dict, *, regime: str, feed: str) -> str:
    """A directly-runnable backtest command for ``cfg`` on this regime's slice.

    Prefers the 2021 orchestrator's ``baseline_explicit_args`` renderer (the
    blessed flag set), overriding charts/start-date/signals for the regime so the
    command actually targets this regime. Falls back to a plain
    ``python -m trading.engine.cli backtest`` line if the renderer is unavailable.
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
            "python -m trading.engine.cli backtest" + cont
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
# Full deployment CLI for a published champion (GENERATE/BACKTEST/LIVE deployment format).
# --------------------------------------------------------------------------
def _generate_command(feed: str) -> str:
    """Render the GENERATE-section command that refreshes ``feed``'s archive.

    Maps a champion feed to the generator that produces its committed archive
    feed file. ``breakout``/``meanrev`` have dedicated generators; everything
    else (the ``ad*`` matrix feeds) comes from the adaptive-self generator. A
    one-line pointer is enough -- the exact ATR-multiplier args live in the
    generator's defaults / the archive itself.
    """
    out = feed_signals(feed)  # signals/adaptive_<feed>.txt
    cont = " `\n  "
    if feed == "breakout":
        return (
            "# ATR-adaptive breakout signals over ALL chart history (SL/TP scale\n"
            "# with M15 ATR, so they auto-size to the regime). Refresh before each run.\n"
            "python tools/generate_breakout_signals.py" + cont
            + cont.join([
                "--m1-charts data/XAUUSD_M1_*_ELEV8.csv",
                f"--output {out}",
                "--start-date 2021-11-01",
                "--source-tz-offset 7",
            ]) + "\n")
    if feed == "meanrev":
        return (
            "# ATR-adaptive mean-reversion signals over ALL chart history (fade back\n"
            "# to the M15 EMA; SL/TP scale with ATR). Refresh before each run.\n"
            "python tools/generate_meanrev_signals.py" + cont
            + cont.join([
                "--m1-charts data/XAUUSD_M1_*_ELEV8.csv",
                f"--output {out}",
                "--start-date 2021-11-01",
                "--source-tz-offset 7",
            ]) + "\n")
    # Any other feed (the adF_tightSL_closeTP-style matrix feeds) is an
    # adaptive-self variant; the generator's feed knobs are baked into its
    # defaults for this label, so the pointer command is sufficient.
    return (
        f"# ATR-adaptive self signals for feed `{feed}` over ALL chart history.\n"
        "# (The feed's SL/TP ATR-multiplier knobs are the generator's defaults\n"
        "#  for this variant; refresh the archive before each run.)\n"
        "python tools/generate_adaptive_self_signals.py" + cont
        + cont.join([
            "--m1-charts data/XAUUSD_M1_*_ELEV8.csv",
            f"--output {out}",
            "--start-date 2021-11-01",
            "--source-tz-offset 7",
        ]) + "\n")


# Backtest-only flags dropped from the LIVE auto_explicit command: they have no
# meaning for a live executor (no historical charts, no output workbook, no
# drawdown-abort, no chart sync, no progress timer).
_LIVE_DROP_FLAGS = frozenset({
    "--charts", "--output-dir", "--start-date", "--max-drawdown-limit-pct",
    "--sync-charts", "--progress-interval-seconds",
})


def render_live_cli(cfg: dict, *, regime: str, feed: str) -> str:
    """A ``tools/auto_explicit.py`` command with the same strategy/sizing flags.

    Reuses the backtest flag mapping (``baseline_explicit_args``) so the live
    executor sends byte-identical strategy params, then strips the
    backtest-only flags and swaps in the live-session flags (positions registry
    per regime, MT5 watch/symbol/offset, self-heal toggles, initial capital).
    Falls back to a minimal but runnable line when the orchestrator renderer is
    unavailable.
    """
    signals = feed_signals(feed)
    cont = " \\\n  "
    live_tail = [
        f"--positions-json positions_{regime}.json",
        "--watch-interval 15",
        "--mt5-symbol XAUUSD",
        "--mt5-server-offset 3",
        "--mt5-history-bars 5000",
        "--replace-missing-entries false",
        "--reopen-missing-positions true",
    ]
    try:
        from sweep2021 import orchestrate as o  # noqa: E402

        args = list(o.baseline_explicit_args(
            cfg, signals=signals, output_dir=f"reports/CHAMPION_{regime}"))
        pairs: list[str] = [f"--signals {signals}"]
        pairs.extend(live_tail)
        i = 0
        while i < len(args):
            flag = str(args[i])
            val = str(args[i + 1]) if i + 1 < len(args) else ""
            if flag in _LIVE_DROP_FLAGS or flag == "--signals":
                i += 2
                continue
            if flag == "--initial-capital":
                val = "5000"
            pairs.append(f"{flag} {val}".rstrip())
            i += 2
        return "python tools/auto_explicit.py" + cont + cont.join(pairs) + "\n"
    except Exception:
        def g(k, d):
            v = cfg.get(k)
            return d if v is None else v
        return (
            "python tools/auto_explicit.py" + cont
            + cont.join([f"--signals {signals}", *live_tail,
                f"--sizing-mode risk",
                f"--risk {g('risk_per_signal', 0.01)}",
                f"--entries {int(g('entry_count', 6))}",
                f"--sl-multiplier {g('sl_multiplier', 2.1)}",
                f"--tp1-lock-delay-minutes {int(g('tp1_lock_delay_minutes', 24))}",
                f"--final-target {g('final_target', 'TP3')}",
                f"--initial-capital 50000",
            ]) + "\n")


def render_deployment_cli(cfg: dict, *, regime: str, feed: str,
                          edge, oos, dd) -> str:
    """The full GENERATE/BACKTEST/LIVE-style deployment file for a champion.

    Three sections -- GENERATE the archive feed, BACKTEST the regime slice, and
    the LIVE auto executor -- under a header comment block that names the
    regime, the champion's feed + edge/oos/dd, the live-regime detector, and the
    <=5% risk caveat. ``render_champion_cli`` supplies the BACKTEST body so the
    backtest command stays the single source of truth for the flag set.
    """
    edge_s = f"${_f(edge):,.0f}"
    oos_s = f"${_f(oos):,.0f}"
    dd_s = f"{_dd({'dd': dd}) or 0.0:.1f}%"
    lines: list[str] = []
    lines.append("# " + "=" * 73)
    lines.append(f"# {regime} champion — deployment CLI (GENERATE / BACKTEST / LIVE)")
    lines.append(f"# regime: {regime}. Detect the live regime with:")
    lines.append("#   python tools/regime_auto.py   # -> picks the live regime by current M15 ATR")
    lines.append(f"# champion: feed={feed} | edge {edge_s} | OOS {oos_s} | "
                 f"DD {dd_s} (<=40% gate)")
    lines.append("# NOTE: the sweep may pick --risk slightly above 5% to maximize profit.")
    lines.append("#       Set --risk 0.05 to honor your <=5% cap (slightly lower profit +")
    lines.append("#       lower DD). risk is your sizing choice; the strategy params define")
    lines.append("#       the champion. compounded/$ are model upper bounds — they rank")
    lines.append("#       configs, not forecast money.")
    lines.append("# " + "=" * 73)
    lines.append("")
    lines.append(f"# ===== 1. GENERATE the {regime} archive feed =====")
    lines.append(_generate_command(feed).rstrip("\n"))
    lines.append("")
    lines.append(f"# ===== 2. BACKTEST {regime} (regime slice only) =====")
    lines.append(render_champion_cli(cfg, regime=regime, feed=feed).rstrip("\n"))
    lines.append("")
    lines.append(f"# ===== 3. LIVE AUTO EXECUTOR (places + manages orders for {regime}) =====")
    lines.append("# Same strategy params as the backtest above. Note: there is no")
    lines.append("# live_feed_loop --family for this feed yet, so regenerate the archive")
    lines.append("# (section 1) on a schedule / before each session instead of a rolling")
    lines.append("# live loop. Set --risk to your live sizing choice (e.g. 0.05).")
    lines.append(render_live_cli(cfg, regime=regime, feed=feed).rstrip("\n"))
    return "\n".join(lines) + "\n"


# Pretty-printed text for a regime with no published DD<=gate champion yet.
def no_champion_note(regime: str, dd_gate: float = DD_GATE) -> str:
    return (f"# {regime}: no DD<={dd_gate:.0f}% champion yet — run your incumbent.\n")


def write_deployment_cli_files(out_dir: Path, regimes: list[str],
                               champions: dict[str, dict | None],
                               *, dd_gate: float = DD_GATE) -> Path:
    """Write ``cli/best_<regime>.txt`` deployment files for every regime.

    ``cli/`` lives at the repo root (``out_dir.parent`` == ROOT in CI). Each
    file is the full deployment CLI when a champion exists, else a one-line
    no-champion note. Returns the ``cli/`` directory so the caller can ``git
    add`` it.
    """
    cli_dir = out_dir.parent / "cli"
    cli_dir.mkdir(parents=True, exist_ok=True)
    for regime in regimes:
        champ = champions.get(regime)
        target = cli_dir / f"best_{regime}.txt"
        if champ:
            target.write_text(render_deployment_cli(
                champ.get("config") or {}, regime=regime,
                feed=champ.get("feed") or "",
                edge=champ.get("edge"), oos=champ.get("oos"), dd=champ.get("dd")))
        else:
            target.write_text(no_champion_note(regime, dd_gate))
    return cli_dir


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

    The challenger row uses sweep keys (risk_net_profit_with_bonus,
    oos_fixed_no_bonus_profit, _feed, config). The stored champion is a flattened
    record (net_bonus/oos/edge/dd/config/feed). A new challenger only replaces the
    stored champion when it STRICTLY beats the stored champion's net+bonus
    (tiebreak oos, edge) -- so the published deploy target is monotonic.
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
            "net_bonus": _net_bonus(challenger),
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
        stretch: dict[str, dict | None] | None = None,
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
    lines.append("**Legend.** net+bonus = compounded net P&L + $3/closed-lot "
                 "bonus (the deploy objective, ranked); oos = fixed-lot no-bonus "
                 "held-out-tail P&L (overfit guard, must be > 0); "
                 f"dd = concurrent risk-sized max drawdown %. DD gate <= {dd_gate:.0f}%. "
                 "VERDICT is **SWITCH** only when the best DD-passing challenger's "
                 "**net+bonus** strictly exceeds the incumbent's (tiebreak oos); else "
                 "**HOLD**. The published champion is **monotonic** — it is only "
                 "replaced when a challenger strictly beats the stored champion, so "
                 "this file never regresses. A **stretch** row (DD "
                 f"{dd_gate:.0f}–{STRETCH_DD_GATE:.0f}%) is shown only when a config "
                 f"beats the DD<={dd_gate:.0f}% champion's net+bonus by "
                 f"≥{(STRETCH_MARGIN - 1) * 100:.0f}%.")
    lines.append("")
    lines.append("| regime | incumbent (net+bonus / oos / dd) | best challenger "
                 "(feed: net+bonus / oos / dd) | VERDICT |")
    lines.append("|---|---|---|---|")

    cli_blocks: list[str] = []

    for regime in regimes:
        inc = incumbents.get(regime)
        champ = champions.get(regime)
        is_live = regime == live_regime
        label = f"**{regime}**" + (" `>>> RUN THIS NOW <<<`" if is_live else "")

        inc_cell = (f"{_fmt(_net_bonus(inc))} / {_fmt(_oos(inc))} / {_fmt(_dd(inc))}%"
                    if inc else "n/a")

        if champ:
            ch_cell = (f"`{champ.get('feed')}`: {_fmt(_net_bonus(champ))} / "
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
                f"net+bonus={_fmt(_net_bonus(champ))} edge={_fmt(_edge(champ))} "
                f"oos={_fmt(_oos(champ))} dd={_fmt(_dd(champ))}%\n\n```bash\n"
                + cli_for_block + "```\n")

    # Stretch tier (DD 40-50%): only the regimes where a higher-DD config beats
    # the compliant champion's net+bonus by the configured margin.
    stretch = stretch or {}
    stretch_rows = [(r, stretch.get(r)) for r in regimes if stretch.get(r)]
    if stretch_rows:
        lines.append("")
        lines.append(f"## Stretch candidates (DD {dd_gate:.0f}–{STRETCH_DD_GATE:.0f}%)")
        lines.append("")
        lines.append("_Higher net+bonus than the compliant champion, but ABOVE the "
                     f"{dd_gate:.0f}% gate — consider only if you accept the extra "
                     "drawdown._")
        lines.append("")
        lines.append("| regime | feed | net+bonus | oos | dd | x champion |")
        lines.append("|---|---|---|---|---|---|")
        for regime, s in stretch_rows:
            champ = champions.get(regime)
            base = _net_bonus(champ) if champ else 0.0
            mult_txt = f"{_net_bonus(s) / base:.2f}x" if base > 0 else "n/a"
            lines.append(
                f"| {regime} | `{s.get('_feed')}` | {_fmt(_net_bonus(s))} | "
                f"{_fmt(_oos(s))} | {_fmt(_dd(s))}% | {mult_txt} |")

    lines.append("")
    lines.append("## Runnable champion commands")
    lines.append("")
    if cli_blocks:
        lines.extend(cli_blocks)
    else:
        lines.append("_No published champion yet for any regime._")
        lines.append("")

    return "\n".join(lines) + "\n"
