"""Regression tests for the v1.9.3 calibration date-key and CSV corrections."""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from optimizer.ibrec import load_ibrec
from optimizer.market_replay import _quantile as replay_quantile
from optimizer.market_replay import _simulate_session_stateful
from optimizer.market_replay_calibration import (
    _execution_session_date,
    load_execution_calibration,
)
from optimizer.market_replay_models import (
    AtrProfile,
    IbrecPeriod,
    IbrecRecording,
    MarketReplayConfig,
)
from optimizer.market_replay_reports import _write_csv
from optimizer.utils import canonical_session_date, percentile
from tests.market_replay_fixtures import make_ticks, write_v3
from tests.test_v180_continuous_replay_and_calibration import (
    _prepare_calibration_database,
)


def _period(session_date: str) -> IbrecPeriod:
    return IbrecPeriod(
        period_id=1,
        session_date=session_date,
        schedule_open_utc="2026-07-21T13:30:00Z",
        schedule_close_utc="2026-07-21T20:00:00Z",
        open_timestamp=1_784_986_200.0,
        close_timestamp=1_785_009_600.0,
        observed_start_utc="2026-07-21T13:30:00Z",
        observed_end_utc="2026-07-21T20:00:00Z",
        observed_start_timestamp=1_784_986_200.0,
        observed_end_timestamp=1_785_009_600.0,
        status="closed",
        close_reason="contract_liquid_hours",
        tick_count=0,
    )


def _profile() -> AtrProfile:
    return AtrProfile(
        period=14,
        bar_seconds=60,
        initial_drop_multiplier=1.5,
        buy_rebound_multiplier=0.75,
        minimum_profit_multiplier=1.0,
        sell_trail_multiplier=1.0,
    )


def test_canonical_session_date_unifies_recording_and_calibration_formats() -> None:
    assert canonical_session_date("20260721") == "2026-07-21"
    assert canonical_session_date("2026-07-21") == "2026-07-21"
    assert canonical_session_date(" 2026-07-21T15:00:00 ") == "2026-07-21"
    assert canonical_session_date("") == ""
    # An unknown format may only ever fail to match, never collide.
    assert canonical_session_date("not-a-date") == "not-a-date"
    assert canonical_session_date("20261340") == "20261340"


def test_execution_dates_use_recorded_exchange_session_not_utc_date(
    tmp_path: Path,
) -> None:
    open_timestamp = datetime(
        2026,
        7,
        21,
        23,
        0,
        tzinfo=timezone.utc,
    ).timestamp()
    close_timestamp = datetime(
        2026,
        7,
        22,
        5,
        0,
        tzinfo=timezone.utc,
    ).timestamp()
    period = replace(
        _period("20260722"),
        schedule_open_utc="2026-07-21T23:00:00Z",
        schedule_close_utc="2026-07-22T05:00:00Z",
        open_timestamp=open_timestamp,
        close_timestamp=close_timestamp,
        observed_start_utc="2026-07-21T23:00:00Z",
        observed_end_utc="2026-07-22T05:00:00Z",
        observed_start_timestamp=open_timestamp,
        observed_end_timestamp=close_timestamp,
    )
    recording = IbrecRecording(
        path=tmp_path / "tokyo.ibrec",
        sha256="00" * 32,
        size_bytes=0,
        input_components=[],
        container_format="sqlite",
        format_version=3,
        manifest={},
        contract={
            "symbol": "7203",
            "con_id": 1,
            "currency": "JPY",
            "security_type": "STK",
            "time_zone_id": "Asia/Tokyo",
            "min_tick": 0.1,
        },
        ticks=[],
        periods=[period],
        issues=[],
        feed_counts={},
        raw_row_count=0,
        retained_row_count=0,
        data_start_utc="",
        data_end_utc="",
    )

    inside_period = datetime(
        2026,
        7,
        21,
        23,
        30,
        tzinfo=timezone.utc,
    ).timestamp()
    before_period_same_local_day = datetime(
        2026,
        7,
        21,
        22,
        30,
        tzinfo=timezone.utc,
    ).timestamp()

    # UTC still says July 21, but both events belong to the Tokyo July 22
    # session. Same-session executions must not leak into prior-only evidence.
    assert _execution_session_date(recording, inside_period) == "2026-07-22"
    assert (
        _execution_session_date(recording, before_period_same_local_day)
        == "2026-07-22"
    )


def test_override_normalization_stores_one_canonical_date_spelling(
    tmp_path: Path,
) -> None:
    compact = MarketReplayConfig(
        recording_path=tmp_path / "probe.ibrec",
        output_root=tmp_path / "reports",
        execution_cost_overrides=(("20260721", 9.0, 7.0),),
        trade_notional_overrides=(("20260721", 5_000.0),),
    ).normalized()
    iso = MarketReplayConfig(
        recording_path=tmp_path / "probe.ibrec",
        output_root=tmp_path / "reports",
        execution_cost_overrides=(("2026-07-21", 9.0, 7.0),),
        trade_notional_overrides=(("2026-07-21", 5_000.0),),
    ).normalized()

    assert compact.execution_cost_overrides == iso.execution_cost_overrides == (
        ("2026-07-21", 9.0, 7.0),
    )
    assert compact.trade_notional_overrides == iso.trade_notional_overrides == (
        ("2026-07-21", 5_000.0),
    )


def test_override_validation_rejects_empty_canonical_dates(tmp_path: Path) -> None:
    cost_config = MarketReplayConfig(
        recording_path=tmp_path / "probe.ibrec",
        output_root=tmp_path / "reports",
        execution_cost_overrides=((None, 9.0, 7.0),),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="non-empty session dates"):
        cost_config.normalized()

    notional_config = MarketReplayConfig(
        recording_path=tmp_path / "probe.ibrec",
        output_root=tmp_path / "reports",
        trade_notional_overrides=((False, 5_000.0),),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="unique dates"):
        notional_config.normalized()

    malformed_cost = MarketReplayConfig(
        recording_path=tmp_path / "probe.ibrec",
        output_root=tmp_path / "reports",
        execution_cost_overrides=(("not-a-date", 9.0, 7.0),),
    )
    with pytest.raises(ValueError, match="valid format"):
        malformed_cost.normalized()

    malformed_notional = MarketReplayConfig(
        recording_path=tmp_path / "probe.ibrec",
        output_root=tmp_path / "reports",
        trade_notional_overrides=(("20261340", 5_000.0),),
    )
    with pytest.raises(ValueError, match="valid format"):
        malformed_notional.normalized()


@pytest.mark.parametrize("override_key", ["2026-07-21", "20260721"])
def test_date_specific_overrides_apply_to_recorded_periods(
    tmp_path: Path,
    override_key: str,
) -> None:
    """Calibration emits ISO override keys; periods store YYYYMMDD dates.

    Before v1.9.3 the ISO-keyed parameterization silently fell back to the
    configured defaults, so the date-cross-fitted calibration never reached
    the simulation.  Both spellings must now select the same override.
    """

    config = MarketReplayConfig(
        recording_path=tmp_path / "probe.ibrec",
        output_root=tmp_path / "reports",
        execution_cost_bps_per_side=1.0,
        assumed_trade_notional=10_000.0,
        execution_cost_overrides=((override_key, 9.0, 7.0),),
        trade_notional_overrides=((override_key, 5_000.0),),
    )
    result, trades, _state = _simulate_session_stateful(
        [],
        _period("20260721"),
        _profile(),
        [],
        0.01,
        config,
        keep_trades=False,
    )
    assert trades == []
    assert result.buy_execution_cost_bps_per_side == pytest.approx(9.0)
    assert result.sell_execution_cost_bps_per_side == pytest.approx(7.0)
    assert result.assumed_trade_notional == pytest.approx(5_000.0)


def test_overrides_for_other_dates_keep_configured_defaults(
    tmp_path: Path,
) -> None:
    config = MarketReplayConfig(
        recording_path=tmp_path / "probe.ibrec",
        output_root=tmp_path / "reports",
        execution_cost_bps_per_side=1.0,
        assumed_trade_notional=10_000.0,
        execution_cost_overrides=(("2026-07-22", 9.0, 7.0),),
        trade_notional_overrides=(("2026-07-22", 5_000.0),),
    )
    result, _trades, _state = _simulate_session_stateful(
        [],
        _period("20260721"),
        _profile(),
        [],
        0.01,
        config,
        keep_trades=False,
    )
    assert result.buy_execution_cost_bps_per_side == pytest.approx(1.0)
    assert result.sell_execution_cost_bps_per_side == pytest.approx(1.0)
    assert result.assumed_trade_notional == pytest.approx(10_000.0)


def test_override_validation_rejects_canonically_equivalent_dates(
    tmp_path: Path,
) -> None:
    """``20260721`` and ``2026-07-21`` are one date; a mixed pair must fail
    closed instead of one entry silently overwriting the other."""

    config = MarketReplayConfig(
        recording_path=tmp_path / "probe.ibrec",
        output_root=tmp_path / "reports",
        execution_cost_overrides=(
            ("2026-07-21", 9.0, 9.0),
            ("20260721", 3.0, 3.0),
        ),
    )
    with pytest.raises(ValueError, match="unique non-empty session dates"):
        config.normalized()

    notional_config = MarketReplayConfig(
        recording_path=tmp_path / "probe.ibrec",
        output_root=tmp_path / "reports",
        trade_notional_overrides=(
            ("2026-07-21", 5_000.0),
            ("20260721", 4_000.0),
        ),
    )
    with pytest.raises(ValueError, match="unique dates"):
        notional_config.normalized()


def test_market_replay_csv_writer_unions_columns_across_rows(
    tmp_path: Path,
) -> None:
    """A combine-time exclusion (four keys) followed by a quality-gate
    exclusion (nine keys) must not truncate the header to the first row."""

    rows = [
        {
            "session_date": "20260720",
            "period_count": 2,
            "recording_hashes": ["aa"],
            "reason": "overlap",
        },
        {
            "reason_type": "quality_gate",
            "session_date": "20260721",
            "period_id": 3,
            "period_count": 1,
            "source_recording_sha256": "bb",
            "recording_hashes": ["bb"],
            "coverage_pct": 42.5,
            "reason": "gap",
            "reasons": ["gap"],
        },
    ]
    path = tmp_path / "excluded_sessions.csv"
    _write_csv(path, rows, list(rows[0]))
    with path.open(encoding="utf-8-sig", newline="") as stream:
        parsed = list(csv.DictReader(stream))
    header = list(parsed[0])
    # The caller's order leads; the second row's extra columns follow.
    assert header[:4] == ["session_date", "period_count", "recording_hashes", "reason"]
    assert {
        "reason_type",
        "period_id",
        "source_recording_sha256",
        "coverage_pct",
        "reasons",
    }.issubset(header)
    assert parsed[1]["coverage_pct"] == "42.5"
    assert parsed[0]["coverage_pct"] == ""


def test_market_replay_csv_writer_keeps_fallback_header_for_empty_tables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "empty.csv"
    _write_csv(path, [], ["session_date", "reason"])
    assert path.read_text(encoding="utf-8-sig") == "session_date,reason\n"


def test_quantile_helpers_share_one_implementation() -> None:
    from optimizer.evidence import _quantile as evidence_quantile
    from optimizer.market_replay_quality import _quantile as quality_quantile

    values = [4.0, 1.0, 3.0, 2.0]
    for probability in (0.0, 0.1, 0.5, 0.75, 0.9, 1.0):
        expected = percentile(values, probability)
        assert replay_quantile(values, probability) == expected
        assert evidence_quantile(values, probability) == expected
        assert quality_quantile(values, probability) == expected
    assert replay_quantile([], 0.5) is None
    assert evidence_quantile([float("nan"), 5.0], 0.5) == 5.0


def _calibration_fixture(
    tmp_path: Path,
) -> tuple[MarketReplayConfig, Path]:
    rows, periods = make_ticks(sessions=5)
    source = tmp_path / "bot"
    _prepare_calibration_database(source, rows)
    recording_path = write_v3(tmp_path / "recording.ibrec", rows, periods)
    return (
        MarketReplayConfig(
            recording_path=recording_path,
            output_root=tmp_path / "reports",
            assumed_trade_notional=10_000.0,
            execution_cost_bps_per_side=1.0,
            calibration_source_dir=source,
            calibration_min_samples=5,
        ),
        source / "bot_state.sqlite",
    )


def test_date_cross_fitted_notional_can_reduce_the_configured_default(
    tmp_path: Path,
) -> None:
    config, _database = _calibration_fixture(tmp_path)
    calibration = load_execution_calibration(load_ibrec(config), config)

    assert calibration.effective_trade_notional < 2_000.0
    assert calibration.used_trade_notional_calibration is True
    assert calibration.date_specific_trade_notionals
    assert all(
        0 < float(row["trade_notional"]) < config.assumed_trade_notional
        for row in calibration.date_specific_trade_notionals
    )

    first = calibration.date_specific_trade_notionals[0]
    effective = replace(
        config.normalized(),
        assumed_trade_notional=calibration.effective_trade_notional,
        trade_notional_overrides=tuple(
            (str(row["session_date"]), float(row["trade_notional"]))
            for row in calibration.date_specific_trade_notionals
        ),
        calibration_source_dir=None,
    ).normalized()
    result, _trades, _state = _simulate_session_stateful(
        [],
        _period(str(first["session_date"]).replace("-", "")),
        _profile(),
        [],
        0.01,
        effective,
        keep_trades=False,
    )
    assert result.assumed_trade_notional == pytest.approx(
        float(first["trade_notional"])
    )


def test_calibration_toggles_disable_global_and_date_specific_replacements(
    tmp_path: Path,
) -> None:
    config, _database = _calibration_fixture(tmp_path)
    disabled = replace(
        config,
        calibration_use_execution_cost=False,
        calibration_use_trade_notional=False,
    )
    calibration = load_execution_calibration(load_ibrec(disabled), disabled)

    assert calibration.applied is False
    assert calibration.used_execution_cost_calibration is False
    assert calibration.used_trade_notional_calibration is False
    assert calibration.effective_buy_execution_cost_bps_per_side == pytest.approx(1.0)
    assert calibration.effective_sell_execution_cost_bps_per_side == pytest.approx(1.0)
    assert calibration.effective_trade_notional == pytest.approx(10_000.0)
    assert all(
        row["buy_mode"] == "disabled"
        and row["sell_mode"] == "disabled"
        and float(row["buy_cost_bps"]) == pytest.approx(1.0)
        and float(row["sell_cost_bps"]) == pytest.approx(1.0)
        for row in calibration.date_specific_execution_costs
    )
    assert all(
        row["mode"] == "disabled"
        and float(row["trade_notional"]) == pytest.approx(10_000.0)
        for row in calibration.date_specific_trade_notionals
    )


def test_date_specific_notional_counts_distinct_cycles_with_equal_values(
    tmp_path: Path,
) -> None:
    config, database = _calibration_fixture(tmp_path)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE executions SET executed_at='2026-01-01T15:00:00+00:00', "
            "price=100.0, avg_price=100.0 WHERE side='BOT'"
        )
        connection.commit()
    finally:
        connection.close()

    calibration = load_execution_calibration(load_ibrec(config), config)
    assert calibration.date_specific_trade_notionals
    assert calibration.date_specific_trade_notionals[0]["samples"] == 5


def test_tiny_calibrated_notionals_remain_valid_after_rounding(
    tmp_path: Path,
) -> None:
    config, database = _calibration_fixture(tmp_path)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE executions SET shares=0.000001, price=1.0, avg_price=1.0 "
            "WHERE side='BOT'"
        )
        connection.commit()
    finally:
        connection.close()

    calibration = load_execution_calibration(load_ibrec(config), config)
    assert calibration.effective_trade_notional == pytest.approx(0.01)
    assert calibration.date_specific_trade_notionals
    assert all(
        float(row["trade_notional"]) >= 0.01
        for row in calibration.date_specific_trade_notionals
    )


def test_prior_only_notional_estimates_honor_the_minimum(tmp_path: Path) -> None:
    """Sub-cent prior-only medians clamp to 0.01 like the other paths.

    Before this correction the prior-only branch returned the raw median, the
    date row rounded it to a zero notional, and the effective configuration
    that ``run_market_replay_analysis`` builds from the calibration rows
    aborted the whole analysis during override normalization.
    """

    config, database = _calibration_fixture(tmp_path)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE executions SET shares=0.000001, price=1.0, avg_price=1.0 "
            "WHERE side='BOT'"
        )
        connection.commit()
    finally:
        connection.close()

    low_min = replace(config, calibration_min_samples=2)
    calibration = load_execution_calibration(load_ibrec(low_min), low_min)
    rows = calibration.date_specific_trade_notionals
    assert any(row["mode"] == "prior_only" for row in rows)
    assert all(float(row["trade_notional"]) >= 0.01 for row in rows)

    effective = replace(
        low_min.normalized(),
        assumed_trade_notional=calibration.effective_trade_notional,
        trade_notional_overrides=tuple(
            (str(row["session_date"]), float(row["trade_notional"]))
            for row in rows
        ),
        calibration_source_dir=None,
    ).normalized()
    assert all(value >= 0.01 for _date, value in effective.trade_notional_overrides)


def test_normalization_validates_the_stored_rounded_values(
    tmp_path: Path,
) -> None:
    """Bounds apply to the exact rounded values ``normalized`` stores.

    A raw value may satisfy a bound and still round across it; such values are
    rejected up front instead of producing a normalized configuration that
    fails its own re-normalization.
    """

    def _config(**overrides: object) -> MarketReplayConfig:
        return MarketReplayConfig(
            recording_path=tmp_path / "probe.ibrec",
            output_root=tmp_path / "reports",
            **overrides,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="at least 0.01"):
        _config(trade_notional_overrides=(("2026-07-21", 0.004),)).normalized()
    with pytest.raises(ValueError, match="at least 0.01"):
        _config(assumed_trade_notional=0.004).normalized()
    with pytest.raises(ValueError, match="below 10,000"):
        _config(
            execution_cost_overrides=(("2026-07-21", 9_999.9999996, 1.0),)
        ).normalized()
    with pytest.raises(ValueError, match="below 10,000"):
        _config(execution_cost_bps_per_side=9_999.99996).normalized()
    with pytest.raises(ValueError, match="below 10,000"):
        _config(buy_execution_cost_bps_per_side=9_999.9999996).normalized()
    with pytest.raises(ValueError, match="below 10,000"):
        _config(sell_execution_cost_bps_per_side=9_999.9999996).normalized()

    # The half-cent boundary rounds up to a valid stored value, and the
    # normalized configuration re-normalizes to itself.
    accepted = _config(
        trade_notional_overrides=(("2026-07-21", 0.005),)
    ).normalized()
    assert accepted.trade_notional_overrides == (("2026-07-21", 0.01),)
    assert (
        accepted.normalized().trade_notional_overrides
        == accepted.trade_notional_overrides
    )

    near_limit = _config(
        execution_cost_bps_per_side=9_999.99994,
        buy_execution_cost_bps_per_side=9_999.9999994,
        sell_execution_cost_bps_per_side=9_999.9999994,
        execution_cost_overrides=(
            ("2026-07-21", 9_999.9999994, 9_999.9999994),
        ),
    ).normalized()
    assert near_limit.execution_cost_bps_per_side == pytest.approx(9_999.9999)
    assert near_limit.buy_execution_cost_bps_per_side == pytest.approx(9_999.999999)
    assert near_limit.sell_execution_cost_bps_per_side == pytest.approx(9_999.999999)
    assert near_limit.normalized() == near_limit


def _fragment_recordings(tmp_path: Path) -> tuple[Path, Path, int]:
    """Split one fixture session into two same-date recordings with a gap."""

    rows, periods = make_ticks(sessions=1)
    first_rows = [dict(row) for row in rows[:60]]
    second_rows = []
    for index, row in enumerate(rows[70:], start=1):
        item = dict(row)
        item["sequence"] = index
        second_rows.append(item)

    def _fragment_period(fragment_rows: list[dict[str, object]]) -> dict[str, object]:
        period = dict(periods[0])
        period["observed_start_utc"] = fragment_rows[0]["captured_at_utc"]
        period["observed_end_utc"] = fragment_rows[-1]["captured_at_utc"]
        period["first_tick_sequence"] = fragment_rows[0]["sequence"]
        period["last_tick_sequence"] = fragment_rows[-1]["sequence"]
        period["tick_count"] = len(fragment_rows)
        return period

    first = write_v3(tmp_path / "morning.ibrec", first_rows, [_fragment_period(first_rows)])
    second = write_v3(tmp_path / "afternoon.ibrec", second_rows, [_fragment_period(second_rows)])
    gap_ns = (70 - 59) * 15 * 1_000_000_000
    return first, second, gap_ns


def test_same_date_fragments_are_stitched_into_one_period(tmp_path: Path) -> None:
    """Non-overlapping recorder restarts merge instead of dropping the date.

    The stitched period keeps every fragment's recorder-monotonic spacing and
    measures the true wall-clock outage across the stitch, so ATR buckets,
    quote ages, and the gap gates see exactly what one recorder with a
    mid-session outage would have produced.
    """

    from optimizer.ibrec import load_ibrec_set

    first, second, gap_ns = _fragment_recordings(tmp_path)
    recording = load_ibrec_set(
        MarketReplayConfig((first, second), tmp_path / "reports")
    )
    assert len(recording.periods) == 1
    period = recording.periods[0]
    assert period.session_date == "20260105"
    assert period.tick_count == 60 + 51
    assert len(period.source_recording_sha256s) == 2
    assert "+" in period.source_recording_sha256
    assert period.observed_start_utc == recording.ticks[0].captured_at_utc
    assert period.observed_end_utc == recording.ticks[-1].captured_at_utc

    elapsed = [tick.elapsed_ns for tick in recording.ticks]
    assert elapsed == sorted(elapsed)
    boundary_deltas = [
        later - earlier for earlier, later in zip(elapsed, elapsed[1:])
    ]
    assert max(boundary_deltas) == gap_ns
    assert boundary_deltas.index(gap_ns) == 59
    assert all(tick.rth_period_id == period.period_id for tick in recording.ticks)
    assert any("Merged trading date 20260105" in issue for issue in recording.issues)

    reordered = load_ibrec_set(
        MarketReplayConfig((second, first), tmp_path / "reports-b")
    )
    assert reordered.sha256 == recording.sha256
    assert [
        (tick.elapsed_ns, tick.bid) for tick in reordered.ticks
    ] == [(tick.elapsed_ns, tick.bid) for tick in recording.ticks]


def test_set_errors_name_the_offending_recording(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optimizer.ibrec as ibrec_module
    from optimizer.ibrec import IbrecError, inspect_ibrec_set, load_ibrec_set

    empty = tmp_path / "empty.ibrec"
    other = tmp_path / "other.ibrec"
    empty.write_bytes(b"")
    other.write_bytes(b"")

    monkeypatch.setattr(
        ibrec_module,
        "inspect_ibrec",
        lambda config: {"row_count": 0},
    )
    with pytest.raises(IbrecError, match="empty.ibrec: Recording contains no market-data rows"):
        inspect_ibrec_set(
            MarketReplayConfig((empty, other), tmp_path / "reports")
        )

    def _fail(config, progress=None):
        raise IbrecError("Recording contains no market-data rows.")

    monkeypatch.setattr(ibrec_module, "load_ibrec", _fail)
    with pytest.raises(IbrecError, match="empty.ibrec: Recording contains no market-data rows"):
        load_ibrec_set(
            MarketReplayConfig((empty, other), tmp_path / "reports")
        )
