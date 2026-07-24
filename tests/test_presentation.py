from __future__ import annotations

from pathlib import Path

from optimizer.analysis import run_analysis
from optimizer.models import AnalysisConfig
from optimizer.presentation import RESULT_COLUMNS, result_cells, ticker_report_path
from optimizer.utils import ticker_folder_name


def test_result_table_cells_have_explanatory_tooltips(
    source_fixture: Path,
    tmp_path: Path,
) -> None:
    ticker = run_analysis(
        AnalysisConfig(source_fixture, tmp_path / "reports")
    ).tickers[0]
    cells = result_cells(ticker)

    assert len(cells) == len(RESULT_COLUMNS) == 8
    assert all(column.heading_tooltip for column in RESULT_COLUMNS)
    assert all(cell.tooltip for cell in cells)
    assert "matched" in cells[4].tooltip.lower()
    assert "not a return" in cells[2].tooltip.lower()
    assert cells[-1].text == "Open report"


def test_ticker_report_path_and_sanitized_names_are_stable_and_collision_resistant(
    tmp_path: Path,
) -> None:
    assert ticker_folder_name("AAPL") == "AAPL"
    unsafe = ticker_folder_name("A/B")
    safe_literal = ticker_folder_name("A_B")
    assert unsafe.startswith("A_B-")
    assert unsafe != safe_literal
    assert ticker_report_path(tmp_path, "A/B") == (
        tmp_path / unsafe / f"{unsafe}_coverage_and_replay.html"
    )
