"""Regression tests for v2.0.2 removal of .ibrec row/count limits."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import optimizer.ibrec as ibrec
from optimizer.cli import _parser
from optimizer.market_replay import market_replay_search_contract
from optimizer.market_replay_models import MarketReplayConfig


def _recording_paths(tmp_path: Path, count: int) -> tuple[Path, ...]:
    paths = tuple(tmp_path / f"recording-{index:04d}.ibrec" for index in range(count))
    for path in paths:
        path.write_bytes(b"")
    return paths


def _detail(path: Path, *, row_count: int) -> dict[str, Any]:
    digest = hashlib.sha256(path.name.encode("utf-8")).hexdigest()
    return {
        "symbol": "AAPL",
        "con_id": 265598,
        "currency": "USD",
        "security_type": "STK",
        "time_zone_id": "America/New_York",
        "min_tick": 0.01,
        "content_sha256": digest,
        "row_count": row_count,
        "component_size_bytes": 1,
        "size_bytes": 1,
        "format_version": 3,
        "container_format": "sqlite",
        "rth_period_count": 1,
        "synthetic": False,
    }


def test_config_accepts_more_than_the_previous_64_file_limit(tmp_path: Path) -> None:
    paths = _recording_paths(tmp_path, 128)
    normalized = MarketReplayConfig(paths, tmp_path / "reports").normalized()
    assert normalized.recording_paths == tuple(
        sorted(paths, key=lambda item: str(item).casefold())
    )


def test_preflight_accepts_rows_above_the_previous_aggregate_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _recording_paths(tmp_path, 3)

    def fake_inspect(config: MarketReplayConfig) -> dict[str, Any]:
        return _detail(config.single_recording_path, row_count=1_500_000)

    monkeypatch.setattr(ibrec, "inspect_ibrec", fake_inspect)
    details = ibrec.inspect_ibrec_set(MarketReplayConfig(paths, tmp_path / "reports"))
    assert details["recording_count"] == 3
    assert details["row_count"] == 4_500_000


def test_loader_accepts_many_files_and_rows_without_hidden_count_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _recording_paths(tmp_path, 80)
    fake_recording: Any = SimpleNamespace(
        raw_row_count=1_000_000,
        input_components=({"size": 1},),
    )
    loaded: list[Path] = []

    def fake_load(config: MarketReplayConfig, *, progress: Any = None) -> Any:
        del progress
        loaded.append(config.single_recording_path)
        return fake_recording

    monkeypatch.setattr(ibrec, "load_ibrec", fake_load)
    monkeypatch.setattr(ibrec, "combine_ibrec_recordings", lambda recordings: tuple(recordings))

    result = ibrec.load_ibrec_set(MarketReplayConfig(paths, tmp_path / "reports"))
    assert len(result) == 80
    assert len(loaded) == 80


def test_cli_no_longer_exposes_row_or_file_count_limit_options() -> None:
    help_text = _parser().format_help()
    assert "--max-ibrec-rows" not in help_text
    assert "--max-ibrec-files" not in help_text


def test_market_replay_contract_explicitly_records_unlimited_counts(
    tmp_path: Path,
) -> None:
    contract = market_replay_search_contract(
        MarketReplayConfig(tmp_path / "recording.ibrec", tmp_path / "reports")
    )
    inputs = contract["input_recordings"]
    assert inputs["recording_count_limit"] is None
    assert inputs["aggregate_row_limit"] is None
    assert inputs["aggregate_input_byte_limit"] > 0


def test_no_market_replay_row_or_file_count_limit_remains_in_source() -> None:
    root = Path(__file__).resolve().parents[1]
    models_source = (root / "optimizer/market_replay_models.py").read_text(
        encoding="utf-8"
    )
    importer_source = (root / "optimizer/ibrec.py").read_text(encoding="utf-8")
    cli_source = (root / "optimizer/cli.py").read_text(encoding="utf-8")

    assert "max_rows" not in MarketReplayConfig.__dataclass_fields__
    assert "max_recordings" not in MarketReplayConfig.__dataclass_fields__
    assert "self.max_rows" not in models_source
    assert "self.max_recordings" not in models_source
    assert "config.max_rows" not in importer_source
    assert "normalized.max_rows" not in importer_source
    assert "max_recordings=" not in importer_source
    assert "--max-ibrec-rows" not in cli_source
    assert "--max-ibrec-files" not in cli_source
