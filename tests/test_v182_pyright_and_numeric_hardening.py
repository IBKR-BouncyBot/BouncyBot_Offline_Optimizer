"""Regression coverage for the v1.8.2 Pyright and numeric-boundary audit."""

from __future__ import annotations

import hashlib
import json
import math
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from optimizer.ibrec import (
    IbrecError,
    _missing_identity_fields,
    _nonnegative_int,
    _quality_event,
    _recording_identity,
    load_ibrec,
)
from optimizer.market_replay_models import MarketReplayConfig
from optimizer.market_replay_quality import (
    _connectivity_events,
    _source_finalized,
    _source_format_version,
)
from optimizer.utils import finite_float, finite_int
from tests.market_replay_fixtures import write_v2, write_v3


def _config(path: Path, tmp_path: Path) -> MarketReplayConfig:
    return MarketReplayConfig(path, tmp_path / "reports")


def _rewrite_v2_manifest(path: Path, mutate) -> None:
    with zipfile.ZipFile(path, "r") as source:
        members = {info.filename: source.read(info.filename) for info in source.infolist()}
    manifest = json.loads(members["manifest.json"])
    mutate(manifest)
    members["manifest.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    checksums = json.loads(members["checksums.json"])
    checksums["files"]["manifest.json"] = hashlib.sha256(
        members["manifest.json"]
    ).hexdigest()
    members["checksums.json"] = (
        json.dumps(checksums, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name in sorted(members):
            target.writestr(name, members[name])


class _FloatOverflow:
    def __float__(self) -> float:
        raise OverflowError("synthetic overflow")


def test_shared_numeric_parsers_reject_bool_fraction_nonfinite_and_overflow() -> None:
    assert finite_float(True) is None
    assert finite_float(_FloatOverflow()) is None
    assert finite_float("1e999999") is None

    assert finite_int(True) is None
    assert finite_int(14.5) is None
    assert finite_int("14.0") == 14
    assert finite_int("9007199254740993") == 9_007_199_254_740_993
    assert finite_int(float(2**53 + 2)) is None
    assert finite_int("1e1000000") is None
    assert finite_int("NaN") is None
    assert finite_int("Infinity") is None


def test_ibrec_integer_parser_does_not_round_large_values() -> None:
    assert _nonnegative_int("9007199254740993", field="sequence") == 9_007_199_254_740_993
    with pytest.raises(IbrecError, match="non-negative integer"):
        _nonnegative_int("9007199254740993.5", field="sequence")


@pytest.mark.parametrize("bad_value", [None, 0, -0.01, True, "nan", "inf", "bad"])
def test_manifest_requires_a_positive_finite_minimum_tick(
    tmp_path: Path,
    bad_value: object,
) -> None:
    path = write_v2(tmp_path / "bad-min-tick.ibrec")

    def mutate(manifest: dict[str, object]) -> None:
        contract = manifest["contract"]
        assert isinstance(contract, dict)
        if bad_value is None:
            contract.pop("min_tick", None)
        else:
            contract["min_tick"] = bad_value

    _rewrite_v2_manifest(path, mutate)

    with pytest.raises(IbrecError, match="contract min_tick.*finite positive"):
        load_ibrec(_config(path, tmp_path))


def test_recording_identity_flags_missing_or_boolean_minimum_tick(tmp_path: Path) -> None:
    recording = load_ibrec(_config(write_v3(tmp_path / "source.ibrec"), tmp_path))

    recording.contract["min_tick"] = None
    identity = _recording_identity(recording)
    assert math.isnan(identity["min_tick"])
    assert "positive minimum tick" in _missing_identity_fields(identity)

    recording.contract["min_tick"] = True
    identity = _recording_identity(recording)
    assert math.isnan(identity["min_tick"])
    assert "positive minimum tick" in _missing_identity_fields(identity)


def test_unknown_component_format_cannot_be_treated_as_finalized_v3(tmp_path: Path) -> None:
    recording = load_ibrec(_config(write_v3(tmp_path / "source.ibrec"), tmp_path))
    period = replace(
        recording.periods[0],
        source_recording_sha256=recording.sha256,
        status="closed",
        close_reason="contract_liquid_hours",
    )
    recording.format_version = 0
    recording.input_components[0]["recording_content_sha256"] = recording.sha256
    recording.input_components[0]["format_version"] = None

    assert _source_format_version(recording, period) == 0
    assert (
        _source_finalized(
            recording,
            period,
            end_lead_seconds=0.0,
            tolerance_seconds=120.0,
        )
        is False
    )

    recording.input_components[0]["format_version"] = "3.0"
    assert _source_format_version(recording, period) == 3
    assert _source_finalized(
        recording,
        period,
        end_lead_seconds=0.0,
        tolerance_seconds=120.0,
    )


def test_connectivity_quality_parsing_is_strict_and_finite(tmp_path: Path) -> None:
    recording = load_ibrec(_config(write_v3(tmp_path / "source.ibrec"), tmp_path))
    period = recording.periods[0]
    valid_timestamp = period.open_timestamp + 1.0
    recording.quality_events = [
        {"disconnect": "false", "timestamp": valid_timestamp},
        {"disconnect": True, "timestamp": None},
        {"disconnect": True, "timestamp": "nan"},
        {"disconnect": "true", "timestamp": valid_timestamp},
    ]

    assert _connectivity_events(recording, period) == [
        {"disconnect": "true", "timestamp": valid_timestamp}
    ]


def test_fractional_error_code_is_not_truncated_into_disconnect_code() -> None:
    event = _quality_event(
        sequence=1,
        created_at_utc="2026-01-05T14:30:00Z",
        event_type="broker_error",
        payload={"error_code": 1100.5},
    )
    assert event is None

    event = _quality_event(
        sequence=2,
        created_at_utc="2026-01-05T14:30:01Z",
        event_type="broker_error",
        payload={"error_code": "1100.0"},
    )
    assert event is not None
    assert event["error_code"] == 1100
    assert event["disconnect"] is True


@pytest.mark.parametrize(
    "field",
    [
        "continuous_overnight_replay",
        "calibration_use_execution_cost",
        "calibration_use_trade_notional",
    ],
)
def test_market_replay_boolean_options_fail_closed(field: str, tmp_path: Path) -> None:
    values = {field: "false"}
    config = MarketReplayConfig(
        tmp_path / "source.ibrec",
        tmp_path / "reports",
        **values,
    )
    with pytest.raises(ValueError, match=f"{field} must be a boolean"):
        config.normalized()


def test_reported_pyright_optional_numeric_patterns_are_removed() -> None:
    root = Path(__file__).resolve().parents[1]
    ibrec_source = (root / "optimizer" / "ibrec.py").read_text(encoding="utf-8")
    quality_source = (root / "optimizer" / "market_replay_quality.py").read_text(
        encoding="utf-8"
    )

    assert "min_tick = float(raw_min_tick)" not in ibrec_source
    assert 'min_tick = float(identity.get("min_tick"))' not in ibrec_source
    assert 'min_tick = float(details[0].get("min_tick"))' not in ibrec_source
    assert "version = int(value)" not in quality_source
    assert 'timestamp = float(event.get("timestamp"))' not in quality_source


def test_native_and_reproducible_windows_gates_pin_reported_pyright_version() -> None:
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    release_lock = (root / "requirements-release-win64.lock").read_text(
        encoding="utf-8"
    )
    assert "pyright[nodejs]==1.1.411" in requirements.splitlines()
    assert "pyright==1.1.411" in release_lock.splitlines()
