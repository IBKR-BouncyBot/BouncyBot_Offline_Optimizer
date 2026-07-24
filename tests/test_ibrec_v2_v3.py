from __future__ import annotations

import csv
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

import optimizer.ibrec as ibrec_module
from optimizer.ibrec import IbrecError, inspect_ibrec, load_ibrec
from optimizer.market_replay_models import MarketReplayConfig
from tests.market_replay_fixtures import make_ticks, write_v2, write_v3


def config(path: Path, tmp_path: Path, **values) -> MarketReplayConfig:
    return MarketReplayConfig(path, tmp_path / "reports", **values)


def test_loads_version_2_zip_and_derives_session(tmp_path: Path) -> None:
    path = write_v2(tmp_path / "v2.ibrec")
    recording = load_ibrec(config(path, tmp_path))
    assert recording.format_version == 2
    assert recording.container_format == "zip"
    assert recording.symbol == "AAPL"
    assert len(recording.ticks) == 121
    assert len(recording.periods) == 1
    assert recording.ticks[0].has_last_event()


def test_loads_version_3_sqlite_and_integrity_chains(tmp_path: Path) -> None:
    path = write_v3(tmp_path / "v3.ibrec")
    recording = load_ibrec(config(path, tmp_path))
    assert recording.format_version == 3
    assert recording.container_format == "sqlite"
    assert recording.con_id == 265598
    assert len(recording.periods) == 1
    assert recording.periods[0].tick_count == len(recording.ticks)


def test_crossed_touch_is_not_used_as_an_executable_quote(tmp_path: Path) -> None:
    ticks, periods = make_ticks()
    ticks[0]["bid"] = 101.0
    ticks[0]["ask"] = 100.0
    path = write_v3(tmp_path / "crossed.ibrec", ticks, periods)
    recording = load_ibrec(config(path, tmp_path))
    first = recording.ticks[0]
    assert first.valid_bid() is None
    assert first.valid_ask() is None
    assert first.executable_price("BUY") is None
    assert first.executable_price("SELL") is None
    assert any("crossed bid/ask" in issue for issue in recording.issues)


def test_preflight_reports_version_and_container_without_full_analysis(tmp_path: Path) -> None:
    v2 = inspect_ibrec(config(write_v2(tmp_path / "v2.ibrec"), tmp_path))
    v3 = inspect_ibrec(config(write_v3(tmp_path / "v3.ibrec"), tmp_path))
    assert (v2["format_version"], v2["container_format"]) == (2, "zip")
    assert (v3["format_version"], v3["container_format"]) == (3, "sqlite")
    assert v3["rth_period_count"] == 1
    assert v3["source_type"] == "test_fixture"
    assert v3["synthetic"] is False


def test_preflight_identifies_synthetic_v3_provenance(tmp_path: Path) -> None:
    path = write_v3(
        tmp_path / "synthetic.ibrec",
        source_overrides={"type": "synthetic_sample"},
        notes="Deterministic synthetic sample",
    )

    details = inspect_ibrec(config(path, tmp_path))

    assert details["source_type"] == "synthetic_sample"
    assert details["synthetic"] is True


def test_rejects_version_1_zip(tmp_path: Path) -> None:
    path = write_v2(tmp_path / "legacy.ibrec")
    with zipfile.ZipFile(path, "r") as source:
        members = {info.filename: source.read(info.filename) for info in source.infolist()}
    manifest = json.loads(members["manifest.json"])
    manifest["format_version"] = 1
    members["manifest.json"] = json.dumps(manifest).encode()
    checksums = json.loads(members["checksums.json"])
    import hashlib

    checksums["files"]["manifest.json"] = hashlib.sha256(members["manifest.json"]).hexdigest()
    members["checksums.json"] = json.dumps(checksums).encode()
    with zipfile.ZipFile(path, "w") as target:
        for name, value in members.items():
            target.writestr(name, value)
    with pytest.raises(IbrecError, match="supports versions 2 and 3"):
        load_ibrec(config(path, tmp_path))


def test_rejects_v2_member_checksum_mismatch(tmp_path: Path) -> None:
    path = write_v2(tmp_path / "bad.ibrec")
    with zipfile.ZipFile(path, "r") as source:
        members = {info.filename: source.read(info.filename) for info in source.infolist()}
    members["ticks.csv"] += b"corrupt"
    with zipfile.ZipFile(path, "w") as target:
        for name, value in sorted(members.items()):
            target.writestr(name, value)
    with pytest.raises(IbrecError, match="Checksum mismatch"):
        load_ibrec(config(path, tmp_path))


def test_rejects_v3_tick_hash_tampering(tmp_path: Path) -> None:
    path = write_v3(tmp_path / "tampered.ibrec")
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE ticks SET last=last+1 WHERE sequence=1")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(IbrecError, match="Tick integrity hash mismatch"):
        load_ibrec(config(path, tmp_path))


def test_rejects_v3_checkpoint_mismatch(tmp_path: Path) -> None:
    path = write_v3(tmp_path / "checkpoint.ibrec")
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE checkpoints SET row_count=row_count+1")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(IbrecError, match="checkpoint metadata"):
        load_ibrec(config(path, tmp_path))


def test_rejects_noncontiguous_v3_event_sequence_with_valid_hashes(tmp_path: Path) -> None:
    ticks, periods = make_ticks()
    path = write_v3(
        tmp_path / "event-sequence.ibrec",
        ticks,
        periods,
        events=[
            {
                "sequence": 2,
                "created_at_utc": ticks[0]["captured_at_utc"],
                "event_type": "fixture",
                "payload": {"ok": True},
            }
        ],
    )
    with pytest.raises(IbrecError, match="Event sequence is not contiguous"):
        load_ibrec(config(path, tmp_path))


def test_rejects_noncanonical_v3_event_payload_with_valid_hash(tmp_path: Path) -> None:
    ticks, periods = make_ticks()
    path = write_v3(
        tmp_path / "event-json.ibrec",
        ticks,
        periods,
        events=[
            {
                "sequence": 1,
                "created_at_utc": ticks[0]["captured_at_utc"],
                "event_type": "fixture",
                "payload_json": '{"z": 1}',
            }
        ],
    )
    with pytest.raises(IbrecError, match="payload JSON is not canonical"):
        load_ibrec(config(path, tmp_path))


def test_rejects_unknown_changed_field(tmp_path: Path) -> None:
    ticks, periods = make_ticks()
    ticks[0]["changed_fields"] = "last,mystery"
    path = write_v3(tmp_path / "unknown.ibrec", ticks, periods)
    with pytest.raises(IbrecError, match="unsupported changed_fields"):
        load_ibrec(config(path, tmp_path))


def test_blank_changed_fields_is_full_snapshot_and_quote_only_is_not_last_event(
    tmp_path: Path,
) -> None:
    ticks, periods = make_ticks()
    ticks[0]["changed_fields"] = ""
    ticks[1]["changed_fields"] = "bid,ask"
    path = write_v3(tmp_path / "events.ibrec", ticks, periods)
    recording = load_ibrec(config(path, tmp_path))
    assert recording.ticks[0].full_snapshot is True
    assert recording.ticks[0].has_last_event() is True
    assert recording.ticks[1].full_snapshot is False
    assert recording.ticks[1].has_last_event() is False


def test_redundant_size_only_rows_are_compressed_but_last_events_are_retained(
    tmp_path: Path,
) -> None:
    ticks, periods = make_ticks(points_per_session=5, step_seconds=1)
    base = ticks[0]["captured_at_utc"]
    timestamps = [
        base,
        base.replace("00.000Z", "00.100Z"),
        base.replace("00.000Z", "00.200Z"),
        base.replace("00.000Z", "00.300Z"),
        base.replace("00.000Z", "01.000Z"),
    ]
    for index, row in enumerate(ticks):
        row["captured_at_utc"] = timestamps[index]
        row["source_time_utc"] = timestamps[index]
        row["elapsed_ns"] = index * 100_000_000
        row["bid"] = 100.0
        row["ask"] = 100.02
        row["last"] = 100.01
        row["mark_price"] = 100.01
        row["close"] = 100.01
        row["changed_fields"] = "bid_size,ask_size,volume"
    ticks[0]["changed_fields"] = ""
    ticks[2]["changed_fields"] = "last,last_size"
    periods[0]["observed_start_utc"] = timestamps[0]
    periods[0]["observed_end_utc"] = timestamps[-1]
    path = write_v3(tmp_path / "compressed.ibrec", ticks, periods)
    recording = load_ibrec(config(path, tmp_path))
    assert recording.raw_row_count == 5
    assert recording.retained_row_count == 3
    assert [tick.sequence for tick in recording.ticks] == [1, 3, 5]
    assert recording.ticks[1].has_last_event() is True
    assert any("redundant size/volume-only" in issue for issue in recording.issues)


def test_rejects_noncontiguous_tick_sequence_even_with_valid_hashes(tmp_path: Path) -> None:
    ticks, _ = make_ticks()
    ticks[2]["sequence"] = 122
    path = write_v2(tmp_path / "sequence.ibrec", ticks)
    with pytest.raises(IbrecError, match="sequence is not contiguous"):
        load_ibrec(config(path, tmp_path))


def test_preserves_sequence_when_receipt_clock_moves_backwards(tmp_path: Path) -> None:
    ticks, periods = make_ticks()
    ticks[2]["captured_at_utc"] = ticks[0]["captured_at_utc"]
    path = write_v3(tmp_path / "time.ibrec", ticks, periods)
    recording = load_ibrec(config(path, tmp_path))
    assert [tick.sequence for tick in recording.ticks[:3]] == [1, 2, 3]
    assert any("captured_at_utc moved backwards" in issue for issue in recording.issues)


def test_rejects_elapsed_time_moving_backwards(tmp_path: Path) -> None:
    ticks, periods = make_ticks()
    ticks[2]["elapsed_ns"] = ticks[0]["elapsed_ns"]
    path = write_v3(tmp_path / "elapsed.ibrec", ticks, periods)
    with pytest.raises(IbrecError, match="elapsed_ns backwards"):
        load_ibrec(config(path, tmp_path))


def test_active_v3_period_without_observed_end_is_valid_and_censored(tmp_path: Path) -> None:
    ticks, periods = make_ticks(points_per_session=20)
    periods[0]["observed_end_utc"] = ""
    periods[0]["status"] = "active"
    periods[0]["close_reason"] = ""
    path = write_v3(
        tmp_path / "active.ibrec",
        ticks,
        periods,
        manifest_status="recording",
    )
    recording = load_ibrec(config(path, tmp_path))
    period = recording.periods[0]
    assert period.status == "active"
    assert period.observed_end_utc == ticks[-1]["captured_at_utc"]
    assert period.observed_end_timestamp == recording.ticks[-1].timestamp
    assert any("remains active" in issue for issue in recording.issues)


def test_active_v3_period_clamps_derived_end_after_receipt_clock_reversal(
    tmp_path: Path,
) -> None:
    ticks, periods = make_ticks(points_per_session=4, step_seconds=1)
    periods[0]["schedule_open_utc"] = "2026-01-05T14:29:00.000Z"
    periods[0]["observed_end_utc"] = ""
    periods[0]["status"] = "active"
    periods[0]["close_reason"] = ""
    ticks[-1]["captured_at_utc"] = "2026-01-05T14:29:59.000Z"
    ticks[-1]["source_time_utc"] = ticks[-1]["captured_at_utc"]
    path = write_v3(
        tmp_path / "active-clock-reversal.ibrec",
        ticks,
        periods,
        manifest_status="recording",
    )

    recording = load_ibrec(config(path, tmp_path))

    assert recording.periods[0].observed_end_utc == recording.periods[0].observed_start_utc
    assert any("derived active-period end was clamped" in issue for issue in recording.issues)
    assert any("captured_at_utc moved backwards" in issue for issue in recording.issues)


def test_source_replaced_by_symlink_during_analysis_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_v3(tmp_path / "source.ibrec")
    backup = tmp_path / "source-original.ibrec"
    original_loader = ibrec_module._load_v3

    def replacing_loader(*args, **kwargs):
        result = original_loader(*args, **kwargs)
        path.replace(backup)
        try:
            path.symlink_to(backup.name)
        except OSError as exc:
            backup.replace(path)
            pytest.skip(f"symlinks are unavailable in this environment: {exc}")
        return result

    monkeypatch.setattr(ibrec_module, "_load_v3", replacing_loader)
    with pytest.raises(IbrecError, match="became unavailable or unsafe"):
        load_ibrec(config(path, tmp_path))


def test_closed_v3_period_requires_observed_end(tmp_path: Path) -> None:
    ticks, periods = make_ticks(points_per_session=20)
    periods[0]["observed_end_utc"] = ""
    path = write_v3(tmp_path / "closed-no-end.ibrec", ticks, periods)
    with pytest.raises(IbrecError, match="observed_end_utc is required"):
        load_ibrec(config(path, tmp_path))


def test_rejects_symbol_mismatch(tmp_path: Path) -> None:
    ticks, periods = make_ticks()
    ticks[1]["symbol"] = "MSFT"
    path = write_v3(tmp_path / "symbol.ibrec", ticks, periods)
    with pytest.raises(IbrecError, match="does not match manifest"):
        load_ibrec(config(path, tmp_path))


def test_rejects_wrong_extension_and_symlink(tmp_path: Path) -> None:
    path = tmp_path / "recording.dat"
    path.write_bytes(b"not relevant")
    with pytest.raises(ValueError, match=".ibrec extension"):
        config(path, tmp_path).normalized()
    target = write_v3(tmp_path / "target.ibrec")
    link = tmp_path / "link.ibrec"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(IbrecError, match="non-symlink"):
        load_ibrec(config(link, tmp_path))


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_rejects_unsupported_sqlite_sidecars(tmp_path: Path, suffix: str) -> None:
    path = write_v3(tmp_path / "sidecar.ibrec")
    Path(str(path) + suffix).write_bytes(b"unexpected")
    with pytest.raises(IbrecError, match="Unsupported SQLite sidecar"):
        load_ibrec(config(path, tmp_path))


def test_rejects_invalid_v3_manifest_source_or_status(tmp_path: Path) -> None:
    source_path = write_v3(tmp_path / "source.ibrec")
    connection = sqlite3.connect(source_path)
    try:
        manifest = json.loads(
            connection.execute(
                "SELECT value_json FROM metadata WHERE key='manifest'"
            ).fetchone()[0]
        )
        manifest["source"] = []
        connection.execute(
            "UPDATE metadata SET value_json=? WHERE key='manifest'",
            (json.dumps(manifest, sort_keys=True, separators=(",", ":")),),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(IbrecError, match="source must contain"):
        load_ibrec(config(source_path, tmp_path))

    ticks, periods = make_ticks()
    periods[0]["status"] = "mystery"
    status_path = write_v3(tmp_path / "period-status.ibrec", ticks, periods)
    with pytest.raises(IbrecError, match="unsupported status"):
        load_ibrec(config(status_path, tmp_path))


def test_v2_rejects_duplicate_csv_column(tmp_path: Path) -> None:
    path = write_v2(tmp_path / "columns.ibrec")
    with zipfile.ZipFile(path, "r") as source:
        members = {info.filename: source.read(info.filename) for info in source.infolist()}
    text = members["ticks.csv"].decode("utf-8")
    header, remainder = text.split("\n", 1)
    members["ticks.csv"] = (header + ",last\n" + remainder).encode()
    rows = list(csv.reader(io.StringIO(members["ticks.csv"].decode())))
    assert rows[0].count("last") == 2
    import hashlib

    checksums = json.loads(members["checksums.json"])
    checksums["files"]["ticks.csv"] = hashlib.sha256(members["ticks.csv"]).hexdigest()
    members["checksums.json"] = json.dumps(checksums).encode()
    with zipfile.ZipFile(path, "w") as target:
        for name, value in sorted(members.items()):
            target.writestr(name, value)
    with pytest.raises(IbrecError, match="duplicate columns"):
        load_ibrec(config(path, tmp_path))


def test_input_and_row_limits_are_enforced(tmp_path: Path) -> None:
    path = write_v3(tmp_path / "limits.ibrec")
    with pytest.raises(ValueError, match="at least 100"):
        config(path, tmp_path, max_rows=99).normalized()
    with pytest.raises(IbrecError, match="configured limit"):
        load_ibrec(config(path, tmp_path, max_rows=100))


@pytest.mark.parametrize("value", [100.5, True, float("inf"), "100.5"])
def test_integer_limits_reject_fractional_boolean_or_nonfinite_values(
    tmp_path: Path,
    value: object,
) -> None:
    path = write_v3(tmp_path / "strict-limits.ibrec")
    with pytest.raises(ValueError, match="max_rows must be an integer"):
        config(path, tmp_path, max_rows=value).normalized()


def test_empty_v3_is_valid_for_preflight_but_not_analysis(tmp_path: Path) -> None:
    path = write_v3(tmp_path / "empty.ibrec", [], [])
    details = inspect_ibrec(config(path, tmp_path))
    assert details["row_count"] == 0
    assert details["format_version"] == 3
    with pytest.raises(IbrecError, match="no market-data rows"):
        load_ibrec(config(path, tmp_path))


def test_output_cannot_replace_or_be_nested_below_recording(tmp_path: Path) -> None:
    path = write_v3(tmp_path / "recording.ibrec")
    with pytest.raises(ValueError, match="output cannot replace"):
        MarketReplayConfig(path, path).normalized()
    with pytest.raises(ValueError, match="nested under"):
        MarketReplayConfig(path, path / "reports").normalized()
