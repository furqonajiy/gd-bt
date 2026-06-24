"""tools/gen_victor_tick_candidates.py: Victor tick-tune candidate matrix."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_ROOT), str(_ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from gen_victor_tick_candidates import CHAMPION, generate  # noqa: E402


def test_champion_is_c000():
    c = generate(40)
    assert c[0]["id"] == "c000"
    assert c[0]["slm"] == CHAMPION["slm"] == 2.1
    assert c[0]["maxhold"] == 240 and c[0]["frac"] == 0.5 and c[0]["entries"] == 8
    assert c[0]["delay"] == 24 and c[0]["lock2"] == "true" and c[0]["final"] == "TP3"
    assert c[0]["slgap"] == 0.5


def test_unique_and_uniform_keys():
    c = generate(100)
    assert len(c) == 100
    assert len({x["id"] for x in c}) == 100
    keys = set(c[0])
    assert all(set(x) == keys for x in c)
    # no feed levers (Victor feed is fixed)
    assert "rr1" not in keys and "bb" not in keys and "rsi_buy" not in keys


def test_deterministic():
    assert generate(50, seed=3) == generate(50, seed=3)
