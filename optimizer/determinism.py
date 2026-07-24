"""Content-derived identifiers and timestamps for repeatable report output."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .database import DatabaseDataset
from .models import AnalysisConfig, CaptureMeta
from .utils import iso_from_timestamp, timestamp_seconds
from .version import APP_VERSION

ANALYSIS_CONTRACT_VERSION = 6


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def analysis_fingerprint(
    *,
    database_state: tuple[tuple[str, int, int, str] | None, ...],
    capture_state: tuple[tuple[str, int, int, str], ...],
    config: AnalysisConfig,
) -> str:
    """Hash only content and analysis parameters, never paths or wall-clock time."""
    database_files = [
        {"name": row[0], "size": row[1], "sha256": row[3]}
        for row in database_state[:2]
        if row is not None
    ]
    captures = [
        {"path": row[0], "size": row[1], "sha256": row[3]}
        for row in capture_state
    ]
    payload = {
        "optimizer_version": APP_VERSION,
        "analysis_contract": ANALYSIS_CONTRACT_VERSION,
        "database_files": database_files,
        "capture_archives": captures,
        "max_archive_uncompressed_bytes": int(config.max_archive_uncompressed_bytes),
        "max_rows_per_capture": int(config.max_rows_per_capture),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def database_content_fingerprint(
    database_state: tuple[tuple[str, int, int, str] | None, ...],
) -> str:
    """Hash the SQLite main file and WAL by content, excluding volatile SHM."""
    files = [
        {"name": row[0], "size": row[1], "sha256": row[3]}
        for row in database_state[:2]
        if row is not None
    ]
    return hashlib.sha256(canonical_json_bytes(files)).hexdigest()


def _timestamps(rows: Iterable[dict[str, Any]]) -> Iterable[float]:
    keys = (
        "created_at",
        "updated_at",
        "executed_at",
        "buy_filled_at",
        "sell_filled_at",
        "protective_sell_filled_at",
    )
    for row in rows:
        for key in keys:
            value = timestamp_seconds(row.get(key))
            if value is not None:
                yield value


def data_through_utc(dataset: DatabaseDataset, metas: list[CaptureMeta]) -> str:
    """Latest timestamp present in analyzed evidence, independent of run time."""
    values: list[float] = list(_timestamps(dataset.cycles))
    for grouped in (
        dataset.orders_by_cycle,
        dataset.executions_by_cycle,
        dataset.decisions_by_cycle,
        dataset.events_by_cycle,
    ):
        for rows in grouped.values():
            values.extend(_timestamps(rows))
    for updated_at in dataset.settings_updated_at.values():
        value = timestamp_seconds(updated_at)
        if value is not None:
            values.append(value)
    for meta in metas:
        for text in (meta.event_time_utc, meta.first_row_utc, meta.last_row_utc):
            value = timestamp_seconds(text)
            if value is not None:
                values.append(value)
    return iso_from_timestamp(max(values)) if values else ""
