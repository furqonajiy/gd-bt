"""Windows file-lock resilience for tick-archive deletes (robust_remove).

A freshly-written tick part is routinely held open for a beat by Windows
Defender, the Search indexer, Explorer's preview pane, or a parallel run. A bare
``Path.unlink()`` then raises ``PermissionError`` (WinError 32) and aborts the
whole tick sync MID-REASSEMBLE -- leaving BOTH the joined window file
(``..._D28_ELEV8.csv``) AND its ``_pN`` parts on disk. Since the consumer glob
``XAUUSD_TICK_*_ELEV8.csv`` matches both and ``load_ticks`` does not dedup, that
window's ticks get DOUBLE-counted in the next backtest (the bug a user hit live).

``robust_remove`` waits the lock out with capped backoff instead of failing, so
the delete eventually succeeds and no half-reassembled duplicate state is left.
These tests pin: (1) the retry/idempotency/raise semantics of the helper, and
(2) the end-to-end invariant that a transient lock during ``join_parts`` leaves
NO orphan parts beside the joined file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.split_ticks_by_size import join_parts, robust_remove, split_file


def _sharing_violation(name: str = "part.csv") -> PermissionError:
    """A PermissionError shaped like a Windows sharing violation (WinError 32)."""
    err = PermissionError(f"{name} is in use by another process")
    err.winerror = 32
    return err


class _FakePath:
    """Path-like stub whose ``.unlink()`` raises ``fails`` sharing-violations
    (or a custom ``exc``) before succeeding. robust_remove only touches
    ``.unlink()`` and ``.name``, so this is enough to drive its retry logic."""

    def __init__(self, fails: int, name: str = "x_p1_ELEV8.csv",
                 exc: BaseException | None = None):
        self._fails = fails
        self.name = name
        self.calls = 0
        self._exc = exc

    def unlink(self) -> None:
        self.calls += 1
        if self.calls <= self._fails:
            raise self._exc if self._exc is not None else _sharing_violation(self.name)


def test_robust_remove_deletes_existing_file(tmp_path):
    p = tmp_path / "real.csv"
    p.write_text("data")
    robust_remove(p)
    assert not p.exists()


def test_robust_remove_idempotent_on_missing_file(tmp_path):
    # An already-gone file is a clean no-op (a prior retry may have removed it).
    p = tmp_path / "ghost.csv"
    robust_remove(p)
    assert not p.exists()


def test_robust_remove_retries_then_succeeds():
    fake = _FakePath(fails=2)
    slept: list[float] = []
    robust_remove(fake, retries=5, base_delay=0.01, _sleep=slept.append)
    assert fake.calls == 3          # 2 sharing violations + 1 success
    assert len(slept) == 2          # one wait before each retry


def test_robust_remove_backoff_is_capped_exponential():
    fake = _FakePath(fails=4)
    slept: list[float] = []
    robust_remove(fake, retries=10, base_delay=1.0, _sleep=slept.append)
    # 1, 2, 4, 8 -> capped at 5.0 thereafter; here only 4 waits happen.
    assert slept == [1.0, 2.0, 4.0, 5.0]


def test_robust_remove_reraises_after_exhaustion():
    fake = _FakePath(fails=99)
    with pytest.raises(PermissionError):
        robust_remove(fake, retries=3, base_delay=0.0, _sleep=lambda *_: None)
    assert fake.calls == 4          # retries + 1 attempts, then give up


def test_robust_remove_does_not_retry_genuine_posix_permission_error():
    # A PermissionError WITHOUT a Windows sharing-violation code (e.g. POSIX
    # EACCES on a read-only dir) must raise immediately -- retrying can't help.
    fake = _FakePath(fails=99, exc=PermissionError("read-only filesystem"))
    slept: list[float] = []
    with pytest.raises(PermissionError):
        robust_remove(fake, retries=5, base_delay=0.0, _sleep=slept.append)
    assert fake.calls == 1
    assert slept == []


def test_join_parts_leaves_no_orphan_parts_after_transient_lock(tmp_path, monkeypatch):
    """The corruption scenario: a part is momentarily locked during the
    post-join cleanup. With robust_remove the unlink retries and ALL parts are
    removed, so the consumer glob matches only the joined file (no duplicates)."""
    base = tmp_path / "XAUUSD_TICK_202606_ELEV8.csv"
    base.write_text("<HDR>\n" + "".join(f"row{i}\n" for i in range(100)))
    parts = split_file(base, 60, remove_source=True)   # tiny cap -> many parts
    assert len(parts) >= 2
    assert not base.exists()

    real_unlink = Path.unlink
    fired = {"once": False}

    def flaky_unlink(self, *a, **k):
        # Fail exactly once, on the first part, as a Windows sharing violation.
        if not fired["once"] and self.name.endswith("_p1_ELEV8.csv"):
            fired["once"] = True
            raise _sharing_violation(self.name)
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    dest = join_parts(parts, base, remove_parts=True)

    assert fired["once"]                       # the transient lock really fired
    assert dest.exists()                       # joined window file is present
    orphans = sorted(tmp_path.glob("XAUUSD_TICK_202606_*p*_ELEV8.csv"))
    assert orphans == [], f"orphan parts left beside the joined file: {orphans}"
