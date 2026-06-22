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
VICTOR = CLI / "champion_victor.txt"


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
    # Victor's leading block (cd / conda activate / git ...) must be filtered out:
    # those change shell state a subprocess can't hand back to the terminal.
    for s in run.parse_sections(VICTOR):
        for cmd in s.commands:
            assert not cmd.startswith(("cd ", "conda ", "git ")), cmd
            assert cmd.startswith("python "), cmd  # only program invocations run


def test_multiline_command_joins_byte_identical():
    # Independently re-derive section 3's command from the raw file region and
    # compare: parse must join the backtick block exactly, lose/add nothing.
    raw = SQZ6.read_text().splitlines()
    block = [l for l in raw[80:115] if l.strip() and not l.lstrip().startswith("#")]
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
    assert run.resolve_section("3", sections).number == 3
    assert run.resolve_section("auto", sections).number == 3   # name match
    assert run.resolve_section("99", sections) is None


def test_print_mode_runs_nothing(capsys):
    rc = run.main(["sqz6", "3", "--print"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "python tools/auto_explicit.py" in out
    assert "$ " not in out  # --print shows the bare command, no run banner
