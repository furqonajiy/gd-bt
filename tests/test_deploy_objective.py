from tools import deploy_objective as obj


def test_fixed_with_bonus_prefers_explicit_metric():
    row = {
        "fixed_no_bonus_profit": 100.0,
        "bonus_contribution": 12.0,
        "fixed_with_bonus_profit": 130.0,
        "risk_net_profit_with_bonus": 9999.0,
    }
    assert obj.fixed_with_bonus_profit(row) == 130.0
    assert obj.bonus_profit(row) == 12.0
    assert obj.compounded_net_bonus(row) == 9999.0


def test_fixed_with_bonus_can_be_derived_from_edge_and_bonus():
    row = {"edge": 100.0, "bonus": 9.0}
    assert obj.fixed_with_bonus_profit(row) == 109.0
    assert obj.edge_profit(row) == 100.0


def test_rank_key_does_not_let_compounded_net_win_primary_objective():
    high_compounded = {
        "fixed_with_bonus_profit": 100.0,
        "oos_fixed_no_bonus_profit": 50.0,
        "fixed_no_bonus_profit": 90.0,
        "risk_net_profit_with_bonus": 1000000.0,
    }
    better_deploy = {
        "fixed_with_bonus_profit": 120.0,
        "oos_fixed_no_bonus_profit": 10.0,
        "fixed_no_bonus_profit": 80.0,
        "risk_net_profit_with_bonus": 1.0,
    }
    assert obj.rank_key(better_deploy) > obj.rank_key(high_compounded)


def test_survivors_gate_and_sort_by_deploy_objective():
    rows = [
        {"fixed_with_bonus_profit": 100.0, "oos_fixed_no_bonus_profit": 10.0,
         "concurrent_risk_max_dd_pct": 20.0},
        {"fixed_with_bonus_profit": 200.0, "oos_fixed_no_bonus_profit": -1.0,
         "concurrent_risk_max_dd_pct": 20.0},
        {"fixed_with_bonus_profit": 300.0, "oos_fixed_no_bonus_profit": 10.0,
         "concurrent_risk_max_dd_pct": 45.0},
        {"fixed_with_bonus_profit": 120.0, "oos_fixed_no_bonus_profit": 5.0,
         "concurrent_risk_max_dd_pct": 30.0},
    ]
    surv = obj.survivors(rows, dd_gate=40.0)
    assert [obj.fixed_with_bonus_profit(r) for r in surv] == [120.0, 100.0]


def test_old_champion_records_fall_back_to_edge():
    stored = {"edge": 500.0, "oos": 50.0, "dd": 30.0, "net_bonus": 999999.0}
    challenger = {"fixed_with_bonus_profit": 600.0, "oos": 10.0, "edge": 100.0}
    assert obj.fixed_with_bonus_profit(stored) == 500.0
    assert obj.strictly_beats(challenger, stored)
