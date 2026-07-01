from __future__ import annotations

import importlib.util
import re
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
    assert [section.number for section in selected] == [4, 5, 6, 7, 8, 9, 10]

    rng, multi = run.resolve_sections("7-9", sections)
    assert multi
    assert [section.number for section in rng] == [7, 8, 9]


def _count_capital(out: str, amount: int) -> int:
    # Word-boundary count so "--initial-capital 5000" does NOT also match the
    # "5000" INSIDE "50000" (a plain str.count would double-count).
    return len(re.findall(rf"--initial-capital {amount}\b", out))


def test_backtest_keyword_prints_single_3k_variant(capsys):
    # The books are now authored at a single --initial-capital 3000 (the 50K/5K
    # dual-variant authoring was retired). V817's ten-section layout has six
    # backtest windows (5=2026-06, 6=2026-05, 7=2026-01, 8=2025, 9=2024,
    # 10=2021-2023): three tick/hybrid 2026 windows + three M1 era windows, each
    # emitted ONCE at 3K -- no _5k clone. (The 50K->5K expansion itself is still
    # covered by test_capital_variants_keep_dates_and_suffix_output_dir.)
    rc = run.main(["v817", "backtest", "--print"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "running sections 5, 6, 7, 8, 9, 10 in order" in out
    assert out.count("tools/backtest_hybrid.py") == 3
    assert out.count("tools/backtest_explicit.py") == 3
    assert _count_capital(out, 3000) == 6
    assert _count_capital(out, 50000) == 0
    assert _count_capital(out, 5000) == 0
    assert "_5k" not in out
    assert "--output-dir reports/V817_202605" in out
    assert "--start-date 2026-05-01" in out
    assert "--output-dir reports/V817_202601" in out
    assert "--start-date 2026-01-01" in out


def test_non_50k_book_backtest_is_not_expanded(capsys):
    # A book NOT authored at $50K is a deliberate sizing choice, NOT a 50K/5K
    # pair: the launcher must leave it untouched (no 5K clone, no _5k output dir),
    # so the capital-variant expansion never rewrites a strategy's authored
    # capital. All books are now at 3K, which is likewise left as a single variant.
    rc = run.main(["v116", "backtest", "--print"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--initial-capital 3000" in out
    assert _count_capital(out, 50000) == 0
    assert _count_capital(out, 5000) == 0
    assert "_5k" not in out


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
