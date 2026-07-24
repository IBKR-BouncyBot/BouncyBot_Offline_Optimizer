from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest


def iso(base: datetime, minutes: float = 0.0) -> str:
    return (base + timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


def create_database(root: Path, *, ticker: str = "AAPL", cycles: int = 2) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "bot_state.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE cycles (
            id TEXT PRIMARY KEY,
            cycle_number INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            stage TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT,
            initial_drop_pct REAL,
            buy_rebound_trail_pct REAL,
            rise_trigger_pct REAL,
            sell_trailing_stop_pct REAL,
            atr_adaptive_enabled INTEGER,
            atr_adapt_minimum_profit_enabled INTEGER,
            atr_adapt_protective_sell_enabled INTEGER,
            atr_period INTEGER,
            atr_bar_seconds INTEGER,
            atr_initial_drop_multiplier REAL,
            atr_buy_rebound_multiplier REAL,
            atr_minimum_profit_multiplier REAL,
            atr_sell_trail_multiplier REAL,
            atr_protective_sell_multiplier REAL,
            atr_min_pct REAL,
            atr_max_pct REAL,
            protective_sell_enabled INTEGER,
            protective_sell_trailing_stop_pct REAL,
            buy_order_ref TEXT,
            buy_filled_qty INTEGER,
            avg_buy_price REAL,
            buy_filled_at TEXT,
            sell_order_ref TEXT,
            sell_filled_qty INTEGER,
            avg_sell_price REAL,
            sell_filled_at TEXT,
            protective_sell_order_ref TEXT,
            protective_sell_filled_qty INTEGER DEFAULT 0,
            protective_avg_sell_price REAL,
            protective_sell_filled_at TEXT,
            gross_pnl REAL,
            net_pnl REAL
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id TEXT,
            ticker TEXT,
            action TEXT,
            order_type TEXT,
            order_ref TEXT,
            quantity INTEGER,
            trailing_percent REAL,
            initial_stop_price REAL,
            status TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id TEXT,
            ticker TEXT,
            order_ref TEXT,
            side TEXT,
            shares REAL,
            price REAL,
            avg_price REAL,
            commission REAL,
            executed_at TEXT
        );
        CREATE TABLE decision_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            event_type TEXT,
            ticker TEXT,
            cycle_id TEXT,
            stage_before TEXT,
            stage_after TEXT,
            decision_result TEXT,
            message TEXT
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            level TEXT,
            ticker TEXT,
            cycle_id TEXT,
            message TEXT
        );
        CREATE TABLE app_settings (key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT);
        """
    )
    base = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)
    for number in range(1, cycles + 1):
        cycle_base = base + timedelta(days=number - 1)
        cycle_id = f"cycle-{number}"
        buy_time = iso(cycle_base, 15)
        sell_time = iso(cycle_base, 60)
        buy_price = 100.0 + number * 0.1
        sell_price = 103.2 + number * 0.1
        connection.execute(
            """
            INSERT INTO cycles(
                id, cycle_number, ticker, stage, created_at, updated_at,
                initial_drop_pct, buy_rebound_trail_pct, rise_trigger_pct, sell_trailing_stop_pct,
                atr_adaptive_enabled, atr_adapt_minimum_profit_enabled,
                atr_adapt_protective_sell_enabled, atr_period, atr_bar_seconds,
                atr_initial_drop_multiplier, atr_buy_rebound_multiplier,
                atr_minimum_profit_multiplier, atr_sell_trail_multiplier,
                atr_protective_sell_multiplier, atr_min_pct, atr_max_pct,
                protective_sell_enabled, protective_sell_trailing_stop_pct,
                buy_order_ref, buy_filled_qty, avg_buy_price, buy_filled_at,
                sell_order_ref, sell_filled_qty, avg_sell_price, sell_filled_at,
                gross_pnl, net_pnl
            ) VALUES (?, ?, ?, '5_CYCLE_COMPLETE', ?, ?, 0.75, 0.375, 0.5, 0.5,
                      1, 1, 0, 14, 60, 1.5, 0.75, 1.0, 1.0, 3.0, 0.1, 20.0,
                      0, 3.0,
                      ?, 10, ?, ?, ?, 10, ?, ?, ?, ?)
            """,
            (
                cycle_id,
                number,
                ticker,
                iso(cycle_base),
                sell_time,
                f"BUY-{number}",
                buy_price,
                buy_time,
                f"SELL-{number}",
                sell_price,
                sell_time,
                (sell_price - buy_price) * 10,
                (sell_price - buy_price) * 10 - 2,
            ),
        )
        connection.execute(
            "INSERT INTO orders(cycle_id,ticker,action,order_type,order_ref,quantity,trailing_percent,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, ticker, "BUY", "TRAIL", f"BUY-{number}", 10, 0.375, "Filled", iso(cycle_base, 4), buy_time),
        )
        connection.execute(
            "INSERT INTO orders(cycle_id,ticker,action,order_type,order_ref,quantity,trailing_percent,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, ticker, "SELL", "TRAIL", f"SELL-{number}", 10, 0.5, "Filled", iso(cycle_base, 45), sell_time),
        )
        connection.execute(
            "INSERT INTO executions(cycle_id,ticker,order_ref,side,shares,price,avg_price,commission,executed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (cycle_id, ticker, f"BUY-{number}", "BOT", 10, buy_price, buy_price, 1.0, buy_time),
        )
        connection.execute(
            "INSERT INTO executions(cycle_id,ticker,order_ref,side,shares,price,avg_price,commission,executed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (cycle_id, ticker, f"SELL-{number}", "SLD", 10, sell_price, sell_price, 1.0, sell_time),
        )
        connection.execute(
            "INSERT INTO decision_events(created_at,event_type,ticker,cycle_id,stage_before,stage_after,decision_result,message) VALUES(?,?,?,?,?,?,?,?)",
            (iso(cycle_base, 4), "BUY_ORDER_SUBMITTED", ticker, cycle_id, "1", "2", "submitted", "Synthetic entry"),
        )
        connection.execute(
            "INSERT INTO decision_events(created_at,event_type,ticker,cycle_id,stage_before,stage_after,decision_result,message) VALUES(?,?,?,?,?,?,?,?)",
            (iso(cycle_base, 45), "SELL_ORDER_SUBMITTED", ticker, cycle_id, "3", "4", "submitted", "Synthetic exit"),
        )
    connection.execute(
        "INSERT INTO app_settings(key,value_json,updated_at) VALUES('strategy', ?, ?)",
        (json.dumps({"ticker": ticker, "atr_period": 14}), iso(base)),
    )
    connection.commit()
    connection.close()
    return path


def _buy_price(minute: int, number: int) -> float:
    if minute <= -10:
        return 101.6 - (minute + 15) * 0.05
    if minute <= -2:
        return 101.35 - (minute + 10) * 0.27
    if minute <= 0:
        return 99.19 + (minute + 2) * 0.48 + number * 0.1
    return 100.15 + number * 0.1 + minute * 0.04


def _sell_price(minute: int, number: int) -> float:
    if minute <= -8:
        return 102.5 + number * 0.1 + (minute + 15) * 0.12
    if minute <= -2:
        return 103.34 + number * 0.1 + (minute + 8) * 0.17
    if minute <= 0:
        return 104.36 + number * 0.1 - (minute + 2) * 0.55
    return 103.26 + number * 0.1 - minute * 0.03


def create_capture(
    root: Path,
    *,
    ticker: str,
    cycle_id: str,
    cycle_number: int,
    event_type: str,
    event_time: datetime,
    order_ref: str,
    price_kind: str,
    include_jsonl: bool = True,
    include_csv: bool = True,
    atr_pct: float = 0.5,
) -> Path:
    folder = root / "debug_captures" / ticker / f"cycle_{cycle_number}"
    folder.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for minute in range(-15, 16):
        price = _buy_price(minute, cycle_number) if price_kind == "buy" else _sell_price(minute, cycle_number)
        captured = event_time + timedelta(minutes=minute)
        rows.append(
            {
                "captured_at_utc": captured.replace(microsecond=0).isoformat(),
                "monotonic_ts": 10_000.0 + minute * 60,
                "ticker": ticker,
                "cycle_id": cycle_id,
                "cycle_number": cycle_number,
                "stage": "2_BUY_TRAIL_ACTIVE" if price_kind == "buy" else "4_SELL_TRAIL_ACTIVE",
                "price": round(price, 4),
                "source": "last",
                "fields": {"last": round(price, 4), "bid": round(price - 0.01, 4), "ask": round(price + 0.01, 4)},
                "atr_pct": atr_pct,
                "market_data_update_consumed": True,
                "strategy_price_usable": True,
            }
        )
    manifest = {
        "event_id": f"event-{cycle_number}-{price_kind}",
        "event_type": event_type,
        "ticker": ticker,
        "cycle_id": cycle_id,
        "cycle_number": cycle_number,
        "order_ref": order_ref,
        "perm_id": cycle_number * 100,
        "started_at_utc": event_time.replace(microsecond=0).isoformat(),
        "finalized_at_utc": (event_time + timedelta(minutes=15)).replace(microsecond=0).isoformat(),
        "pre_window_seconds": 900,
        "post_window_seconds": 900,
        "rows": len(rows),
        "first_row_utc": rows[0]["captured_at_utc"],
        "last_row_utc": rows[-1]["captured_at_utc"],
    }
    event = {
        "event_type": event_type,
        "event_time_utc": event_time.replace(microsecond=0).isoformat(),
        "cycle": {"id": cycle_id, "cycle_number": cycle_number, "ticker": ticker},
        "order_ref": order_ref,
        "strategy": {"atr_period": 14, "atr_bar_seconds": 60},
    }
    path = folder / f"{event_type.lower()}_{cycle_number}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("event.json", json.dumps(event))
        if include_jsonl:
            archive.writestr("market_data.jsonl", "".join(json.dumps(row) + "\n" for row in rows))
        if include_csv:
            columns = ["captured_at_utc", "monotonic_ts", "ticker", "cycle_id", "cycle_number", "stage", "price", "source", "atr_pct"]
            lines = [",".join(columns)]
            for row in rows:
                lines.append(",".join(str(row.get(column, "")) for column in columns))
            archive.writestr("market_data.csv", "\n".join(lines) + "\n")
    return path


def create_source_fixture(root: Path, *, ticker: str = "AAPL", cycles: int = 2) -> Path:
    create_database(root, ticker=ticker, cycles=cycles)
    base = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)
    for number in range(1, cycles + 1):
        cycle_base = base + timedelta(days=number - 1)
        create_capture(
            root,
            ticker=ticker,
            cycle_id=f"cycle-{number}",
            cycle_number=number,
            event_type="BUY_FILL",
            event_time=cycle_base + timedelta(minutes=15),
            order_ref=f"BUY-{number}",
            price_kind="buy",
        )
        create_capture(
            root,
            ticker=ticker,
            cycle_id=f"cycle-{number}",
            cycle_number=number,
            event_type="SELL_FILL",
            event_time=cycle_base + timedelta(minutes=60),
            order_ref=f"SELL-{number}",
            price_kind="sell",
        )
    return root


@pytest.fixture
def source_fixture(tmp_path: Path) -> Path:
    return create_source_fixture(tmp_path / "bot")
