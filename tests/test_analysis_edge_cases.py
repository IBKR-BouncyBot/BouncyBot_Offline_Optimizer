from __future__ import annotations

import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from optimizer.analysis import AnalysisError, run_analysis
from optimizer.models import AnalysisConfig
from tests.conftest import create_capture, create_database, create_source_fixture


def test_database_without_capture_directory_still_produces_ticker_report(tmp_path: Path) -> None:
    source = tmp_path / "bot"
    create_database(source, cycles=1)
    result = run_analysis(AnalysisConfig(source_dir=source, output_root=tmp_path / "reports"))
    assert result.tickers[0].coverage["coverage_grade"] == "D"
    assert result.tickers[0].coverage["capture_archives"] == 0
    assert result.tickers[0].suggested_settings[0]["profile"].startswith(
        "Evaluation control: latest complete historical ATR snapshot"
    )


def test_capture_ticker_without_cycle_rows_is_reported(source_fixture: Path, tmp_path: Path) -> None:
    source_capture = next((source_fixture / "debug_captures" / "AAPL").rglob("*.zip"))
    target = source_fixture / "debug_captures" / "ORPHAN" / "cycle_9" / "orphan.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(source_capture.read_bytes())
    # The copied manifest still says AAPL, proving that metadata rather than folder names owns ticker identity.
    result = run_analysis(AnalysisConfig(source_dir=source_fixture, output_root=tmp_path / "reports"))
    assert [ticker.ticker for ticker in result.tickers] == ["AAPL"]


def test_completed_quantity_fallback_supports_legacy_stage(tmp_path: Path) -> None:
    source = tmp_path / "bot"
    database = create_database(source, cycles=1)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE cycles SET stage='ERROR' WHERE id='cycle-1'")
        connection.commit()
    result = run_analysis(AnalysisConfig(source_dir=source, output_root=tmp_path / "reports"))
    assert result.tickers[0].coverage["completed_cycles"] == 1


def test_execution_rows_recover_missing_fill_price_and_time(tmp_path: Path) -> None:
    source = create_source_fixture(tmp_path / "bot", cycles=1)
    with closing(sqlite3.connect(source / "bot_state.sqlite")) as connection:
        connection.execute(
            """
            UPDATE cycles
            SET avg_buy_price=NULL, buy_filled_at=NULL,
                avg_sell_price=NULL, sell_filled_at=NULL
            WHERE id='cycle-1'
            """
        )
        connection.commit()
    result = run_analysis(
        AnalysisConfig(source_dir=source, output_root=tmp_path / "reports")
    )
    coverage = result.tickers[0].coverage
    assert coverage["buy_fills"] == 1
    assert coverage["normal_sell_fills"] == 1
    assert coverage["matched_buy_captures"] == 1
    assert coverage["matched_sell_captures"] == 1


def test_protective_exit_is_inventoried_but_not_ranked_as_profit_sell(tmp_path: Path) -> None:
    source = tmp_path / "bot"
    database = create_database(source, cycles=1)
    base = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)
    protective_time = (base + timedelta(minutes=40)).replace(microsecond=0).isoformat()
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DELETE FROM orders WHERE cycle_id='cycle-1' AND action='SELL'")
        connection.execute("DELETE FROM executions WHERE cycle_id='cycle-1' AND side='SLD'")
        connection.execute(
            """
            UPDATE cycles
            SET sell_order_ref='PROT-1', sell_filled_qty=10, avg_sell_price=98.5,
                sell_filled_at=?, protective_sell_enabled=1,
                protective_sell_order_ref='PROT-1', protective_sell_filled_qty=10,
                protective_avg_sell_price=98.5, protective_sell_filled_at=?
            WHERE id='cycle-1'
            """,
            (protective_time, protective_time),
        )
        connection.execute(
            """
            INSERT INTO orders(
                cycle_id,ticker,action,order_type,order_ref,quantity,
                trailing_percent,status,created_at,updated_at
            ) VALUES('cycle-1','AAPL','PROTECTIVE_SELL','TRAIL','PROT-1',10,
                     3.0,'Filled',?,?)
            """,
            (protective_time, protective_time),
        )
        connection.execute(
            """
            INSERT INTO executions(
                cycle_id,ticker,order_ref,side,shares,price,avg_price,
                commission,executed_at
            ) VALUES('cycle-1','AAPL','PROT-1','SLD',10,98.5,98.5,1.0,?)
            """,
            (protective_time,),
        )
        connection.commit()
    create_capture(
        source,
        ticker="AAPL",
        cycle_id="cycle-1",
        cycle_number=1,
        event_type="BUY_FILL",
        event_time=base + timedelta(minutes=15),
        order_ref="BUY-1",
        price_kind="buy",
    )
    create_capture(
        source,
        ticker="AAPL",
        cycle_id="cycle-1",
        cycle_number=1,
        event_type="PROTECTIVE_SELL_FILL",
        event_time=base + timedelta(minutes=40),
        order_ref="PROT-1",
        price_kind="sell",
    )
    result = run_analysis(
        AnalysisConfig(source_dir=source, output_root=tmp_path / "reports")
    )
    ticker = result.tickers[0]
    assert ticker.coverage["normal_sell_fills"] == 0
    assert ticker.coverage["protective_sell_fills"] == 1
    assert ticker.coverage["matched_protective_sell_captures"] == 1
    assert ticker.coverage["replayable_sell_windows"] == 0
    assert not any(row.leg == "sell" for row in ticker.replay_observations)


def test_output_inside_capture_directory_is_rejected(source_fixture: Path) -> None:
    output = source_fixture / "debug_captures" / "reports"
    with pytest.raises(AnalysisError, match="Output path cannot"):
        run_analysis(AnalysisConfig(source_dir=source_fixture, output_root=output))


@pytest.mark.parametrize(
    "relative_output",
    [
        "ibkr_trading_bot.lock",
        "ibkr_trading_bot.lock/reports",
        "bot_state.sqlite",
        "bot_state.sqlite-wal",
        "bot_state.sqlite-shm/reports",
    ],
)
def test_output_cannot_replace_reserved_bot_files(
    source_fixture: Path,
    relative_output: str,
) -> None:
    output = source_fixture / relative_output
    with pytest.raises(AnalysisError, match="bot lock"):
        run_analysis(AnalysisConfig(source_dir=source_fixture, output_root=output))

    # In particular, selecting the lock path must never leave a directory named
    # like the lock; that would make the trading bot appear permanently active.
    assert not (source_fixture / "ibkr_trading_bot.lock").exists()


def test_source_change_during_analysis_aborts_and_releases_lock(
    source_fixture: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    import optimizer.analysis as analysis_module

    states = iter(
        [
            (("bot_state.sqlite", 100, 1), None, None),
            (("bot_state.sqlite", 101, 2), None, None),
        ]
    )
    monkeypatch.setattr(analysis_module, "source_state", lambda database: next(states))
    with pytest.raises(AnalysisError, match="changed during analysis"):
        run_analysis(
            AnalysisConfig(
                source_dir=source_fixture,
                output_root=tmp_path / "reports",
            )
        )
    assert not (source_fixture / "ibkr_trading_bot.lock").exists()


def test_atr_experiments_enable_adaptation_when_history_was_manual(tmp_path: Path) -> None:
    source = create_source_fixture(tmp_path / "bot", cycles=4)
    with closing(sqlite3.connect(source / "bot_state.sqlite")) as connection:
        connection.execute("UPDATE cycles SET atr_adaptive_enabled=0")
        connection.commit()

    result = run_analysis(
        AnalysisConfig(source_dir=source, output_root=tmp_path / "reports")
    )
    profiles = result.tickers[0].suggested_settings

    assert profiles[0]["profile"].startswith(
        "Evaluation control: latest complete historical ATR snapshot"
    )
    assert profiles[0]["atr_adaptive_enabled"] is False
    assert len(profiles) > 1
    assert all(profile["atr_adaptive_enabled"] is True for profile in profiles[1:])
    assert any(
        "historical manual-percentage control" in profile["evidence"]
        for profile in profiles[1:]
    )


def test_empty_nearest_capture_does_not_hide_later_usable_match(tmp_path: Path) -> None:
    source = create_source_fixture(tmp_path / "bot", cycles=1)
    folder = source / "debug_captures" / "AAPL" / "cycle_1"
    original = folder / "buy_fill_1.zip"
    usable = folder / "zzz_usable_buy.zip"
    original.rename(usable)

    with zipfile.ZipFile(usable, "r") as archive:
        manifest = archive.read("manifest.json")
        event = archive.read("event.json")

    empty = folder / "000_empty_buy.zip"
    with zipfile.ZipFile(empty, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", manifest)
        archive.writestr("event.json", event)
        archive.writestr(
            "market_data.jsonl",
            json.dumps(
                {
                    "captured_at_utc": "not-a-timestamp",
                    "price": 0,
                }
            )
            + "\n",
        )

    result = run_analysis(
        AnalysisConfig(source_dir=source, output_root=tmp_path / "reports")
    )
    ticker = result.tickers[0]

    assert ticker.coverage["matched_buy_captures"] == 1
    assert ticker.coverage["replayable_buy_windows"] == 1
    assert ticker.coverage["usable_capture_archives"] == 2
    assert ticker.coverage["archives_without_usable_prices"] == 1
    assert ticker.coverage["corrupt_or_unsupported_archives"] == 1
    assert any(row.leg == "buy" for row in ticker.replay_observations)
    empty_inventory = next(
        row for row in ticker.capture_inventory if row["path"].endswith("000_empty_buy.zip")
    )
    assert empty_inventory["usable_points"] == 0
    assert any(
        issue == "fatal: no usable positive-price rows were found"
        for issue in empty_inventory["issues"]
    )
    assert not any("matching BUY capture archive(s) contained no usable" in issue for issue in ticker.issues)
