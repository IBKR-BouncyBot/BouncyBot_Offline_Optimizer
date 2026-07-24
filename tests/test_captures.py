from __future__ import annotations

import json
import warnings
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import optimizer.captures as captures_module
from optimizer.captures import _int_or_none, inspect_capture, inventory_captures, load_capture
from tests.conftest import create_capture


def test_inspect_and_load_jsonl_capture(tmp_path: Path) -> None:
    path = create_capture(
        tmp_path,
        ticker="AAPL",
        cycle_id="cycle-1",
        cycle_number=1,
        event_type="BUY_FILL",
        event_time=datetime(2026, 1, 5, 14, 15, tzinfo=timezone.utc),
        order_ref="BUY-1",
        price_kind="buy",
    )
    meta = inspect_capture(path, max_archive_uncompressed_bytes=50_000_000, hash_file=True)
    assert meta.usable
    assert meta.ticker == "AAPL"
    assert meta.cycle_number == 1
    assert len(meta.sha256) == 64
    data = load_capture(meta, max_rows=1000, max_archive_uncompressed_bytes=50_000_000)
    assert data.raw_rows == 31
    assert len(data.points) == 31
    assert data.points[0].trigger_price > 0
    assert data.points[0].atr_pct == 0.5


def test_csv_fallback_and_inventory(tmp_path: Path) -> None:
    path = create_capture(
        tmp_path,
        ticker="MSFT",
        cycle_id="cycle-1",
        cycle_number=1,
        event_type="SELL_FILL",
        event_time=datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc),
        order_ref="SELL-1",
        price_kind="sell",
        include_jsonl=False,
    )
    metas = inventory_captures(
        tmp_path / "debug_captures",
        max_archive_uncompressed_bytes=50_000_000,
        hash_files=False,
    )
    assert [meta.path for meta in metas] == [path]
    data = load_capture(metas[0], max_rows=1000, max_archive_uncompressed_bytes=50_000_000)
    assert len(data.points) == 31


def test_capture_integer_parser_rejects_fractional_or_nonfinite_metadata() -> None:
    assert _int_or_none(14) == 14
    assert _int_or_none("14.0") == 14
    assert _int_or_none(14.5) is None
    assert _int_or_none(float("nan")) is None
    assert _int_or_none(float("inf")) is None
    assert _int_or_none(True) is None


def test_explicit_zero_metadata_is_not_replaced_by_nested_fallbacks(
    tmp_path: Path,
) -> None:
    path = tmp_path / "zero-metadata.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "ticker": "AAPL",
                    "event_type": "BUY_FILL",
                    "cycle_number": 0,
                    "perm_id": 0,
                }
            ),
        )
        archive.writestr(
            "event.json",
            json.dumps(
                {
                    "event_time_utc": "2026-01-01T00:00:00+00:00",
                    "cycle": {"cycle_number": 5},
                    "perm_id": 99,
                }
            ),
        )
        archive.writestr("market_data.jsonl", "")

    meta = inspect_capture(
        path,
        max_archive_uncompressed_bytes=50_000,
        hash_file=False,
    )
    assert meta.cycle_number == 0
    assert meta.perm_id == 0


def test_csv_capture_is_streamed_instead_of_read_as_one_large_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = create_capture(
        tmp_path,
        ticker="MSFT",
        cycle_id="cycle-1",
        cycle_number=1,
        event_type="SELL_FILL",
        event_time=datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc),
        order_ref="SELL-1",
        price_kind="sell",
        include_jsonl=False,
    )
    original = captures_module._read_limited

    def guarded_read(archive, member: str, maximum: int):
        assert member != "market_data.csv"
        return original(archive, member, maximum)

    monkeypatch.setattr(captures_module, "_read_limited", guarded_read)
    meta = inspect_capture(
        path,
        max_archive_uncompressed_bytes=50_000_000,
        hash_file=False,
    )
    data = load_capture(
        meta,
        max_rows=1000,
        max_archive_uncompressed_bytes=50_000_000,
    )
    assert len(data.points) == 31


def test_corrupt_and_oversized_archives_fail_closed(tmp_path: Path) -> None:
    corrupt = tmp_path / "bad.zip"
    corrupt.write_bytes(b"not a zip")
    meta = inspect_capture(corrupt, max_archive_uncompressed_bytes=1000, hash_file=False)
    assert not meta.usable
    assert meta.issues[0].startswith("fatal:")

    oversized = tmp_path / "large.zip"
    with zipfile.ZipFile(oversized, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"ticker": "AAPL"}))
        archive.writestr("event.json", "{}")
        archive.writestr("market_data.jsonl", "x" * 5000)
    meta = inspect_capture(oversized, max_archive_uncompressed_bytes=1000, hash_file=False)
    assert not meta.usable
    assert "above the configured" in meta.issues[0]


def test_duplicate_member_names_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"ticker": "AAPL"}))
            archive.writestr("manifest.json", json.dumps({"ticker": "MSFT"}))
            archive.writestr("event.json", "{}")
            archive.writestr("market_data.jsonl", "")
    meta = inspect_capture(path, max_archive_uncompressed_bytes=50_000, hash_file=False)
    assert not meta.usable
    assert "duplicate member" in meta.issues[0]


def test_invalid_rows_are_counted_and_no_path_is_extracted(tmp_path: Path) -> None:
    path = tmp_path / "capture.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"ticker": "AAPL", "event_type": "BUY_FILL", "rows": 2}))
        archive.writestr("event.json", json.dumps({"event_time_utc": "2026-01-01T00:00:00+00:00"}))
        archive.writestr("market_data.jsonl", "not-json\n{}\n")
        archive.writestr("../../should_not_exist", "x")
    meta = inspect_capture(path, max_archive_uncompressed_bytes=50_000, hash_file=False)
    data = load_capture(meta, max_rows=100, max_archive_uncompressed_bytes=50_000)
    assert data.invalid_rows == 2
    assert not (tmp_path.parent / "should_not_exist").exists()


def test_missing_members_row_limit_and_fresh_filtering(tmp_path: Path) -> None:
    missing = tmp_path / "missing.zip"
    with zipfile.ZipFile(missing, "w") as archive:
        archive.writestr("manifest.json", "{}")
    meta = inspect_capture(missing, max_archive_uncompressed_bytes=50_000, hash_file=False)
    assert not meta.usable
    assert "missing required" in meta.issues[0]

    limited = tmp_path / "limited.zip"
    rows = [
        {
            "captured_at_utc": f"2026-01-01T00:00:0{index}+00:00",
            "price": 100 + index,
            "fields": {"last": 100 + index},
            "market_data_update_consumed": index != 1,
        }
        for index in range(3)
    ]
    with zipfile.ZipFile(limited, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"ticker": "AAPL", "event_type": "BUY_FILL", "rows": 3}))
        archive.writestr("event.json", json.dumps({"event_time_utc": "2026-01-01T00:00:01+00:00"}))
        archive.writestr("market_data.jsonl", "".join(json.dumps(row) + "\n" for row in rows))
    meta = inspect_capture(limited, max_archive_uncompressed_bytes=50_000, hash_file=False)
    data = load_capture(meta, max_rows=2, max_archive_uncompressed_bytes=50_000)
    assert not meta.usable
    assert "more than 2 rows" in meta.issues[-1]

    meta = inspect_capture(limited, max_archive_uncompressed_bytes=50_000, hash_file=False)
    data = load_capture(meta, max_rows=10, max_archive_uncompressed_bytes=50_000)
    assert len(data.points) == 2


def test_invalid_utf8_csv_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "badcsv.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"ticker": "AAPL", "event_type": "BUY_FILL"}))
        archive.writestr("event.json", json.dumps({"event_time_utc": "2026-01-01T00:00:00+00:00"}))
        archive.writestr("market_data.csv", b"\xff\xfe\x00")
    meta = inspect_capture(path, max_archive_uncompressed_bytes=50_000, hash_file=False)
    load_capture(meta, max_rows=10, max_archive_uncompressed_bytes=50_000)
    assert not meta.usable
    assert "not valid UTF-8" in meta.issues[-1]


def test_capture_with_only_stale_rows_is_unusable(tmp_path: Path) -> None:
    path = tmp_path / "stale.zip"
    row = {
        "captured_at_utc": "2026-01-01T00:00:00+00:00",
        "price": 100.0,
        "fields": {"last": 100.0},
        "market_data_update_consumed": False,
        "strategy_price_usable": False,
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"ticker": "AAPL", "event_type": "BUY_FILL", "rows": 1}),
        )
        archive.writestr(
            "event.json",
            json.dumps({"event_time_utc": "2026-01-01T00:00:00+00:00"}),
        )
        archive.writestr("market_data.jsonl", json.dumps(row) + "\n")
    meta = inspect_capture(path, max_archive_uncompressed_bytes=50_000, hash_file=False)
    data = load_capture(meta, max_rows=10, max_archive_uncompressed_bytes=50_000)
    assert data.raw_rows == 1
    assert data.points == []
    assert any(issue.startswith("fatal: no usable") for issue in meta.issues)


def test_same_timestamp_distinct_market_updates_are_not_deduplicated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "same-time.zip"
    base = {
        "captured_at_utc": "2026-01-01T00:00:00+00:00",
        "fields": {"last": 100.0, "bid": 99.99, "ask": 100.01},
        "atr_pct": 0.5,
        "stage": "2_BUY_TRAIL_ACTIVE",
        "market_data_update_consumed": True,
    }
    first = {**base, "price": 100.0}
    selected_price_update = {**base, "price": 100.02}
    bid_ask_update = {
        **base,
        "price": 100.0,
        "fields": {"last": 100.0, "bid": 100.0, "ask": 100.02},
    }
    rows = [first, selected_price_update, bid_ask_update, first]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"ticker": "AAPL", "event_type": "BUY_FILL", "rows": 4}),
        )
        archive.writestr(
            "event.json",
            json.dumps({"event_time_utc": "2026-01-01T00:00:00+00:00"}),
        )
        archive.writestr(
            "market_data.jsonl",
            "".join(json.dumps(row) + "\n" for row in rows),
        )

    meta = inspect_capture(path, max_archive_uncompressed_bytes=50_000, hash_file=False)
    data = load_capture(meta, max_rows=10, max_archive_uncompressed_bytes=50_000)

    assert len(data.points) == 3
    assert data.duplicate_rows == 1
    assert [point.price for point in data.points] == [100.0, 100.02, 100.0]
    assert data.points[-1].bid == 100.0


def test_inventory_rejects_capture_symlink_that_escapes_root(tmp_path: Path) -> None:
    capture_root = tmp_path / "debug_captures"
    capture_root.mkdir()
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"outside")
    linked = capture_root / "linked.zip"
    try:
        linked.symlink_to(outside)
    except (OSError, NotImplementedError):
        return

    import pytest

    with pytest.raises(ValueError, match="resolves outside debug_captures"):
        inventory_captures(
            capture_root,
            max_archive_uncompressed_bytes=50_000,
            hash_files=False,
        )


def test_csv_is_used_when_jsonl_contains_no_usable_rows(tmp_path: Path) -> None:
    path = tmp_path / "jsonl-fallback.zip"
    csv_payload = (
        "captured_at_utc,price,fields.last,fields.bid,fields.ask,atr_pct,stage\n"
        "2026-01-01T00:00:00+00:00,100.0,100.0,99.99,100.01,0.5,2_BUY_TRAIL_ACTIVE\n"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"ticker": "AAPL", "event_type": "BUY_FILL", "rows": 1}),
        )
        archive.writestr(
            "event.json",
            json.dumps({"event_time_utc": "2026-01-01T00:00:00+00:00"}),
        )
        archive.writestr("market_data.jsonl", "not-json\n")
        archive.writestr("market_data.csv", csv_payload)

    meta = inspect_capture(path, max_archive_uncompressed_bytes=50_000, hash_file=False)
    data = load_capture(meta, max_rows=10, max_archive_uncompressed_bytes=50_000)

    assert len(data.points) == 1
    assert data.raw_rows == 1
    assert data.invalid_rows == 0
    assert data.points[0].trigger_price == 100.0
    assert any("market_data.csv was used" in issue for issue in meta.issues)
