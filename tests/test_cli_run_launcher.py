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
TSL18 = CLI / "candidate_TSL18_trailing_tick.txt"
V072 = CLI / "candidate_V072_victor_trailing_combo.txt"
TS3K = CLI / "candidate_TS3K_small_account_tick.txt"


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
    # V072's section 4 is an N/A note (the backtest reads victor_signals.txt
    # directly), so it must not surface as a runnable section.
    numbers = [section.number for section in run.parse_sections(V072)]
    assert 4 not in numbers
    assert {1, 2, 3, 5, 6} <= set(numbers)


def test_sectionless_file_uses_leading_commands():
    sections = run.parse_sections(CLI / "resync_m1_from_2020.txt")
    assert any(
        cmd.startswith("python -m trading.engine.cli fetch")
        for section in sections
        for cmd in section.commands
    )


def test_resolvers_and_aliases():
    files = run.discover()
    assert run.resolve_strategy("tsl18", files) == TSL18
    assert run.resolve_strategy("v072", files) == V072
    assert run.resolve_strategy("ts3k", files) == TS3K
    assert run.resolve_strategy("nope", files) is None

    sections = run.parse_sections(TSL18)
    selected, multi = run.resolve_sections("backtest", sections)
    assert multi
    assert [section.number for section in selected] == [4, 5, 6, 7, 8, 9]

    rng, multi = run.resolve_sections("7-9", sections)
    assert multi
    assert [section.number for section in rng] == [7, 8, 9]


def _count_capital(out: str, amount: int) -> int:
    # Word-boundary count so "--initial-capital 5000" does NOT also match the
    # "5000" INSIDE "50000" (a plain str.count would double-count).
    return len(re.findall(rf"--initial-capital {amount}\b", out))


def test_backtest_keyword_prints_50k_and_5k_variants(capsys):
    # TSL18 is a deployed $50K book: its hybrid/explicit backtest sections each
    # emit a 50K command + a 5K clone (same dates, output dir suffixed _5k).
    # (TS3K is intentionally a $3K book, so it is NOT the fixture here -- see
    # test_non_50k_book_backtest_is_not_expanded.)
    rc = run.main(["tsl18", "backtest", "--print"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("tools/backtest_hybrid.py") == 4      # sections 5,6 x (50K+5K)
    assert out.count("tools/backtest_explicit.py") == 6    # sections 7,8,9 x (50K+5K)
    assert _count_capital(out, 50000) == 5
    assert _count_capital(out, 5000) == 5
    assert "--output-dir reports/TSL18_202606_5k" in out
    assert "--start-date 2026-06-01" in out
    assert "--output-dir reports/TSL18_202601_5k" in out
    assert "--start-date 2026-01-01" in out


def test_non_50k_book_backtest_is_not_expanded(capsys):
    # A book authored at a non-$50K capital (TS3K = $3K, the live small-account
    # book) is a deliberate sizing choice, NOT a 50K/5K pair: the launcher must
    # leave it untouched (no 5K clone, no _5k output dir), so the capital-variant
    # expansion never rewrites a strategy's authored capital.
    rc = run.main(["ts3k", "backtest", "--print"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--initial-capital 3000" in out
    assert _count_capital(out, 50000) == 0
    assert _count_capital(out, 5000) == 0
    assert "_5k" not in out


def test_capital_variants_keep_dates_and_suffix_output_dir():
    cmd = (
        "python tools/backtest_hybrid.py "
        "--output-dir reports/TSL18_202607 "
        "--initial-capital 50000 "
        "--start-date 2026-07-01 "
        "--end-date 2026-07-31"
    )
    variants = run._capital_variants(cmd)
    assert variants[0] == cmd
    assert "--initial-capital 5000" in variants[1]
    assert "--output-dir reports/TSL18_202607_5k" in variants[1]
    assert "--start-date 2026-07-01" in variants[1]
    assert "--end-date 2026-07-31" in variants[1]


def test_non_50k_command_is_not_expanded():
    cmd = "python tools/backtest_hybrid.py --output-dir reports/X --initial-capital 10000"
    assert run._capital_variants(cmd) == [cmd]
