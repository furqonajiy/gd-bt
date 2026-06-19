import csv
import json
from pathlib import Path

from tools import strategy_tournament_report as tr


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_load_source_picks_best_dd_oos_candidate(tmp_path):
    csv_path = tmp_path / "victor" / "leaderboard.csv"
    _write_csv(csv_path, [
        {
            "candidate_id": "bad_dd",
            "fixed_with_bonus_profit": "9999",
            "fixed_no_bonus_profit": "9000",
            "oos_fixed_no_bonus_profit": "1000",
            "concurrent_risk_max_dd_pct": "45",
            "config_json": "{}",
        },
        {
            "candidate_id": "best",
            "fixed_with_bonus_profit": "2000",
            "fixed_no_bonus_profit": "1500",
            "bonus_contribution": "500",
            "oos_fixed_no_bonus_profit": "300",
            "concurrent_risk_max_dd_pct": "30",
            "walk_forward_positive_fraction": "0.75",
            "config_json": json.dumps({"entry_count": 8, "sl_multiplier": 2.1}),
        },
    ])

    rows = tr.load_source(f"Victor={tmp_path / 'victor'}", dd_gate=40.0)
    assert len(rows) == 1
    assert rows[0].source == "Victor"
    assert rows[0].label == "victor"
    assert rows[0].objective == 2000.0
    assert rows[0].bonus == 500.0
    assert rows[0].wf_positive_fraction == 0.75
    assert "e8" in rows[0].config_brief


def test_load_source_rejects_failed_walk_forward(tmp_path):
    csv_path = tmp_path / "self" / "leaderboard.csv"
    _write_csv(csv_path, [
        {
            "candidate_id": "unstable",
            "fixed_with_bonus_profit": "3000",
            "fixed_no_bonus_profit": "2500",
            "oos_fixed_no_bonus_profit": "500",
            "concurrent_risk_max_dd_pct": "20",
            "passes_walk_forward": "False",
            "config_json": "{}",
        },
        {
            "candidate_id": "stable",
            "fixed_with_bonus_profit": "1000",
            "fixed_no_bonus_profit": "900",
            "oos_fixed_no_bonus_profit": "200",
            "concurrent_risk_max_dd_pct": "20",
            "passes_walk_forward": "true",
            "config_json": "{}",
        },
    ])

    rows = tr.load_source(f"Self={tmp_path / 'self'}", dd_gate=40.0)
    assert len(rows) == 1
    assert rows[0].objective == 1000.0


def test_champion_json_normalizes_record(tmp_path):
    champ = tmp_path / "CHAMPION_R4parab.json"
    champ.write_text(json.dumps({
        "regime": "R4parab",
        "feed": "scalper24",
        "edge": 1000.0,
        "bonus": 120.0,
        "oos": 200.0,
        "dd": 35.0,
        "config": {"entry_count": 6},
    }), encoding="utf-8")

    rows = tr.load_source(f"Self={champ}", dd_gate=40.0)
    assert len(rows) == 1
    assert rows[0].label == "CHAMPION_R4parab"
    assert rows[0].objective == 1120.0
    assert rows[0].feed == "scalper24"
    assert rows[0].regime == "R4parab"


def test_compounded_net_is_context_not_deploy_objective(tmp_path):
    champ = tmp_path / "CHAMPION_R4parab.json"
    champ.write_text(json.dumps({
        "regime": "R4parab",
        "feed": "scalper24",
        "edge": 1000.0,
        "net_bonus": 999999.0,
        "oos": 200.0,
        "dd": 35.0,
        "config": {"entry_count": 6},
    }), encoding="utf-8")

    rows = tr.load_source(f"Self={champ}", dd_gate=40.0)
    assert len(rows) == 1
    assert rows[0].objective == 1000.0
    assert rows[0].bonus == 0.0
    assert rows[0].compounded == 999999.0


def test_champion_json_respects_dd_gate(tmp_path):
    champ = tmp_path / "CHAMPION_R4parab.json"
    champ.write_text(json.dumps({
        "regime": "R4parab",
        "feed": "scalper24",
        "edge": 1000.0,
        "oos": 200.0,
        "dd": 45.0,
        "config": {"entry_count": 6},
    }), encoding="utf-8")

    assert tr.load_source(f"Self={champ}", dd_gate=40.0) == []


def test_render_markdown_groups_by_source(tmp_path):
    victor = tr.Candidate(
        source="Victor", label="R4", path="victor.csv", kind="leaderboard",
        feed="-", regime="R4", objective=2000.0, edge=1500.0, bonus=500.0,
        oos=300.0, dd=30.0, wf_positive_fraction=0.75, compounded=5000.0,
        config_brief="e8", config={})
    self = tr.Candidate(
        source="Self", label="R4", path="self.json", kind="champion",
        feed="scalper24", regime="R4", objective=1000.0, edge=900.0, bonus=100.0,
        oos=200.0, dd=20.0, wf_positive_fraction=None, compounded=1000.0,
        config_brief="e6", config={})

    text = tr.render_markdown([self, victor], dd_gate=40.0)
    assert "## Best by Source" in text
    assert "| Victor | R4 | $2,000" in text
    assert "| Self | R4 | $1,000" in text
    assert "| 1 | Victor | R4 | $2,000" in text


def test_cli_writes_markdown_and_json(tmp_path):
    csv_path = tmp_path / "victor" / "leaderboard.csv"
    out = tmp_path / "report.md"
    json_out = tmp_path / "report.json"
    _write_csv(csv_path, [{
        "candidate_id": "best",
        "fixed_with_bonus_profit": "2000",
        "fixed_no_bonus_profit": "1500",
        "oos_fixed_no_bonus_profit": "300",
        "concurrent_risk_max_dd_pct": "30",
        "config_json": "{}",
    }])

    rc = tr.main([
        "--source", f"Victor={tmp_path / 'victor'}",
        "--out", str(out),
        "--json-out", str(json_out),
    ])
    assert rc == 0
    assert "Strategy tournament report" in out.read_text(encoding="utf-8")
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data[0]["source"] == "Victor"
