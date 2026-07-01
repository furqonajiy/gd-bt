from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "cli"


def _load():
    spec = importlib.util.spec_from_file_location("cli_run", CLI / "run.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["cli_run"] = module
    spec.loader.exec_module(module)
    return module


run = _load()
SQZ6 = CLI / "champion_R4_SQZ6_no_trailing.txt"
VICTOR = CLI / "candidate_VIC_C116_tick.txt"


def test_every_snapshot_parses_into_runnable_sections():
    for txt in CLI.glob("*.txt"):
        sections = run.parse_sections(txt)
        assert sections, f"{txt.name} produced no sections"
        for section in sections:
            for cmd in section.commands:
                assert "`" not in cmd, f"{txt.name}: stray backtick in {cmd!r}"
                assert "  " not in cmd, f"{txt.name}: double space in {cmd!r}"
                assert cmd == cmd.strip()


def test_note_only_section_is_skipped():
    numbers = [section.number for section in run.parse_sections(VICTOR)]
    assert 4 not in numbers
    assert {1, 2, 3, 5, 6, 7, 8, 9} <= set(numbers)


def test_sectionless_file_uses_leading_commands():
    sections = run.parse_sections(CLI / "resync_m1_from_2020.txt")
    assert any(
        cmd.startswith("python -m trading.engine.cli fetch")
        for section in sections
        for cmd in section.commands
    )


def test_resolvers_and_aliases():
    files = run.discover()
    assert run.resolve_strategy("sqz6", files) == SQZ6
    assert run.resolve_strategy("vic", files) == VICTOR
    assert run.resolve_strategy("nope", files) is None

    sections = run.parse_sections(SQZ6)
    selected, multi = run.resolve_sections("backtest", sections)
    assert multi
    assert [section.number for section in selected] == [4, 5, 6, 7, 8, 9]

    rng, multi = run.resolve_sections("7-9", sections)
    assert multi
    assert [section.number for section in rng] == [7, 8, 9]


def test_backtest_keyword_prints_50k_and_5k_variants(capsys):
    rc = run.main(["v116", "backtest", "--print"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "running sections 5, 6, 7, 8, 9 in order" in out
    assert out.count("tools/backtest_hybrid.py") == 4
    assert out.count("tools/backtest_explicit.py") == 6
    assert out.count("--initial-capital 50000") == 5
    assert out.count("--initial-capital 5000") == 5
    assert "--output-dir reports/V116_202606_5k" in out
    assert "--start-date 2026-06-01" in out
    assert "--output-dir reports/V116_202601_5k" in out
    assert "--start-date 2026-01-01" in out


def test_capital_variants_keep_dates_and_suffix_output_dir():
    cmd = (
        "python tools/backtest_hybrid.py "
        "--output-dir reports/T818_202607 "
        "--initial-capital 50000 "
        "--start-date 2026-07-01 "
        "--end-date 2026-07-31"
    )
    variants = run._capital_variants(cmd)
    assert variants[0] == cmd
    assert "--initial-capital 5000" in variants[1]
    assert "--output-dir reports/T818_202607_5k" in variants[1]
    assert "--start-date 2026-07-01" in variants[1]
    assert "--end-date 2026-07-31" in variants[1]


def test_non_50k_command_is_not_expanded():
    cmd = "python tools/backtest_hybrid.py --output-dir reports/X --initial-capital 10000"
    assert run._capital_variants(cmd) == [cmd]
