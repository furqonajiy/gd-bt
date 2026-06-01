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
