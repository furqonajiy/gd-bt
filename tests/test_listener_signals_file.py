"""The listener's output feed is selectable via --signals-file.

Default stays signals.txt (backward compatible); a relative override resolves
against the repo root, and an absolute path is used as-is. This is what lets the
live pipeline point the listener at victor_signals.txt.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "listeners" / "telegram"))
import listener as tl  # noqa: E402


def test_signals_file_defaults_to_signals_txt():
    args = tl._build_parser().parse_args([])
    assert args.signals_file == "signals.txt"


def test_signals_file_override_parsed():
    args = tl._build_parser().parse_args(["--signals-file", "victor_signals.txt"])
    assert args.signals_file == "victor_signals.txt"


def test_resolve_relative_path_is_under_repo_root():
    assert tl._resolve_signals_path("victor_signals.txt") == tl.REPO_ROOT / "victor_signals.txt"


def test_resolve_absolute_path_is_unchanged(tmp_path):
    target = tmp_path / "feed.txt"
    assert tl._resolve_signals_path(str(target)) == target
