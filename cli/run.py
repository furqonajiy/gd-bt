#!/usr/bin/env python3
"""Clickable launcher for the cli/*.txt deployment snapshots.

The cli/*.txt files are the canonical, byte-identical deployment commands, but
they are multi-line PowerShell blocks you have to copy-paste. This runs them
instead: pick a strategy + a numbered section (the same 1-9 map as the README),
and the command runs **in the current terminal** (foreground, inheriting this
terminal's stdio) -- so you click into one of your terminals, launch the section
you want there (e.g. the listener in one, the auto executor in another), and it
streams + Ctrl+C exactly as if you'd typed it.

Usage (run in the terminal you want the process to live in):

    python cli/run.py                  # menu: pick a strategy, then a section
    python cli/run.py sqz6             # menu: sections of the SQZ6 champion
    python cli/run.py sqz6 3           # run section 3 (live auto executor) here
    python cli/run.py victor listener  # match a section by name too
    python cli/run.py v116 backtest    # run sections 4..end (signal-gen + all backtests)
    python cli/run.py c160 5-9         # run a range of sections, in order
    python cli/run.py sqz6 3 --print   # show the exact command, don't run it

Section selectors: a number (3), a name substring (listener), a range (5-9 / 4-),
or a keyword -- `backtest`/`bt` (sections >= 4: signal-gen + every era backtest),
`live` (1-3), `all`. Multi-section selectors run their sections in order and stop
at the first one that fails.

Aliases: victor/vic, sqz6, e640, rr08, sl19, c160, v116, resync, ticks (or any
unique part of a filename). The reconstructed command is byte-identical to the
snapshot (PowerShell ` continuations are joined into one line), so this never
diverges from cli/*.txt.
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
BANNER_RE = re.compile(r"^#\s*=+\s*$")          # the # ====...==== rule lines
NUM_NAME_RE = re.compile(r"^(\d+)\.\s*(.*)$")   # "3. LIVE AUTO EXECUTOR"

# Friendly aliases -> filename-stem substring.
ALIASES = {
    "victor": "champion_victor", "vic": "champion_victor",
    "sqz6": "champion_R4_SQZ6", "e640": "E640",
    "rr08": "rr08x15x30", "resync": "resync_m1_from_2020",
    "resync-ticks": "resync_ticks", "ticks": "resync_ticks",
    "sl19": "candidate_R4_SL19", "c160": "candidate_R4_C160",
    "v116": "candidate_VIC_C116", "vic116": "candidate_VIC_C116",
    "tr40": "trailing_open_R4", "tr30": "trailing_open_R3",
    "tr20": "trailing_open_R2", "tr10": "trailing_open_R1",
    "ts01": "trailing_small_0101",
}


@dataclass
class Section:
    number: int          # the 1-9 map number (0 = SETUP preamble)
    name: str
    commands: list[str] = field(default_factory=list)  # one runnable line each


def _join_command(lines: list[str]) -> str:
    """Join a PowerShell command block (backtick line continuations) into one
    runnable line, byte-for-byte equivalent to the snapshot."""
    parts: list[str] = []
    for ln in lines:
        s = ln.rstrip()
        if s.endswith("`"):              # strip the PowerShell continuation
            s = s[:-1].rstrip()
        parts.append(s.strip())
    return " ".join(p for p in parts if p)


def _command_runs(body: list[str]) -> list[str]:
    """Split a section body into runnable commands. A command is a run of lines
    where every line but the last ends with a PowerShell ` continuation; a line
    that does NOT end with ` terminates the command. So consecutive standalone
    lines (e.g. cd / git) become separate commands, while a backtick-continued
    `python ... ` block becomes one."""
    cmds: list[str] = []
    buf: list[str] = []
    for ln in body:
        s = ln.strip()
        if not s or s.startswith("#"):
            if buf:
                cmds.append(_join_command(buf))
                buf = []
            continue
        buf.append(ln)
        if not ln.rstrip().endswith("`"):
            cmds.append(_join_command(buf))
            buf = []
    if buf:
        cmds.append(_join_command(buf))
    return cmds


def parse_sections(path: Path) -> list[Section]:
    """Parse a cli snapshot into its runnable '# ===== N. NAME =====' sections.

    The leading block (cd / conda activate / git pull) is NOT exposed: those
    change shell state that a subprocess can't pass back to your terminal, so you
    run them yourself once. If a snapshot has no '=====' sections at all, the
    leading commands ARE the content and become the sections.
    """
    leading: list[str] = []
    raw_sections: list[tuple[str, list[str]]] = []
    cur: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if BANNER_RE.match(line):                 # decorative rule, not a section
            (cur if cur is not None else leading).append(line)
            continue
        m = SECTION_RE.match(line)
        if m:
            cur = []
            raw_sections.append((m.group(1).strip(), cur))
        else:
            (cur if cur is not None else leading).append(line)

    out: list[Section] = []
    if not raw_sections:                           # whole file is one command block
        for i, cmd in enumerate(_command_runs(leading), start=1):
            out.append(Section(i, f"command {i}", [cmd]))
        return out
    for idx, (title, body) in enumerate(raw_sections, start=1):
        nm = NUM_NAME_RE.match(title)
        number = int(nm.group(1)) if nm else idx
        name = nm.group(2).strip() if nm else title
        cmds = _command_runs(body)
        if cmds:                                   # skip note-only sections (e.g. N/A)
            out.append(Section(number, name, cmds))
    return out


def discover() -> list[Path]:
    return sorted(p for p in CLI_DIR.glob("*.txt"))


def resolve_strategy(token: str, files: list[Path]) -> Path | None:
    key = ALIASES.get(token.lower(), token).lower()
    hits = [p for p in files if key in p.stem.lower()]
    if len(hits) == 1:
        return hits[0]
    # exact-stem fallback
    exact = [p for p in files if p.stem.lower() == token.lower()]
    return exact[0] if len(exact) == 1 else None


def resolve_sections(token: str, sections: list[Section]) -> tuple[list[Section], bool]:
    """Resolve a section selector to an ORDERED list of sections + whether multiple
    are intentional. Accepts a keyword (backtest/bt -> number>=4; live -> 1-3; all),
    a range (N-M or N-), a number, or a name substring. The bool is True when the
    selector deliberately picks many (keyword/range) so the caller runs them all;
    False for number/name (a name matching >1 is ambiguous, not a batch)."""
    t = token.lower().strip()
    if t in ("backtest", "backtests", "bt"):
        return [s for s in sections if s.number >= 4], True
    if t == "live":
        return [s for s in sections if 1 <= s.number <= 3], True
    if t == "all":
        return list(sections), True
    m = re.fullmatch(r"(\d+)-(\d*)", t)
    if m:
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else max((s.number for s in sections), default=lo)
        return [s for s in sections if lo <= s.number <= hi], True
    if t.isdigit():
        return [s for s in sections if s.number == int(t)], False
    hits = [s for s in sections if t in s.name.lower()]
    return hits, False


def _choose(prompt: str, labels: list[str]) -> int | None:
    for i, lab in enumerate(labels, start=1):
        print(f"  [{i}] {lab}")
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


def run_section(section: Section, *, dry: bool) -> int:
    for cmd in section.commands:
        print(f"\n$ {cmd}\n" if not dry else cmd)
        if dry:
            continue
        rc = subprocess.run(cmd, shell=True).returncode  # current terminal, foreground
        if rc != 0:
            return rc
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run a cli/*.txt section in the current terminal.")
    p.add_argument("strategy", nargs="?", help="victor | sqz6 | e640 | rr08 | resync (or part of a filename)")
    p.add_argument("section", nargs="?",
                   help="section number (1-9), name substring, range (5-9 / 4-), or a "
                        "keyword: backtest/bt (>=4), live (1-3), all")
    p.add_argument("--print", "--dry-run", dest="dry", action="store_true",
                   help="show the exact command instead of running it")
    args = p.parse_args(argv)

    files = discover()
    if not files:
        print("no cli/*.txt snapshots found", file=sys.stderr)
        return 1

    # --- pick strategy ---
    path: Path | None = None
    if args.strategy:
        path = resolve_strategy(args.strategy, files)
        if path is None:
            print(f"no single match for strategy {args.strategy!r}; choose one:")
    if path is None:
        idx = _choose("Strategy", [p.stem for p in files])
        if idx is None:
            return 1
        path = files[idx]

    sections = parse_sections(path)
    if not sections:
        print(f"{path.name} has no runnable sections", file=sys.stderr)
        return 1

    # --- pick section(s) ---
    selected: list[Section] = []
    if args.section:
        sel, multi = resolve_sections(args.section, sections)
        if not sel:
            print(f"no match for section {args.section!r} in {path.stem}; choose one:")
        elif len(sel) > 1 and not multi:
            print(f"section {args.section!r} is ambiguous in {path.stem}; choose one:")
        else:
            selected = sel
    if not selected:
        labels = [f"{s.number}. {s.name}" for s in sections]
        idx = _choose(f"Section of {path.stem}", labels)
        if idx is None:
            return 1
        selected = [sections[idx]]

    if len(selected) > 1:
        print(f"=== {path.stem}: running sections "
              f"{', '.join(str(s.number) for s in selected)} in order ===")
    for s in selected:
        print(f"\n=== {path.stem}  ->  {s.number}. {s.name} ===")
        rc = run_section(s, dry=args.dry)
        if rc != 0:
            print(f"[stopped: section {s.number} ({s.name}) exited {rc}]", file=sys.stderr)
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
