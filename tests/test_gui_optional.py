from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from optimizer.analysis import run_analysis
from optimizer.gui import MainWindow
from optimizer.market_replay import run_market_replay_analysis
from optimizer.market_replay_models import MarketReplayConfig
from optimizer.market_replay_reports import write_market_replay_report
from optimizer.models import AnalysisConfig
from optimizer.presentation import RESULT_COLUMNS, ticker_report_path
from optimizer.reports import write_reports
from tests.market_replay_fixtures import make_ticks, write_v3


def test_completed_results_wire_tooltips_and_per_ticker_report_button(
    source_fixture: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    result = write_reports(
        run_analysis(AnalysisConfig(source_fixture, tmp_path / "reports"))
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    opened: list[Path] = []

    window = MainWindow(source_fixture, tmp_path / "reports")
    monkeypatch.setattr(window, "_open_ticker_report", opened.append)
    window._on_completed(result)

    assert window.table.rowCount() == 1
    assert all(
        window.table.horizontalHeaderItem(column).toolTip()
        for column in range(len(RESULT_COLUMNS))
    )
    assert all(
        window.table.item(0, column).toolTip()
        for column in range(len(RESULT_COLUMNS) - 1)
    )
    button = window.table.cellWidget(0, len(RESULT_COLUMNS) - 1)
    assert isinstance(button, QPushButton)
    assert button.toolTip()
    button.click()
    assert opened == [ticker_report_path(result.output_dir, "AAPL")]

    window.close()
    app.processEvents()


def test_market_replay_tab_preflights_v3_and_opens_its_separate_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    rows, periods = make_ticks()
    recording = write_v3(tmp_path / "recordings" / "AAPL.ibrec", rows, periods)
    result = write_market_replay_report(
        run_market_replay_analysis(
            MarketReplayConfig(recording, tmp_path / "market-replay-reports")
        )
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)

    window = MainWindow(tmp_path, tmp_path / "bot-reports", recording)
    assert window.tabs.count() == 2
    assert window.tabs.tabText(1) == "Market Replay (.ibrec v2/v3)"
    assert window._check_ibrec() is True
    preflight_text = window.ibrec_preflight_label.text()
    assert "v3 sqlite" in preflight_text.lower()
    assert "format(s) [3]" not in preflight_text

    opened: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        window,
        "_open_report",
        lambda path, description: opened.append((path, description)),
    )
    window._on_ibrec_completed(result)

    assert window.ibrec_table.rowCount() == 1
    assert all(
        window.ibrec_table.horizontalHeaderItem(column).toolTip()
        for column in range(window.ibrec_table.columnCount())
    )
    button = window.ibrec_table.cellWidget(0, window.ibrec_table.columnCount() - 1)
    assert isinstance(button, QPushButton)
    assert button.toolTip()
    button.click()
    assert opened == [(result.output_dir / "index.html", "Market Replay")]

    window.close()
    app.processEvents()
