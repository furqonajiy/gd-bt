from __future__ import annotations

from cli.run import _capital_variants


def test_backtest_command_expands_to_50k_and_5k_with_same_dates():
    cmd = (
        "python tools/backtest_hybrid.py "
        "--signals signals/t818.txt "
        "--output-dir reports/T818_202607 "
        "--initial-capital 50000 "
        "--start-date 2026-07-01 "
        "--end-date 2026-07-31"
    )

    variants = _capital_variants(cmd)

    assert len(variants) == 2
    assert variants[0] == cmd
    assert "--initial-capital 50000" in variants[0]
    assert "--output-dir reports/T818_202607" in variants[0]
    assert "--initial-capital 5000" in variants[1]
    assert "--output-dir reports/T818_202607_5k" in variants[1]
    assert "--start-date 2026-07-01" in variants[1]
    assert "--end-date 2026-07-31" in variants[1]


def test_non_backtest_or_non_50k_command_is_unchanged():
    cmd = "python tools/live_provider_signal_filter.py --input x --output y"
    assert _capital_variants(cmd) == [cmd]


def test_existing_5k_output_dir_is_not_double_suffixed():
    cmd = (
        "python tools/backtest_hybrid.py "
        "--output-dir reports/T818_202607_5k "
        "--initial-capital 50000 "
        "--start-date 2026-07-01"
    )

    variants = _capital_variants(cmd)

    assert len(variants) == 2
    assert variants[1].count("reports/T818_202607_5k") == 1
    assert "reports/T818_202607_5k_5k" not in variants[1]
