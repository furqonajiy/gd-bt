"""tools/gen_tick_sweep_candidates.py: the full tick-sweep candidate matrix."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_ROOT), str(_ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from gen_tick_sweep_candidates import CHAMPION, generate  # noqa: E402


def test_champion_is_c000_with_known_values():
    cands = generate(50)
    c0 = cands[0]
    assert c0["id"] == "c000"
    assert c0["slm"] == CHAMPION["slm"] == 2.1
    assert c0["entries"] == 8 and c0["maxhold"] == 240 and c0["frac"] == 0.5
    assert (c0["rr1"], c0["rr2"], c0["rr3"]) == (1.0, 2.0, 4.0)
    assert c0["rsi_buy"] == 75 and c0["rsi_sell"] == 25 and c0["bb"] == "0.0006"


def test_count_uniqueness_and_uniform_keys():
    cands = generate(120)
    assert len(cands) == 120
    ids = [c["id"] for c in cands]
    assert len(set(ids)) == 120                       # unique ids
    keys = set(cands[0])
    assert all(set(c) == keys for c in cands)         # every cell has the same keys
    # distinct parameter combos (ignoring the id field)
    combos = {tuple(sorted((k, v) for k, v in c.items() if k != "id")) for c in cands}
    assert len(combos) == 120


def test_deterministic_for_a_seed():
    assert generate(60, seed=7) == generate(60, seed=7)
    assert generate(60, seed=7) != generate(60, seed=8)


def test_generate_scales_without_exploding():
    # The space is huge (>340k combos), so generate honors n without hanging;
    # main() is what clamps to the GitHub 256-matrix limit.
    cands = generate(300)
    assert len(cands) == 300
    assert len({c["id"] for c in cands}) == 300
