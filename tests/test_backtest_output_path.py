from pathlib import Path

from xauusd_trading.strategy.backtest import _backtest_output_path


def test_legacy_reports_dir_writes_default_file():
    assert _backtest_output_path(Path("reports")) == Path("reports/backtest_results.xlsx")


def test_named_output_stem_writes_xlsx_directly_under_reports():
    assert _backtest_output_path(Path("reports/trailing_open_2_risk_0034")) == Path(
        "reports/trailing_open_2_risk_0034.xlsx"
    )


def test_explicit_xlsx_output_path_is_preserved():
    assert _backtest_output_path(Path("reports/custom.xlsx")) == Path("reports/custom.xlsx")


def test_named_output_scenario_appends_suffix_to_same_stem():
    assert _backtest_output_path(
        Path("reports/trailing_open_2_risk_0034"),
        filename="backtest_results_1500_2026-05-19.xlsx",
    ) == Path("reports/trailing_open_2_risk_0034_1500_2026-05-19.xlsx")


def test_dotted_run_name_is_sanitized_not_truncated():
    # A dotted run name used to be mangled by Path.with_suffix(): everything
    # after the last dot was treated as the extension, silently writing
    # reports/BEST_slm2.1_gap0.xlsx. The stem is now rendered dot-free.
    assert _backtest_output_path(
        Path("reports/BEST_slm2.1_gap0.5_tp1delay24_risk005_2025")
    ) == Path("reports/BEST_slm21_gap05_tp1delay24_risk005_2025.xlsx")


def test_explicit_xlsx_with_dotted_stem_is_sanitized():
    assert _backtest_output_path(Path("reports/run2.1.xlsx")) == Path("reports/run21.xlsx")


def test_scenario_suffix_with_dotted_capital_is_sanitized():
    assert _backtest_output_path(
        Path("reports/run_a"),
        filename="backtest_results_1500.5_2026-05-19.xlsx",
    ) == Path("reports/run_a_15005_2026-05-19.xlsx")
