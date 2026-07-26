"""Schema-tolerant reader for BouncyBot SQLite snapshots."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .utils import finite_float, finite_int


class _ClosingConnection(sqlite3.Connection):
    """SQLite connection whose context manager also closes the handle."""

    def __exit__(self, exc_type, exc, tb):  # type: ignore[override]
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


class DatabaseFormatError(RuntimeError):
    """Raised when the selected database is not a compatible bot database."""


@dataclass(slots=True)
class DatabaseDataset:
    schema: dict[str, Any]
    cycles: list[dict[str, Any]]
    orders_by_cycle: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    executions_by_cycle: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    decisions_by_cycle: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    events_by_cycle: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    settings_updated_at: dict[str, str] = field(default_factory=dict)

    @property
    def tickers(self) -> list[str]:
        return sorted({str(row.get("ticker") or "").strip().upper() for row in self.cycles if row.get("ticker")})


class SnapshotDatabase:
    """Read data from a temporary SQLite snapshot, never the source file."""

    _OPTIONAL_TABLES = ("orders", "executions", "decision_events", "events", "app_settings")

    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, factory=_ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _quote_identifier(value: str) -> str:
        """Return a safely quoted SQLite identifier discovered from sqlite_master."""
        return '"' + str(value).replace('"', '""') + '"'

    @staticmethod
    def _order_clause(columns: set[str], candidates: tuple[str, ...]) -> str:
        selected = [f'{SnapshotDatabase._quote_identifier(name)} ASC' for name in candidates if name in columns]
        return f" ORDER BY {', '.join(selected)}" if selected else ""

    def inspect_schema(self) -> dict[str, Any]:
        with self.connect() as connection:
            quick = connection.execute("PRAGMA quick_check").fetchone()
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            table_rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            tables = [str(row[0]) for row in table_rows]
            columns: dict[str, list[str]] = {}
            counts: dict[str, int | None] = {}
            counted_tables = {
                "cycles",
                "orders",
                "executions",
                "decision_events",
                "events",
                "app_settings",
            }
            for table in tables:
                quoted = self._quote_identifier(table)
                columns[table] = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted})")]
                if table not in counted_tables:
                    counts[table] = None
                    continue
                try:
                    counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
                except sqlite3.Error:
                    counts[table] = -1
        return {
            "quick_check": str(quick[0]) if quick else "unknown",
            "user_version": user_version,
            "tables": tables,
            "columns": columns,
            "row_counts": counts,
        }

    @staticmethod
    def _rows(connection: sqlite3.Connection, query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in connection.execute(query, tuple(params)).fetchall()]

    @staticmethod
    def _group(rows: list[dict[str, Any]], key: str = "cycle_id") -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            cycle_id = str(row.get(key) or "").strip()
            if cycle_id:
                grouped.setdefault(cycle_id, []).append(row)
        return grouped

    @staticmethod
    def _parse_settings(
        rows: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        settings: dict[str, Any] = {}
        updated_at: dict[str, str] = {}
        for row in rows:
            key = str(row.get("key") or "")
            if not key:
                continue
            raw = row.get("value_json")
            try:
                settings[key] = json.loads(str(raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                settings[key] = raw
            updated_at[key] = str(row.get("updated_at") or "")
        return settings, updated_at

    def load(self) -> DatabaseDataset:
        schema = self.inspect_schema()
        if schema.get("quick_check") != "ok":
            raise DatabaseFormatError(f"SQLite quick_check did not return 'ok': {schema.get('quick_check')}")
        tables = set(schema.get("tables") or [])
        required = {"cycles"}
        missing = sorted(required - tables)
        if missing:
            raise DatabaseFormatError(f"Database is missing required table(s): {', '.join(missing)}")

        with self.connect() as connection:
            cycle_columns = set(schema["columns"].get("cycles") or [])
            order_clause = self._order_clause(
                cycle_columns,
                ("ticker", "cycle_number", "created_at", "id"),
            )
            cycles = self._rows(connection, f"SELECT * FROM cycles{order_clause}")

            orders: list[dict[str, Any]] = []
            executions: list[dict[str, Any]] = []
            decisions: list[dict[str, Any]] = []
            events: list[dict[str, Any]] = []
            setting_rows: list[dict[str, Any]] = []
            if "orders" in tables:
                columns = set(schema["columns"].get("orders") or [])
                orders = self._rows(connection, f"SELECT * FROM orders{self._order_clause(columns, ('cycle_id', 'id'))}")
            if "executions" in tables:
                columns = set(schema["columns"].get("executions") or [])
                executions = self._rows(
                    connection,
                    f"SELECT * FROM executions{self._order_clause(columns, ('cycle_id', 'executed_at', 'id'))}",
                )
            if "decision_events" in tables:
                columns = set(schema["columns"].get("decision_events") or [])
                where = ""
                if "event_type" in columns:
                    where = (
                        " WHERE UPPER(event_type) LIKE '%BUY_ORDER%'"
                        " OR UPPER(event_type) LIKE '%BUY_TRAIL%'"
                        " OR UPPER(event_type) LIKE '%SELL_ORDER%'"
                        " OR UPPER(event_type) LIKE '%SELL_TRAIL%'"
                        " OR UPPER(event_type) LIKE '%PROTECTIVE_SELL%'"
                    )
                decisions = self._rows(
                    connection,
                    f"SELECT * FROM decision_events{where}{self._order_clause(columns, ('cycle_id', 'created_at', 'id'))}",
                )
            if "events" in tables:
                columns = set(schema["columns"].get("events") or [])
                events = self._rows(
                    connection,
                    f"SELECT * FROM events{self._order_clause(columns, ('cycle_id', 'created_at', 'id'))}",
                )
            if "app_settings" in tables:
                columns = set(schema["columns"].get("app_settings") or [])
                setting_rows = self._rows(
                    connection,
                    f"SELECT * FROM app_settings{self._order_clause(columns, ('key', 'updated_at', 'id'))}",
                )

        settings, settings_updated_at = self._parse_settings(setting_rows)
        return DatabaseDataset(
            schema=schema,
            cycles=cycles,
            orders_by_cycle=self._group(orders),
            executions_by_cycle=self._group(executions),
            decisions_by_cycle=self._group(decisions),
            events_by_cycle=self._group(events),
            settings=settings,
            settings_updated_at=settings_updated_at,
        )


def completed_cycle(cycle: dict[str, Any]) -> bool:
    """Return whether a row represents a completed normal or protective exit."""
    stage = str(cycle.get("stage") or "").strip().upper()
    if stage == "5_CYCLE_COMPLETE" or stage.endswith("CYCLE_COMPLETE"):
        return True
    bought = max(0.0, safe_float(cycle.get("buy_filled_qty")) or 0.0)
    normal_sold = max(0.0, safe_float(cycle.get("sell_filled_qty")) or 0.0)
    protective_sold = max(
        0.0,
        safe_float(cycle.get("protective_sell_filled_qty")) or 0.0,
    )
    # Mirror BouncyBot's app-owned-position rule exactly. A protective fill is
    # copied into the normal SELL history fields, and order references/times can
    # remain different after cancellation or recovery. Summing the two columns
    # can therefore double-count one economic exit. The production bot uses the
    # greater cumulative close quantity, not their sum.
    sold = max(normal_sold, protective_sold)
    tolerance = max(1e-8, bought * 1e-9)
    return bool(bought > 0 and sold >= bought - tolerance)


def safe_float(value: Any) -> float | None:
    # A SQLite/JSON boolean is not a quantity, price, or percentage.  Python's
    # normal ``float(True) == 1.0`` coercion would otherwise make malformed data
    # look valid and could change coverage or replay results.
    return finite_float(value)


def safe_int(value: Any) -> int | None:
    """Parse an integer without silently truncating fractional values.

    SQLite and JSON may expose integer columns as ``14``, ``14.0`` or
    ``"14.0"``.  All three are accepted.  Values such as ``14.5`` are rejected
    rather than being silently truncated to 14, which would change an ATR
    period or cycle number without any warning.
    """

    return finite_int(value)
