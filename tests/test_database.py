from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from optimizer.database import DatabaseFormatError, SnapshotDatabase, completed_cycle, safe_float, safe_int
from tests.conftest import create_database


def test_snapshot_database_loads_schema_and_groups_cycle_rows(tmp_path: Path) -> None:
    database = create_database(tmp_path, cycles=2)
    dataset = SnapshotDatabase(database).load()
    assert dataset.tickers == ["AAPL"]
    assert len(dataset.cycles) == 2
    assert len(dataset.orders_by_cycle["cycle-1"]) == 2
    assert len(dataset.executions_by_cycle["cycle-2"]) == 2
    assert dataset.settings["strategy"]["atr_period"] == 14
    assert dataset.schema["quick_check"] == "ok"
    assert dataset.schema["row_counts"]["events"] == 0


def test_snapshot_database_loads_events_in_deterministic_cycle_order(
    tmp_path: Path,
) -> None:
    database = create_database(tmp_path, cycles=1)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO events(created_at,level,ticker,cycle_id,message) VALUES(?,?,?,?,?)",
            ("2026-01-05T14:02:00+00:00", "INFO", "AAPL", "cycle-1", "second"),
        )
        connection.execute(
            "INSERT INTO events(created_at,level,ticker,cycle_id,message) VALUES(?,?,?,?,?)",
            ("2026-01-05T14:01:00+00:00", "INFO", "AAPL", "cycle-1", "first"),
        )
        connection.commit()

    dataset = SnapshotDatabase(database).load()

    assert [row["message"] for row in dataset.events_by_cycle["cycle-1"]] == [
        "first",
        "second",
    ]
    assert dataset.schema["row_counts"]["events"] == 2


def test_snapshot_database_filters_unrelated_decision_events(tmp_path: Path) -> None:
    database = create_database(tmp_path, cycles=1)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """
            INSERT INTO decision_events(
                created_at,event_type,ticker,cycle_id,stage_before,
                stage_after,decision_result,message
            ) VALUES('2026-01-05T14:01:00+00:00','PRICE_TICK','AAPL',
                     'cycle-1','1','1','observed','not order relevant')
            """
        )
        connection.commit()
    dataset = SnapshotDatabase(database).load()
    event_types = {
        str(row.get("event_type"))
        for row in dataset.decisions_by_cycle["cycle-1"]
    }
    assert "PRICE_TICK" not in event_types
    assert event_types == {"BUY_ORDER_SUBMITTED", "SELL_ORDER_SUBMITTED"}


def test_cycle_query_uses_id_as_a_deterministic_final_tie_breaker(
    tmp_path: Path,
) -> None:
    database = create_database(tmp_path, cycles=1)
    with closing(sqlite3.connect(database)) as connection:
        original = connection.execute(
            "SELECT * FROM cycles WHERE id = 'cycle-1'"
        ).fetchone()
        assert original is not None
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(cycles)").fetchall()
        ]
        values = dict(zip(columns, original, strict=True))
        values["id"] = "aaa-same-sort-fields"
        placeholders = ",".join("?" for _ in columns)
        quoted_columns = ",".join(f'"{name}"' for name in columns)
        connection.execute(
            f"INSERT INTO cycles({quoted_columns}) VALUES({placeholders})",
            [values[name] for name in columns],
        )
        connection.commit()

    ids = [row["id"] for row in SnapshotDatabase(database).load().cycles]
    assert ids == ["aaa-same-sort-fields", "cycle-1"]


def test_snapshot_database_rejects_unrelated_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "other.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    with pytest.raises(DatabaseFormatError, match="missing required table"):
        SnapshotDatabase(path).load()


def test_completed_cycle_and_numeric_helpers() -> None:
    assert completed_cycle({"stage": "5_CYCLE_COMPLETE"})
    assert completed_cycle({"stage": "ERROR", "buy_filled_qty": 5, "sell_filled_qty": 5})
    assert not completed_cycle({"stage": "ERROR", "buy_filled_qty": 5, "sell_filled_qty": 4})
    assert not completed_cycle(
        {
            "stage": "ERROR",
            "buy_filled_qty": 10,
            "sell_filled_qty": 6,
            "protective_sell_filled_qty": 4,
            "sell_order_ref": "SELL-CANCELLED",
            "protective_sell_order_ref": "PROTECTIVE-FILLED",
        }
    )
    assert not completed_cycle(
        {
            "stage": "ERROR",
            "buy_filled_qty": 10,
            "sell_filled_qty": 6,
            "protective_sell_filled_qty": 4,
            "sell_order_ref": "SELL-PARTIAL",
            "protective_sell_order_ref": "PROTECTIVE-FILLED",
            "sell_filled_at": "2026-01-01T15:00:00+00:00",
            "protective_sell_filled_at": "2026-01-01T15:05:00+00:00",
        }
    )
    assert safe_float("1.25") == 1.25
    assert safe_float(True) is None
    assert safe_float("nan") is None
    assert safe_int("4") == 4
    assert safe_int(True) is None
    assert safe_int("x") is None
