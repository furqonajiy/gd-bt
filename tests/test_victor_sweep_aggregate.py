from tools.victor_sweep_aggregate import survivors


def _row(name, *, wf=True, oos=1.0, dd=20.0, edge=100.0, bonus=120.0):
    return {
        "candidate_id": name,
        "passes_walk_forward": wf,
        "oos_fixed_no_bonus_profit": oos,
        "concurrent_risk_max_dd_pct": dd,
        "fixed_no_bonus_profit": edge,
        "fixed_with_bonus_profit": bonus,
    }


def test_survivors_reject_failed_walk_forward_when_present():
    rows = [
        _row("failed", wf=False, bonus=1000.0),
        _row("stable", wf=True, bonus=200.0),
    ]
    assert [r["candidate_id"] for r in survivors(rows, dd_gate=40.0)] == ["stable"]


def test_survivors_accept_legacy_rows_without_walk_forward():
    legacy = _row("legacy")
    legacy.pop("passes_walk_forward")
    assert survivors([legacy], dd_gate=40.0) == [legacy]


def test_survivors_parse_csv_false_text():
    rows = [
        _row("csv_false", wf="False", bonus=1000.0),
        _row("csv_true", wf="true", bonus=200.0),
    ]
    assert [r["candidate_id"] for r in survivors(rows, dd_gate=40.0)] == ["csv_true"]
