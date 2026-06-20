"""Signal sanity auto-correction: the wrong-hundreds far-SL repair.

VICTOR sometimes posts a stop that is on the correct side of the entry but a
full hundred-plus points away -- a wrong-hundreds-digit mistype, e.g. a BUY at
4319-4321 with `SL 4214` that he meant as 4314. `apply_signal_corrections` must
repair that with a clean +/-100*n shift, while leaving genuine (modest) stops
and already-tight stops untouched. RR is never tuned here.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "listeners" / "telegram"))
import listener as tl  # noqa: E402


def _fix(side, r1, r2, sl, tp1, tp2, tp3):
    parsed = tl.ParsedSignal(side=side, r1=r1, r2=r2, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3)
    return tl.apply_signal_corrections(parsed)


def test_buy_far_sl_wrong_hundreds_is_repaired():
    # The reported case: BUY 4319-4321, meant SL 4314, typed 4214.
    fix = _fix("BUY", 4321, 4319, 4214, 4329, 4339, 4359)
    assert fix.corrected.sl == 4314
    assert any("SL: 4214 -> 4314" in c for c in fix.changes)
    # Entry/TP are not disturbed by the SL repair.
    assert (fix.corrected.r1, fix.corrected.r2) == (4321, 4319)
    assert (fix.corrected.tp1, fix.corrected.tp2, fix.corrected.tp3) == (4329, 4339, 4359)


def test_buy_far_sl_second_real_example_from_feed():
    # 2026-06-15 #6 in victor_signals.txt: BUY 4325-4327 SL 4219 -> 4319.
    fix = _fix("BUY", 4327, 4325, 4219, 4335, 4345, 4365)
    assert fix.corrected.sl == 4319


def test_sell_far_sl_wrong_hundreds_is_repaired():
    # Mirror for SELL: stop sits above the high; 4426 meant 4326.
    fix = _fix("SELL", 4319, 4321, 4426, 4311, 4301, 4281)
    assert fix.corrected.sl == 4326
    assert any("SL: 4426 -> 4326" in c for c in fix.changes)


def test_tight_valid_sl_is_left_alone():
    fix = _fix("BUY", 4321, 4319, 4314, 4329, 4339, 4359)
    assert fix.corrected.sl == 4314
    assert not any(c.startswith("SL:") for c in fix.changes)


def test_moderately_wide_valid_sl_is_left_alone():
    # A real wider stop (~15 pts) under a 30-pt TP3 is plausible -- never touch.
    fix = _fix("BUY", 4500, 4498, 4483, 4510, 4520, 4530)
    assert fix.corrected.sl == 4483
    assert not any(c.startswith("SL:") for c in fix.changes)


def test_far_sl_with_no_clean_hundreds_repair_is_left_unchanged():
    # 78 pts away (outlier vs a 30-pt TP3) but no +/-100 shift lands it back on
    # the correct side inside the band -> we never guess, so leave it as posted.
    fix = _fix("BUY", 4500, 4498, 4420, 4510, 4520, 4530)
    assert fix.corrected.sl == 4420
    assert not any(c.startswith("SL:") for c in fix.changes)


def test_wrong_side_sl_still_handled_by_legacy_fixer():
    # SL above the entry on a BUY is the pre-existing wrong-side path, untouched
    # by the new outlier repair.
    fix = _fix("BUY", 4321, 4319, 4330, 4329, 4339, 4359)
    assert fix.corrected.sl < 4319
    assert any(c.startswith("SL:") for c in fix.changes)
