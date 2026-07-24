from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

FORMAT_NAME = "IBKR Market Replay Recording"
ZERO_HASH = "0" * 64
TICK_FIELDS = (
    "sequence",
    "captured_at_utc",
    "elapsed_ns",
    "symbol",
    "con_id",
    "source_time_utc",
    "bid",
    "bid_size",
    "ask",
    "ask_size",
    "last",
    "last_size",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "mark_price",
    "market_data_type",
    "changed_fields",
)
RTH_FIELDS = (
    "period_id",
    "session_date",
    "schedule_open_utc",
    "schedule_close_utc",
    "observed_start_utc",
    "observed_end_utc",
    "source",
    "status",
    "close_reason",
    "first_tick_sequence",
    "last_tick_sequence",
    "tick_count",
)


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def hash_json(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def chain(previous: str, record_hash: str) -> str:
    return hashlib.sha256((previous + record_hash).encode("ascii")).hexdigest()


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def price_path(points: int = 121) -> list[float]:
    anchors = [
        (0, 100.0),
        (24, 100.3),
        (32, 102.0),
        (44, 99.0),
        (54, 101.5),
        (76, 105.0),
        (88, 102.6),
        (104, 104.0),
        (120, 102.8),
    ]
    output: list[float] = []
    for index in range(points):
        left, right = anchors[0], anchors[-1]
        for start, end in zip(anchors, anchors[1:]):
            if start[0] <= index <= end[0]:
                left, right = start, end
                break
        fraction = 0.0 if right[0] == left[0] else (index - left[0]) / (right[0] - left[0])
        base = left[1] + (right[1] - left[1]) * fraction
        output.append(round(base + (0.03 if index % 2 else -0.03), 4))
    return output


def make_ticks(
    *,
    sessions: int = 1,
    points_per_session: int = 121,
    step_seconds: int = 15,
    symbol: str = "AAPL",
    con_id: int = 265598,
    market_data_type: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ticks: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    sequence = 0
    elapsed_ns = 0
    for session_index in range(sessions):
        session_open = datetime(2026, 1, 5 + session_index, 14, 30, tzinfo=timezone.utc)
        session_close = session_open + timedelta(minutes=35)
        prices = price_path(points_per_session)
        first_sequence = sequence + 1
        for point_index, price in enumerate(prices):
            sequence += 1
            timestamp = session_open + timedelta(seconds=point_index * step_seconds)
            ticks.append(
                {
                    "sequence": sequence,
                    "captured_at_utc": iso(timestamp),
                    "elapsed_ns": elapsed_ns,
                    "symbol": symbol,
                    "con_id": con_id,
                    "source_time_utc": iso(timestamp),
                    "bid": round(price - 0.01, 4),
                    "bid_size": 100.0,
                    "ask": round(price + 0.01, 4),
                    "ask_size": 100.0,
                    "last": price,
                    "last_size": 10.0,
                    "open": prices[0],
                    "high": max(prices[: point_index + 1]),
                    "low": min(prices[: point_index + 1]),
                    "close": price,
                    "volume": float(1000 + point_index * 10),
                    "mark_price": price,
                    "market_data_type": market_data_type,
                    "changed_fields": "bid,ask,last,mark_price",
                    "rth_period_id": session_index + 1,
                }
            )
            elapsed_ns += step_seconds * 1_000_000_000
        periods.append(
            {
                "period_id": session_index + 1,
                "session_date": session_open.strftime("%Y%m%d"),
                "schedule_open_utc": iso(session_open),
                "schedule_close_utc": iso(session_close),
                "observed_start_utc": ticks[first_sequence - 1]["captured_at_utc"],
                "observed_end_utc": ticks[-1]["captured_at_utc"],
                "source": "test_fixture",
                "status": "closed",
                "close_reason": "fixture_complete",
                "first_tick_sequence": first_sequence,
                "last_tick_sequence": sequence,
                "tick_count": points_per_session,
            }
        )
        elapsed_ns += 60 * 1_000_000_000
    return ticks, periods


def contract(symbol: str = "AAPL", con_id: int = 265598) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "exchange": "SMART",
        "primary_exchange": "NASDAQ",
        "currency": "USD",
        "sec_type": "STK",
        "con_id": con_id,
        "local_symbol": symbol,
        "trading_class": "NMS",
        "description": "TEST SECURITY",
        "min_tick": 0.01,
        "time_zone_id": "America/New_York",
        "trading_hours": "20260105:0400-20260105:2000",
        "liquid_hours": "20260105:0930-20260105:1005",
    }


def write_v2(path: Path, ticks: list[dict[str, Any]] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = ticks or make_ticks()[0]
    manifest = {
        "format_name": FORMAT_NAME,
        "format_version": 2,
        "recording_id": "fixture-v2",
        "started_at_utc": rows[0]["captured_at_utc"],
        "ended_at_utc": rows[-1]["captured_at_utc"],
        "status": "complete",
        "row_count": len(rows),
        "source": {"type": "test_fixture"},
        "contract": contract(str(rows[0]["symbol"]), int(rows[0]["con_id"])),
    }
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(TICK_FIELDS), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in TICK_FIELDS})
    members = {
        "manifest.json": (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        "ticks.csv": output.getvalue().encode("utf-8"),
        "events.jsonl": b"",
    }
    checksums = {
        "algorithm": "sha256",
        "files": {name: hashlib.sha256(value).hexdigest() for name, value in members.items()},
    }
    members["checksums.json"] = (json.dumps(checksums, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, members[name])
    return path


_SCHEMA = """
CREATE TABLE metadata(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE rth_periods(
    period_id INTEGER PRIMARY KEY,
    session_date TEXT NOT NULL,
    schedule_open_utc TEXT NOT NULL,
    schedule_close_utc TEXT NOT NULL,
    observed_start_utc TEXT NOT NULL,
    observed_end_utc TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    close_reason TEXT NOT NULL,
    first_tick_sequence INTEGER,
    last_tick_sequence INTEGER,
    tick_count INTEGER NOT NULL,
    record_hash TEXT NOT NULL
);
CREATE TABLE ticks(
    sequence INTEGER PRIMARY KEY,
    captured_at_utc TEXT NOT NULL,
    elapsed_ns INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    con_id INTEGER NOT NULL,
    source_time_utc TEXT NOT NULL,
    bid REAL,
    bid_size REAL,
    ask REAL,
    ask_size REAL,
    last REAL,
    last_size REAL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    mark_price REAL,
    market_data_type INTEGER NOT NULL,
    changed_fields TEXT NOT NULL,
    rth_period_id INTEGER REFERENCES rth_periods(period_id),
    record_hash TEXT NOT NULL,
    chain_hash TEXT NOT NULL
);
CREATE TABLE events(
    sequence INTEGER PRIMARY KEY,
    created_at_utc TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    chain_hash TEXT NOT NULL
);
CREATE TABLE checkpoints(
    checkpoint_id INTEGER PRIMARY KEY,
    created_at_utc TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    rth_period_count INTEGER NOT NULL,
    tick_chain_hash TEXT NOT NULL,
    event_chain_hash TEXT NOT NULL,
    rth_period_digest TEXT NOT NULL,
    status TEXT NOT NULL
);
"""


def write_v3(
    path: Path,
    ticks: list[dict[str, Any]] | None = None,
    periods: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    *,
    source_overrides: dict[str, Any] | None = None,
    manifest_status: str = "complete",
    notes: str = "",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows, default_periods = make_ticks() if ticks is None else (ticks, periods or [])
    period_rows = periods if periods is not None else default_periods
    if rows and not period_rows:
        raise ValueError("At least one RTH period is required.")
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_SCHEMA)
        connection.execute("PRAGMA user_version=3")
        raw_periods: list[dict[str, Any]] = []
        for period in period_rows:
            raw = {field: period.get(field) for field in RTH_FIELDS}
            record_hash = hash_json(raw)
            raw_periods.append({**raw, "record_hash": record_hash})
            connection.execute(
                "INSERT INTO rth_periods VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(raw[field] for field in RTH_FIELDS) + (record_hash,),
            )
        tick_chain = ZERO_HASH
        for row in rows:
            record = {field: row.get(field, "") for field in TICK_FIELDS}
            record["rth_period_id"] = row.get("rth_period_id")
            record_hash = hash_json(record)
            tick_chain = chain(tick_chain, record_hash)
            connection.execute(
                "INSERT INTO ticks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(record[field] for field in TICK_FIELDS)
                + (record["rth_period_id"], record_hash, tick_chain),
            )
        event_chain = ZERO_HASH
        event_rows = events or []
        for event in event_rows:
            payload_json = str(event.get("payload_json", canonical(event.get("payload", {}))))
            record = {
                "sequence": event.get("sequence"),
                "created_at_utc": event.get("created_at_utc"),
                "event_type": event.get("event_type", "fixture_event"),
                "payload_json": payload_json,
            }
            record_hash = hash_json(record)
            event_chain = chain(event_chain, record_hash)
            connection.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?)",
                (
                    record["sequence"],
                    record["created_at_utc"],
                    record["event_type"],
                    payload_json,
                    record_hash,
                    event_chain,
                ),
            )
        period_digest = hash_json(
            [
                {field: row.get(field) for field in (*RTH_FIELDS, "record_hash")}
                for row in raw_periods
            ]
        )
        timestamp = rows[0]["captured_at_utc"] if rows else "2026-01-05T14:30:00.000Z"
        source = {
            "type": "test_fixture",
            "rth_only_capture": True,
            "tick_chain_hash": tick_chain,
            "event_chain_hash": event_chain,
            "rth_period_digest": period_digest,
        }
        source.update(source_overrides or {})
        contract_value = (
            contract(str(rows[0]["symbol"]), int(rows[0]["con_id"]))
            if rows
            else contract()
        )
        manifest = {
            "format_name": FORMAT_NAME,
            "format_version": 3,
            "recording_id": "fixture-v3",
            "started_at_utc": timestamp,
            "ended_at_utc": rows[-1]["captured_at_utc"] if rows else timestamp,
            "status": manifest_status,
            "row_count": len(rows),
            "committed_row_count": len(rows),
            "rth_period_count": len(period_rows),
            "source": source,
            "contract": contract_value,
            "notes": notes,
        }
        connection.execute(
            "INSERT INTO metadata(key, value_json) VALUES ('manifest', ?)",
            (canonical(manifest),),
        )
        connection.execute(
            "INSERT INTO checkpoints VALUES (1,?,?,?,?,?,?,?,?)",
            (
                rows[-1]["captured_at_utc"] if rows else timestamp,
                len(rows),
                len(event_rows),
                len(period_rows),
                tick_chain,
                event_chain,
                period_digest,
                manifest_status,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def copy_with_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
