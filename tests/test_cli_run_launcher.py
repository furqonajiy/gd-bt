"""cli/run.py turns the cli/*.txt snapshots into clickable, run-in-this-terminal
sections. The reconstructed command must stay byte-identical to the snapshot
(PowerShell ` continuations joined into one line), the cd/conda/git setup
preamble must NOT be exposed (a subprocess can't change the parent shell), and
note-only sections (N/A) must be skipped. Deterministic, no MT5.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "cli"


def _load():
    spec = importlib.util.spec_from_file_location("cli_run", CLI / "run.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["cli_run"] = m  # so @dataclass can resolve the module's namespace
    spec.loader.exec_module(m)
    return m


run = _load()
SQZ6 = CLI / "champion_R4_SQZ6_no_trailing.txt"
VICTOR = CLI / "candidate_VIC_C116_tick.txt"   # V116 is now the Victor champion


def test_every_snapshot_parses_into_runnable_sections():
    for txt in CLI.glob("*.txt"):
        sections = run.parse_sections(txt)
        assert sections, f"{txt.name} produced no sections"
        for s in sections:
            for cmd in s.commands:
                assert "`" not in cmd, f"{txt.name}: stray backtick in {cmd!r}"
                assert "  " not in cmd, f"{txt.name}: double space in {cmd!r}"
                assert cmd == cmd.strip()


def test_setup_preamble_is_not_exposed():
    # Any leading cd / conda activate / git block must be filtered out (those change
    # shell state a subprocess can't hand back); every exposed command is a program
    # invocation that runs in the current terminal.
    for s in run.parse_sections(VICTOR):
        for cmd in s.commands:
            assert not cmd.startswith(("cd ", "conda ", "git ")), cmd
            assert cmd.startswith("python "), cmd  # only program invocations run


def test_multiline_command_joins_byte_identical():
    # Independently re-derive section 3's command from the raw file region and
    # compare: parse must join the backtick block exactly, lose/add nothing.
    # Derive the block dynamically (the `python tools/auto_explicit.py` line plus
    # its PowerShell backtick continuations) rather than hard-coding line numbers,
    # so adding a flag like --maximum-lot never silently truncates the expectation.
    raw = SQZ6.read_text().splitlines()
    start = next(i for i, l in enumerate(raw)
                 if l.strip().startswith("python tools/auto_explicit.py"))
    end = start
    while raw[end].rstrip().endswith("`"):   # walk to the final (un-continued) line
        end += 1
    block = [l for l in raw[start:end + 1] if l.strip() and not l.lstrip().startswith("#")]
    expected = " ".join(l.rstrip().rstrip("`").strip() for l in block)
    auto = next(s for s in run.parse_sections(SQZ6) if s.number == 3)
    assert auto.commands == [expected]
    assert auto.commands[0].startswith("python tools/auto_explicit.py ")
    assert "--strategy-tag SQZ6" in auto.commands[0]
    assert "--forensic-log forensic_sqz6.jsonl" in auto.commands[0]
    assert auto.commands[0].endswith("--trailing-close-distance 0.0")


def test_note_only_section_is_skipped():
    # Victor section 4 (SIGNAL GENERATOR) is "N/A" -> no command -> not listed.
    numbers = [s.number for s in run.parse_sections(VICTOR)]
    assert 4 not in numbers
    assert {1, 2, 3, 5, 6, 7, 8, 9} <= set(numbers)


def test_sectionless_file_uses_leading_commands():
    # resync's commands sit under un-numbered '=====' sections; still runnable.
    fetch = run.parse_sections(CLI / "resync_m1_from_2020.txt")
    assert any(c.startswith("python -m trading.engine.cli fetch")
               for s in fetch for c in s.commands)


def test_resolvers_and_aliases():
    files = run.discover()
    assert run.resolve_strategy("sqz6", files) == SQZ6
    assert run.resolve_strategy("vic", files) == VICTOR
    assert run.resolve_strategy("nope", files) is None
    sections = run.parse_sections(SQZ6)

    def one(token):
        sel, _multi = run.resolve_sections(token, sections)
        return sel[0] if len(sel) == 1 else None

    assert one("3").number == 3
    assert one("auto").number == 3                    # name match
    assert run.resolve_sections("99", sections)[0] == []   # no such number


def test_section_keywords_and_ranges():
    sections = run.parse_sections(SQZ6)

    bt, multi = run.resolve_sections("backtest", sections)
    assert multi and [s.number for s in bt] == [4, 5, 6, 7, 8, 9]  # >= 4 (4=signal-gen for SQZ6)
    assert run.resolve_sections("bt", sections)[0] == bt

    rng, multi = run.resolve_sections("7-9", sections)
    assert multi and [s.number for s in rng] == [7, 8, 9]
    assert [s.number for s in run.resolve_sections("8-", sections)[0]] == [8, 9]

    live, multi = run.resolve_sections("live", sections)
    assert multi and [s.number for s in live] == [2, 3]          # 1 (listener) is N/A here


def test_backtest_keyword_runs_sections_4_to_end(capsys):
    # V116: section 4 is N/A, so `backtest` is sections 5..9; all reconstruct.
    rc = run.main(["v116", "backtest", "--print"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "running sections 5, 6, 7, 8, 9 in order" in out
    assert out.count("tools/backtest_hybrid.py") == 2          # 2026-06 + 2026-01
    assert out.count("tools/backtest_explicit.py") == 3        # 2025 / 2024 / 2021-2023


def test_print_mode_runs_nothing(capsys):
    rc = run.main(["sqz6", "3", "--print"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "python tools/auto_explicit.py" in out
    assert "$ " not in out  # --print shows the bare command, no run banner
