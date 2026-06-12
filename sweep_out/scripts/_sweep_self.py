"""Research scratch driver (gitignored). Wraps tools/sweep_self_limit.py:
  * pins sizing to risk 5% on every candidate (live cap);
  * seeds the Victor-winner config family for comparability;
  * --mode limit -> trailing_open 0 (default); --mode trailopen -> only
    trailing_open>0 candidates (reported separately).
"""
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import sweep  # noqa: E402
import sweep_self_limit  # noqa: E402

MODE = "limit"
if "--mode" in sys.argv:
    i = sys.argv.index("--mode")
    MODE = sys.argv[i + 1]
    del sys.argv[i:i + 2]


def _seed_configs() -> list[dict]:
    s1 = dict(sweep.base_config_dict())
    s1.update({
        "entry_count": 8, "entry_ladder": "range_to_sl", "entry_sl_gap": 0.5,
        "activation_delay_minutes": 2, "pending_expiry_minutes": 180,
        "max_hold_minutes": 240, "sl_multiplier": 2.1, "final_target": "TP3",
        "lock_after_tp1": True, "lock_after_tp2": True,
        "tp1_lock_delay_minutes": 24, "tp2_lock_delay_minutes": 2,
        "profit_lock_mode": "tp_levels", "tp1_lock_fraction": 0.5,
        "tp2_lock_target": "TP1", "trailing_open_distance": 0.0,
        "trailing_close_distance": 0.0,
    })
    out = [s1]
    for mut in ({"sl_multiplier": 2.2}, {"entry_count": 6},
                {"tp1_lock_delay_minutes": 12}, {"trailing_close_distance": 5.0},
                {"entry_count": 4, "sl_multiplier": 1.61}):
        c = dict(s1); c.update(mut); out.append(c)
    if MODE == "trailopen":
        out = []
        for od in (1.0, 2.0, 3.0):
            for cd in (2.0, 5.0):
                c = dict(s1)
                c.update({"trailing_open_distance": od, "trailing_close_distance": cd})
                out.append(c)
    return out


_orig = sweep_self_limit.make_limit_candidates


def patched(seed, max_candidates):
    if MODE == "limit":
        drawn = _orig(seed, max_candidates)
    else:
        rng = random.Random(seed)
        drawn, seen, attempts = [], set(), 0
        while len(drawn) < max_candidates and attempts < max_candidates * 60:
            cfg = sweep.candidate_config(rng, include_trend_runner=False)
            attempts += 1
            if cfg.get("trailing_open_distance", 0.0) <= 0.0:
                continue
            h = sweep._json_hash(cfg)
            if h not in seen:
                seen.add(h); drawn.append(cfg)
    out, seen2 = [], set()
    for c in _seed_configs() + drawn:
        c = dict(c); c["sizing_mode"] = "risk"; c["risk_per_signal"] = 0.05
        h = sweep._json_hash(c)
        if h not in seen2:
            seen2.add(h); out.append(c)
    return out[: max_candidates + len(_seed_configs())]


sweep_self_limit.make_limit_candidates = patched

if __name__ == "__main__":
    raise SystemExit(sweep_self_limit.main(sys.argv[1:]))
