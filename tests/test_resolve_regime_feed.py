from __future__ import annotations

from pathlib import Path

import tools.resolve_regime_feed as rf


def test_scalper_variant_command_carries_exact_flags(tmp_path: Path):
    out = tmp_path / "feed.txt"
    cmd = rf.scalper_variant_command(
        "rsi70_sqz6_rr08",
        ["data/XAUUSD_M1_2025_ELEV8.csv"],
        out,
        "R3strong",
    )
    assert cmd is not None
    assert cmd[:2] == ["--charts", "data/XAUUSD_M1_2025_ELEV8.csv"]
    assert cmd[cmd.index("--output") + 1] == str(out)
    assert cmd[cmd.index("--start") + 1] == "2025-01-01"
    assert cmd[cmd.index("--rsi-buy-max") + 1] == "70"
    assert cmd[cmd.index("--rsi-sell-min") + 1] == "30"
    assert cmd[cmd.index("--bb-bandwidth-min") + 1] == "0.0006"
    assert cmd[cmd.index("--rr1") + 1] == "0.8"
    assert cmd[cmd.index("--rr2") + 1] == "1.5"
    assert cmd[cmd.index("--rr3") + 1] == "3.0"


def test_non_variant_feed_has_no_scalper_command(tmp_path: Path):
    assert rf.scalper_variant_command(
        "breakout", ["chart.csv"], tmp_path / "feed.txt", "R3strong") is None


def test_materialize_slices_existing_archive(tmp_path: Path, monkeypatch):
    source = tmp_path / "adaptive_breakout.txt"
    source.write_text(
        "2024-12-31 GMT+7\n"
        "1. BUY XAUUSD 1 - 2 SL 0 TP1 3 TP2 4 TP3 5\n\n"
        "2025-01-01 GMT+7\n"
        "1. BUY XAUUSD 1 - 2 SL 0 TP1 3 TP2 4 TP3 5\n\n"
        "2025-12-31 GMT+7\n"
        "1. SELL XAUUSD 2 - 1 SL 3 TP1 0 TP2 -1 TP3 -2\n\n"
        "2026-01-01 GMT+7\n"
        "1. SELL XAUUSD 2 - 1 SL 3 TP1 0 TP2 -1 TP3 -2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rf, "feed_signals", lambda feed: str(source))

    out = tmp_path / "out.txt"
    rf.materialize_regime_feed("breakout", "R3strong", out, ["chart.csv"])
    text = out.read_text(encoding="utf-8")
    assert "2024-12-31" not in text
    assert "2025-01-01" in text
    assert "2025-12-31" in text
    assert "2026-01-01" not in text
