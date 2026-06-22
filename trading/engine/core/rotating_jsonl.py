"""Size-bounded append for the live JSONL sinks (forensic + notifications).

The forensic log and the notifier are append-only and run inside every `auto`
/ `manage` cycle -- forensic emits a full engine_snapshot + mt5_snapshot per
tracked signal per cycle. Left uncapped, a long-running live executor grows
these files without bound and eventually fills the disk (the 2026-06 incident).

`append_jsonl_line` keeps each file under `max_bytes` by rotating to a single
`.1` backup when the next write would cross the cap, so on-disk usage for one
sink is bounded at ~2 x max_bytes (current + one backup) regardless of uptime.
The newest events always live in the active file; the previous window is the
`.1` backup. Best-effort: any failure is swallowed by the callers (observability
must never break trading), so this helper only does the rotation + append.
"""
from __future__ import annotations

from pathlib import Path

# Per-sink defaults. Forensic is the verbose one (per-cycle, per-signal), so it
# gets the larger cap; notifications are lighter (closures / locks only).
DEFAULT_FORENSIC_MAX_BYTES = 50 * 1024 * 1024        # 50 MB -> <=100 MB with backup
DEFAULT_NOTIFICATIONS_MAX_BYTES = 10 * 1024 * 1024   # 10 MB -> <=20 MB with backup


def append_jsonl_line(path: Path, line: str, max_bytes: int | None) -> None:
    """Append one already-newline-terminated `line` to `path`, rotating first.

    When `max_bytes` is a positive int and the existing file plus this line
    would exceed it, the current file is moved to `path` + ".1" (replacing any
    previous backup) and a fresh file is started. `max_bytes` None/<=0 disables
    rotation (unbounded, the legacy behaviour).
    """
    if max_bytes and max_bytes > 0:
        try:
            current = path.stat().st_size
        except OSError:
            current = 0
        if current and current + len(line.encode("utf-8")) > max_bytes:
            backup = path.with_name(path.name + ".1")
            path.replace(backup)  # atomic; drops the old .1 if present
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
