"""PySide6 desktop interface for both offline optimization workflows."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .analysis import run_analysis
from .ibrec import IbrecError, inspect_ibrec_set
from .market_replay import run_market_replay_analysis
from .market_replay_models import MarketReplayAnalysisResult, MarketReplayConfig
from .market_replay_reports import write_market_replay_report
from .models import AnalysisConfig, AnalysisResult
from .presentation import (
    RESULT_COLUMNS,
    market_replay_format_label,
    result_cells,
    ticker_report_path,
)
from .reports import write_reports
from .safety import (
    SourceSafetyError,
    source_paths,
    validate_database_source,
    validate_source,
)
from .version import APP_NAME, APP_VERSION


class AnalysisWorker(QObject):
    progress = Signal(str, int, int)
    completed = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, config: AnalysisConfig):
        super().__init__()
        self.config = config

    @Slot()
    def run(self) -> None:
        try:
            result = write_reports(run_analysis(self.config, progress=self.progress.emit))
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.finished.emit()


class MarketReplayWorker(QObject):
    progress = Signal(str, int, int)
    completed = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, config: MarketReplayConfig):
        super().__init__()
        self.config = config

    @Slot()
    def run(self) -> None:
        try:
            result = write_market_replay_report(
                run_market_replay_analysis(self.config, progress=self.progress.emit)
            )
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.finished.emit()


class ReplayPreflightWorker(QObject):
    """Run the structural .ibrec preflight off the interface thread.

    ``inspect_ibrec_set`` copies every selected recording and verifies its
    content hashes, which can take many seconds for multi-gigabyte inputs.
    Before version 1.9.3 this ran synchronously in the check handler and
    froze the window.  The exact fail-closed checks are unchanged, and the
    analysis worker still re-copies and re-verifies everything itself.
    """

    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, config: MarketReplayConfig):
        super().__init__()
        self.config = config

    @Slot()
    def run(self) -> None:
        try:
            details = inspect_ibrec_set(self.config)
            if self.config.calibration_source_dir is not None:
                validate_database_source(
                    source_paths(self.config.calibration_source_dir)
                )
            self.succeeded.emit(details)
        except (IbrecError, OSError, SourceSafetyError, ValueError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            # A background exception must never leave the interface stuck in
            # its amber "Checking…" state. Unexpected failures remain
            # fail-closed and include their type for diagnostics, matching the
            # full analysis workers.
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.finished.emit()


_REPLAY_COLUMNS = (
    ("Ticker", "Contract symbol stored in the Market Replay recording."),
    ("Format", "Accepted .ibrec format version(s) and number of input files."),
    ("ATR period", "Number of true-range observations in the suggested complete profile."),
    ("Bar seconds", "Monotonic-clock OHLC bar duration used for ATR."),
    ("Drop ×", "Initial-drop ATR multiplier in the suggested profile."),
    ("BUY ×", "BUY-rebound ATR multiplier in the suggested profile."),
    ("Profit ×", "Minimum-profit ATR multiplier in the suggested profile."),
    ("SELL ×", "SELL-trail ATR multiplier in the suggested profile."),
    ("Score", "Bounded-grid screening score. It is not expected profit or a forecast."),
    (
        "Stable",
        "Whether a changed profile passed source quality, stable-region, paired-day, bootstrap, leave-one-day-out, tail-risk, and ATR-phase checks.",
    ),
    ("Report", "Open the detailed deterministic HTML report."),
)


class MainWindow(QMainWindow):
    def __init__(
        self,
        source_dir: Path,
        output_dir: Path,
        ibrec_path: Path | tuple[Path, ...] | None = None,
    ):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1120, 780)
        self._thread: QThread | None = None
        self._worker: QObject | None = None
        self._preflight_thread: QThread | None = None
        self._preflight_worker: ReplayPreflightWorker | None = None
        self._preflight_pending = False
        self._active_mode: str | None = None
        self._last_result: AnalysisResult | None = None
        self._last_market_result: MarketReplayAnalysisResult | None = None
        self._busy = False

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        title = QLabel(
            f"<h2>{APP_NAME}</h2>"
            "<p>Use BouncyBot's saved trades, or independently optimize one or more Market Replay Lab recordings for one instrument.</p>"
        )
        title.setWordWrap(True)
        layout.addWidget(title)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._build_bot_tab(source_dir, output_dir)
        self._build_replay_tab(ibrec_path, output_dir)
        self._check_source()
        if ibrec_path is not None:
            self.tabs.setCurrentWidget(self.replay_tab)
            self._check_ibrec()

    def _build_bot_tab(self, source_dir: Path, output_dir: Path) -> None:
        self.bot_tab = QWidget()
        self.tabs.addTab(self.bot_tab, "BouncyBot SQLite & captures")
        layout = QVBoxLayout(self.bot_tab)
        safety = QLabel(
            "This workflow runs only when ibkr_trading_bot.lock is absent. After confirmation it temporarily acquires "
            "that lock, copies SQLite/WAL through read-only file access into a private snapshot, and never writes to "
            "bot_state.sqlite or debug_captures."
        )
        safety.setWordWrap(True)
        safety.setStyleSheet("padding: 10px; background: #fff4d6; border: 1px solid #d79b20;")
        layout.addWidget(safety)

        form = QFormLayout()
        source_row = QHBoxLayout()
        self.source_edit = QLineEdit(str(source_dir.resolve()))
        self.source_button = QPushButton("Browse…")
        self.source_button.clicked.connect(self._browse_source)
        source_row.addWidget(self.source_edit)
        source_row.addWidget(self.source_button)
        form.addRow("Bot data folder", source_row)

        output_row = QHBoxLayout()
        self.output_edit = QLineEdit(str(output_dir.resolve()))
        self.output_button = QPushButton("Browse…")
        self.output_button.clicked.connect(self._browse_output)
        output_row.addWidget(self.output_edit)
        output_row.addWidget(self.output_button)
        form.addRow("Report output root", output_row)

        layout.addLayout(form)

        status_row = QHBoxLayout()
        self.preflight_label = QLabel("Not checked")
        self.check_button = QPushButton("Check source")
        self.check_button.clicked.connect(self._check_source)
        self.analyze_button = QPushButton("Analyze read-only data")
        self.analyze_button.clicked.connect(self._start_analysis)
        self.open_button = QPushButton("Open latest report")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_latest)
        for widget in (self.check_button, self.analyze_button, self.open_button):
            status_row.addWidget(widget)
        status_row.addStretch(1)
        status_row.addWidget(self.preflight_label)
        layout.addLayout(status_row)

        self.progress_label = QLabel("Ready")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.progress_bar)

        self.table = QTableWidget(0, len(RESULT_COLUMNS))
        self.table.setHorizontalHeaderLabels([column.heading for column in RESULT_COLUMNS])
        for index, column in enumerate(RESULT_COLUMNS):
            item = self.table.horizontalHeaderItem(index)
            if item is not None:
                item.setToolTip(column.heading_tooltip)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

    def _build_replay_tab(
        self,
        ibrec_path: Path | tuple[Path, ...] | None,
        output_dir: Path,
    ) -> None:
        self.replay_tab = QWidget()
        self.tabs.addTab(self.replay_tab, "Market Replay (.ibrec v2/v3)")
        layout = QVBoxLayout(self.replay_tab)
        explanation = QLabel(
            "This separate workflow reads one or more Market Replay Lab .ibrec format-v2 ZIP or format-v3 SQLite recordings for the same instrument. "
            "By default it carries open positions and active SELL trails across provably consecutive complete RTH recordings. "
            "Optionally, a stopped BouncyBot data folder can calibrate the assumed notional and conservative execution-cost reserve from actual executions. "
            "It never connects to IBKR or writes to the BouncyBot database."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet("padding: 10px; background: #eef6ff; border: 1px solid #2c6fb7;")
        layout.addWidget(explanation)

        form = QFormLayout()
        input_container = QWidget()
        input_row = QHBoxLayout(input_container)
        self.ibrec_list = QListWidget()
        self.ibrec_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        initial_paths = (
            ()
            if ibrec_path is None
            else ((ibrec_path,) if isinstance(ibrec_path, Path) else tuple(ibrec_path))
        )
        for path in initial_paths:
            self.ibrec_list.addItem(str(path.expanduser().absolute()))
        button_column = QVBoxLayout()
        self.ibrec_button = QPushButton("Add recordings…")
        self.ibrec_button.clicked.connect(self._browse_ibrec)
        self.ibrec_remove_button = QPushButton("Remove selected")
        self.ibrec_remove_button.clicked.connect(self._remove_selected_ibrec)
        self.ibrec_clear_button = QPushButton("Clear")
        self.ibrec_clear_button.clicked.connect(self._clear_ibrec)
        for button in (
            self.ibrec_button,
            self.ibrec_remove_button,
            self.ibrec_clear_button,
        ):
            button_column.addWidget(button)
        button_column.addStretch(1)
        input_row.addWidget(self.ibrec_list, 1)
        input_row.addLayout(button_column)
        form.addRow("Market Replay recordings", input_container)

        output_row = QHBoxLayout()
        self.replay_output_edit = QLineEdit(str(output_dir.resolve()))
        self.replay_output_button = QPushButton("Browse…")
        self.replay_output_button.clicked.connect(self._browse_replay_output)
        output_row.addWidget(self.replay_output_edit)
        output_row.addWidget(self.replay_output_button)
        form.addRow("Report output root", output_row)

        calibration_row = QHBoxLayout()
        self.calibration_source_edit = QLineEdit("")
        self.calibration_source_edit.setPlaceholderText(
            "Optional folder containing bot_state.sqlite"
        )
        self.calibration_source_button = QPushButton("Browse…")
        self.calibration_source_button.clicked.connect(self._browse_calibration_source)
        self.calibration_source_clear_button = QPushButton("Clear")
        self.calibration_source_clear_button.clicked.connect(
            self._clear_calibration_source
        )
        calibration_row.addWidget(self.calibration_source_edit)
        calibration_row.addWidget(self.calibration_source_button)
        calibration_row.addWidget(self.calibration_source_clear_button)
        form.addRow("Optional execution calibration", calibration_row)

        self.overnight_replay_checkbox = QCheckBox(
            "Carry open long positions and active SELL trails across consecutive RTH recordings"
        )
        self.overnight_replay_checkbox.setChecked(True)
        self.overnight_replay_checkbox.setToolTip(
            "Continuity is used only when adjacent sessions are complete, primary-eligible, and the next weekday recording is present. Gaps fail closed."
        )
        form.addRow("Overnight replay", self.overnight_replay_checkbox)

        self.ibrec_notional_spin = QDoubleSpinBox()
        self.ibrec_notional_spin.setRange(100.0, 100_000_000.0)
        self.ibrec_notional_spin.setDecimals(2)
        self.ibrec_notional_spin.setValue(10_000.0)
        self.ibrec_notional_spin.setToolTip(
            "Fixed notional in the recording instrument's currency. It is used only "
            "to estimate whole-share quantity and whether recorded top-of-book size was "
            "sufficient; it is not an account-sizing or full partial-fill model."
        )
        form.addRow(
            "Assumed trade notional (instrument currency)",
            self.ibrec_notional_spin,
        )

        self.ibrec_cost_spin = QDoubleSpinBox()
        self.ibrec_cost_spin.setRange(0.0, 100.0)
        self.ibrec_cost_spin.setDecimals(4)
        self.ibrec_cost_spin.setValue(1.0)
        self.ibrec_cost_spin.setSuffix(" bps / side")
        self.ibrec_cost_spin.setToolTip(
            "Fixed conservative execution-cost reserve charged on every modeled BUY and SELL side."
        )
        form.addRow("Execution-cost reserve", self.ibrec_cost_spin)

        self.ibrec_turnover_spin = QDoubleSpinBox()
        self.ibrec_turnover_spin.setRange(0.0, 100.0)
        self.ibrec_turnover_spin.setDecimals(4)
        self.ibrec_turnover_spin.setValue(0.25)
        self.ibrec_turnover_spin.setSuffix(" score bps / trade")
        self.ibrec_turnover_spin.setToolTip(
            "Additional screening-score penalty per completed cycle to discourage turnover-driven zero-trail profiles."
        )
        form.addRow("Turnover penalty", self.ibrec_turnover_spin)

        self.ibrec_workers_spin = QSpinBox()
        self.ibrec_workers_spin.setRange(0, 64)
        self.ibrec_workers_spin.setValue(0)
        self.ibrec_workers_spin.setSpecialValueText("Automatic")
        self.ibrec_workers_spin.setToolTip(
            "Worker processes used for independent Stage 3 profiles and outward boundary probes. "
            "Automatic uses up to eight logical processors while leaving one processor available; "
            "set 1 to disable multiprocessing. Worker count does not change calculations or reports."
        )
        form.addRow("Profile-evaluation workers", self.ibrec_workers_spin)
        layout.addLayout(form)

        status_row = QHBoxLayout()
        self.ibrec_preflight_label = QLabel("Not checked")
        self.ibrec_check_button = QPushButton("Check recordings")
        self.ibrec_check_button.clicked.connect(self._check_ibrec)
        self.ibrec_analyze_button = QPushButton("Analyze recordings")
        self.ibrec_analyze_button.clicked.connect(self._start_ibrec_analysis)
        self.ibrec_open_button = QPushButton("Open latest report")
        self.ibrec_open_button.setEnabled(False)
        self.ibrec_open_button.clicked.connect(self._open_latest_ibrec)
        for widget in (
            self.ibrec_check_button,
            self.ibrec_analyze_button,
            self.ibrec_open_button,
        ):
            status_row.addWidget(widget)
        status_row.addStretch(1)
        status_row.addWidget(self.ibrec_preflight_label)
        layout.addLayout(status_row)

        self.ibrec_progress_label = QLabel("Ready")
        self.ibrec_progress_bar = QProgressBar()
        self.ibrec_progress_bar.setRange(0, 1)
        self.ibrec_progress_bar.setValue(0)
        layout.addWidget(self.ibrec_progress_label)
        layout.addWidget(self.ibrec_progress_bar)

        self.ibrec_table = QTableWidget(0, len(_REPLAY_COLUMNS))
        self.ibrec_table.setHorizontalHeaderLabels([item[0] for item in _REPLAY_COLUMNS])
        for index, (_, tooltip) in enumerate(_REPLAY_COLUMNS):
            item = self.ibrec_table.horizontalHeaderItem(index)
            if item is not None:
                item.setToolTip(tooltip)
        self.ibrec_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.ibrec_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.ibrec_table, 1)
        self.ibrec_analyze_button.setEnabled(False)

    @Slot()
    def _browse_source(self) -> None:
        if self._busy:
            return
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Select BouncyBot data folder",
            self.source_edit.text(),
        )
        if chosen:
            self.source_edit.setText(chosen)
            self.output_edit.setText(str(Path(chosen) / "optimizer_reports"))
            self._check_source()

    @Slot()
    def _browse_output(self) -> None:
        if self._busy:
            return
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Select report output root",
            self.output_edit.text(),
        )
        if chosen:
            self.output_edit.setText(chosen)

    @Slot()
    def _browse_ibrec(self) -> None:
        if self._busy:
            return
        current = self._selected_ibrec_paths()
        chosen, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Market Replay recordings",
            str(current[0].parent) if current else "",
            "Market Replay recordings (*.ibrec);;All files (*)",
        )
        if chosen:
            existing = {str(path).casefold() for path in current}
            for value in chosen:
                path = Path(value).expanduser().absolute()
                if str(path).casefold() not in existing:
                    self.ibrec_list.addItem(str(path))
                    existing.add(str(path).casefold())
            self._check_ibrec()

    def _selected_ibrec_paths(self) -> tuple[Path, ...]:
        return tuple(
            Path(self.ibrec_list.item(index).text()).expanduser().absolute()
            for index in range(self.ibrec_list.count())
        )

    @Slot()
    def _remove_selected_ibrec(self) -> None:
        if self._busy:
            return
        for item in list(self.ibrec_list.selectedItems()):
            self.ibrec_list.takeItem(self.ibrec_list.row(item))
        self._check_ibrec()

    @Slot()
    def _clear_ibrec(self) -> None:
        if self._busy:
            return
        self.ibrec_list.clear()
        self._check_ibrec()

    @Slot()
    def _browse_replay_output(self) -> None:
        if self._busy:
            return
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Select Market Replay report output root",
            self.replay_output_edit.text(),
        )
        if chosen:
            self.replay_output_edit.setText(chosen)

    @Slot()
    def _browse_calibration_source(self) -> None:
        if self._busy:
            return
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Select stopped BouncyBot data folder for execution calibration",
            self.calibration_source_edit.text(),
        )
        if chosen:
            self.calibration_source_edit.setText(chosen)
            self._check_ibrec()

    @Slot()
    def _clear_calibration_source(self) -> None:
        if self._busy:
            return
        self.calibration_source_edit.clear()
        self._check_ibrec()

    @Slot()
    def _check_source(self) -> bool:
        try:
            paths = source_paths(Path(self.source_edit.text()))
            warnings = validate_source(paths)
        except SourceSafetyError as exc:
            self.preflight_label.setText(f"Blocked: {exc}")
            self.preflight_label.setStyleSheet("color: #a00000;")
            self.analyze_button.setEnabled(False)
            return False
        message = "Ready: database found; bot lock absent"
        if warnings:
            message += f"; {len(warnings)} warning(s)"
        self.preflight_label.setText(message)
        self.preflight_label.setStyleSheet("color: #176b36;")
        self.analyze_button.setEnabled(not self._busy)
        return True

    @Slot()
    def _check_ibrec(self) -> None:
        paths = self._selected_ibrec_paths()
        if not paths:
            # When an older check is still running, remember that its result is
            # stale. The completion handler will ignore it and re-evaluate the
            # now-empty selection before enabling Analyze.
            self._preflight_pending = self._preflight_thread is not None
            self.ibrec_preflight_label.setText("Select one or more .ibrec v2/v3 recordings")
            self.ibrec_preflight_label.setStyleSheet("color: #a00000;")
            self.ibrec_analyze_button.setEnabled(False)
            return
        if self._preflight_thread is not None:
            # A structural check is already hashing recordings on its worker
            # thread.  Remember that the selection changed and run one fresh
            # check when it finishes instead of stacking threads that would
            # re-copy the same multi-gigabyte inputs.
            self._preflight_pending = True
            return
        config = self._market_replay_config(paths)
        self.ibrec_preflight_label.setText(
            f"Checking {len(paths)} recording(s): copying and verifying content…"
        )
        self.ibrec_preflight_label.setStyleSheet("color: #9a5a00;")
        self.ibrec_analyze_button.setEnabled(False)
        self._start_preflight_worker(ReplayPreflightWorker(config))

    def _start_preflight_worker(self, worker: ReplayPreflightWorker) -> None:
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)  # type: ignore[attr-defined]
        worker.succeeded.connect(self._on_preflight_succeeded)  # type: ignore[attr-defined]
        worker.failed.connect(self._on_preflight_failed)  # type: ignore[attr-defined]
        worker.finished.connect(thread.quit)  # type: ignore[attr-defined]
        worker.finished.connect(worker.deleteLater)  # type: ignore[attr-defined]
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._preflight_thread_finished)
        self._preflight_thread = thread
        self._preflight_worker = worker
        thread.start()

    @Slot(object)
    def _on_preflight_succeeded(self, details: Any) -> None:
        if self._preflight_pending:
            # The recording set or calibration folder changed while this
            # worker was hashing its immutable snapshot. Never publish or act
            # on that stale result; one fresh check starts after thread cleanup.
            return
        worker = self._preflight_worker
        calibration_selected = bool(
            worker is not None and worker.config.calibration_source_dir is not None
        )
        qualifiers = [f"{details['recording_count']} recording(s)"]
        if details.get("synthetic"):
            qualifiers.append("synthetic sample")
        if calibration_selected:
            qualifiers.append("read-only SQLite calibration ready")
        format_label = market_replay_format_label(details)
        self.ibrec_preflight_label.setText(
            f"Ready: {details['symbol']} · {format_label} · "
            f"{details['row_count']:,} rows · {' · '.join(qualifiers)}"
        )
        self.ibrec_preflight_label.setStyleSheet(
            "color: #9a5a00;" if details.get("synthetic") else "color: #176b36;"
        )
        self.ibrec_analyze_button.setEnabled(not self._busy)

    @Slot(str)
    def _on_preflight_failed(self, message: str) -> None:
        if self._preflight_pending:
            return
        self.ibrec_preflight_label.setText(f"Blocked: {message}")
        self.ibrec_preflight_label.setStyleSheet("color: #a00000;")
        self.ibrec_analyze_button.setEnabled(False)

    @Slot()
    def _preflight_thread_finished(self) -> None:
        self._preflight_thread = None
        self._preflight_worker = None
        if self._preflight_pending:
            # The selection changed while the previous check ran; the stale
            # result was displayed briefly and is corrected by this rerun.
            self._preflight_pending = False
            self._check_ibrec()

    def _market_replay_config(
        self,
        paths: tuple[Path, ...] | None = None,
    ) -> MarketReplayConfig:
        selected = paths if paths is not None else self._selected_ibrec_paths()
        return MarketReplayConfig(
            recording_path=selected,
            output_root=Path(self.replay_output_edit.text() or "."),
            assumed_trade_notional=self.ibrec_notional_spin.value(),
            execution_cost_bps_per_side=self.ibrec_cost_spin.value(),
            turnover_penalty_bps_per_completed_trade=(
                self.ibrec_turnover_spin.value()
            ),
            continuous_overnight_replay=self.overnight_replay_checkbox.isChecked(),
            calibration_source_dir=(
                Path(self.calibration_source_edit.text()).expanduser()
                if self.calibration_source_edit.text().strip()
                else None
            ),
            worker_processes=self.ibrec_workers_spin.value(),
        )

    @Slot()
    def _start_analysis(self) -> None:
        if self._busy or not self._check_source():
            return
        source = Path(self.source_edit.text()).expanduser().resolve()
        output = Path(self.output_edit.text()).expanduser().resolve()
        answer = QMessageBox.question(
            self,
            "Confirm offline read-only analysis",
            "The trading-bot lock is currently absent. Confirm that BouncyBot is fully closed.\n\n"
            "The optimizer will temporarily acquire the same lock, copy SQLite/WAL through read-only file access, "
            "create a private temporary snapshot, read capture ZIP files without extraction, and write reports only "
            f"to the selected output folder.\n\nSource: {source}\nOutput: {output}\n\nContinue?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return
        self._active_mode = "bot"
        self._set_busy(True)
        self.table.setRowCount(0)
        self.progress_label.setText("Starting analysis…")
        self.progress_bar.setRange(0, 0)
        self._start_worker(AnalysisWorker(AnalysisConfig(source_dir=source, output_root=output)))

    @Slot()
    def _start_ibrec_analysis(self) -> None:
        if self._busy or self._preflight_thread is not None:
            return
        if not self.ibrec_preflight_label.text().startswith("Ready:"):
            # The button is normally disabled without a green preflight; this
            # guard also refreshes the check if state drifted.  The analysis
            # worker independently re-copies and re-verifies every recording,
            # so preflight remains a courtesy, not the safety boundary.
            self._check_ibrec()
            return
        # Preserve the final path component. MarketReplayConfig/load_ibrec must
        # be able to reject an input symlink instead of receiving its resolved
        # target and silently bypassing the safety check.
        sources = self._selected_ibrec_paths()
        output = Path(self.replay_output_edit.text()).expanduser().resolve()
        config = self._market_replay_config(sources)
        calibration_text = (
            "No BouncyBot database will be read and no bot lock is required."
            if config.calibration_source_dir is None
            else (
                "Confirm that BouncyBot is fully closed. The optimizer will temporarily acquire "
                "ibkr_trading_bot.lock, copy SQLite/WAL through read-only file access into a private "
                "snapshot, derive ticker-specific execution-cost/notional evidence, and release only "
                "the lock it created. It will not write to the trading database.\n"
                f"Calibration folder: {config.calibration_source_dir}"
            )
        )
        overnight_text = (
            "Enabled: open long positions and active native SELL trails may continue only across "
            "provably consecutive, complete, primary-eligible RTH recordings."
            if config.continuous_overnight_replay
            else "Disabled: every RTH recording starts flat."
        )
        answer = QMessageBox.question(
            self,
            "Confirm Market Replay optimization",
            "The optimizer will copy and verify the selected .ibrec recordings, combine only unambiguous trading dates, evaluate the bounded ATR grid, and "
            "write a separate deterministic report. It will never connect to IBKR.\n\n"
            f"{calibration_text}\n\n"
            f"Overnight replay: {overnight_text}\n\n"
            f"Recordings: {len(sources)}\nOutput: {output}\n"
            f"Assumed notional: {self.ibrec_notional_spin.value():,.2f}\n"
            f"Execution-cost reserve: {self.ibrec_cost_spin.value():.4f} bps per side\n"
            f"Turnover penalty: {self.ibrec_turnover_spin.value():.4f} score points per completed trade\n"
            f"Profile workers: {'Automatic' if self.ibrec_workers_spin.value() == 0 else self.ibrec_workers_spin.value()}\n\nContinue?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return
        self._active_mode = "ibrec"
        self._set_busy(True)
        self.ibrec_table.setRowCount(0)
        self.ibrec_progress_label.setText("Starting Market Replay analysis…")
        self.ibrec_progress_bar.setRange(0, 0)
        self._start_worker(MarketReplayWorker(config))

    def _start_worker(self, worker: QObject) -> None:
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)  # type: ignore[attr-defined]
        worker.progress.connect(self._on_progress)  # type: ignore[attr-defined]
        worker.completed.connect(self._on_completed_any)  # type: ignore[attr-defined]
        worker.failed.connect(self._on_failed)  # type: ignore[attr-defined]
        worker.finished.connect(thread.quit)  # type: ignore[attr-defined]
        worker.finished.connect(worker.deleteLater)  # type: ignore[attr-defined]
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._thread_finished)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        enabled = not busy
        for widget in (
            self.source_edit,
            self.output_edit,
            self.source_button,
            self.output_button,
            self.check_button,
            self.ibrec_list,
            self.replay_output_edit,
            self.ibrec_button,
            self.ibrec_remove_button,
            self.ibrec_clear_button,
            self.replay_output_button,
            self.ibrec_check_button,
            self.ibrec_notional_spin,
            self.ibrec_cost_spin,
            self.ibrec_turnover_spin,
            self.ibrec_workers_spin,
            self.calibration_source_edit,
            self.calibration_source_button,
            self.calibration_source_clear_button,
            self.overnight_replay_checkbox,
        ):
            widget.setEnabled(enabled)
        self.analyze_button.setEnabled(enabled and self.preflight_label.text().startswith("Ready:"))
        self.ibrec_analyze_button.setEnabled(
            enabled and self.ibrec_preflight_label.text().startswith("Ready:")
        )
        self.tabs.tabBar().setEnabled(enabled)

    @Slot(str, int, int)
    def _on_progress(self, message: str, current: int, total: int) -> None:
        label = self.ibrec_progress_label if self._active_mode == "ibrec" else self.progress_label
        bar = self.ibrec_progress_bar if self._active_mode == "ibrec" else self.progress_bar
        label.setText(message)
        if total > 0:
            bar.setRange(0, total)
            bar.setValue(min(total, max(0, current)))
        else:
            bar.setRange(0, 0)

    @Slot(object)
    def _on_completed_any(self, result: Any) -> None:
        if isinstance(result, AnalysisResult):
            self._on_completed(result)
        elif isinstance(result, MarketReplayAnalysisResult):
            self._on_ibrec_completed(result)
        else:
            self._on_failed("Analysis returned an unexpected result type.")

    @Slot(object)
    def _on_completed(self, result: Any) -> None:
        if not isinstance(result, AnalysisResult):
            self._on_failed("Analysis returned an unexpected result type.")
            return
        self._last_result = result
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.progress_label.setText(f"Complete: {result.output_dir}")
        self.open_button.setEnabled(True)
        self.table.setRowCount(len(result.tickers))
        for row_index, ticker in enumerate(result.tickers):
            cells = result_cells(ticker)
            for column, cell in enumerate(cells[:-1]):
                item = QTableWidgetItem(cell.text)
                item.setToolTip(cell.tooltip)
                self.table.setItem(row_index, column, item)
            report_path = ticker_report_path(result.output_dir, ticker.ticker)
            report_button = QPushButton(cells[-1].text)
            report_button.setToolTip(cells[-1].tooltip)
            report_button.clicked.connect(
                lambda _checked=False, path=report_path: self._open_ticker_report(path)
            )
            self.table.setCellWidget(row_index, len(RESULT_COLUMNS) - 1, report_button)
        QMessageBox.information(self, "Analysis complete", f"Reports were written to:\n{result.output_dir}")

    def _on_ibrec_completed(self, result: MarketReplayAnalysisResult) -> None:
        self._last_market_result = result
        self.ibrec_progress_bar.setRange(0, 1)
        self.ibrec_progress_bar.setValue(1)
        self.ibrec_progress_label.setText(f"Complete: {result.output_dir}")
        self.ibrec_open_button.setEnabled(True)
        profile = result.recommendation.profile
        values = [
            result.recording.symbol,
            f"{result.recording.format_label} · {result.recording.input_recording_count} file(s)",
            str(profile.period),
            str(profile.bar_seconds),
            f"{profile.initial_drop_multiplier:.2f}",
            f"{profile.buy_rebound_multiplier:.2f}",
            f"{profile.minimum_profit_multiplier:.2f}",
            f"{profile.sell_trail_multiplier:.2f}",
            f"{result.recommendation.score:.2f}",
            "Yes" if result.recommendation.evidence_stable else "No",
        ]
        self.ibrec_table.setRowCount(1)
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(_REPLAY_COLUMNS[column][1])
            self.ibrec_table.setItem(0, column, item)
        report_button = QPushButton("Open report")
        report_button.setToolTip(_REPLAY_COLUMNS[-1][1])
        report_button.clicked.connect(
            lambda _checked=False, path=result.output_dir / "index.html": self._open_report(
                path,
                "Market Replay",
            )
        )
        self.ibrec_table.setCellWidget(0, len(_REPLAY_COLUMNS) - 1, report_button)
        QMessageBox.information(
            self,
            "Market Replay analysis complete",
            f"The separate report was written to:\n{result.output_dir}",
        )

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        bar = self.ibrec_progress_bar if self._active_mode == "ibrec" else self.progress_bar
        label = self.ibrec_progress_label if self._active_mode == "ibrec" else self.progress_label
        bar.setRange(0, 1)
        bar.setValue(0)
        label.setText("Analysis failed")
        QMessageBox.critical(self, "Analysis failed", message)

    @Slot()
    def _thread_finished(self) -> None:
        completed_mode = self._active_mode
        self._thread = None
        self._worker = None
        self._active_mode = None
        self._set_busy(False)
        if completed_mode == "ibrec":
            self._check_ibrec()
            if self._last_market_result is not None:
                self.ibrec_open_button.setEnabled(True)
        else:
            self._check_source()
            if self._last_result is not None:
                self.open_button.setEnabled(True)

    @Slot()
    def _open_latest(self) -> None:
        if self._last_result is not None:
            self._open_report(self._last_result.output_dir / "index.html", "summary")

    @Slot()
    def _open_latest_ibrec(self) -> None:
        if self._last_market_result is not None:
            self._open_report(
                self._last_market_result.output_dir / "index.html",
                "Market Replay",
            )

    def _open_ticker_report(self, path: Path) -> None:
        self._open_report(path, "ticker")

    def _open_report(self, path: Path, description: str) -> None:
        if not path.exists() or not path.is_file():
            QMessageBox.warning(
                self,
                "Report not found",
                f"The {description} report is missing:\n{path}",
            )
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(
                self,
                "Could not open report",
                f"Windows could not open the {description} report:\n{path}",
            )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._thread is not None and self._thread.isRunning():
            QMessageBox.warning(
                self,
                "Analysis is running",
                "Wait for the analysis to finish so temporary files and any owned lock can be released cleanly.",
            )
            event.ignore()
            return
        if self._preflight_thread is not None and self._preflight_thread.isRunning():
            QMessageBox.information(
                self,
                "Recording check is running",
                "Wait for the recording structure check to finish so its temporary copies can be released cleanly.",
            )
            event.ignore()
            return
        event.accept()


def launch_gui(
    *,
    source_dir: Path,
    output_dir: Path,
    ibrec_path: Path | tuple[Path, ...] | None = None,
) -> int:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow(source_dir, output_dir, ibrec_path)
    window.show()
    return int(app.exec())
