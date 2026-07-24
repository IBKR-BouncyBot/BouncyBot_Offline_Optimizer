from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from optimizer.analysis import AnalysisError, run_analysis
from optimizer.models import AnalysisConfig
from optimizer.reports import write_reports
from optimizer.safety import SourceSafetyError
from tests.conftest import create_capture


def test_end_to_end_analysis_is_read_only_and_writes_complete_report(source_fixture: Path, tmp_path: Path) -> None:
    database = source_fixture / "bot_state.sqlite"
    before_db = database.read_bytes()
    capture_bytes = {path: path.read_bytes() for path in (source_fixture / "debug_captures").rglob("*.zip")}
    progress_messages: list[str] = []
    result = run_analysis(
        AnalysisConfig(source_dir=source_fixture, output_root=tmp_path / "reports"),
        progress=lambda message, current, total: progress_messages.append(message),
    )
    assert not (source_fixture / "ibkr_trading_bot.lock").exists()
    assert result.database_sha256
    assert len(result.tickers) == 1
    ticker = result.tickers[0]
    assert ticker.ticker == "AAPL"
    assert ticker.coverage["completed_cycles"] == 2
    assert ticker.coverage["matched_buy_captures"] == 2
    assert ticker.coverage["matched_sell_captures"] == 2
    assert ticker.coverage["replayable_buy_windows"] == 2
    assert ticker.coverage["replayable_sell_windows"] == 2
    assert ticker.suggested_settings
    assert ticker.candidate_summaries
    assert all(
        row.priority
        in {
            "insufficient ATR coverage",
            "insufficient evidence",
            "unranked",
        }
        for row in ticker.candidate_summaries
    )
    assert progress_messages

    write_reports(result)
    assert (result.output_dir / "index.html").exists()
    assert (result.output_dir / "analysis_manifest.json").exists()
    assert (result.output_dir / "SHA256SUMS.txt").exists()
    ticker_dir = result.output_dir / "AAPL"
    assert (ticker_dir / "AAPL_coverage_and_replay.html").exists()
    assert (ticker_dir / "AAPL_atr_settings_to_evaluate.csv").exists()
    manifest = json.loads((result.output_dir / "analysis_manifest.json").read_text(encoding="utf-8"))
    assert manifest["tickers"][0]["ticker"] == "AAPL"
    assert database.read_bytes() == before_db
    for path, payload in capture_bytes.items():
        assert path.read_bytes() == payload


def test_analysis_refuses_existing_bot_lock(source_fixture: Path, tmp_path: Path) -> None:
    lock = source_fixture / "ibkr_trading_bot.lock"
    lock.write_text("123", encoding="ascii")
    with pytest.raises(SourceSafetyError, match="lock file exists"):
        run_analysis(AnalysisConfig(source_dir=source_fixture, output_root=tmp_path / "reports"))


def test_analysis_reports_corrupt_capture_without_stopping_valid_ticker(source_fixture: Path, tmp_path: Path) -> None:
    bad = source_fixture / "debug_captures" / "AAPL" / "cycle_1" / "corrupt.zip"
    bad.write_bytes(b"bad")
    result = run_analysis(AnalysisConfig(source_dir=source_fixture, output_root=tmp_path / "reports"))
    assert any("corrupt" in issue.lower() for issue in result.global_issues)
    assert any("Unreadable capture" in issue for issue in result.tickers[0].issues)


def test_analysis_separates_multiple_tickers_into_independent_reports(
    source_fixture: Path,
    tmp_path: Path,
) -> None:
    database = source_fixture / "bot_state.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        cycle = dict(
            connection.execute("SELECT * FROM cycles WHERE id='cycle-1'").fetchone()
        )
        cycle.update(
            {
                "id": "msft-cycle-1",
                "ticker": "MSFT",
                "buy_order_ref": "MSFT-BUY-1",
                "sell_order_ref": "MSFT-SELL-1",
            }
        )
        cycle_columns = list(cycle)
        connection.execute(
            f"INSERT INTO cycles({','.join(cycle_columns)}) "
            f"VALUES({','.join('?' for _ in cycle_columns)})",
            [cycle[column] for column in cycle_columns],
        )
        for table, reference_map in (
            (
                "orders",
                {"BUY-1": "MSFT-BUY-1", "SELL-1": "MSFT-SELL-1"},
            ),
            (
                "executions",
                {"BUY-1": "MSFT-BUY-1", "SELL-1": "MSFT-SELL-1"},
            ),
            ("decision_events", {}),
        ):
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE cycle_id='cycle-1'"
            ).fetchall()
            for original in rows:
                copied = dict(original)
                copied.pop("id", None)
                copied["cycle_id"] = "msft-cycle-1"
                copied["ticker"] = "MSFT"
                if "order_ref" in copied:
                    copied["order_ref"] = reference_map.get(
                        str(copied.get("order_ref") or ""),
                        copied.get("order_ref"),
                    )
                columns = list(copied)
                connection.execute(
                    f"INSERT INTO {table}({','.join(columns)}) "
                    f"VALUES({','.join('?' for _ in columns)})",
                    [copied[column] for column in columns],
                )
        connection.commit()

    base = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)
    create_capture(
        source_fixture,
        ticker="MSFT",
        cycle_id="msft-cycle-1",
        cycle_number=1,
        event_type="BUY_FILL",
        event_time=base + timedelta(minutes=15),
        order_ref="MSFT-BUY-1",
        price_kind="buy",
    )
    create_capture(
        source_fixture,
        ticker="MSFT",
        cycle_id="msft-cycle-1",
        cycle_number=1,
        event_type="SELL_FILL",
        event_time=base + timedelta(minutes=60),
        order_ref="MSFT-SELL-1",
        price_kind="sell",
    )

    result = write_reports(
        run_analysis(
            AnalysisConfig(
                source_dir=source_fixture,
                output_root=tmp_path / "reports",
            )
        )
    )
    assert [ticker.ticker for ticker in result.tickers] == ["AAPL", "MSFT"]
    assert (result.output_dir / "AAPL" / "AAPL_coverage_and_replay.html").exists()
    assert (result.output_dir / "MSFT" / "MSFT_coverage_and_replay.html").exists()


def test_analysis_rejects_output_replacing_input(source_fixture: Path) -> None:
    with pytest.raises(AnalysisError, match="cannot replace"):
        run_analysis(
            AnalysisConfig(
                source_dir=source_fixture,
                output_root=source_fixture / "debug_captures",
            )
        )
