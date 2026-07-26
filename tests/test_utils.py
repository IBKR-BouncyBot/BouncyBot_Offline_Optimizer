from __future__ import annotations

from datetime import timezone
from pathlib import Path

from optimizer.utils import (
    finite_float,
    finite_int,
    iso_from_timestamp,
    mean,
    median,
    parse_datetime,
    percentile,
    relative_or_absolute,
    safe_name,
    timestamp_seconds,
    truthy,
)


def test_datetime_and_numeric_helpers() -> None:
    parsed = parse_datetime("2026-01-01T12:00:00")
    assert parsed is not None and parsed.tzinfo == timezone.utc
    assert parse_datetime("bad") is None
    assert parse_datetime("") is None
    timestamp = timestamp_seconds("2026-01-01T12:00:00Z")
    assert timestamp is not None
    assert iso_from_timestamp(timestamp).startswith("2026-01-01T12:00:00")
    assert iso_from_timestamp(None) == ""
    assert finite_float("1.5") == 1.5
    assert finite_float(True) is None
    assert finite_float("bad") is None
    assert finite_float("inf") is None
    assert finite_int("14.0") == 14
    assert finite_int("14.5") is None
    assert finite_int(True) is None


def test_boolean_and_statistics_helpers() -> None:
    assert truthy(True)
    assert truthy(1)
    assert not truthy(2)
    assert truthy(0.5, default=True)
    assert truthy("yes")
    assert not truthy("off", default=True)
    assert truthy("unknown", default=True)
    assert median([1.0, 3.0, 2.0]) == 2.0
    assert median([]) is None
    assert mean([1.0, 3.0]) == 2.0
    assert mean([]) is None
    assert percentile([0.0, 10.0], 0.25) == 2.5
    assert percentile([5.0], 0.9) == 5.0
    assert percentile([], 0.5) is None


def test_name_and_relative_path_helpers(tmp_path: Path) -> None:
    assert safe_name("AAPL / unsafe") == "AAPL___unsafe"
    assert safe_name("...") == "unknown"
    assert safe_name("CON") == "_CON"
    assert safe_name("lpt1.csv") == "_lpt1.csv"
    child = tmp_path / "child.txt"
    assert relative_or_absolute(child, tmp_path) == "child.txt"
    outside = tmp_path.parent / "outside.txt"
    assert relative_or_absolute(outside, tmp_path) == str(outside.resolve())
