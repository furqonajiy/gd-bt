#!/usr/bin/env python3
"""Clickable launcher for the cli/*.txt deployment snapshots.

The snapshot files remain the canonical command source. This launcher joins the
PowerShell-style continuation lines and runs the selected section in the current
terminal.

Backtest sections are expanded at launch time: a snapshot command authored with
``--initial-capital 50000`` and ``--output-dir`` runs/prints twice. The first run
is the original 50K command; the second is a 5K clone with the same dates/months
and the output directory suffixed with ``_5k``.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parent

SECTION_RE = re.compile(r"^#\s*=====\s*(.*?)\s*=====\s*$")
BANNER_RE = re.compile(r"^#\s*=+\s*$")
NUM_NAME_RE = re.compile(r"^(\d+)\.\s*(.*)$")

ALIASES = {
    "victor": "candidate_VIC_C116", "vic": "candidate_VIC_C116",
    "v116": "candidate_VIC_C116", "vic116": "candidate_VIC_C116",
    "sqz6": "champion_R4_SQZ6", "c160": "candidate_R4_C160",
    "vct5": "candidate_VCT5_victor_trailing",
    "vs17": "candidate_VS17_victor_trailing",
    "v017": "candidate_V017_victor_july_entry_level",
    "v817": "candidate_V817_victor_trailing",
    "t160": "candidate_T160_trailing_tick",
    "t18s": "candidate_T18S_trailing_tick",
    "t818": "candidate_T818_trailing_tick",
    "tsl18": "candidate_TSL18_trailing_tick",
    "twl25": "candidate_TWL25_loss_filtered_tick",
    "resync": "resync_m1_from_2020",
    "resync-ticks": "resync_ticks", "ticks": "resync_ticks",
}


@dataclass
class Section:
    number: int
    name: str
    commands: list[str] = field(default_factory=list)


def _join_command(lines: list[str]) -> str:
    parts: list[str] = []
    for line in lines:
        text = line.rstrip()
        if text.endswith("`"):
            text = text[:-1].rstrip()
        parts.append(text.strip())
    return " ".join(part for part in parts if part)


def _command_runs(body: list[str]) -> list[str]:
    cmds: list[str] = []
    buf: list[str] = []
    for line in body:
        text = line.strip()
        if not text or text.startswith("#"):
            if buf:
                cmds.append(_join_command(buf))
                buf = []
            continue
        buf.append(line)
        if not line.rstrip().endswith("`"):
            cmds.append(_join_command(buf))
            buf = []
    if buf:
        cmds.append(_join_command(buf))
    return cmds


def parse_sections(path: Path) -> list[Section]:
    leading: list[str] = []
    raw_sections: list[tuple[str, list[str]]] = []
    cur: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if BANNER_RE.match(line):
            (cur if cur is not None else leading).append(line)
            continue
        match = SECTION_RE.match(line)
        if match:
            cur = []
            raw_sections.append((match.group(1).strip(), cur))
        else:
            (cur if cur is not None else leading).append(line)

    if not raw_sections:
        return [Section(i, f"command {i}", [cmd]) for i, cmd in enumerate(_command_runs(leading), start=1)]

    out: list[Section] = []
    for idx, (title, body) in enumerate(raw_sections, start=1):
        match = NUM_NAME_RE.match(title)
        number = int(match.group(1)) if match else idx
        name = match.group(2).strip() if match else title
        cmds = _command_runs(body)
        if cmds:
            out.append(Section(number, name, cmds))
    return out


def discover() -> list[Path]:
    return sorted(CLI_DIR.glob("*.txt"))


def resolve_strategy(token: str, files: list[Path]) -> Path | None:
    key = ALIASES.get(token.lower(), token).lower()
    hits = [path for path in files if key in path.stem.lower()]
    if len(hits) == 1:
        return hits[0]
    exact = [path for path in files if path.stem.lower() == token.lower()]
    return exact[0] if len(exact) == 1 else None


def resolve_sections(token: str, sections: list[Section]) -> tuple[list[Section], bool]:
    text = token.lower().strip()
    if text in ("backtest", "backtests", "bt"):
        return [section for section in sections if section.number >= 4], True
    if text == "live":
        return [section for section in sections if 1 <= section.number <= 3], True
    if text == "all":
        return list(sections), True
    match = re.fullmatch(r"(\d+)-(\d*)", text)
    if match:
        lo = int(match.group(1))
        hi = int(match.group(2)) if match.group(2) else max((section.number for section in sections), default=lo)
        return [section for section in sections if lo <= section.number <= hi], True
    if text.isdigit():
        return [section for section in sections if section.number == int(text)], False
    return [section for section in sections if text in section.name.lower()], False


def _choose(prompt: str, labels: list[str]) -> int | None:
    for i, label in enumerate(labels, start=1):
        print(f"  [{i}] {label}")
    try:
        raw = input(f"{prompt} (number, or blank to cancel): ").strip()
    except EOFError:
        return None
    if not raw:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(labels):
        return int(raw) - 1
    print(f"  ? '{raw}' is not 1-{len(labels)}")
    return None


def _capital_variants(cmd: str) -> list[str]:
    if "--initial-capital 50000" not in cmd or "--output-dir " not in cmd:
        return [cmd]

    small = re.sub(r"(--initial-capital\s+)50000\b", r"\g<1>5000", cmd, count=1)

    def suffix_output_dir(match: re.Match[str]) -> str:
        prefix, output_dir = match.groups()
        if output_dir.endswith("_5k"):
            return match.group(0)
        return f"{prefix}{output_dir}_5k"

    small = re.sub(r"(--output-dir\s+)(\S+)", suffix_output_dir, small, count=1)
    return [cmd] if small == cmd else [cmd, small]


def run_section(section: Section, *, dry: bool) -> int:
    for snapshot_cmd in section.commands:
        for cmd in _capital_variants(snapshot_cmd):
            print(f"\n$ {cmd}\n" if not dry else cmd)
            if dry:
                continue
            rc = subprocess.run(cmd, shell=True).returncode
            if rc != 0:
                return rc
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a cli/*.txt section in the current terminal.")
    parser.add_argument("strategy", nargs="?", help="strategy alias or part of a cli/*.txt filename")
    parser.add_argument("section", nargs="?", help="section number/name, range, backtest/bt, live, or all")
    parser.add_argument("--print", "--dry-run", dest="dry", action="store_true", help="show command(s) instead of running")
    args = parser.parse_args(argv)

    files = discover()
    if not files:
        print("no cli/*.txt snapshots found", file=sys.stderr)
        return 1

    path: Path | None = None
    if args.strategy:
        path = resolve_strategy(args.strategy, files)
        if path is None:
            print(f"no single match for strategy {args.strategy!r}; choose one:")
    if path is None:
        idx = _choose("Strategy", [path.stem for path in files])
        if idx is None:
            return 1
        path = files[idx]

    sections = parse_sections(path)
    if not sections:
        print(f"{path.name} has no runnable sections", file=sys.stderr)
        return 1

    selected: list[Section] = []
    if args.section:
        selected, multi = resolve_sections(args.section, sections)
        if not selected:
            print(f"no match for section {args.section!r} in {path.stem}; choose one:")
        elif len(selected) > 1 and not multi:
            print(f"section {args.section!r} is ambiguous in {path.stem}; choose one:")
            selected = []
    if not selected:
        idx = _choose(f"Section of {path.stem}", [f"{section.number}. {section.name}" for section in sections])
        if idx is None:
            return 1
        selected = [sections[idx]]

    if len(selected) > 1:
        print(f"=== {path.stem}: running sections {', '.join(str(section.number) for section in selected)} in order ===")
    for section in selected:
        print(f"\n=== {path.stem}  ->  {section.number}. {section.name} ===")
        rc = run_section(section, dry=args.dry)
        if rc != 0:
            print(f"[stopped: section {section.number} ({section.name}) exited {rc}]", file=sys.stderr)
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
