"""sweep.py live_command must emit a command auto_explicit.py can actually run.

config.py is env-immune and auto_explicit.py makes the trailing distances
required flags, so the generated live command must pass them as --trailing-*
(never the dead XAUUSD_* env prefix). The strongest guard is a round-trip:
tokenize the generated command and feed it back through auto_explicit's own
parser, asserting it parses and carries the configured trailing values.
"""
from __future__ import annotations

import importlib.util
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(module_name: str):
    # tools/ is not a package, so load each runner module from its file path.
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "tools" / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sweep = _load("sweep")
auto_explicit = _load("auto_explicit")


def _base_config(**overrides) -> dict:
    cfg = {
        "initial_capital": 1000.0,
        "sizing_mode": "risk",
        "lot_per_entry": 0.01,
        "risk_per_signal": 0.022,
        "minimum_lot": 0.01,
        "lot_step": 0.01,
        "bonus_per_closed_lot": 3.0,
        "entry_count": 1,
        "entry_ladder": "range_uniform",
        "entry_sl_gap": 2.0,
        "activation_delay_minutes": 0,
        "pending_expiry_minutes": 630,
        "max_hold_minutes": 15,
        "sl_multiplier": 0.85,
        "final_target": "TP1",
        "lock_after_tp1": True,
        "lock_after_tp2": True,
        "tp1_lock_delay_minutes": 0,
        "tp2_lock_delay_minutes": 0,
        "profit_lock_mode": "tp_levels",
        "bep_trigger_distance": 3.0,
        "tp1_lock_fraction": 0.5,
        "tp2_lock_target": "TP1",
        "runner_after_tp3": False,
        "tp3_lock_target": "TP2",
        "trailing_open_distance": 0.0,
        "trailing_close_distance": 0.0,
    }
    cfg.update(overrides)
    return cfg


def _argv_for_auto_explicit(command: str) -> list[str]:
    """Strip comment lines + line-continuations and return the auto_explicit argv."""
    body = " ".join(
        line.rstrip("\\").strip()
        for line in command.splitlines()
        if not line.lstrip().startswith("#")
    )
    tokens = shlex.split(body)
    script_idx = tokens.index("tools/auto_explicit.py")
    return tokens[script_idx + 1:]


def _parse(command: str):
    return auto_explicit.build_parser().parse_args(_argv_for_auto_explicit(command))


def test_no_dead_env_prefix():
    cmd = sweep.live_command({"config": _base_config(trailing_open_distance=0.25, trailing_close_distance=0.25)})
    assert "XAUUSD_" not in cmd
    assert "XAUUSD_TRAILING_OPEN_DISTANCE" not in cmd


def test_trailing_flags_round_trip_through_auto_explicit():
    cmd = sweep.live_command({"config": _base_config(trailing_open_distance=0.25, trailing_close_distance=1.5)})
    # The generated command must parse with auto_explicit's own (required-flag) parser.
    args = _parse(cmd)
    assert args.trailing_open_distance == 0.25
    assert args.trailing_close_distance == 1.5


def test_disabled_trailing_still_emits_required_zero_flags():
    # Both flags are required even when disabled; 0.0 disables.
    cmd = sweep.live_command({"config": _base_config(trailing_open_distance=0.0, trailing_close_distance=0.0)})
    assert "--trailing-open-distance" in cmd and "--trailing-close-distance" in cmd
    args = _parse(cmd)
    assert args.trailing_open_distance == 0.0
    assert args.trailing_close_distance == 0.0


def test_config_json_fallback_is_supported():
    import json
    cfg = _base_config(trailing_open_distance=2.0, trailing_close_distance=3.0)
    cmd = sweep.live_command({"config_json": json.dumps(cfg), "filter_preset": "high_growth_hour_side"})
    args = _parse(cmd)
    assert args.trailing_open_distance == 2.0
    assert args.trailing_close_distance == 3.0
    assert "signals/live_provider_high_growth_hour_side.txt" in cmd


def test_trend_runner_config_warns_and_avoids_dead_env():
    cmd = sweep.live_command({"config": _base_config(trend_runner_enabled=True)})
    assert "XAUUSD_" not in cmd
    assert cmd.lstrip().startswith("# WARNING")
    assert "cli auto" in cmd and "--trend-runner" in cmd
    # The auto_explicit portion must still parse (it just runs without the runner).
    args = _parse(cmd)
    assert args.trailing_open_distance == 0.0