"""Regression tests for v1.9.4 same-date fragment merging and diagnostics."""

from __future__ import annotations

import csv
from dataclasses import replace
from pathlib import Path

import pytest

import optimizer.ibrec as ibrec
from optimizer.market_replay import run_market_replay_analysis
from optimizer.market_replay_models import IbrecTick, MarketReplayConfig
from optimizer.market_replay_quality import (
    _source_format_version,
    assess_market_replay_session,
)
from optimizer.market_replay_reports import write_market_replay_report
from tests.market_replay_fixtures import make_ticks, write_v3


def _reindex_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return a valid standalone format-v3 tick sequence."""

    first_elapsed = int(rows[0]["elapsed_ns"])
    return [
        {
            **row,
            "sequence": index,
            "elapsed_ns": int(row["elapsed_ns"]) - first_elapsed,
            "rth_period_id": 1,
        }
        for index, row in enumerate(rows, start=1)
    ]


def _period_for_rows(
    template: dict[str, object],
    rows: list[dict[str, object]],
    *,
    close_reason: str | None = None,
) -> dict[str, object]:
    period = {
        **template,
        "period_id": 1,
        "observed_start_utc": rows[0]["captured_at_utc"],
        "observed_end_utc": rows[-1]["captured_at_utc"],
        "first_tick_sequence": 1,
        "last_tick_sequence": len(rows),
        "tick_count": len(rows),
    }
    if close_reason is not None:
        period["close_reason"] = close_reason
    return period


def test_fragment_selection_prefers_time_coverage_over_callback_density(
    tmp_path: Path,
) -> None:
    """A sparse complete recording beats denser but incomplete fragments."""

    rows, periods = make_ticks(points_per_session=140)
    full_sparse_source = [*rows[::4]]
    if full_sparse_source[-1] is not rows[-1]:
        full_sparse_source.append(rows[-1])
    full_sparse = _reindex_rows(full_sparse_source)
    full = write_v3(
        tmp_path / "full-sparse.ibrec",
        full_sparse,
        [_period_for_rows(periods[0], full_sparse, close_reason="contract_liquid_hours")],
    )

    morning_source = rows[:70]
    afternoon_source = rows[72:]
    morning = _reindex_rows(morning_source)
    afternoon = _reindex_rows(afternoon_source)
    morning_path = write_v3(
        tmp_path / "morning-dense.ibrec",
        morning,
        [_period_for_rows(periods[0], morning)],
    )
    afternoon_path = write_v3(
        tmp_path / "afternoon-dense.ibrec",
        afternoon,
        [_period_for_rows(periods[0], afternoon, close_reason="contract_liquid_hours")],
    )

    recording = ibrec.load_ibrec_set(
        MarketReplayConfig(
            (morning_path, full, afternoon_path),
            tmp_path / "reports",
        )
    )
    assert len(recording.periods) == 1
    period = recording.periods[0]
    assert period.tick_count == len(full_sparse)
    assert len(period.source_recording_sha256s) == 1
    statuses = [row["status"] for row in recording.fragment_evidence]
    assert statuses.count("retained_single") == 1
    assert statuses.count("dropped_overlap") == 2


def test_conflicting_same_date_schedules_are_excluded_fail_closed(
    tmp_path: Path,
) -> None:
    rows, periods = make_ticks(sessions=2, points_per_session=140)
    first_day = [row for row in rows if int(row["rth_period_id"]) == 1]
    second_day = [row for row in rows if int(row["rth_period_id"]) == 2]

    first = _reindex_rows(first_day)
    first_period = _period_for_rows(
        periods[0],
        first,
        close_reason="contract_liquid_hours",
    )
    changed_first = [dict(row) for row in first]
    changed_first[-1]["ask"] = float(changed_first[-1]["ask"]) + 0.001
    conflicting_period = dict(first_period)
    conflicting_period["schedule_close_utc"] = "2026-01-05T21:00:00.000Z"

    first_path = write_v3(tmp_path / "first.ibrec", first, [first_period])
    conflict_path = write_v3(
        tmp_path / "conflict.ibrec",
        changed_first,
        [conflicting_period],
    )
    second = _reindex_rows(second_day)
    second_path = write_v3(
        tmp_path / "second-day.ibrec",
        second,
        [_period_for_rows(periods[1], second, close_reason="contract_liquid_hours")],
    )

    recording = ibrec.load_ibrec_set(
        MarketReplayConfig(
            (first_path, conflict_path, second_path),
            tmp_path / "reports",
        )
    )
    assert [period.session_date for period in recording.periods] == ["20260106"]
    excluded = recording.excluded_sessions
    assert len(excluded) == 1
    assert excluded[0]["reason_type"] == "schedule_conflict"
    assert excluded[0]["session_date"] == "20260105"
    assert sum(
        row["status"] == "excluded_schedule_conflict"
        for row in recording.fragment_evidence
    ) == 2


def test_merged_period_requires_complete_component_format_provenance(
    tmp_path: Path,
) -> None:
    path = write_v3(tmp_path / "source.ibrec")
    config = MarketReplayConfig(path, tmp_path / "reports")
    recording = ibrec.load_ibrec(config)
    known = recording.sha256
    recording.input_components[0]["recording_content_sha256"] = known
    recording.input_components[0]["format_version"] = 3
    recording.format_version = 0
    period = replace(
        recording.periods[0],
        source_recording_sha256=f"{known}+{'f' * 64}",
        source_recording_sha256s=(known, "f" * 64),
    )
    assert _source_format_version(recording, period) == 0


def test_stitched_gap_and_all_source_hashes_reach_quality_evidence(
    tmp_path: Path,
) -> None:
    rows, periods = make_ticks(points_per_session=140)
    first_source = rows[:70]
    second_source = rows[75:]
    first = _reindex_rows(first_source)
    second = _reindex_rows(second_source)
    first_path = write_v3(
        tmp_path / "morning.ibrec",
        first,
        [_period_for_rows(periods[0], first)],
    )
    second_path = write_v3(
        tmp_path / "afternoon.ibrec",
        second,
        [_period_for_rows(periods[0], second, close_reason="contract_liquid_hours")],
    )
    config = MarketReplayConfig(
        (first_path, second_path),
        tmp_path / "reports",
        max_market_event_gap_seconds=30.0,
    )
    recording = ibrec.load_ibrec_set(config)
    period = recording.periods[0]
    ticks = [tick for tick in recording.ticks if tick.rth_period_id == period.period_id]
    quality = assess_market_replay_session(recording, period, ticks, config)
    assert quality.source_recording_sha256s == period.source_recording_sha256s
    assert len(quality.source_recording_sha256s) == 2
    assert quality.maximum_event_gap_seconds > 30.0
    assert quality.primary_eligible is False
    assert any("market-event stream contains a gap" in reason for reason in quality.exclusion_reasons)


def test_fragment_diagnostics_are_exported_to_csv_html_and_json(
    tmp_path: Path,
) -> None:
    rows, periods = make_ticks(points_per_session=140)
    first = _reindex_rows(rows[:70])
    second = _reindex_rows(rows[70:])
    first_path = write_v3(
        tmp_path / "morning.ibrec",
        first,
        [_period_for_rows(periods[0], first)],
    )
    second_path = write_v3(
        tmp_path / "afternoon.ibrec",
        second,
        [_period_for_rows(periods[0], second, close_reason="contract_liquid_hours")],
    )
    result = write_market_replay_report(
        run_market_replay_analysis(
            MarketReplayConfig((first_path, second_path), tmp_path / "reports")
        )
    )
    csv_path = result.output_dir / "recording_fragment_evidence.csv"
    assert csv_path.is_file()
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows_out = list(csv.DictReader(stream))
    assert len(rows_out) == 2
    assert {row["status"] for row in rows_out} == {"retained_stitched"}
    html = (result.output_dir / "index.html").read_text(encoding="utf-8")
    assert "Same-date fragment decisions" in html
    payload = (result.output_dir / "market_replay_analysis.json").read_text(
        encoding="utf-8"
    )
    assert '"fragment_evidence"' in payload


def test_set_error_does_not_duplicate_an_existing_filename(tmp_path: Path) -> None:
    missing = tmp_path / "missing.ibrec"
    with pytest.raises(ibrec.IbrecError) as exc_info:
        ibrec.load_ibrec_set(MarketReplayConfig(missing, tmp_path / "reports"))
    assert str(exc_info.value).lower().count(missing.name.lower()) == 1


def test_identical_touching_boundary_is_deduplicated_but_conflict_is_not(
    tmp_path: Path,
) -> None:
    rows, periods = make_ticks(points_per_session=140)
    first = _reindex_rows(rows[:70])
    second = _reindex_rows(rows[69:])
    first_path = write_v3(
        tmp_path / "first.ibrec",
        first,
        [_period_for_rows(periods[0], first)],
    )
    second_path = write_v3(
        tmp_path / "second.ibrec",
        second,
        [_period_for_rows(periods[0], second, close_reason="contract_liquid_hours")],
    )
    merged = ibrec.load_ibrec_set(
        MarketReplayConfig((first_path, second_path), tmp_path / "reports")
    )
    assert merged.periods[0].tick_count == len(rows)
    assert any("duplicate boundary row" in issue for issue in merged.issues)

    conflicting = [dict(row) for row in second]
    conflicting[0]["ask"] = float(conflicting[0]["ask"]) + 0.001
    conflict_path = write_v3(
        tmp_path / "second-conflict.ibrec",
        conflicting,
        [_period_for_rows(periods[0], conflicting, close_reason="contract_liquid_hours")],
    )
    not_merged = ibrec.load_ibrec_set(
        MarketReplayConfig((first_path, conflict_path), tmp_path / "reports-b")
    )
    statuses = [row["status"] for row in not_merged.fragment_evidence]
    assert statuses.count("retained_single") == 1
    assert statuses.count("dropped_overlap") == 1


def test_equal_coverage_prefers_live_normal_close_evidence(tmp_path: Path) -> None:
    rows, periods = make_ticks(points_per_session=140)
    live_sparse_source = [*rows[::4]]
    if live_sparse_source[-1] is not rows[-1]:
        live_sparse_source.append(rows[-1])
    live_sparse = _reindex_rows(live_sparse_source)
    live_path = write_v3(
        tmp_path / "live-sparse.ibrec",
        live_sparse,
        [_period_for_rows(periods[0], live_sparse, close_reason="contract_liquid_hours")],
    )

    delayed_dense = _reindex_rows(
        [{**row, "market_data_type": 3} for row in rows]
    )
    delayed_path = write_v3(
        tmp_path / "delayed-dense.ibrec",
        delayed_dense,
        [_period_for_rows(periods[0], delayed_dense, close_reason="contract_liquid_hours")],
    )
    combined = ibrec.load_ibrec_set(
        MarketReplayConfig((delayed_path, live_path), tmp_path / "reports")
    )
    assert {tick.market_data_type for tick in combined.ticks} == {1}


def test_empty_fragment_metadata_cannot_invalidate_a_valid_schedule(
    tmp_path: Path,
) -> None:
    """Schedule checks use only fragments with strategy-relevant rows."""

    first_path = write_v3(tmp_path / "valid.ibrec")
    empty_path = write_v3(tmp_path / "empty-source.ibrec", notes="empty fragment")
    valid = ibrec.load_ibrec(MarketReplayConfig(first_path, tmp_path / "reports-a"))
    empty = ibrec.load_ibrec(MarketReplayConfig(empty_path, tmp_path / "reports-b"))
    empty.ticks = []
    empty.periods = [
        replace(
            empty.periods[0],
            schedule_close_utc="2026-01-05T21:00:00.000Z",
            close_timestamp=empty.periods[0].close_timestamp + 3_600.0,
            tick_count=0,
        )
    ]

    combined = ibrec.combine_ibrec_recordings((empty, valid))
    assert [period.session_date for period in combined.periods] == ["20260105"]
    assert combined.periods[0].tick_count == len(valid.ticks)
    assert not any(
        row.get("reason_type") == "schedule_conflict"
        for row in combined.excluded_sessions
    )
    statuses = [row["status"] for row in combined.fragment_evidence]
    assert statuses.count("dropped_no_rows") == 1
    assert statuses.count("retained_single") == 1


def test_clock_reversal_fragment_is_never_stitched_into_another_recording(
    tmp_path: Path,
) -> None:
    """A UTC clock reversal remains isolated instead of fabricating chronology."""

    rows, periods = make_ticks(points_per_session=140)
    first = _reindex_rows(rows[:75])
    reversed_clock = _reindex_rows(rows[75:])
    # Keep recorder-monotonic elapsed_ns intact while moving one receipt UTC
    # timestamp behind its predecessor. Format-v3 permits this as clock-anomaly
    # evidence, but it cannot prove ordering against another recording.
    reversed_clock[5]["captured_at_utc"] = reversed_clock[3]["captured_at_utc"]
    first_path = write_v3(
        tmp_path / "first.ibrec",
        first,
        [_period_for_rows(periods[0], first)],
    )
    reversed_path = write_v3(
        tmp_path / "reversed.ibrec",
        reversed_clock,
        [
            _period_for_rows(
                periods[0],
                reversed_clock,
                close_reason="contract_liquid_hours",
            )
        ],
    )

    combined = ibrec.load_ibrec_set(
        MarketReplayConfig((first_path, reversed_path), tmp_path / "reports")
    )
    assert len(combined.periods) == 1
    statuses = [row["status"] for row in combined.fragment_evidence]
    assert statuses.count("retained_single") == 1
    assert statuses.count("dropped_overlap") == 1
    assert not any("stitched 2" in issue for issue in combined.issues)


def test_fragment_selection_checks_the_actual_chain_boundary(
    tmp_path: Path,
) -> None:
    """A same-end best prefix cannot smuggle in an incompatible boundary."""

    source = ibrec.load_ibrec(
        MarketReplayConfig(write_v3(tmp_path / "source.ibrec"), tmp_path / "reports")
    )
    period = source.periods[0]
    template = source.ticks[0]

    def _tick(timestamp: float, *, ask: float) -> IbrecTick:
        rendered = f"2026-01-05T14:30:{int(timestamp):02d}.000Z"
        return replace(
            template,
            timestamp=timestamp,
            captured_at_utc=rendered,
            source_time_utc=rendered,
            ask=ask,
        )

    shared_boundary = _tick(10.0, ask=100.01)
    compatible = ibrec._FragmentCandidate(
        period=period,
        ticks=(_tick(0.0, ask=100.01), shared_boundary),
        fingerprint="b-compatible",
        start=0.0,
        end=10.0,
        live_only=True,
        normal_close=False,
        clock_monotonic=True,
    )
    incompatible_dense = ibrec._FragmentCandidate(
        period=period,
        ticks=(
            _tick(0.0, ask=100.01),
            _tick(2.0, ask=100.01),
            _tick(4.0, ask=100.01),
            _tick(6.0, ask=100.01),
            _tick(8.0, ask=100.01),
            _tick(10.0, ask=101.01),
        ),
        fingerprint="a-incompatible",
        start=0.0,
        end=10.0,
        live_only=True,
        normal_close=False,
        clock_monotonic=True,
    )
    successor = ibrec._FragmentCandidate(
        period=period,
        ticks=(shared_boundary, _tick(20.0, ask=100.01)),
        fingerprint="c-successor",
        start=10.0,
        end=20.0,
        live_only=True,
        normal_close=True,
        clock_monotonic=True,
    )

    kept, dropped = ibrec._select_non_overlapping_fragments(
        [incompatible_dense, compatible, successor]
    )
    assert [fragment.fingerprint for fragment in kept] == [
        "b-compatible",
        "c-successor",
    ]
    assert [fragment.fingerprint for fragment in dropped] == ["a-incompatible"]
