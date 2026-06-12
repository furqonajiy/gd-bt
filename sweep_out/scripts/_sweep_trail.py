"""Trailing-sweep v2 candidate wrapper (post-#77 honest trailing engine).

Wraps tools/sweep_self_limit.py the same way _sweep_self.py did, but for the
trailing re-sweep:

  --mode trail     -> only candidates with trailing_open>0 OR trailing_close>0
                      (the point of the sweep), risk pinned to 5% (live cap;
                      the deployable risk is set later by the post-pass walk).
                      Seeds a structured grid around the old (pre-fix) trailing
                      champion plus trailing-close-only variants of the
                      no-trailing reference, so the known families are always
                      evaluated before the random draw.
  --mode baseline  -> ONLY the user's no-trailing reference config at its own
                      risk 1% (the bar the sweep must beat), no random draws.

Run from repo root: python sweep_out/scripts/_sweep_trail.py --mode trail ...
"""
import random
import sys
from pathlib import Path

ROOT = Path("/home/user/xauusd-backtest")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import sweep  # noqa: E402
import sweep_self_limit  # noqa: E402

MODE = "trail"
if "--mode" in sys.argv:
    i = sys.argv.index("--mode")
    MODE = sys.argv[i + 1]
    del sys.argv[i:i + 2]

# The bar to beat: user's revised reference CLI (scalper24, no trailing, risk 1%).
REFERENCE_NO_TRAIL = {
    "entry_count": 6, "entry_ladder": "range_to_sl", "entry_sl_gap": 0.5,
    "activation_delay_minutes": 2, "pending_expiry_minutes": 180,
    "max_hold_minutes": 240, "sl_multiplier": 2.1, "final_target": "TP3",
    "lock_after_tp1": True, "lock_after_tp2": True,
    "tp1_lock_delay_minutes": 24, "tp2_lock_delay_minutes": 2,
    "profit_lock_mode": "tp_levels", "bep_trigger_distance": 3.0,
    "tp1_lock_fraction": 0.5, "tp2_lock_target": "TP1",
    "runner_after_tp3": False, "tp3_lock_target": "TP2",
    "trailing_open_distance": 0.0, "trailing_close_distance": 0.0,
    "sizing_mode": "risk", "risk_per_signal": 0.01,
}

# The old trailing champion family (now honest after #77) as a seed center.
OLD_TRAIL_CHAMP = {
    "entry_count": 4, "entry_ladder": "range_uniform", "entry_sl_gap": 0.0,
    "activation_delay_minutes": 5, "pending_expiry_minutes": 630,
    "max_hold_minutes": 30, "sl_multiplier": 1.15, "final_target": "TP2",
    "lock_after_tp1": True, "lock_after_tp2": True,
    "tp1_lock_delay_minutes": 3, "tp2_lock_delay_minutes": 5,
    "profit_lock_mode": "bep_plus_half_tp1", "bep_trigger_distance": 4.0,
    "tp1_lock_fraction": 0.75, "tp2_lock_target": "TP2",
    "runner_after_tp3": False, "tp3_lock_target": "TP2",
    "trailing_open_distance": 5.0, "trailing_close_distance": 8.0,
    "shared_sl": True,
}


def _full(c: dict) -> dict:
    base = dict(sweep.base_config_dict())
    base.update(c)
    return base


def _seed_configs() -> list[dict]:
    if MODE == "baseline":
        return [_full(REFERENCE_NO_TRAIL)]
    out = []
    for to in (3.0, 5.0, 8.0):
        for tc in (0.0, 8.0):
            for sh in (True, False):
                c = dict(OLD_TRAIL_CHAMP)
                c.update({"trailing_open_distance": to,
                          "trailing_close_distance": tc, "shared_sl": sh})
                out.append(_full(c))
    # trailing-close-only variants of the no-trailing reference (to=0, tc>0):
    # "trailing stop" without the trailing entry.
    for tc in (3.0, 5.0, 8.0):
        c = dict(REFERENCE_NO_TRAIL)
        c["trailing_close_distance"] = tc
        out.append(_full(c))
    return out


_orig = sweep_self_limit.make_limit_candidates


def patched(seed, max_candidates):
    if MODE == "baseline":
        return _seed_configs()  # exactly the reference, at its own risk 1%
    rng = random.Random(seed)
    drawn, seen, attempts = [], set(), 0
    while len(drawn) < max_candidates and attempts < max_candidates * 60:
        cfg = sweep.candidate_config(rng, include_trend_runner=False)
        attempts += 1
        if (cfg.get("trailing_open_distance", 0.0) <= 0.0
                and cfg.get("trailing_close_distance", 0.0) <= 0.0):
            continue  # trailing sweep: at least one trailing dimension on
        h = sweep._json_hash(cfg)
        if h not in seen:
            seen.add(h)
            drawn.append(cfg)
    out, seen2 = [], set()
    for c in _seed_configs() + drawn:
        c = dict(c)
        c["sizing_mode"] = "risk"
        c["risk_per_signal"] = 0.05
        h = sweep._json_hash(c)
        if h not in seen2:
            seen2.add(h)
            out.append(c)
    return out[: max_candidates + len(_seed_configs())]


sweep_self_limit.make_limit_candidates = patched

if __name__ == "__main__":
    raise SystemExit(sweep_self_limit.main(sys.argv[1:]))
