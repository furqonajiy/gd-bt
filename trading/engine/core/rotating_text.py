"""Time-windowed text log for the live ``auto`` console.

The live executor prints a timestamped event stream (``[YYYY-MM-DD HH:MM:SS]
Signal ...`` / ``EXECUTION: ...`` / ``RECONCILIATION: ...`` / heartbeat). When
the MT5 terminal or the ``auto`` process crashes, that stream is gone -- there is
no post-mortem unless it was tee'd to disk. ``RotatingTextLog`` is that sink: the
console is mirrored to a ``.txt`` file, and the file is kept to the **last N
hours** so it never grows without bound and a crash always leaves the recent
history on disk to analyze.

Design (crash-safe first):
  * **Append-only** on the hot path -- every ``write`` just appends bytes, so a
    crash loses at most the line being written.
  * **Time-based prune** runs at most once per ``prune_interval_seconds``: it
    reads the file, drops lines whose leading ``[timestamp]`` is older than
    ``now - retain_hours``, and swaps the result in via an **atomic**
    temp-file + ``os.replace`` -- a crash mid-prune leaves the original intact.
  * A line without a parseable leading stamp (a blank line, a wrapped
    continuation, a banner) **inherits the previous line's timestamp**, so
    multi-line blocks are kept or dropped together.
  * **Best-effort**: every failure is swallowed. Observability must never break
    trading, so a bad path / full disk / permission error only skips logging.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

# The prefix _stamped() writes: "[2026-07-01 15:17:35] ...". Parsed to decide a
# line's age; anything else carries the previous line's timestamp forward.
_STAMP_LEN = len("[YYYY-MM-DD HH:MM:SS]")
_STAMP_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_stamp(line: str) -> datetime | None:
    """Return the datetime in a leading ``[YYYY-MM-DD HH:MM:SS]`` stamp, else None."""
    if len(line) >= _STAMP_LEN and line[0] == "[" and line[_STAMP_LEN - 1] == "]":
        try:
            return datetime.strptime(line[1:_STAMP_LEN - 1], _STAMP_FMT)
        except ValueError:
            return None
    return None


class RotatingTextLog:
    """A tee target for ``sys.stdout`` that keeps the last ``retain_hours`` on disk.

    ``write`` accepts arbitrary chunks (as ``sys.stdout.write`` does); only whole
    lines are pruned. ``retain_hours <= 0`` disables pruning (unbounded)."""

    def __init__(self, path: str | Path, retain_hours: float = 24.0,
                 prune_interval_seconds: float = 300.0) -> None:
        self.path = Path(path)
        self.retain_hours = float(retain_hours or 0.0)
        self.prune_interval = float(prune_interval_seconds or 0.0)
        self._last_prune = 0.0
        try:
            if self.path.parent and not self.path.parent.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # -- hot path -------------------------------------------------------------
    def write(self, data: str) -> int:
        """Append ``data`` verbatim (best-effort) and prune on a slow cadence."""
        if not data:
            return 0
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(data)
        except OSError:
            return len(data)
        self._maybe_prune()
        return len(data)

    def flush(self) -> None:  # stdout-tee protocol; append() already flushes on close
        return None

    # -- retention ------------------------------------------------------------
    def _maybe_prune(self) -> None:
        if self.retain_hours <= 0 or self.prune_interval <= 0:
            return
        now = time.monotonic()
        if self._last_prune and (now - self._last_prune) < self.prune_interval:
            return
        self._last_prune = now
        self.prune()

    def prune(self, *, _now: datetime | None = None) -> None:
        """Rewrite the file keeping only lines within the last ``retain_hours``.

        Atomic (temp + ``os.replace``) and best-effort. ``_now`` is injectable for
        tests. A line with no parseable stamp inherits the previous line's time."""
        if self.retain_hours <= 0:
            return
        cutoff = (_now or datetime.now()) - timedelta(hours=self.retain_hours)
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return
        kept: list[str] = []
        carried: datetime | None = None
        for line in lines:
            stamp = _parse_stamp(line)
            if stamp is not None:
                carried = stamp
            # Keep if within window, or if we have no timestamp context yet
            # (never drop head lines we can't date -- conservative).
            if carried is None or carried >= cutoff:
                kept.append(line)
        if len(kept) == len(lines):
            return  # nothing aged out; skip the rewrite
        tmp = self.path.with_name(self.path.name + ".prune.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                f.writelines(kept)
            os.replace(tmp, self.path)  # atomic swap
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
