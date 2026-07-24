"""Safe inventory and parsing of BouncyBot market-data capture ZIP files."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import zipfile
from pathlib import Path
from typing import Any, Iterable

from .models import CaptureData, CaptureMeta, PricePoint, ProgressCallback
from .utils import finite_float, timestamp_seconds, truthy

_KNOWN_JSON_MEMBERS = ("manifest.json", "event.json")
_PRICE_JSONL = "market_data.jsonl"
_PRICE_CSV = "market_data.csv"
_MAX_ARCHIVE_MEMBERS = 128


class CaptureFormatError(RuntimeError):
    """Raised when a capture cannot safely be parsed."""


def _add_issue(meta: CaptureMeta, text: str) -> None:
    """Record one diagnostic once, even when an archive is retried."""

    if text not in meta.issues:
        meta.issues.append(text)


def _validated_members(
    archive: zipfile.ZipFile,
    maximum_uncompressed_bytes: int,
) -> set[str]:
    infos = archive.infolist()
    if len(infos) > _MAX_ARCHIVE_MEMBERS:
        raise CaptureFormatError(
            f"archive contains {len(infos)} members, above the configured "
            f"{_MAX_ARCHIVE_MEMBERS}-member limit"
        )
    total = sum(max(0, int(info.file_size)) for info in infos)
    if total > maximum_uncompressed_bytes:
        raise CaptureFormatError(
            f"archive expands to {total:,} bytes, above the configured "
            f"{maximum_uncompressed_bytes:,}-byte limit"
        )
    names = [info.filename for info in infos]
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        raise CaptureFormatError(
            f"archive contains duplicate member name(s): {', '.join(duplicates)}"
        )
    return set(names)


def _read_limited(archive: zipfile.ZipFile, member: str, maximum: int) -> bytes:
    info = archive.getinfo(member)
    if info.file_size > maximum:
        raise CaptureFormatError(f"{member} exceeds the configured uncompressed-size limit")
    with archive.open(info, "r") as handle:
        payload = handle.read(maximum + 1)
    if len(payload) > maximum:
        raise CaptureFormatError(f"{member} exceeded the configured read limit")
    return payload


def _json_object(payload: bytes, member: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureFormatError(f"{member} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CaptureFormatError(f"{member} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_capture(
    path: Path,
    *,
    max_archive_uncompressed_bytes: int,
    hash_file: bool,
) -> CaptureMeta:
    path = Path(path)
    meta = CaptureMeta(path=path)
    # Preserve a useful scope for corrupt archives by inferring the standard
    # debug_captures/<TICKER>/cycle_<N>/ layout before opening the ZIP.
    if path.parent.name.lower().startswith("cycle_"):
        meta.ticker = path.parent.parent.name.strip().upper()
        meta.cycle_number = _int_or_none(path.parent.name.split("_", 1)[1])
    try:
        meta.archive_bytes = path.stat().st_size
        with zipfile.ZipFile(path, "r") as archive:
            names = _validated_members(
                archive,
                max_archive_uncompressed_bytes,
            )
            meta.uncompressed_bytes = sum(
                max(0, int(info.file_size)) for info in archive.infolist()
            )
            missing = [name for name in _KNOWN_JSON_MEMBERS if name not in names]
            if missing:
                raise CaptureFormatError(f"missing required member(s): {', '.join(missing)}")
            manifest = _json_object(_read_limited(archive, "manifest.json", 2 * 1024 * 1024), "manifest.json")
            event = _json_object(_read_limited(archive, "event.json", 8 * 1024 * 1024), "event.json")
            meta.manifest = manifest
            meta.event = event
            meta.ticker = str(
                manifest.get("ticker") or event.get("ticker") or meta.ticker
            ).strip().upper()
            meta.cycle_id = str(manifest.get("cycle_id") or _nested(event, "cycle", "id") or "").strip()
            manifest_cycle_number = _int_or_none(
                _first_present(
                    manifest.get("cycle_number"),
                    _nested(event, "cycle", "cycle_number"),
                )
            )
            if manifest_cycle_number is not None:
                meta.cycle_number = manifest_cycle_number
            meta.event_type = str(manifest.get("event_type") or event.get("event_type") or "").strip().upper()
            meta.event_time_utc = str(event.get("event_time_utc") or manifest.get("started_at_utc") or "").strip()
            meta.order_ref = str(manifest.get("order_ref") or event.get("order_ref") or "").strip()
            meta.perm_id = _int_or_none(
                _first_present(manifest.get("perm_id"), event.get("perm_id"))
            )
            meta.rows_declared = _int_or_none(manifest.get("rows"))
            meta.first_row_utc = str(manifest.get("first_row_utc") or "")
            meta.last_row_utc = str(manifest.get("last_row_utc") or "")
            meta.pre_window_seconds = finite_float(manifest.get("pre_window_seconds"))
            meta.post_window_seconds = finite_float(manifest.get("post_window_seconds"))
            if _PRICE_JSONL not in names and _PRICE_CSV not in names:
                raise CaptureFormatError("neither market_data.jsonl nor market_data.csv is present")
            if not meta.ticker:
                _add_issue(meta, "ticker is missing from the manifest")
            if not meta.cycle_id and meta.cycle_number is None:
                _add_issue(meta, "cycle identity is missing from the manifest")
            if timestamp_seconds(meta.event_time_utc) is None:
                _add_issue(meta, "event timestamp is missing or invalid")
    except (
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        KeyError,
        CaptureFormatError,
    ) as exc:
        _add_issue(meta, f"fatal: {exc}")
    if hash_file and meta.usable:
        try:
            meta.sha256 = _sha256(path)
        except OSError as exc:
            # A file that disappears or becomes unreadable after inspection is
            # not a stable analysis input. Treat it as fatal so it cannot count
            # toward usable coverage without a deterministic content hash.
            _add_issue(meta, f"fatal: capture hash could not be calculated: {exc}")
    return meta


def inventory_captures(
    root: Path,
    *,
    max_archive_uncompressed_bytes: int,
    hash_files: bool,
    progress: ProgressCallback | None = None,
) -> list[CaptureMeta]:
    root = Path(root).resolve()
    if not root.exists() or not root.is_dir():
        return []
    files: list[Path] = []
    for path in root.rglob("*.zip"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if root not in resolved.parents:
            raise ValueError(
                f"Capture archive resolves outside debug_captures: {path.name}"
            )
        files.append(path)
    files.sort(key=lambda path: path.relative_to(root).as_posix())
    result: list[CaptureMeta] = []
    total = len(files)
    for index, path in enumerate(files, start=1):
        if progress:
            progress(f"Inspecting capture {path.name}", index, total)
        result.append(
            inspect_capture(
                path,
                max_archive_uncompressed_bytes=max_archive_uncompressed_bytes,
                hash_file=hash_files,
            )
        )
    return result


def _nested(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_present(*values: Any) -> Any:
    """Return the first value that is not ``None``.

    Numeric metadata such as cycle number or permanent order ID can validly be
    zero in malformed/legacy evidence. Using boolean ``or`` would silently
    replace that explicit value with a nested fallback and make provenance
    depend on truthiness rather than field presence.
    """

    return next((value for value in values if value is not None), None)


def _int_or_none(value: Any) -> int | None:
    """Parse integer-like capture metadata without truncating fractions."""

    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


def _row_value(row: dict[str, Any], key: str, *nested: str) -> Any:
    if key in row:
        return row.get(key)
    dotted = ".".join((key, *nested)) if nested else key
    if dotted in row:
        return row.get(dotted)
    current: Any = row.get(key)
    for part in nested:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _price_point(row: dict[str, Any]) -> PricePoint | None:
    captured = str(row.get("captured_at_utc") or row.get("timestamp") or "").strip()
    timestamp = timestamp_seconds(captured)
    if timestamp is None:
        # monotonic_ts is process-local and cannot be compared with SQLite UTC
        # order/fill timestamps. Reject such rows rather than interpreting a
        # monotonic clock value as a Unix epoch.
        return None
    price = finite_float(row.get("price"))
    fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
    last = finite_float(fields.get("last") if fields else row.get("fields.last"))
    bid = finite_float(fields.get("bid") if fields else row.get("fields.bid"))
    ask = finite_float(fields.get("ask") if fields else row.get("fields.ask"))
    if price is None or price <= 0:
        candidates = [last, finite_float(fields.get("marketPrice") if fields else row.get("fields.marketPrice"))]
        price = next((value for value in candidates if value is not None and value > 0), None)
    if price is None or price <= 0:
        return None
    trigger = last if last is not None and last > 0 else price
    atr_pct = finite_float(row.get("atr_pct"))
    if atr_pct is None:
        atr_pct = finite_float(_nested(row, "atr", "atr_pct"))
    # Newer BouncyBot captures persist ``strategy_price_usable`` as the final,
    # order-driving usability decision. It already incorporates fresh-event,
    # upstream-connectivity and selected-price checks, so a lower-level flag
    # must not override a saved False value. Fall back through older schema
    # generations only when the authoritative field is absent.
    if "strategy_price_usable" in row:
        fresh = truthy(row.get("strategy_price_usable"))
    elif "api_data_received_in_latest_read" in row:
        fresh = truthy(row.get("api_data_received_in_latest_read"))
    elif "market_data_update_consumed" in row:
        fresh = truthy(row.get("market_data_update_consumed"))
    else:
        fresh = True
    return PricePoint(
        timestamp=float(timestamp),
        captured_at_utc=captured,
        price=float(price),
        trigger_price=float(trigger),
        bid=bid,
        ask=ask,
        atr_pct=atr_pct,
        stage=str(row.get("stage") or ""),
        fresh_update=fresh,
    )


def _iter_jsonl(payload: Iterable[bytes], max_rows: int) -> Iterable[dict[str, Any]]:
    for index, raw in enumerate(payload, start=1):
        if index > max_rows:
            raise CaptureFormatError(f"capture contains more than {max_rows:,} rows")
        if len(raw) > 4 * 1024 * 1024:
            raise CaptureFormatError(f"JSONL row {index} exceeds 4 MiB")
        try:
            value = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            yield {"__invalid__": True}
            continue
        yield value if isinstance(value, dict) else {"__invalid__": True}


def _limited_text_lines(
    payload: Iterable[str],
    *,
    maximum_line_characters: int = 4 * 1024 * 1024,
) -> Iterable[str]:
    for index, line in enumerate(payload, start=1):
        if len(line) > maximum_line_characters:
            raise CaptureFormatError(f"CSV physical line {index} exceeds 4 MiB")
        yield line


def _iter_csv(payload: Iterable[str], max_rows: int) -> Iterable[dict[str, Any]]:
    try:
        reader = csv.DictReader(_limited_text_lines(payload))
        for index, row in enumerate(reader, start=1):
            if index > max_rows:
                raise CaptureFormatError(
                    f"capture contains more than {max_rows:,} rows"
                )
            yield dict(row)
    except UnicodeDecodeError as exc:
        raise CaptureFormatError(f"market_data.csv is not valid UTF-8: {exc}") from exc
    except csv.Error as exc:
        raise CaptureFormatError(f"market_data.csv could not be parsed: {exc}") from exc


def _collect_price_points(
    rows: Iterable[dict[str, Any]],
) -> tuple[list[PricePoint], int, int]:
    points: list[PricePoint] = []
    raw_rows = 0
    invalid_rows = 0
    for row in rows:
        raw_rows += 1
        if row.get("__invalid__"):
            invalid_rows += 1
            continue
        point = _price_point(row)
        if point is None:
            invalid_rows += 1
            continue
        points.append(point)
    return points, raw_rows, invalid_rows


def load_capture(meta: CaptureMeta, *, max_rows: int, max_archive_uncompressed_bytes: int) -> CaptureData:
    data = CaptureData(meta=meta)
    if not meta.usable:
        return data
    try:
        with zipfile.ZipFile(meta.path, "r") as archive:
            names = _validated_members(
                archive,
                max_archive_uncompressed_bytes,
            )
            missing = [name for name in _KNOWN_JSON_MEMBERS if name not in names]
            if missing:
                raise CaptureFormatError(
                    f"missing required member(s): {', '.join(missing)}"
                )
            points: list[PricePoint] = []
            if _PRICE_JSONL in names:
                with archive.open(_PRICE_JSONL, "r") as handle:
                    points, data.raw_rows, data.invalid_rows = _collect_price_points(
                        _iter_jsonl(handle, max_rows)
                    )
            if not points and _PRICE_CSV in names:
                info = archive.getinfo(_PRICE_CSV)
                if info.file_size > max_archive_uncompressed_bytes:
                    raise CaptureFormatError(
                        f"{_PRICE_CSV} exceeds the configured uncompressed-size limit"
                    )
                with archive.open(info, "r") as raw_handle:
                    with io.TextIOWrapper(
                        raw_handle,
                        encoding="utf-8-sig",
                        errors="strict",
                        newline="",
                    ) as text_handle:
                        csv_points, csv_raw_rows, csv_invalid_rows = (
                            _collect_price_points(_iter_csv(text_handle, max_rows))
                        )
                if csv_points:
                    if _PRICE_JSONL in names:
                        _add_issue(
                            meta,
                            "market_data.jsonl contained no usable rows; market_data.csv was used",
                        )
                    points = csv_points
                    data.raw_rows = csv_raw_rows
                    data.invalid_rows = csv_invalid_rows
    except (
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        KeyError,
        CaptureFormatError,
    ) as exc:
        _add_issue(meta, f"fatal: could not load market rows: {exc}")
        return data

    # Sort only by timestamp. Python's sort is stable, so capture order is
    # retained for distinct market updates that share a timestamp.
    points.sort(key=lambda point: point.timestamp)
    if any(not point.fresh_update for point in points):
        points = [point for point in points if point.fresh_update]
    deduped: list[PricePoint] = []
    seen: set[tuple[Any, ...]] = set()
    for point in points:
        # Deduplicate only semantically identical rows. Earlier releases used
        # only (timestamp, Last/trigger price), which could discard a real
        # selected-price, bid, ask, ATR, or stage update at the same timestamp.
        key = (
            point.timestamp,
            point.price,
            point.trigger_price,
            point.bid,
            point.ask,
            point.atr_pct,
            point.stage,
            point.fresh_update,
        )
        if key in seen:
            data.duplicate_rows += 1
            continue
        seen.add(key)
        deduped.append(point)
    data.points = deduped
    if meta.rows_declared is not None and meta.rows_declared != data.raw_rows:
        _add_issue(
            meta,
            f"manifest declares {meta.rows_declared} rows but {data.raw_rows} rows were read"
        )
    if not data.points:
        _add_issue(meta, "fatal: no usable positive-price rows were found")
    return data
