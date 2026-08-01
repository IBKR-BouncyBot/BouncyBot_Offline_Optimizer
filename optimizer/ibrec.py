"""Strict read-only importer for Market Replay Lab ``.ibrec`` versions 2 and 3."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import shutil
import sqlite3
import tempfile
import zipfile
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .market_replay_models import (
    IbrecPeriod,
    IbrecRecording,
    IbrecTick,
    MarketReplayConfig,
)
from .utils import finite_float, finite_int


class IbrecError(RuntimeError):
    """Raised when a Market Replay recording is unsafe or structurally invalid."""


ProgressCallback = Callable[[str, int, int], None]
_SQLITE_MAGIC = b"SQLite format 3\x00"
_FORMAT_NAME = "IBKR Market Replay Recording"
_ZERO_HASH = "0" * 64
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_CSV_FIELD_BYTES = 4 * 1024 * 1024
_MAX_CSV_PHYSICAL_LINE_CHARS = 8 * 1024 * 1024
_MAX_ZIP_MEMBERS = 64
_TICK_FIELDS = (
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
_LEGACY_REQUIRED_FIELDS = frozenset(_TICK_FIELDS)
_NUMERIC_FIELDS = (
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
)
_CHANGED_FIELD_ORDER = (
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
)
_ALLOWED_CHANGED_FIELDS = frozenset(_CHANGED_FIELD_ORDER)
_RTH_HASH_FIELDS = (
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
_REQUIRED_V3_TABLES = frozenset({"metadata", "ticks", "events", "rth_periods", "checkpoints"})
_REQUIRED_V3_COLUMNS = {
    "metadata": {"key", "value_json"},
    "ticks": set(_TICK_FIELDS) | {"rth_period_id", "record_hash", "chain_hash"},
    "events": {"sequence", "created_at_utc", "event_type", "payload_json", "record_hash", "chain_hash"},
    "rth_periods": set(_RTH_HASH_FIELDS) | {"record_hash"},
    "checkpoints": {
        "checkpoint_id",
        "created_at_utc",
        "row_count",
        "event_count",
        "rth_period_count",
        "tick_chain_hash",
        "event_chain_hash",
        "rth_period_digest",
        "status",
    },
}
_CONNECTIVITY_ERROR_CODES = frozenset({1100, 1300, 2103})
_CONNECTIVITY_RESTORE_CODES = frozenset({1101, 1102, 2104, 2106, 2158})
_CONNECTIVITY_EVENT_NAMES = frozenset(
    {
        "IBKR_API_DISCONNECTED",
        "IBKR_MARKET_DATA_FARM_DISCONNECTED",
        "IBKR_UPSTREAM_DISCONNECTED",
        "gateway_connection_failed",
        "recording_callback_error",
        "recording_failed",
    }
)
_CONNECTIVITY_RESTORE_NAMES = frozenset(
    {
        "IBKR_MARKET_DATA_FARM_RESTORED",
        "IBKR_UPSTREAM_RESTORED_DATA_LOST",
        "IBKR_UPSTREAM_RESTORED_DATA_MAINTAINED",
        "gateway_connected",
        "market_data_reselected",
    }
)


def _object_dict(value: object) -> dict[str, Any]:
    """Return a string-keyed copy when ``value`` is a JSON-style object."""

    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _quality_event(
    *,
    sequence: int,
    created_at_utc: object,
    event_type: object,
    payload: object,
) -> dict[str, Any] | None:
    """Return a normalized connectivity-quality event when relevant."""

    name = str(event_type or "").strip()
    details = _object_dict(payload)
    # Format-v2 exports normally flatten event fields, while other producers
    # can retain a nested ``payload`` mapping. Accept both representations so a
    # connectivity loss cannot be missed merely because of container layout.
    nested = _object_dict(details.get("payload"))
    if nested:
        details = {**details, **nested}
    error_code = finite_int(details.get("error_code"))
    upper_name = name.upper()
    is_disconnect = (
        name in _CONNECTIVITY_EVENT_NAMES
        or upper_name in _CONNECTIVITY_EVENT_NAMES
        or error_code in _CONNECTIVITY_ERROR_CODES
        or "DISCONNECT" in upper_name
        or "CONNECTION_FAILED" in upper_name
    )
    is_restore = (
        name in _CONNECTIVITY_RESTORE_NAMES
        or upper_name in _CONNECTIVITY_RESTORE_NAMES
        or error_code in _CONNECTIVITY_RESTORE_CODES
        or "RESTORED" in upper_name
        or upper_name == "GATEWAY_CONNECTED"
    )
    if not is_disconnect and not is_restore:
        return None
    normalized_time, timestamp = _parse_utc(
        created_at_utc,
        field=f"event {sequence} created_at_utc",
    )
    return {
        "sequence": int(sequence),
        "created_at_utc": normalized_time,
        "timestamp": timestamp,
        "event_type": name,
        "error_code": error_code,
        "message": str(details.get("message") or details.get("error") or ""),
        "disconnect": bool(is_disconnect),
        "restore": bool(is_restore),
    }


def _read_v2_quality_events(
    archive: zipfile.ZipFile,
    names: set[str],
) -> list[dict[str, Any]]:
    if "events.jsonl" not in names:
        return []
    events: list[dict[str, Any]] = []
    try:
        with archive.open("events.jsonl", "r") as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            for sequence, line in enumerate(text, start=1):
                if len(line) > _MAX_CSV_PHYSICAL_LINE_CHARS:
                    raise IbrecError(
                        f"events.jsonl line {sequence} exceeds the "
                        f"{_MAX_CSV_PHYSICAL_LINE_CHARS:,}-character safety limit."
                    )
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise IbrecError(
                        f"events.jsonl line {sequence} is invalid JSON: {exc}"
                    ) from exc
                if not isinstance(value, dict):
                    raise IbrecError(
                        f"events.jsonl line {sequence} must contain a JSON object."
                    )
                event = _quality_event(
                    sequence=sequence,
                    created_at_utc=value.get("created_at_utc"),
                    event_type=value.get("event_type"),
                    payload=value,
                )
                if event is not None:
                    events.append(event)
    except UnicodeDecodeError as exc:
        raise IbrecError(f"events.jsonl is not valid UTF-8: {exc}") from exc
    return events


def _emit(progress: ProgressCallback | None, message: str, current: int, total: int) -> None:
    if progress is not None:
        progress(message, current, total)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _chain_hash(previous: str, record_hash: str) -> str:
    return hashlib.sha256((previous + record_hash).encode("ascii")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_state(path: Path) -> tuple[int, int, str]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, _file_hash(path)


def _component_fingerprint(components: Iterable[dict[str, Any]]) -> str:
    """Return a path-independent fingerprint for all physical input components."""

    normalized = sorted(
        (
            {
                "role": str(component.get("role") or ""),
                "size": int(component.get("size") or 0),
                "sha256": str(component.get("sha256") or ""),
            }
            for component in components
        ),
        key=lambda item: (item["role"], item["sha256"], item["size"]),
    )
    return hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()


def _copy_source_components(source: Path, destination_dir: Path) -> tuple[Path, list[dict[str, Any]]]:
    copied = destination_dir / source.name
    components: list[dict[str, Any]] = []
    for suffix in ("", "-journal"):
        original = Path(str(source) + suffix)
        if not original.exists() and not original.is_symlink():
            continue
        if not original.is_file() or original.is_symlink():
            raise IbrecError(f"Recording component must be a regular non-symlink file: {original}")
        before = _file_state(original)
        target = Path(str(copied) + suffix)
        shutil.copyfile(original, target)
        after = _file_state(original)
        if before != after:
            raise IbrecError(f"Recording changed while it was being copied: {original.name}")
        copied_hash = _file_hash(target)
        if copied_hash != before[2] or target.stat().st_size != before[0]:
            raise IbrecError(f"Private recording copy failed verification: {original.name}")
        role = "recording" if not suffix else "rollback_journal"
        components.append(
            {
                "role": role,
                "name": "recording.ibrec" if not suffix else "recording.ibrec-journal",
                "size": before[0],
                "sha256": before[2],
            }
        )
    if not components:
        raise IbrecError(f"Recording does not exist: {source}")
    return copied, components


def _reject_unsupported_sqlite_sidecars(source: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise IbrecError(
                f"Unsupported SQLite sidecar is present: {sidecar.name}. "
                "Market Replay format v3 uses rollback-journal mode; close the recorder and use a committed .ibrec file."
            )


def _parse_utc(value: Any, *, field: str) -> tuple[str, float]:
    text = str(value or "").strip()
    if not text:
        raise IbrecError(f"{field} is required.")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IbrecError(f"{field} is not a valid ISO timestamp: {text!r}") from exc
    if parsed.tzinfo is None:
        raise IbrecError(f"{field} must include an explicit UTC offset.")
    utc = parsed.astimezone(timezone.utc)
    normalized = utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return normalized, utc.timestamp()


def _nonnegative_int(value: Any, *, field: str) -> int:
    number = finite_int(value)
    if number is None or number < 0:
        raise IbrecError(f"{field} must be a non-negative integer.")
    return number


def _optional_nonnegative_float(value: Any, *, field: str) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise IbrecError(f"{field} must be a finite non-negative number.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise IbrecError(f"{field} must be a finite non-negative number.") from exc
    if not math.isfinite(number) or number < 0:
        raise IbrecError(f"{field} must be a finite non-negative number.")
    return number


def _positive_finite_float(value: Any, *, field: str) -> float:
    """Parse a required positive finite number without bool coercion."""

    number = finite_float(value)
    if number is None or number <= 0:
        raise IbrecError(f"{field} must be a finite positive number.")
    return number


def _changed_fields(value: Any, *, row_number: int) -> tuple[tuple[str, ...], bool]:
    text = str(value or "").strip()
    if not text:
        return (), True
    requested = [part.strip().lower() for part in text.split(",") if part.strip()]
    unknown = sorted(set(requested).difference(_ALLOWED_CHANGED_FIELDS))
    if unknown:
        raise IbrecError(
            f"ticks row {row_number} contains unsupported changed_fields: {', '.join(unknown)}"
        )
    if len(requested) != len(set(requested)):
        raise IbrecError(f"ticks row {row_number} repeats a changed_fields value.")
    return tuple(name for name in _CHANGED_FIELD_ORDER if name in requested), False


def _tick_from_mapping(
    row: dict[str, Any],
    *,
    row_number: int,
    expected_symbol: str,
    expected_con_id: int,
    rth_period_id: Any = None,
) -> IbrecTick:
    sequence = _nonnegative_int(row.get("sequence"), field=f"ticks row {row_number} sequence")
    if sequence < 1:
        raise IbrecError(f"ticks row {row_number} sequence must start at one.")
    elapsed_ns = _nonnegative_int(row.get("elapsed_ns"), field=f"ticks row {row_number} elapsed_ns")
    captured_at_utc, timestamp = _parse_utc(
        row.get("captured_at_utc"),
        field=f"ticks row {row_number} captured_at_utc",
    )
    symbol = str(row.get("symbol") or "").upper().strip()
    if not symbol:
        raise IbrecError(f"ticks row {row_number} has no symbol.")
    if expected_symbol and symbol != expected_symbol:
        raise IbrecError(
            f"ticks row {row_number} symbol {symbol!r} does not match manifest {expected_symbol!r}."
        )
    con_id = _nonnegative_int(row.get("con_id", 0), field=f"ticks row {row_number} con_id")
    if expected_con_id and con_id not in {0, expected_con_id}:
        raise IbrecError(
            f"ticks row {row_number} con_id {con_id} does not match manifest {expected_con_id}."
        )
    source_time = str(row.get("source_time_utc") or "").strip()
    if source_time:
        source_time, _ = _parse_utc(source_time, field=f"ticks row {row_number} source_time_utc")
    values = {
        name: _optional_nonnegative_float(row.get(name), field=f"ticks row {row_number} {name}")
        for name in _NUMERIC_FIELDS
    }
    market_data_type = _nonnegative_int(
        row.get("market_data_type", 1),
        field=f"ticks row {row_number} market_data_type",
    )
    if market_data_type not in {1, 2, 3, 4}:
        raise IbrecError(
            f"ticks row {row_number} has invalid market_data_type {market_data_type}."
        )
    fields, full_snapshot = _changed_fields(row.get("changed_fields"), row_number=row_number)
    period_id = None
    if rth_period_id not in (None, ""):
        period_id = _nonnegative_int(rth_period_id, field=f"ticks row {row_number} rth_period_id")
        if period_id < 1:
            raise IbrecError(f"ticks row {row_number} rth_period_id must be positive when present.")
    return IbrecTick(
        sequence=sequence,
        captured_at_utc=captured_at_utc,
        timestamp=timestamp,
        elapsed_ns=elapsed_ns,
        symbol=symbol,
        con_id=con_id,
        source_time_utc=source_time,
        bid=values["bid"],
        bid_size=values["bid_size"],
        ask=values["ask"],
        ask_size=values["ask_size"],
        last=values["last"],
        last_size=values["last_size"],
        open=values["open"],
        high=values["high"],
        low=values["low"],
        close=values["close"],
        volume=values["volume"],
        mark_price=values["mark_price"],
        market_data_type=market_data_type,
        changed_fields=fields,
        full_snapshot=full_snapshot,
        rth_period_id=period_id,
    )


def _validate_tick_order(
    ticks: Iterable[IbrecTick],
    expected_rows: int,
    *,
    issues: list[str] | None = None,
) -> list[IbrecTick]:
    output: list[IbrecTick] = []
    previous_elapsed = -1
    previous_timestamp = -math.inf
    timestamp_reversals = 0
    for expected_sequence, tick in enumerate(ticks, start=1):
        if tick.sequence != expected_sequence:
            raise IbrecError(
                f"ticks sequence is not contiguous at row {expected_sequence}: found {tick.sequence}."
            )
        if tick.elapsed_ns < previous_elapsed:
            raise IbrecError(f"ticks row {expected_sequence} moves elapsed_ns backwards.")
        if tick.timestamp < previous_timestamp:
            timestamp_reversals += 1
        previous_elapsed = tick.elapsed_ns
        previous_timestamp = tick.timestamp
        output.append(tick)
    if len(output) != expected_rows:
        raise IbrecError(
            f"Manifest row_count is {expected_rows}, but the recording contains {len(output)} rows."
        )
    if timestamp_reversals and issues is not None:
        issues.append(
            "captured_at_utc moved backwards on "
            f"{timestamp_reversals:,} row(s). Sequence and monotonic elapsed_ns were preserved for "
            "event ordering and elapsed-time calculations; calendar-time grouping can be less precise."
        )
    return output


def _validate_manifest(raw: dict[str, Any], *, container: str) -> tuple[dict[str, Any], int, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise IbrecError("Recording manifest must contain a JSON object.")
    manifest = json.loads(_canonical_json(raw))
    if manifest.get("format_name") != _FORMAT_NAME:
        raise IbrecError(f"Unsupported recording format name {manifest.get('format_name')!r}.")
    version = _nonnegative_int(manifest.get("format_version"), field="manifest format_version")
    if version not in {2, 3}:
        raise IbrecError(
            f"Unsupported recording format version {version}; this optimizer supports versions 2 and 3."
        )
    if container == "zip" and version != 2:
        raise IbrecError("ZIP .ibrec recordings must use format version 2.")
    if container == "sqlite" and version != 3:
        raise IbrecError("SQLite .ibrec recordings must use format version 3.")
    contract = manifest.get("contract")
    if not isinstance(contract, dict):
        raise IbrecError("Recording manifest contract must contain a JSON object.")
    symbol = str(contract.get("symbol") or "").upper().strip()
    if not symbol:
        raise IbrecError("Recording manifest contract symbol is required.")
    contract["symbol"] = symbol
    contract["con_id"] = _nonnegative_int(
        contract.get("con_id", contract.get("conId", 0)),
        field="manifest contract con_id",
    )
    contract["min_tick"] = _positive_finite_float(
        contract.get("min_tick", contract.get("minTick")),
        field="manifest contract min_tick",
    )
    row_count = _nonnegative_int(manifest.get("row_count", 0), field="manifest row_count")
    manifest["row_count"] = row_count
    if version == 3:
        if not isinstance(manifest.get("source"), dict):
            raise IbrecError("Format-v3 manifest source must contain a JSON object.")
        status = str(manifest.get("status") or "").strip().lower()
        if status not in {"recording", "complete", "interrupted", "error"}:
            raise IbrecError(f"Format-v3 manifest has unsupported status {status!r}.")
        manifest["status"] = status
        committed = _nonnegative_int(
            manifest.get("committed_row_count", row_count),
            field="manifest committed_row_count",
        )
        if committed != row_count:
            raise IbrecError("Format-v3 committed_row_count must equal row_count.")
        manifest["committed_row_count"] = committed
        manifest["rth_period_count"] = _nonnegative_int(
            manifest.get("rth_period_count", 0),
            field="manifest rth_period_count",
        )
    return manifest, version, contract


def _read_small_json(archive: zipfile.ZipFile, name: str) -> dict[str, Any]:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise IbrecError(f"Recording is missing {name}.") from exc
    if info.file_size > _MAX_JSON_BYTES:
        raise IbrecError(f"{name} exceeds the {_MAX_JSON_BYTES:,}-byte safety limit.")
    try:
        value = json.loads(archive.read(name).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError, OSError) as exc:
        raise IbrecError(f"{name} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise IbrecError(f"{name} must contain a JSON object.")
    return value


def _zip_member_hash(archive: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with archive.open(name, "r") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_csv_lines(stream: Iterable[str]) -> Iterator[str]:
    for line_number, line in enumerate(stream, start=1):
        if len(line) > _MAX_CSV_PHYSICAL_LINE_CHARS:
            raise IbrecError(
                f"ticks.csv physical line {line_number} exceeds the "
                f"{_MAX_CSV_PHYSICAL_LINE_CHARS:,}-character safety limit."
            )
        yield line


def _verify_v2_checksums(archive: zipfile.ZipFile, names: set[str], issues: list[str]) -> None:
    if "checksums.json" not in names:
        issues.append("Recording has no checksums.json; whole-file SHA-256 was verified, but member hashes were unavailable.")
        return
    checksums = _read_small_json(archive, "checksums.json")
    if str(checksums.get("algorithm", "sha256")).lower() != "sha256":
        raise IbrecError("Unsupported checksums.json algorithm; expected sha256.")
    files = checksums.get("files")
    if not isinstance(files, dict):
        raise IbrecError("checksums.json files must contain a JSON object.")
    required = {"manifest.json", "ticks.csv"}
    if "events.jsonl" in names:
        required.add("events.jsonl")
    missing = required.difference(files)
    if missing:
        raise IbrecError("checksums.json is missing: " + ", ".join(sorted(missing)))
    for name, expected in sorted(files.items()):
        if name not in names:
            raise IbrecError(f"Checksum references missing ZIP member {name!r}.")
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in expected)
        ):
            raise IbrecError(f"Checksum for {name!r} is not a valid SHA-256 digest.")
        if _zip_member_hash(archive, name).lower() != expected.lower():
            raise IbrecError(f"Checksum mismatch for {name!r}.")


def _load_v2(
    path: Path,
    config: MarketReplayConfig,
    progress: ProgressCallback | None,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[IbrecTick],
    list[IbrecPeriod],
    list[str],
    list[dict[str, Any]],
]:
    issues: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_ZIP_MEMBERS:
                raise IbrecError(
                    f"Recording contains {len(infos):,} ZIP members; limit is {_MAX_ZIP_MEMBERS}."
                )
            names = [info.filename for info in infos]
            duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
            if duplicates:
                raise IbrecError("Recording contains duplicate ZIP members: " + ", ".join(duplicates))
            name_set = set(names)
            missing = {"manifest.json", "ticks.csv"}.difference(name_set)
            if missing:
                raise IbrecError("Recording is missing: " + ", ".join(sorted(missing)))
            total_uncompressed = 0
            for info in infos:
                member_path = Path(info.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise IbrecError(f"Recording contains an unsafe ZIP member path: {info.filename}")
                total_uncompressed += int(info.file_size)
                if total_uncompressed > config.max_zip_uncompressed_bytes:
                    raise IbrecError(
                        "Recording exceeds the configured uncompressed ZIP safety limit "
                        f"of {config.max_zip_uncompressed_bytes:,} bytes."
                    )
            manifest, version, contract = _validate_manifest(
                _read_small_json(archive, "manifest.json"),
                container="zip",
            )
            if version != 2:
                raise IbrecError("Only Market Replay ZIP format version 2 is supported.")
            _verify_v2_checksums(archive, name_set, issues)
            symbol = str(contract["symbol"])
            con_id = int(contract["con_id"])
            row_count = int(manifest["row_count"])
            if row_count > config.max_rows:
                raise IbrecError(
                    f"Recording contains {row_count:,} rows; configured limit is {config.max_rows:,}."
                )
            previous_field_limit = csv.field_size_limit()
            csv.field_size_limit(_MAX_CSV_FIELD_BYTES)
            ticks: list[IbrecTick] = []
            try:
                with archive.open("ticks.csv", "r") as raw:
                    text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                    reader = csv.DictReader(_bounded_csv_lines(text))
                    fieldnames = reader.fieldnames or []
                    duplicate_columns = sorted(
                        name for name, count in Counter(fieldnames).items() if count > 1
                    )
                    if duplicate_columns:
                        raise IbrecError(
                            "ticks.csv contains duplicate columns: "
                            + ", ".join(duplicate_columns)
                        )
                    missing_columns = _LEGACY_REQUIRED_FIELDS.difference(fieldnames)
                    if missing_columns:
                        raise IbrecError(
                            "ticks.csv is missing columns: "
                            + ", ".join(sorted(missing_columns))
                        )
                    for row_number, row in enumerate(reader, start=1):
                        if None in row:
                            raise IbrecError(
                                f"ticks.csv row {row_number} contains more values than its header."
                            )
                        ticks.append(
                            _tick_from_mapping(
                                row,
                                row_number=row_number,
                                expected_symbol=symbol,
                                expected_con_id=con_id,
                            )
                        )
                        if len(ticks) > config.max_rows:
                            raise IbrecError(
                                f"Recording exceeds the configured limit of {config.max_rows:,} rows."
                            )
                        if row_number % 25_000 == 0:
                            _emit(progress, "Reading Market Replay v2 rows", row_number, row_count)
            finally:
                csv.field_size_limit(previous_field_limit)
            ticks = _validate_tick_order(ticks, row_count, issues=issues)
            if not ticks:
                raise IbrecError("Recording contains no market-data rows.")
            periods = _derive_v2_periods(ticks, contract, issues)
            quality_events = _read_v2_quality_events(archive, name_set)
            return manifest, contract, ticks, periods, issues, quality_events
    except zipfile.BadZipFile as exc:
        raise IbrecError(f"Not a valid Market Replay v2 ZIP container: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise IbrecError(f"ticks.csv is not valid UTF-8: {exc}") from exc
    except csv.Error as exc:
        raise IbrecError(f"ticks.csv is invalid CSV: {exc}") from exc


def _open_v3_copy(path: Path) -> sqlite3.Connection:
    # The path is a private copy. Opening it read/write is permitted solely so
    # SQLite can roll back a copied hot journal after an interrupted recorder.
    recovery = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    try:
        recovery.execute("PRAGMA busy_timeout=30000")
        recovery.execute("PRAGMA foreign_keys=ON")
        recovery.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    finally:
        recovery.close()
    uri = path.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30.0, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA query_only=ON")
    return connection


def _validate_v3_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != 3:
        raise IbrecError(f"Unsupported SQLite recording schema {version}; expected 3.")
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = _REQUIRED_V3_TABLES.difference(tables)
    if missing:
        raise IbrecError("Format-v3 recording is missing tables: " + ", ".join(sorted(missing)))
    for table, required in _REQUIRED_V3_COLUMNS.items():
        columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        absent = required.difference(columns)
        if absent:
            raise IbrecError(
                f"Format-v3 table {table} is missing columns: {', '.join(sorted(absent))}"
            )


def _read_v3_manifest(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        "SELECT value_json FROM metadata WHERE key='manifest'"
    ).fetchone()
    if row is None:
        raise IbrecError("Format-v3 recording has no manifest metadata.")
    try:
        value = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as exc:
        raise IbrecError(f"Format-v3 manifest metadata is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise IbrecError("Format-v3 manifest metadata must contain a JSON object.")
    return value


def _v3_periods(
    connection: sqlite3.Connection,
    expected_count: int,
    issues: list[str],
) -> tuple[list[IbrecPeriod], dict[int, dict[str, Any]]]:
    connection.row_factory = sqlite3.Row
    periods: list[IbrecPeriod] = []
    raw_periods: dict[int, dict[str, Any]] = {}
    for row in connection.execute("SELECT * FROM rth_periods ORDER BY period_id"):
        value = dict(row)
        period_id = _nonnegative_int(value.get("period_id"), field="rth_periods period_id")
        if period_id < 1:
            raise IbrecError("RTH period_id must be positive.")
        open_iso, open_timestamp = _parse_utc(
            value.get("schedule_open_utc"),
            field=f"RTH period {period_id} schedule_open_utc",
        )
        close_iso, close_timestamp = _parse_utc(
            value.get("schedule_close_utc"),
            field=f"RTH period {period_id} schedule_close_utc",
        )
        if close_timestamp <= open_timestamp:
            raise IbrecError(f"RTH period {period_id} has invalid boundaries.")
        observed_start, observed_start_timestamp = _parse_utc(
            value.get("observed_start_utc"),
            field=f"RTH period {period_id} observed_start_utc",
        )
        status = str(value.get("status") or "").strip().lower()
        if status not in {"active", "closed"}:
            raise IbrecError(
                f"RTH period {period_id} has unsupported status {status!r}; expected active or closed."
            )
        raw_observed_end = str(value.get("observed_end_utc") or "").strip()
        if raw_observed_end:
            observed_end, observed_end_timestamp = _parse_utc(
                raw_observed_end,
                field=f"RTH period {period_id} observed_end_utc",
            )
        elif status == "active":
            latest_tick = connection.execute(
                "SELECT captured_at_utc FROM ticks WHERE rth_period_id=? "
                "ORDER BY sequence DESC LIMIT 1",
                (period_id,),
            ).fetchone()
            if latest_tick is None:
                observed_end = observed_start
                observed_end_timestamp = observed_start_timestamp
            else:
                observed_end, observed_end_timestamp = _parse_utc(
                    latest_tick[0],
                    field=f"RTH period {period_id} latest tick captured_at_utc",
                )
                if observed_end_timestamp < observed_start_timestamp:
                    # ``captured_at_utc`` is recorder wall-clock receipt time and
                    # may move backwards if the machine clock is corrected.  An
                    # active period has no committed end to validate, so clamp
                    # the derived end conservatively to its observed start.  The
                    # separate tick-order audit records the clock reversal and
                    # forces the recommendation evidence to remain unstable.
                    observed_end = observed_start
                    observed_end_timestamp = observed_start_timestamp
                    issues.append(
                        f"RTH period {period_id} latest receipt timestamp precedes its observed start. "
                        "The derived active-period end was clamped to the observed start."
                    )
            issues.append(
                f"RTH period {period_id} remains active with no committed observed_end_utc. "
                "Its last committed tick time was used as the observed end, and the period remains right-censored."
            )
        else:
            raise IbrecError(f"RTH period {period_id} observed_end_utc is required when status is not active.")
        if observed_end_timestamp < observed_start_timestamp:
            raise IbrecError(f"RTH period {period_id} observed end precedes its start.")
        session_date = str(value.get("session_date") or "")
        if not re.fullmatch(r"\d{8}", session_date):
            raise IbrecError(f"RTH period {period_id} session_date must use YYYYMMDD.")
        tick_count = _nonnegative_int(
            value.get("tick_count"),
            field=f"RTH period {period_id} tick_count",
        )
        periods.append(
            IbrecPeriod(
                period_id=period_id,
                session_date=session_date,
                schedule_open_utc=open_iso,
                schedule_close_utc=close_iso,
                open_timestamp=open_timestamp,
                close_timestamp=close_timestamp,
                observed_start_utc=observed_start,
                observed_end_utc=observed_end,
                observed_start_timestamp=observed_start_timestamp,
                observed_end_timestamp=observed_end_timestamp,
                status=status,
                close_reason=str(value.get("close_reason") or ""),
                tick_count=tick_count,
            )
        )
        raw_periods[period_id] = value
    if len(periods) != expected_count:
        raise IbrecError(
            f"Manifest rth_period_count is {expected_count}, but rth_periods contains {len(periods)} rows."
        )
    return periods, raw_periods


def _verify_v3_integrity(
    connection: sqlite3.Connection,
    manifest: dict[str, Any],
    raw_periods: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    quick_rows = connection.execute("PRAGMA quick_check").fetchall()
    quick = [tuple(row) for row in quick_rows]
    if quick != [("ok",)]:
        raise IbrecError(f"SQLite quick_check failed: {quick[:5]}")
    foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign:
        raise IbrecError(f"SQLite foreign-key validation failed: {foreign[:5]}")
    connection.row_factory = sqlite3.Row
    tick_chain = _ZERO_HASH
    _tick_count = 0
    period_stats: dict[int, dict[str, int]] = {}
    for _tick_count, row in enumerate(
        connection.execute("SELECT * FROM ticks ORDER BY sequence"),
        start=1,
    ):
        record = {name: row[name] for name in (*_TICK_FIELDS, "rth_period_id")}
        expected_hash = _hash_json(record)
        if str(row["record_hash"]) != expected_hash:
            raise IbrecError(f"Tick integrity hash mismatch at sequence {row['sequence']}.")
        tick_chain = _chain_hash(tick_chain, expected_hash)
        if str(row["chain_hash"]) != tick_chain:
            raise IbrecError(f"Tick chain hash mismatch at sequence {row['sequence']}.")
        period_id = row["rth_period_id"]
        if period_id is not None:
            period_number = int(period_id)
            stats = period_stats.setdefault(
                period_number,
                {"count": 0, "first": int(row["sequence"]), "last": int(row["sequence"])},
            )
            stats["count"] += 1
            stats["last"] = int(row["sequence"])
    if _tick_count != int(manifest["row_count"]):
        raise IbrecError("Format-v3 tick count does not match the manifest.")
    source = _object_dict(manifest.get("source"))
    if tick_chain != str(source.get("tick_chain_hash", _ZERO_HASH)):
        raise IbrecError("Final tick integrity chain does not match the manifest.")

    event_chain = _ZERO_HASH
    event_count = 0
    quality_events: list[dict[str, Any]] = []
    for event_count, row in enumerate(
        connection.execute("SELECT * FROM events ORDER BY sequence"),
        start=1,
    ):
        if int(row["sequence"]) != event_count:
            raise IbrecError(
                f"Event sequence is not contiguous at expected sequence {event_count}."
            )
        record = {
            "sequence": row["sequence"],
            "created_at_utc": row["created_at_utc"],
            "event_type": row["event_type"],
            "payload_json": row["payload_json"],
        }
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError as exc:
            raise IbrecError(f"Event {row['sequence']} has invalid payload JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise IbrecError(f"Event {row['sequence']} payload must be a JSON object.")
        if _canonical_json(payload) != str(row["payload_json"]):
            raise IbrecError(f"Event {row['sequence']} payload JSON is not canonical.")
        expected_hash = _hash_json(record)
        if str(row["record_hash"]) != expected_hash:
            raise IbrecError(f"Event integrity hash mismatch at sequence {row['sequence']}.")
        event_chain = _chain_hash(event_chain, expected_hash)
        if str(row["chain_hash"]) != event_chain:
            raise IbrecError(f"Event chain hash mismatch at sequence {row['sequence']}.")
        event = _quality_event(
            sequence=int(row["sequence"]),
            created_at_utc=row["created_at_utc"],
            event_type=row["event_type"],
            payload=payload,
        )
        if event is not None:
            quality_events.append(event)
    if event_chain != str(source.get("event_chain_hash", _ZERO_HASH)):
        raise IbrecError("Final event integrity chain does not match the manifest.")

    for period_id, period in raw_periods.items():
        expected = _hash_json({name: period.get(name) for name in _RTH_HASH_FIELDS})
        if str(period.get("record_hash")) != expected:
            raise IbrecError(f"RTH period integrity hash mismatch for period {period_id}.")
        stats = period_stats.get(period_id, {"count": 0, "first": None, "last": None})
        if int(period.get("tick_count") or 0) != int(stats["count"]):
            raise IbrecError(f"RTH period {period_id} tick_count does not match its ticks.")
        if period.get("first_tick_sequence") != stats["first"]:
            raise IbrecError(f"RTH period {period_id} first tick sequence is inconsistent.")
        if period.get("last_tick_sequence") != stats["last"]:
            raise IbrecError(f"RTH period {period_id} last tick sequence is inconsistent.")
    digest_payload = [
        {name: raw_periods[period_id].get(name) for name in (*_RTH_HASH_FIELDS, "record_hash")}
        for period_id in sorted(raw_periods)
    ]
    period_digest = _hash_json(digest_payload)
    if period_digest != str(source.get("rth_period_digest", hashlib.sha256(b"[]").hexdigest())):
        raise IbrecError("RTH period digest does not match the manifest.")

    latest = connection.execute(
        "SELECT row_count, event_count, rth_period_count, tick_chain_hash, event_chain_hash, "
        "rth_period_digest, status FROM checkpoints ORDER BY checkpoint_id DESC LIMIT 1"
    ).fetchone()
    if latest is None:
        raise IbrecError("Format-v3 recording has no committed checkpoint.")
    expected_checkpoint = (
        int(manifest["row_count"]),
        event_count,
        int(manifest.get("rth_period_count", 0)),
        tick_chain,
        event_chain,
        period_digest,
        str(manifest.get("status") or ""),
    )
    if tuple(latest) != expected_checkpoint:
        raise IbrecError("Latest checkpoint metadata is inconsistent with committed content.")
    return quality_events


def _load_v3(
    path: Path,
    config: MarketReplayConfig,
    progress: ProgressCallback | None,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[IbrecTick],
    list[IbrecPeriod],
    list[str],
    list[dict[str, Any]],
]:
    issues: list[str] = []
    try:
        with closing(_open_v3_copy(path)) as connection:
            _validate_v3_schema(connection)
            manifest, version, contract = _validate_manifest(
                _read_v3_manifest(connection),
                container="sqlite",
            )
            if version != 3:
                raise IbrecError("Only Market Replay SQLite format version 3 is supported.")
            row_count = int(manifest["row_count"])
            if row_count > config.max_rows:
                raise IbrecError(
                    f"Recording contains {row_count:,} rows; configured limit is {config.max_rows:,}."
                )
            periods, raw_periods = _v3_periods(
                connection,
                int(manifest.get("rth_period_count", 0)),
                issues,
            )
            quality_events = _verify_v3_integrity(connection, manifest, raw_periods)
            if row_count == 0:
                raise IbrecError("Recording contains no market-data rows.")
            connection.row_factory = sqlite3.Row
            symbol = str(contract["symbol"])
            con_id = int(contract["con_id"])
            ticks: list[IbrecTick] = []
            query = "SELECT " + ", ".join((*_TICK_FIELDS, "rth_period_id")) + " FROM ticks ORDER BY sequence"
            for row_number, row in enumerate(connection.execute(query), start=1):
                ticks.append(
                    _tick_from_mapping(
                        dict(row),
                        row_number=row_number,
                        expected_symbol=symbol,
                        expected_con_id=con_id,
                        rth_period_id=row["rth_period_id"],
                    )
                )
                if row_number % 25_000 == 0:
                    _emit(progress, "Reading Market Replay v3 rows", row_number, row_count)
            ticks = _validate_tick_order(ticks, row_count, issues=issues)
            period_ids = {period.period_id for period in periods}
            for tick in ticks:
                if tick.rth_period_id is not None and tick.rth_period_id not in period_ids:
                    raise IbrecError(
                        f"Tick {tick.sequence} references unknown RTH period {tick.rth_period_id}."
                    )
            source = _object_dict(manifest.get("source"))
            if bool(source.get("rth_only_capture", False)):
                period_map = {period.period_id: period for period in periods}
                for tick in ticks:
                    if tick.rth_period_id is None:
                        raise IbrecError(
                            f"RTH-only recording tick {tick.sequence} has no RTH period."
                        )
                    period = period_map[tick.rth_period_id]
                    if not (period.open_timestamp <= tick.timestamp < period.close_timestamp):
                        raise IbrecError(
                            f"RTH-only recording tick {tick.sequence} is outside its scheduled period."
                        )
            status = str(manifest.get("status") or "").lower()
            if status != "complete":
                issues.append(
                    f"Recording status is {status or 'unknown'}; the final session can be incomplete and right-censored."
                )
            return manifest, contract, ticks, periods, issues, quality_events
    except sqlite3.DatabaseError as exc:
        raise IbrecError(f"Not a valid Market Replay v3 SQLite container: {exc}") from exc


def _derive_v2_periods(
    ticks: list[IbrecTick],
    contract: dict[str, Any],
    issues: list[str],
) -> list[IbrecPeriod]:
    timezone_name = str(
        contract.get("time_zone_id", contract.get("timeZoneId", "America/New_York"))
        or "America/New_York"
    )
    try:
        local_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        local_zone = timezone.utc
        issues.append(
            f"Contract time zone {timezone_name!r} was unavailable; v2 sessions were grouped by UTC date."
        )
    schedule = _parse_liquid_hours(str(contract.get("liquid_hours", contract.get("liquidHours", ""))), local_zone)
    groups: dict[str, list[IbrecTick]] = {}
    for tick in ticks:
        local_date = datetime.fromtimestamp(tick.timestamp, tz=timezone.utc).astimezone(local_zone).strftime("%Y%m%d")
        groups.setdefault(local_date, []).append(tick)
    periods: list[IbrecPeriod] = []
    for period_id, (session_date, rows) in enumerate(sorted(groups.items()), start=1):
        first = rows[0]
        last = rows[-1]
        schedule_pair = schedule.get(session_date)
        if schedule_pair is None:
            open_timestamp = first.timestamp
            close_timestamp = max(last.timestamp + 0.001, first.timestamp + 0.001)
            issues.append(
                f"No parseable liquid-hours interval was found for {session_date}; observed bounds were used."
            )
        else:
            open_timestamp, close_timestamp = schedule_pair
        periods.append(
            IbrecPeriod(
                period_id=period_id,
                session_date=session_date,
                schedule_open_utc=datetime.fromtimestamp(open_timestamp, tz=timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                schedule_close_utc=datetime.fromtimestamp(close_timestamp, tz=timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                open_timestamp=open_timestamp,
                close_timestamp=close_timestamp,
                observed_start_utc=first.captured_at_utc,
                observed_end_utc=last.captured_at_utc,
                observed_start_timestamp=first.timestamp,
                observed_end_timestamp=last.timestamp,
                status="derived",
                close_reason="legacy_v2_manifest_schedule" if schedule_pair else "observed_bounds",
                tick_count=len(rows),
            )
        )
    if not periods:
        raise IbrecError("No sessions could be derived from the Market Replay v2 recording.")
    return periods


def _parse_liquid_hours(value: str, local_zone: ZoneInfo | timezone) -> dict[str, tuple[float, float]]:
    result: dict[str, tuple[float, float]] = {}
    pattern = re.compile(r"(?P<open_date>\d{8}):(?P<open_time>\d{4})-(?P<close_date>\d{8}):(?P<close_time>\d{4})")
    for match in pattern.finditer(value):
        open_local = datetime.strptime(
            match.group("open_date") + match.group("open_time"),
            "%Y%m%d%H%M",
        ).replace(tzinfo=local_zone)
        close_local = datetime.strptime(
            match.group("close_date") + match.group("close_time"),
            "%Y%m%d%H%M",
        ).replace(tzinfo=local_zone)
        if close_local > open_local:
            result[match.group("open_date")] = (
                open_local.astimezone(timezone.utc).timestamp(),
                close_local.astimezone(timezone.utc).timestamp(),
            )
    return result


def _feed_counts(ticks: Iterable[IbrecTick]) -> dict[str, int]:
    labels = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed_frozen"}
    counts = {label: 0 for label in labels.values()}
    for tick in ticks:
        counts[labels[tick.market_data_type]] += 1
    return counts


def _strategy_tick_signature(tick: IbrecTick) -> tuple[Any, ...]:
    """Return fields that can change ATR, triggers, stop normalization, or fills."""

    return (
        tick.selected_price(),
        tick.bid,
        tick.ask,
        tick.last,
        tick.mark_price,
        tick.close,
        tick.market_data_type,
    )


def _retain_strategy_ticks(ticks: list[IbrecTick]) -> list[IbrecTick]:
    """Drop only redundant non-price events while preserving replay semantics.

    Market Replay rows are normalized snapshots. Size-, volume-, or high/low-only
    updates cannot alter the optimizer's selected price, native Last trigger,
    executable quote, or controller-side stop normalization. We nevertheless
    retain at least one usable state per UTC second so elapsed strategy gates and
    flat ATR bars are not lost. Every full snapshot, Last event, feed transition,
    price-state change, first row, and final row is retained.
    """

    if len(ticks) <= 2:
        return list(ticks)
    retained: list[IbrecTick] = []
    previous_signature: tuple[Any, ...] | None = None
    previous_feed: int | None = None
    last_retained_second: int | None = None
    final_index = len(ticks) - 1
    for index, tick in enumerate(ticks):
        signature = _strategy_tick_signature(tick)
        utc_second = math.floor(tick.timestamp)
        usable_state = tick.selected_price() is not None
        keep = (
            index == 0
            or index == final_index
            or tick.full_snapshot
            or tick.has_last_event()
            or tick.market_data_type != previous_feed
            or signature != previous_signature
            or (usable_state and utc_second != last_retained_second)
        )
        if keep:
            retained.append(tick)
            previous_signature = signature
            previous_feed = tick.market_data_type
            if usable_state:
                last_retained_second = utc_second
    return retained


def load_ibrec(
    config: MarketReplayConfig,
    *,
    progress: ProgressCallback | None = None,
) -> IbrecRecording:
    """Copy, verify, parse, and re-verify one version-2 or version-3 recording."""

    normalized = config.normalized()
    source = normalized.single_recording_path
    if not source.exists():
        raise IbrecError(f"Recording does not exist: {source}")
    if not source.is_file() or source.is_symlink():
        raise IbrecError("Recording path must be a regular non-symlink file.")
    if source.stat().st_size > normalized.max_input_bytes:
        raise IbrecError(
            f"Recording is {source.stat().st_size:,} bytes; configured limit is {normalized.max_input_bytes:,}."
        )
    with source.open("rb") as stream:
        source_header = stream.read(16)
    if source_header == _SQLITE_MAGIC:
        _reject_unsupported_sqlite_sidecars(source)
    journal_path = Path(str(source) + "-journal")
    initial_journal_present = journal_path.exists() or journal_path.is_symlink()
    _emit(progress, "Copying and hashing Market Replay recording", 0, 1)
    with tempfile.TemporaryDirectory(prefix="bouncybot-ibrec-") as temporary:
        copied, components = _copy_source_components(source, Path(temporary))
        total_component_bytes = sum(int(component["size"]) for component in components)
        if total_component_bytes > normalized.max_input_bytes:
            raise IbrecError(
                f"Recording components total {total_component_bytes:,} bytes; configured limit is "
                f"{normalized.max_input_bytes:,}."
            )
        with copied.open("rb") as stream:
            header = stream.read(16)
        if header == _SQLITE_MAGIC:
            container = "sqlite"
            (
                manifest,
                contract,
                ticks,
                periods,
                issues,
                quality_events,
            ) = _load_v3(copied, normalized, progress)
        elif zipfile.is_zipfile(copied):
            container = "zip"
            (
                manifest,
                contract,
                ticks,
                periods,
                issues,
                quality_events,
            ) = _load_v2(copied, normalized, progress)
        else:
            raise IbrecError("Input is neither a Market Replay v2 ZIP nor a v3 SQLite .ibrec container.")
        # Re-hash the original components after the complete parse. This catches
        # a writer that changed content after the private snapshot was created.
        for component in components:
            original = (
                source
                if component["role"] == "recording"
                else Path(str(source) + "-journal")
            )
            if not original.exists() or not original.is_file() or original.is_symlink():
                raise IbrecError(
                    f"Recording component became unavailable or unsafe during analysis: {component['name']}"
                )
            current_size, _, current_hash = _file_state(original)
            if current_size != component["size"] or current_hash != component["sha256"]:
                raise IbrecError(f"Recording changed during analysis: {component['name']}")
        journal_present = journal_path.exists() or journal_path.is_symlink()
        if journal_present != initial_journal_present:
            raise IbrecError("Recording rollback-journal state changed during analysis.")
        if source_header == _SQLITE_MAGIC:
            _reject_unsupported_sqlite_sidecars(source)
        raw_ticks = ticks
        feed_counts = _feed_counts(raw_ticks)
        if feed_counts["frozen"] or feed_counts["delayed_frozen"]:
            issues.append(
                "Frozen market-data rows are retained for audit but excluded from strategy optimization."
            )
        active_feed_types = [name for name, count in feed_counts.items() if count]
        if len(active_feed_types) > 1:
            issues.append(
                "Recording contains multiple market-data feed types: " + ", ".join(active_feed_types) + "."
            )
        crossed_quotes = sum(
            1
            for tick in raw_ticks
            if tick.bid is not None
            and tick.ask is not None
            and tick.bid > 0
            and tick.ask > 0
            and tick.ask < tick.bid
        )
        if crossed_quotes:
            issues.append(
                f"Detected {crossed_quotes:,} crossed bid/ask snapshot(s). Crossed touches are excluded from modeled fills and stop-reference normalization."
            )
        whole_file_hash = str(components[0]["sha256"])
        ticks = _retain_strategy_ticks(raw_ticks)
        if len(ticks) < len(raw_ticks):
            issues.append(
                f"Discarded {len(raw_ticks) - len(ticks):,} redundant size/volume-only event row(s) after full integrity validation. "
                "All Last events, full snapshots, price/feed changes, first/final rows, and at least one usable state per UTC second were retained."
            )
        recording = IbrecRecording(
            path=source,
            sha256=whole_file_hash,
            size_bytes=int(components[0]["size"]),
            input_components=components,
            container_format=container,
            format_version=int(manifest["format_version"]),
            manifest=manifest,
            contract=contract,
            ticks=ticks,
            periods=periods,
            issues=issues,
            feed_counts=feed_counts,
            raw_row_count=len(raw_ticks),
            retained_row_count=len(ticks),
            data_start_utc=ticks[0].captured_at_utc,
            data_end_utc=ticks[-1].captured_at_utc,
            quality_events=quality_events,
        )
        if recording.is_synthetic:
            recording.issues.append(
                "The manifest identifies this as synthetic sample data. Analysis is supported for software validation, "
                "but the recommendation cannot be treated as stable live-market evidence."
            )
        recording.issues = sorted(set(recording.issues))
        return recording


def inspect_ibrec(config: MarketReplayConfig) -> dict[str, Any]:
    """Perform a bounded structural preflight without loading all tick rows."""

    normalized = config.normalized()
    source = normalized.single_recording_path
    if not source.exists():
        raise IbrecError(f"Recording does not exist: {source}")
    if not source.is_file() or source.is_symlink():
        raise IbrecError("Recording path must be a regular non-symlink file.")
    if source.stat().st_size > normalized.max_input_bytes:
        raise IbrecError(
            f"Recording is {source.stat().st_size:,} bytes; configured limit is "
            f"{normalized.max_input_bytes:,}."
        )
    with source.open("rb") as stream:
        source_header = stream.read(16)
    if source_header == _SQLITE_MAGIC:
        _reject_unsupported_sqlite_sidecars(source)
    with tempfile.TemporaryDirectory(prefix="bouncybot-ibrec-preflight-") as temporary:
        copied, components = _copy_source_components(source, Path(temporary))
        total_component_bytes = sum(int(component["size"]) for component in components)
        if total_component_bytes > normalized.max_input_bytes:
            raise IbrecError(
                f"Recording components total {total_component_bytes:,} bytes; configured limit is "
                f"{normalized.max_input_bytes:,}."
            )
        with copied.open("rb") as stream:
            header = stream.read(16)
        if header == _SQLITE_MAGIC:
            with closing(_open_v3_copy(copied)) as connection:
                _validate_v3_schema(connection)
                manifest, version, contract = _validate_manifest(
                    _read_v3_manifest(connection),
                    container="sqlite",
                )
                checkpoint = connection.execute(
                    "SELECT status FROM checkpoints ORDER BY checkpoint_id DESC LIMIT 1"
                ).fetchone()
                if checkpoint is None:
                    raise IbrecError("Format-v3 recording has no committed checkpoint.")
                container = "sqlite"
                status = str(checkpoint[0] or manifest.get("status") or "")
        elif zipfile.is_zipfile(copied):
            with zipfile.ZipFile(copied, "r") as archive:
                infos = archive.infolist()
                if len(infos) > _MAX_ZIP_MEMBERS:
                    raise IbrecError(
                        f"Recording contains {len(infos):,} ZIP members; limit is "
                        f"{_MAX_ZIP_MEMBERS}."
                    )
                names = [info.filename for info in infos]
                duplicates = sorted(
                    name for name, count in Counter(names).items() if count > 1
                )
                if duplicates:
                    raise IbrecError(
                        "Recording contains duplicate ZIP members: " + ", ".join(duplicates)
                    )
                missing = {"manifest.json", "ticks.csv"}.difference(names)
                if missing:
                    raise IbrecError("Recording is missing: " + ", ".join(sorted(missing)))
                uncompressed = sum(int(info.file_size) for info in infos)
                if uncompressed > normalized.max_zip_uncompressed_bytes:
                    raise IbrecError(
                        f"Recording expands to {uncompressed:,} bytes; configured limit is "
                        f"{normalized.max_zip_uncompressed_bytes:,}."
                    )
                manifest, version, contract = _validate_manifest(
                    _read_small_json(archive, "manifest.json"),
                    container="zip",
                )
                container = "zip"
                status = str(manifest.get("status") or "")
        else:
            raise IbrecError(
                "Input is neither a Market Replay v2 ZIP nor a v3 SQLite .ibrec container."
            )
    source_metadata = _object_dict(manifest.get("source"))
    provenance_values = [manifest.get("notes"), *source_metadata.values()]
    synthetic = any("synthetic" in str(value or "").lower() for value in provenance_values)
    return {
        "path": str(source),
        "file_name": source.name,
        "content_sha256": _component_fingerprint(components),
        "container_format": container,
        "format_version": version,
        "symbol": str(contract.get("symbol") or "UNKNOWN"),
        "con_id": int(contract.get("con_id") or 0),
        "currency": _contract_text(contract, "currency").upper(),
        "security_type": _contract_text(
            contract,
            "sec_type",
            "secType",
            "security_type",
        ).upper(),
        "time_zone_id": _contract_text(
            contract,
            "time_zone_id",
            "timeZoneId",
        ),
        "min_tick": _positive_finite_float(
            contract.get("min_tick", contract.get("minTick")),
            field="manifest contract min_tick",
        ),
        "exchange": _contract_text(contract, "exchange").upper(),
        "primary_exchange": _contract_text(
            contract,
            "primary_exchange",
            "primaryExchange",
        ).upper(),
        "row_count": int(manifest.get("row_count") or 0),
        "rth_period_count": int(manifest.get("rth_period_count") or 0),
        "status": status or "unknown",
        "source_type": str(source_metadata.get("type") or "unknown"),
        "synthetic": synthetic,
        "size_bytes": int(components[0]["size"]),
        "component_size_bytes": total_component_bytes,
        "component_count": len(components),
    }


def _contract_text(contract: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = contract.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _recording_identity(recording: IbrecRecording) -> dict[str, Any]:
    contract = recording.contract
    raw_min_tick = contract.get("min_tick", contract.get("minTick"))
    min_tick = finite_float(raw_min_tick)
    return {
        "symbol": recording.symbol,
        "con_id": recording.con_id,
        "currency": _contract_text(contract, "currency").upper(),
        "security_type": _contract_text(
            contract,
            "sec_type",
            "secType",
            "security_type",
        ).upper(),
        "time_zone_id": _contract_text(
            contract,
            "time_zone_id",
            "timeZoneId",
        ),
        "min_tick": min_tick if min_tick is not None else math.nan,
    }


def _missing_identity_fields(identity: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not str(identity.get("symbol") or "").strip() or identity.get("symbol") == "UNKNOWN":
        missing.append("symbol")
    con_id = finite_int(identity.get("con_id")) or 0
    if con_id <= 0:
        missing.append("positive conId")
    for field, label in (
        ("currency", "currency"),
        ("security_type", "security type"),
        ("time_zone_id", "exchange time zone"),
    ):
        if not str(identity.get(field) or "").strip():
            missing.append(label)
    min_tick = finite_float(identity.get("min_tick"))
    if min_tick is None or min_tick <= 0:
        missing.append("positive minimum tick")
    return missing


def _recording_exchange_metadata(recording: IbrecRecording) -> tuple[str, str]:
    return (
        _contract_text(recording.contract, "exchange").upper(),
        _contract_text(
            recording.contract,
            "primary_exchange",
            "primaryExchange",
        ).upper(),
    )


def _period_ticks_for_combination(
    recording: IbrecRecording,
    period: IbrecPeriod,
) -> list[IbrecTick]:
    if recording.format_version == 3:
        return [
            tick
            for tick in recording.ticks
            if tick.rth_period_id == period.period_id
        ]
    return [
        tick
        for tick in recording.ticks
        if period.open_timestamp <= tick.timestamp <= period.close_timestamp
    ]


@dataclass(slots=True, frozen=True)
class _FragmentCandidate:
    """One verified same-date recording fragment considered for stitching."""

    period: IbrecPeriod
    ticks: tuple[IbrecTick, ...]
    fingerprint: str
    start: float
    end: float
    live_only: bool
    normal_close: bool
    clock_monotonic: bool

    @property
    def coverage_ns(self) -> int:
        """Return observed wall-clock coverage without depending on file order."""

        return max(0, int(round((self.end - self.start) * 1_000_000_000)))

    @property
    def signature(self) -> tuple[float, float, str, int]:
        return (self.start, self.end, self.fingerprint, self.period.period_id)


@dataclass(slots=True, frozen=True)
class _FragmentSelection:
    """Dynamic-programming state for deterministic interval selection."""

    coverage_ns: int = 0
    live_coverage_ns: int = 0
    normal_close_count: int = 0
    retained_rows: int = 0
    indices: tuple[int, ...] = ()


def _boundary_tick_signature(tick: IbrecTick) -> tuple[Any, ...]:
    """Return replay-relevant boundary state without recorder-local counters."""

    return (
        tick.captured_at_utc,
        tick.timestamp,
        tick.symbol,
        tick.con_id,
        tick.source_time_utc,
        tick.bid,
        tick.bid_size,
        tick.ask,
        tick.ask_size,
        tick.last,
        tick.last_size,
        tick.open,
        tick.high,
        tick.low,
        tick.close,
        tick.volume,
        tick.mark_price,
        tick.market_data_type,
        tick.changed_fields,
        tick.full_snapshot,
    )


def _fragments_are_compatible(
    left: _FragmentCandidate,
    right: _FragmentCandidate,
) -> bool:
    """Return whether fragments can be joined without conflicting coverage."""

    if not left.clock_monotonic or not right.clock_monotonic:
        # A receipt-clock reversal is valid single-recording recovery evidence,
        # but it cannot safely establish chronology against another recorder.
        return False
    if left.end < right.start:
        return True
    if left.end > right.start:
        return False
    # Equal boundary timestamps are safe only when both recorders captured the
    # exact same replay-relevant state.  The duplicate boundary row is removed
    # during stitching.  Conflicting same-instant states remain overlapping.
    return _boundary_tick_signature(left.ticks[-1]) == _boundary_tick_signature(
        right.ticks[0]
    )


def _prefer_fragment_selection(
    left: _FragmentSelection,
    right: _FragmentSelection,
    order: list[_FragmentCandidate],
) -> _FragmentSelection:
    """Prefer coverage, then evidence rows, then fewer stitches, deterministically.

    Retained-row count alone is not a safe objective: a dense partial feed can
    contain more callbacks than a sparse complete recording.  Primary strategy
    reconstruction benefits first from the widest verified time coverage.
    Equal-coverage choices prefer live-only evidence and normal-close evidence,
    then row count, fewer fragments (less synthetic stitching), and finally a
    content-derived lexical signature.
    """

    left_rank = (
        left.coverage_ns,
        left.live_coverage_ns,
        left.normal_close_count,
        left.retained_rows,
        -len(left.indices),
    )
    right_rank = (
        right.coverage_ns,
        right.live_coverage_ns,
        right.normal_close_count,
        right.retained_rows,
        -len(right.indices),
    )
    if left_rank != right_rank:
        return left if left_rank > right_rank else right
    left_signature = tuple(order[index].signature for index in left.indices)
    right_signature = tuple(order[index].signature for index in right.indices)
    return left if left_signature <= right_signature else right


def _select_non_overlapping_fragments(
    fragments: list[_FragmentCandidate],
) -> tuple[list[_FragmentCandidate], list[_FragmentCandidate]]:
    """Choose the best deterministic non-overlapping same-date fragment set.

    The dynamic program maximizes observed wall-clock coverage first and
    retained strategy evidence second.  Ranges touching at a boundary are
    compatible; only strict overlap conflicts.  Exact ties prefer fewer
    stitches and then a content-derived lexical signature, so input order,
    path, and filename cannot affect the result.
    """

    order = sorted(
        fragments,
        key=lambda item: (
            item.end,
            item.start,
            item.fingerprint,
            item.period.period_id,
        ),
    )
    # Ordinary weighted interval scheduling can retain only one best prefix for
    # each end-time index. That is insufficient here because fragments that
    # merely touch are compatible only when their replay-relevant boundary
    # rows agree. Two fragments can share the same end timestamp while only
    # one can precede the next fragment. Keep the best chain ending at every
    # concrete fragment so that boundary compatibility is checked against the
    # chain's actual final member rather than an unrelated best prefix.
    ending_at: list[_FragmentSelection] = []
    best_overall = _FragmentSelection()
    for index, candidate in enumerate(order):
        best_ending_here = _FragmentSelection(
            coverage_ns=candidate.coverage_ns,
            live_coverage_ns=(candidate.coverage_ns if candidate.live_only else 0),
            normal_close_count=int(candidate.normal_close),
            retained_rows=len(candidate.ticks),
            indices=(index,),
        )
        for earlier_index in range(index):
            if not _fragments_are_compatible(order[earlier_index], candidate):
                continue
            previous = ending_at[earlier_index]
            include = _FragmentSelection(
                coverage_ns=previous.coverage_ns + candidate.coverage_ns,
                live_coverage_ns=(
                    previous.live_coverage_ns
                    + (candidate.coverage_ns if candidate.live_only else 0)
                ),
                normal_close_count=(
                    previous.normal_close_count + int(candidate.normal_close)
                ),
                retained_rows=previous.retained_rows + len(candidate.ticks),
                indices=(*previous.indices, index),
            )
            best_ending_here = _prefer_fragment_selection(
                include,
                best_ending_here,
                order,
            )
        ending_at.append(best_ending_here)
        best_overall = _prefer_fragment_selection(
            best_ending_here,
            best_overall,
            order,
        )
    kept_indices = set(best_overall.indices)
    kept = sorted(
        (order[index] for index in kept_indices),
        key=lambda item: (
            item.start,
            item.fingerprint,
            item.period.period_id,
        ),
    )
    dropped = [
        order[index] for index in range(len(order)) if index not in kept_indices
    ]
    return kept, dropped


def combine_ibrec_recordings(
    recordings: Iterable[IbrecRecording],
) -> IbrecRecording:
    """Combine verified recordings into one deterministic analysis dataset.

    Each calendar date is represented by exactly one RTH period in the
    combined dataset.  When several verified fragments cover one date, the
    deterministic maximal non-overlapping subset (coverage first, then retained
    strategy-relevant rows, with content-hash tie-breaking) is stitched
    chronologically into one period; recorder restarts therefore no longer
    discard the whole date.
    Fragments whose observed tick ranges overlap retained coverage are dropped
    with an explanatory issue: overlapping tick streams are never interleaved,
    because two capture processes can disagree tick-by-tick and would fabricate
    a market path neither recorder observed.  Inter-fragment gaps remain fully
    visible to the coverage, event-gap, and last-event quality gates, exactly
    as a data outage inside a single recording would be.
    """

    ordered = sorted(
        list(recordings),
        key=lambda item: (
            _component_fingerprint(item.input_components),
            item.data_start_utc,
            item.data_end_utc,
        ),
    )
    if not ordered:
        raise IbrecError("Select at least one verified Market Replay recording.")
    hashes = [_component_fingerprint(item.input_components) for item in ordered]
    if len(set(hashes)) != len(hashes):
        raise IbrecError("The same Market Replay recording content was selected more than once.")

    base_identity = _recording_identity(ordered[0])
    base_missing = _missing_identity_fields(base_identity)
    if len(ordered) == 1:
        if "symbol" in base_missing or "positive conId" in base_missing:
            raise IbrecError(
                "Market Replay analysis requires an explicit symbol and positive conId."
            )
    elif base_missing:
        raise IbrecError(
            "Multiple-recording analysis cannot prove instrument identity because the first "
            "recording is missing: " + ", ".join(base_missing)
        )
    for recording in ordered[1:]:
        identity = _recording_identity(recording)
        missing = _missing_identity_fields(identity)
        if missing:
            raise IbrecError(
                "Multiple-recording analysis cannot prove instrument identity because a "
                "recording is missing: " + ", ".join(missing)
            )
        mismatches: list[str] = []
        for key in ("symbol", "con_id", "currency", "security_type", "time_zone_id"):
            if identity[key] != base_identity[key]:
                mismatches.append(
                    f"{key}: {identity[key]!r} versus {base_identity[key]!r}"
                )
        identity_min_tick = finite_float(identity.get("min_tick"))
        base_min_tick = finite_float(base_identity.get("min_tick"))
        if (
            identity_min_tick is None
            or base_min_tick is None
            or not math.isclose(
                identity_min_tick,
                base_min_tick,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            mismatches.append(
                f"min_tick: {identity['min_tick']!r} versus {base_identity['min_tick']!r}"
            )
        if mismatches:
            raise IbrecError(
                "Selected recordings do not describe the same instrument: "
                + "; ".join(mismatches)
            )

    issues: list[str] = []
    exchange_values = sorted({_recording_exchange_metadata(item) for item in ordered})
    if len(exchange_values) > 1:
        issues.append(
            "Selected recordings use different exchange-routing metadata. Instrument identity matches by symbol, "
            "positive conId, currency, security type, time zone, and minimum tick, so the sessions were retained."
        )

    by_date: dict[str, list[tuple[IbrecRecording, IbrecPeriod]]] = {}
    for recording in ordered:
        for period in recording.periods:
            by_date.setdefault(period.session_date, []).append((recording, period))

    excluded_sessions: list[dict[str, Any]] = []
    combined_periods: list[IbrecPeriod] = []
    combined_ticks: list[IbrecTick] = []
    fragment_evidence: list[dict[str, Any]] = []
    next_sequence = 1
    next_period_id = 1
    merged_date_count = 0
    dropped_fragment_count = 0
    for session_date, values in sorted(by_date.items()):
        fragments: list[_FragmentCandidate] = []
        for recording, period in values:
            fingerprint = _component_fingerprint(recording.input_components)
            source_ticks = sorted(
                _period_ticks_for_combination(recording, period),
                key=lambda tick: (tick.elapsed_ns, tick.sequence),
            )
            if not source_ticks:
                if len(values) > 1:
                    issues.append(
                        f"Trading date {session_date}: a fragment from recording "
                        f"{fingerprint[:12]} retained no strategy-relevant rows and "
                        "was skipped."
                    )
                fragment_evidence.append(
                    {
                        "session_date": session_date,
                        "status": "dropped_no_rows",
                        "recording_sha256": fingerprint,
                        "source_period_id": period.period_id,
                        "merged_period_id": "",
                        "observed_start_utc": period.observed_start_utc,
                        "observed_end_utc": period.observed_end_utc,
                        "retained_rows": 0,
                        "reason": "The verified fragment retained no strategy-relevant rows.",
                    }
                )
                continue
            fragments.append(
                _FragmentCandidate(
                    period=period,
                    ticks=tuple(source_ticks),
                    fingerprint=fingerprint,
                    # Receipt timestamps can reverse when the recorder's UTC
                    # clock moves backwards.  The fragment remains useful as
                    # isolated quality evidence, but min/max bounds and an
                    # explicit monotonic flag are required so it cannot be
                    # stitched into a fabricated chronology.
                    start=min(tick.timestamp for tick in source_ticks),
                    end=max(tick.timestamp for tick in source_ticks),
                    live_only=bool(source_ticks)
                    and all(tick.market_data_type == 1 for tick in source_ticks),
                    normal_close=(
                        (
                            recording.format_version == 3
                            and period.status.strip().lower() == "closed"
                            and period.close_reason.strip().lower()
                            == "contract_liquid_hours"
                        )
                        or (
                            recording.format_version == 2
                            and str(recording.manifest.get("status") or "")
                            .strip()
                            .lower()
                            == "complete"
                            and period.close_reason.strip().lower()
                            == "legacy_v2_manifest_schedule"
                        )
                    ),
                    clock_monotonic=all(
                        right.timestamp >= left.timestamp
                        for left, right in zip(source_ticks, source_ticks[1:])
                    ),
                )
            )
        if not fragments:
            source_hashes = sorted(
                {
                    _component_fingerprint(recording.input_components)
                    for recording, _ in values
                }
            )
            excluded_sessions.append(
                {
                    "reason_type": "no_retained_rows",
                    "session_date": session_date,
                    "period_count": len(values),
                    "recording_hashes": source_hashes,
                    "reason": (
                        "The verified RTH period contains no retained strategy-relevant rows."
                        if len(values) == 1
                        else f"None of the {len(values)} same-date fragments retained a strategy-relevant row."
                    ),
                }
            )
            continue

        # Schedule compatibility is evaluated only across fragments that
        # retained strategy-relevant rows.  An empty recovery fragment must
        # not invalidate an otherwise valid date merely because its metadata
        # was incomplete or stale.
        schedules = sorted(
            {
                (fragment.period.open_timestamp, fragment.period.close_timestamp)
                for fragment in fragments
            }
        )
        reference_open, reference_close = schedules[0]
        if any(
            not math.isclose(open_timestamp, reference_open, rel_tol=0.0, abs_tol=1.0)
            or not math.isclose(
                close_timestamp,
                reference_close,
                rel_tol=0.0,
                abs_tol=1.0,
            )
            for open_timestamp, close_timestamp in schedules[1:]
        ):
            source_hashes = sorted(fragment.fingerprint for fragment in fragments)
            reason = (
                "Same-date fragments with retained strategy evidence disagree on "
                "the scheduled RTH open or close; the optimizer cannot safely "
                "stitch them into one strategy session."
            )
            excluded_sessions.append(
                {
                    "reason_type": "schedule_conflict",
                    "session_date": session_date,
                    "period_count": len(fragments),
                    "recording_hashes": source_hashes,
                    "reason": reason,
                }
            )
            for fragment in fragments:
                fragment_evidence.append(
                    {
                        "session_date": session_date,
                        "status": "excluded_schedule_conflict",
                        "recording_sha256": fragment.fingerprint,
                        "source_period_id": fragment.period.period_id,
                        "merged_period_id": "",
                        "observed_start_utc": fragment.period.observed_start_utc,
                        "observed_end_utc": fragment.period.observed_end_utc,
                        "retained_rows": len(fragment.ticks),
                        "reason": reason,
                    }
                )
            continue

        kept, dropped = _select_non_overlapping_fragments(fragments)
        for fragment in dropped:
            dropped_fragment_count += 1
            issues.append(
                f"Trading date {session_date}: dropped an overlapping fragment from "
                f"recording {fragment.fingerprint[:12]} "
                f"({len(fragment.ticks):,} rows, "
                f"{fragment.period.observed_start_utc} to "
                f"{fragment.period.observed_end_utc}); its observed range conflicts "
                "with retained coverage. Overlapping tick streams are never "
                "interleaved because two capture processes can disagree tick-by-tick."
            )
            fragment_evidence.append(
                {
                    "session_date": session_date,
                    "status": "dropped_overlap",
                    "recording_sha256": fragment.fingerprint,
                    "source_period_id": fragment.period.period_id,
                    "merged_period_id": next_period_id,
                    "observed_start_utc": fragment.period.observed_start_utc,
                    "observed_end_utc": fragment.period.observed_end_utc,
                    "retained_rows": len(fragment.ticks),
                    "reason": (
                        "Observed tick coverage overlaps the selected coverage-first "
                        "non-overlapping fragment set."
                    ),
                }
            )

        template = kept[0].period
        open_source = min(kept, key=lambda item: item.period.open_timestamp).period
        close_source = max(kept, key=lambda item: item.period.close_timestamp).period
        last_period = kept[-1].period
        fingerprints: list[str] = []
        for fragment in kept:
            if fragment.fingerprint not in fingerprints:
                fingerprints.append(fragment.fingerprint)
            fragment_evidence.append(
                {
                    "session_date": session_date,
                    "status": "retained_stitched" if len(kept) > 1 else "retained_single",
                    "recording_sha256": fragment.fingerprint,
                    "source_period_id": fragment.period.period_id,
                    "merged_period_id": next_period_id,
                    "observed_start_utc": fragment.period.observed_start_utc,
                    "observed_end_utc": fragment.period.observed_end_utc,
                    "retained_rows": len(fragment.ticks),
                    "reason": (
                        "Selected by the deterministic coverage-first maximal "
                        "non-overlapping fragment algorithm."
                    ),
                }
            )

        # Every fragment keeps its recorder-monotonic intra-fragment spacing.
        # Later fragments are shifted by the wall-clock offset of their first
        # retained tick so ATR buckets, quote ages, and event gaps measure the
        # true elapsed outage across the stitch, exactly as a mid-session data
        # outage inside a single recording would.  A strictly increasing clock
        # is enforced even if two recorder monotonic clocks drift.
        base_start_timestamp = kept[0].ticks[0].timestamp
        base_elapsed_ns = kept[0].ticks[0].elapsed_ns
        rebase = len(kept) > 1
        merged_tick_count = 0
        boundary_duplicate_count = 0
        previous_elapsed_ns: int | None = None
        previous_fragment: _FragmentCandidate | None = None
        for fragment in kept:
            fragment_ticks = fragment.ticks
            if (
                previous_fragment is not None
                and previous_fragment.end == fragment.start
                and _boundary_tick_signature(previous_fragment.ticks[-1])
                == _boundary_tick_signature(fragment.ticks[0])
            ):
                fragment_ticks = fragment_ticks[1:]
                boundary_duplicate_count += 1
            if not fragment_ticks:
                previous_fragment = fragment
                continue
            delta_ns = 0
            if rebase:
                wall_offset_ns = int(
                    round(
                        (fragment_ticks[0].timestamp - base_start_timestamp)
                        * 1_000_000_000
                    )
                )
                delta_ns = (
                    base_elapsed_ns + wall_offset_ns - fragment_ticks[0].elapsed_ns
                )
                if previous_elapsed_ns is not None:
                    first_rebased = fragment_ticks[0].elapsed_ns + delta_ns
                    if first_rebased <= previous_elapsed_ns:
                        delta_ns += previous_elapsed_ns + 1 - first_rebased
            for tick in fragment_ticks:
                combined_ticks.append(
                    replace(
                        tick,
                        sequence=next_sequence,
                        elapsed_ns=tick.elapsed_ns + delta_ns,
                        rth_period_id=next_period_id,
                    )
                )
                next_sequence += 1
                merged_tick_count += 1
            previous_elapsed_ns = fragment_ticks[-1].elapsed_ns + delta_ns
            previous_fragment = fragment

        combined_periods.append(
            replace(
                template,
                period_id=next_period_id,
                schedule_open_utc=open_source.schedule_open_utc,
                open_timestamp=open_source.open_timestamp,
                schedule_close_utc=close_source.schedule_close_utc,
                close_timestamp=close_source.close_timestamp,
                observed_start_utc=kept[0].period.observed_start_utc,
                observed_start_timestamp=kept[0].period.observed_start_timestamp,
                observed_end_utc=last_period.observed_end_utc,
                observed_end_timestamp=last_period.observed_end_timestamp,
                status=last_period.status,
                close_reason=last_period.close_reason,
                tick_count=merged_tick_count,
                source_recording_sha256="+".join(fingerprints),
                source_recording_sha256s=tuple(fingerprints),
            )
        )
        if len(kept) > 1:
            merged_date_count += 1
            issues.append(
                f"Merged trading date {session_date}: stitched {len(kept)} "
                f"non-overlapping fragment(s) from {len(fingerprints)} recording(s) "
                f"into one RTH period ({merged_tick_count:,} rows). Inter-fragment "
                "gaps remain visible to the coverage, event-gap, and last-event "
                "quality gates."
            )
        if boundary_duplicate_count:
            issues.append(
                f"Trading date {session_date}: removed {boundary_duplicate_count:,} "
                "identical duplicate boundary row(s) while stitching fragments."
            )
        next_period_id += 1
    if not combined_periods or not combined_ticks:
        raise IbrecError("No strategy-relevant RTH data remained after combining recordings.")
    # Summaries are emitted only after every date has been processed so counts
    # can never be understated.
    if merged_date_count:
        issues.append(
            f"Merged {merged_date_count:,} trading date(s) from multiple same-date "
            "fragments instead of excluding them."
        )
    if dropped_fragment_count:
        issues.append(
            f"Dropped {dropped_fragment_count:,} overlapping same-date fragment(s); "
            "those dates retain the deterministically selected non-overlapping "
            "fragment(s)."
        )
    if excluded_sessions:
        issues.append(
            f"Excluded {len(excluded_sessions):,} trading date(s) because no safe "
            "merged RTH period could be constructed; see excluded-session and "
            "fragment diagnostics."
        )

    for index, recording in enumerate(ordered, start=1):
        recording_fingerprint = _component_fingerprint(recording.input_components)
        issues.extend(
            f"Recording {index} ({recording_fingerprint[:12]}): {issue}"
            for issue in recording.issues
        )
    generic_components: list[dict[str, Any]] = []
    for index, recording in enumerate(ordered, start=1):
        recording_fingerprint = _component_fingerprint(recording.input_components)
        for component in sorted(
            recording.input_components,
            key=lambda item: (
                str(item.get("role") or ""),
                str(item.get("sha256") or ""),
            ),
        ):
            role = str(component.get("role") or "component")
            suffix = "" if role == "recording" else "-journal"
            generic_components.append(
                {
                    "role": role,
                    "recording_index": index,
                    "name": f"recording_{index:03d}.ibrec{suffix}",
                    "sha256": str(component.get("sha256") or ""),
                    "size": int(component.get("size") or 0),
                    "recording_content_sha256": recording_fingerprint,
                    "format_version": recording.format_version,
                    "container_format": recording.container_format,
                    "manifest_status": str(recording.manifest.get("status") or ""),
                    "data_start_utc": (
                        recording.data_start_utc if role == "recording" else ""
                    ),
                    "data_end_utc": (
                        recording.data_end_utc if role == "recording" else ""
                    ),
                }
            )
    combined_hash = hashlib.sha256(
        json.dumps(
            [
                {
                    "role": item["role"],
                    "recording_index": item["recording_index"],
                    "sha256": item["sha256"],
                    "size": item["size"],
                    "recording_content_sha256": item[
                        "recording_content_sha256"
                    ],
                    "format_version": item["format_version"],
                }
                for item in generic_components
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    formats = {recording.format_version for recording in ordered}
    feed_counts = {
        key: sum(recording.feed_counts.get(key, 0) for recording in ordered)
        for key in ("live", "frozen", "delayed", "delayed_frozen")
    }
    is_synthetic = any(recording.is_synthetic for recording in ordered)
    manifest = {
        "format_name": _FORMAT_NAME,
        "format_version": next(iter(formats)) if len(formats) == 1 else 0,
        "status": "combined",
        "recording_count": len(ordered),
        "row_count": sum(recording.raw_row_count for recording in ordered),
        "rth_period_count": len(combined_periods),
        "source": {
            "type": "combined_verified_recordings",
            "synthetic": is_synthetic,
        },
        "notes": "synthetic" if is_synthetic else "",
        "contract": dict(ordered[0].contract),
    }
    first_tick = min(combined_ticks, key=lambda tick: tick.timestamp)
    last_tick = max(combined_ticks, key=lambda tick: tick.timestamp)
    combined_quality_events: list[dict[str, Any]] = []
    for recording in ordered:
        recording_fingerprint = _component_fingerprint(recording.input_components)
        for event in recording.quality_events:
            combined_quality_events.append(
                {
                    **event,
                    "source_recording_sha256": recording_fingerprint,
                }
            )
    combined_quality_events.sort(
        key=lambda item: (
            float(item.get("timestamp") or 0.0),
            str(item.get("source_recording_sha256") or ""),
            int(item.get("sequence") or 0),
        )
    )
    return IbrecRecording(
        path=Path("selected_recordings.ibrec"),
        sha256=combined_hash,
        size_bytes=sum(int(item["size"]) for item in generic_components),
        input_components=generic_components,
        container_format=(ordered[0].container_format if len(ordered) == 1 else "combined"),
        format_version=(next(iter(formats)) if len(formats) == 1 else 0),
        manifest=manifest,
        contract=dict(ordered[0].contract),
        ticks=combined_ticks,
        periods=combined_periods,
        issues=sorted(set(issues)),
        feed_counts=feed_counts,
        raw_row_count=sum(recording.raw_row_count for recording in ordered),
        retained_row_count=len(combined_ticks),
        data_start_utc=first_tick.captured_at_utc,
        data_end_utc=last_tick.captured_at_utc,
        excluded_sessions=excluded_sessions,
        quality_events=combined_quality_events,
        fragment_evidence=sorted(
            fragment_evidence,
            key=lambda item: (
                str(item.get("session_date") or ""),
                int(finite_int(item.get("merged_period_id")) or 0),
                str(item.get("observed_start_utc") or ""),
                str(item.get("status") or ""),
                str(item.get("recording_sha256") or ""),
                int(finite_int(item.get("source_period_id")) or 0),
            ),
        ),
    )


def _recording_set_error(path: Path, exc: Exception) -> IbrecError:
    """Attach one unambiguous filename to a per-recording set failure."""

    message = str(exc).strip() or exc.__class__.__name__
    if path.name.lower() in message.lower():
        return IbrecError(message)
    return IbrecError(f"{path.name}: {message}")


def load_ibrec_set(
    config: MarketReplayConfig,
    *,
    progress: ProgressCallback | None = None,
) -> IbrecRecording:
    """Load and combine one or more verified version-2/version-3 recordings."""

    normalized = config.normalized()
    paths = normalized.recording_paths
    main_size = sum(path.stat().st_size for path in paths if path.exists())
    if main_size > normalized.max_input_bytes:
        raise IbrecError(
            f"Selected recording files total {main_size:,} bytes; configured aggregate limit is "
            f"{normalized.max_input_bytes:,}."
        )
    recordings: list[IbrecRecording] = []
    cumulative_rows = 0
    cumulative_component_bytes = 0
    for index, path in enumerate(paths, start=1):
        _emit(progress, f"Loading Market Replay recording {index} of {len(paths)}", index - 1, len(paths))
        single = MarketReplayConfig(
            recording_path=path,
            output_root=normalized.output_root,
            max_rows=normalized.max_rows,
            max_input_bytes=normalized.max_input_bytes,
            max_zip_uncompressed_bytes=normalized.max_zip_uncompressed_bytes,
            max_recordings=1,
            min_atr_pct=normalized.min_atr_pct,
            max_atr_pct=normalized.max_atr_pct,
            assumed_trade_notional=normalized.assumed_trade_notional,
            execution_cost_bps_per_side=normalized.execution_cost_bps_per_side,
            buy_execution_cost_bps_per_side=(
                normalized.buy_execution_cost_bps_per_side
            ),
            sell_execution_cost_bps_per_side=(
                normalized.sell_execution_cost_bps_per_side
            ),
            execution_cost_overrides=normalized.execution_cost_overrides,
            trade_notional_overrides=normalized.trade_notional_overrides,
            turnover_penalty_bps_per_completed_trade=(
                normalized.turnover_penalty_bps_per_completed_trade
            ),
            execution_quote_max_age_seconds=(
                normalized.execution_quote_max_age_seconds
            ),
            entry_open_delay_seconds=normalized.entry_open_delay_seconds,
            entry_cutoff_seconds=normalized.entry_cutoff_seconds,
            buy_trail_cancel_seconds=normalized.buy_trail_cancel_seconds,
            session_boundary_tolerance_seconds=(
                normalized.session_boundary_tolerance_seconds
            ),
            max_market_event_gap_seconds=normalized.max_market_event_gap_seconds,
            min_last_event_minute_coverage_pct=(
                normalized.min_last_event_minute_coverage_pct
            ),
            max_last_event_gap_p95_seconds=(
                normalized.max_last_event_gap_p95_seconds
            ),
            min_touch_liquidity_coverage_pct=(
                normalized.min_touch_liquidity_coverage_pct
            ),
        )
        try:
            recording = load_ibrec(single, progress=progress)
        except (IbrecError, OSError) as exc:
            # Ten selected files and one bare message are undiagnosable; every
            # per-recording failure names its source file exactly once.
            raise _recording_set_error(path, exc) from exc
        cumulative_rows += recording.raw_row_count
        cumulative_component_bytes += sum(
            int(component.get("size") or 0)
            for component in recording.input_components
        )
        if cumulative_component_bytes > normalized.max_input_bytes:
            raise IbrecError(
                f"Selected recording components total {cumulative_component_bytes:,} bytes after file {index}; "
                f"configured aggregate limit is {normalized.max_input_bytes:,}."
            )
        if cumulative_rows > normalized.max_rows:
            raise IbrecError(
                f"Selected recordings contain {cumulative_rows:,} rows after file {index}; configured aggregate "
                f"limit is {normalized.max_rows:,}."
            )
        recordings.append(recording)
    _emit(progress, "Combining verified Market Replay recordings", len(paths), len(paths))
    return combine_ibrec_recordings(recordings)


def inspect_ibrec_set(config: MarketReplayConfig) -> dict[str, Any]:
    """Preflight one or more recordings and summarize their combined identity."""

    normalized = config.normalized()
    details: list[dict[str, Any]] = []
    for path in normalized.recording_paths:
        try:
            detail = inspect_ibrec(
                MarketReplayConfig(
                    recording_path=path,
                    output_root=normalized.output_root,
                    max_rows=normalized.max_rows,
                    max_input_bytes=normalized.max_input_bytes,
                    max_zip_uncompressed_bytes=normalized.max_zip_uncompressed_bytes,
                    max_recordings=1,
                    min_atr_pct=normalized.min_atr_pct,
                    max_atr_pct=normalized.max_atr_pct,
                )
            )
        except (IbrecError, OSError) as exc:
            raise _recording_set_error(path, exc) from exc
        if int(finite_int(detail.get("row_count")) or 0) <= 0:
            # Loading is guaranteed to reject an empty recording, so the
            # preflight must reject it too; otherwise the interface shows a
            # green "Ready" state that the analysis immediately contradicts.
            raise IbrecError(f"{path.name}: Recording contains no market-data rows.")
        details.append(detail)
    hashes: set[str] = set()
    symbol = str(details[0]["symbol"]).upper()
    con_id = finite_int(details[0].get("con_id")) or 0
    if not symbol or symbol == "UNKNOWN" or con_id <= 0:
        raise IbrecError(
            "Market Replay analysis requires an explicit symbol and positive conId."
        )
    if len(details) > 1:
        missing = [
            label
            for key, label in (
                ("currency", "currency"),
                ("security_type", "security type"),
                ("time_zone_id", "exchange time zone"),
            )
            if not str(details[0].get(key) or "").strip()
        ]
        min_tick = finite_float(details[0].get("min_tick"))
        if min_tick is None or min_tick <= 0:
            missing.append("positive minimum tick")
        if missing:
            raise IbrecError(
                "Multiple-recording analysis cannot prove instrument identity because the "
                "first recording is missing: " + ", ".join(missing)
            )
    identity_keys = (
        "symbol",
        "con_id",
        "currency",
        "security_type",
        "time_zone_id",
        "min_tick",
    )
    expected = {key: details[0][key] for key in identity_keys}
    for detail in details:
        digest = str(detail["content_sha256"])
        if digest in hashes:
            raise IbrecError("The same Market Replay recording content was selected more than once.")
        hashes.add(digest)
        mismatches: list[str] = []
        for key in identity_keys:
            if key != "min_tick":
                if detail[key] != expected[key]:
                    mismatches.append(key)
                continue
            detail_min_tick = finite_float(detail.get(key))
            expected_min_tick = finite_float(expected.get(key))
            if (
                detail_min_tick is None
                or expected_min_tick is None
                or not math.isclose(
                    detail_min_tick,
                    expected_min_tick,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                mismatches.append(key)
        if mismatches:
            raise IbrecError(
                "Selected recordings do not share the same instrument identity: "
                + ", ".join(mismatches)
            )
    total_rows = sum(int(item["row_count"]) for item in details)
    total_component_bytes = sum(
        int(item.get("component_size_bytes") or item["size_bytes"])
        for item in details
    )
    if total_rows > normalized.max_rows:
        raise IbrecError(
            f"Selected recordings declare {total_rows:,} rows; configured aggregate limit is {normalized.max_rows:,}."
        )
    if total_component_bytes > normalized.max_input_bytes:
        raise IbrecError(
            f"Selected recording components total {total_component_bytes:,} bytes; configured aggregate limit is "
            f"{normalized.max_input_bytes:,}."
        )
    return {
        "recording_count": len(details),
        "symbol": symbol,
        "con_id": con_id,
        "format_versions": sorted({int(item["format_version"]) for item in details}),
        "containers": sorted({str(item["container_format"]) for item in details}),
        "row_count": total_rows,
        "rth_period_count": sum(int(item["rth_period_count"]) for item in details),
        "size_bytes": total_component_bytes,
        "synthetic": any(bool(item["synthetic"]) for item in details),
        "recordings": details,
    }
