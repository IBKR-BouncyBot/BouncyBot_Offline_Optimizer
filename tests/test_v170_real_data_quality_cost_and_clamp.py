from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from optimizer.ibrec import _quality_event, load_ibrec, load_ibrec_set
from optimizer.market_replay import (
    _CONTROL_PROFILE,
    _candidate_robustness,
    _coarse_profiles,
    _effective_percentage_state,
    _precompute_atr,
    _session_ticks,
    _simulate_session,
    _summary,
    _window_screen_profiles,
    run_market_replay_analysis,
)
from optimizer.market_replay_models import (
    AtrProfile,
    MarketReplayConfig,
    MarketReplaySessionResult,
)
from optimizer.market_replay_quality import assess_market_replay_session
from optimizer.market_replay_reports import write_market_replay_report
from tests.market_replay_fixtures import make_ticks, write_v2, write_v3


def _loaded(tmp_path: Path, *, sessions: int = 1):
    rows, periods = make_ticks(sessions=sessions, points_per_session=140)
    for period in periods:
        period["close_reason"] = "contract_liquid_hours"
    path = write_v3(tmp_path / "recording.ibrec", rows, periods)
    config = MarketReplayConfig(path, tmp_path / "reports")
    return config, load_ibrec(config)


def _session(
    day: str,
    result: float,
    *,
    touch_pct: float = 100.0,
    clamp_pct: float = 0.0,
    clamp_component: str | None = None,
    max_clamp: bool = False,
):
    total = 10
    min_count = 0 if max_clamp else round(total * clamp_pct / 100.0)
    max_count = round(total * clamp_pct / 100.0) if max_clamp else 0
    raw_count = total - min_count - max_count
    component_counts = {}
    component_rates = {}
    if clamp_component is not None:
        component_counts[clamp_component] = {
            "min": min_count,
            "max": max_count,
            "raw": raw_count,
            "zero": 0,
        }
        component_rates[clamp_component] = {
            "min": clamp_pct if not max_clamp else 0.0,
            "max": clamp_pct if max_clamp else 0.0,
            "raw": 100.0 - clamp_pct,
            "zero": 0.0,
        }
    return MarketReplaySessionResult(
        session_date=day,
        period_id=int(day[-2:]),
        scheduled_open_utc=f"{day}T13:30:00Z",
        scheduled_close_utc=f"{day}T20:00:00Z",
        observed_start_utc=f"{day}T13:30:00Z",
        observed_end_utc=f"{day}T20:00:00Z",
        ticks=100,
        trades=1,
        completed_trades=1,
        no_trade=False,
        open_position=False,
        realized_return_bps=result,
        marked_return_bps=result,
        conservative_return_bps=result,
        max_drawdown_bps=5.0,
        touch_liquidity_checks=total,
        touch_liquidity_sufficient_checks=round(total * touch_pct / 100.0),
        touch_liquidity_coverage_pct=touch_pct,
        clamp_min_count=min_count,
        clamp_max_count=max_count,
        clamp_raw_count=raw_count,
        clamp_total_count=total,
        clamp_min_rate_pct=clamp_pct if not max_clamp else 0.0,
        clamp_max_rate_pct=clamp_pct if max_clamp else 0.0,
        clamp_component_counts=component_counts,
        clamp_component_rates_pct=component_rates,
    )


def test_full_session_is_primary_eligible_and_partial_start_is_not(tmp_path: Path) -> None:
    config, recording = _loaded(tmp_path)
    period = recording.periods[0]
    ticks = _session_ticks(recording, period)
    quality = assess_market_replay_session(recording, period, ticks, config)
    assert quality.primary_eligible is True
    assert quality.coverage_pct == 100.0

    partial = replace(
        period,
        observed_start_timestamp=period.open_timestamp + 600.0,
        observed_start_utc="2026-01-05T14:40:00.000Z",
    )
    quality = assess_market_replay_session(recording, partial, ticks, config)
    assert quality.primary_eligible is False
    assert any("starts after" in reason for reason in quality.exclusion_reasons)


def test_complete_container_does_not_hide_a_partial_rth_tail(tmp_path: Path) -> None:
    rows, periods = make_ticks(points_per_session=121)
    path = write_v3(
        tmp_path / "partial-complete.ibrec",
        rows,
        periods,
        manifest_status="complete",
    )
    config = MarketReplayConfig(path, tmp_path / "reports")
    recording = load_ibrec(config)
    period = recording.periods[0]
    quality = assess_market_replay_session(
        recording,
        period,
        _session_ticks(recording, period),
        config,
    )
    assert quality.manifest_status == "complete"
    assert quality.source_finalized is False
    assert quality.end_lead_seconds == 300.0
    assert quality.primary_eligible is False
    assert any("ends before" in reason for reason in quality.exclusion_reasons)


def test_complete_container_does_not_override_non_normal_close_near_rth_end(
    tmp_path: Path,
) -> None:
    rows, periods = make_ticks(points_per_session=140)
    periods[0]["close_reason"] = "capture_complete"
    path = write_v3(
        tmp_path / "manual-close.ibrec",
        rows,
        periods,
        manifest_status="complete",
    )
    config = MarketReplayConfig(path, tmp_path / "reports")
    recording = load_ibrec(config)
    period = recording.periods[0]
    quality = assess_market_replay_session(
        recording,
        period,
        _session_ticks(recording, period),
        config,
    )
    assert quality.end_lead_seconds == 15.0
    assert quality.source_finalized is False
    assert quality.primary_eligible is False


def test_open_recorder_normal_contract_close_is_finalized(tmp_path: Path) -> None:
    rows, periods = make_ticks(points_per_session=140)
    periods[0]["close_reason"] = "contract_liquid_hours"
    path = write_v3(
        tmp_path / "normal-close.ibrec",
        rows,
        periods,
        manifest_status="recording",
    )
    config = MarketReplayConfig(path, tmp_path / "reports")
    recording = load_ibrec(config)
    period = recording.periods[0]
    quality = assess_market_replay_session(
        recording,
        period,
        _session_ticks(recording, period),
        config,
    )
    assert quality.manifest_status == "recording"
    assert quality.source_finalized is True
    assert quality.primary_eligible is True


def test_mixed_v2_v3_set_uses_each_periods_source_format(tmp_path: Path) -> None:
    rows, _ = make_ticks(points_per_session=140)
    v2 = write_v2(tmp_path / "day-1.ibrec", rows)
    all_rows, all_periods = make_ticks(sessions=2, points_per_session=140)
    second_rows = [row for row in all_rows if int(row["rth_period_id"]) == 2]
    first_elapsed = int(second_rows[0]["elapsed_ns"])
    normalized_rows = [
        {
            **row,
            "sequence": index,
            "elapsed_ns": int(row["elapsed_ns"]) - first_elapsed,
            "rth_period_id": 1,
        }
        for index, row in enumerate(second_rows, start=1)
    ]
    period = {
        **all_periods[1],
        "period_id": 1,
        "close_reason": "contract_liquid_hours",
        "first_tick_sequence": 1,
        "last_tick_sequence": len(normalized_rows),
        "tick_count": len(normalized_rows),
    }
    v3 = write_v3(
        tmp_path / "day-2.ibrec",
        normalized_rows,
        [period],
        manifest_status="recording",
    )
    config = MarketReplayConfig((v3, v2), tmp_path / "reports")
    recording = load_ibrec_set(config)
    assert recording.format_version == 0
    qualities = [
        assess_market_replay_session(
            recording,
            item,
            _session_ticks(recording, item),
            config,
        )
        for item in recording.periods
    ]
    assert len(qualities) == 2
    assert all(item.source_finalized for item in qualities)
    assert all(item.primary_eligible for item in qualities)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("min_atr_pct", True),
        ("min_atr_pct", "invalid"),
        ("max_atr_pct", False),
        ("max_atr_pct", object()),
    ),
)
def test_atr_clamps_reject_boolean_and_unconvertible_values(
    field: str,
    value: object,
) -> None:
    config = MarketReplayConfig(Path("recording.ibrec"), Path("reports"))
    setattr(config, field, value)
    with pytest.raises(ValueError):
        config.normalized()


def test_execution_cost_must_keep_modeled_prices_positive() -> None:
    with pytest.raises(ValueError, match="below 10,000"):
        MarketReplayConfig(
            Path("recording.ibrec"),
            Path("reports"),
            execution_cost_bps_per_side=10_000.0,
        ).normalized()


def test_connectivity_loss_blocks_session_but_restore_code_does_not(tmp_path: Path) -> None:
    config, recording = _loaded(tmp_path)
    period = recording.periods[0]
    ticks = _session_ticks(recording, period)
    lost = _quality_event(
        sequence=1,
        created_at_utc=period.schedule_open_utc,
        event_type="IBKR_UPSTREAM_DISCONNECTED",
        payload={"error_code": 1100, "message": "lost"},
    )
    restored = _quality_event(
        sequence=2,
        created_at_utc=period.schedule_open_utc,
        event_type="IBKR_UPSTREAM_RESTORED_DATA_LOST",
        payload={"error_code": 1101, "message": "restored"},
    )
    assert lost is not None and lost["disconnect"] is True
    assert restored is not None and restored["disconnect"] is False
    recording.quality_events = [lost, restored]
    quality = assess_market_replay_session(recording, period, ticks, config)
    assert quality.primary_eligible is False
    assert quality.connectivity_event_count == 1


def test_event_gap_and_last_density_are_primary_quality_gates(tmp_path: Path) -> None:
    config, recording = _loaded(tmp_path)
    period = recording.periods[0]
    ticks = _session_ticks(recording, period)
    sparse = [ticks[0], replace(ticks[-1], elapsed_ns=ticks[0].elapsed_ns + 300_000_000_000)]
    quality = assess_market_replay_session(recording, period, sparse, config)
    assert quality.primary_eligible is False
    assert quality.maximum_event_gap_seconds == 300.0
    assert quality.last_event_minute_coverage_pct < 80.0


def test_quality_gate_sees_delayed_or_frozen_rows_hidden_from_live_replay(
    tmp_path: Path,
) -> None:
    config, recording = _loaded(tmp_path)
    period = recording.periods[0]
    selected = _session_ticks(recording, period)
    assert selected and all(tick.market_data_type == 1 for tick in selected)

    recording.ticks[0] = replace(recording.ticks[0], market_data_type=3)
    recording.ticks[1] = replace(recording.ticks[1], market_data_type=2)
    selected = _session_ticks(recording, period)
    assert selected and all(tick.market_data_type == 1 for tick in selected)

    quality = assess_market_replay_session(recording, period, selected, config)
    assert quality.primary_eligible is False
    assert quality.mixed_feed is True
    assert quality.frozen_feed is True


def test_execution_cost_and_touch_size_change_replay_evidence(tmp_path: Path) -> None:
    config, recording = _loaded(tmp_path)
    ticks = _session_ticks(recording, recording.periods[0])
    profile = AtrProfile(5, 15, 1.50, 0.75, 1.00, 1.00)
    atr = _precompute_atr(ticks, profile.period, profile.bar_seconds)
    no_cost, _ = _simulate_session(
        ticks,
        recording.periods[0],
        profile,
        atr,
        recording.min_tick,
        MarketReplayConfig(
            config.recording_path,
            config.output_root,
            execution_cost_bps_per_side=0.0,
            assumed_trade_notional=10_000.0,
        ),
        keep_trades=True,
    )
    costly, trades = _simulate_session(
        ticks,
        recording.periods[0],
        profile,
        atr,
        recording.min_tick,
        MarketReplayConfig(
            config.recording_path,
            config.output_root,
            execution_cost_bps_per_side=2.0,
            assumed_trade_notional=10_000.0,
        ),
        keep_trades=True,
    )
    assert costly.conservative_return_bps < no_cost.conservative_return_bps
    assert costly.total_execution_cost_bps > 0.0
    assert trades and trades[0].assumed_quantity > 0
    assert costly.touch_liquidity_checks > 0


def test_turnover_penalty_lowers_candidate_score() -> None:
    sessions = [_session("2026-07-06", 20.0)]
    sessions[0].completed_trades = 10
    plain = _summary(
        _CONTROL_PROFILE,
        sessions,
        MarketReplayConfig(
            Path("recording.ibrec"),
            Path("reports"),
            turnover_penalty_bps_per_completed_trade=0.0,
        ),
    )
    penalized = _summary(
        _CONTROL_PROFILE,
        sessions,
        MarketReplayConfig(
            Path("recording.ibrec"),
            Path("reports"),
            turnover_penalty_bps_per_completed_trade=1.5,
        ),
    )
    assert penalized.score == plain.score - 15.0
    assert penalized.turnover_penalty_points == 15.0


def test_no_trade_day_is_not_directly_penalized() -> None:
    session = _session("2026-07-06", 0.0)
    session.trades = 0
    session.completed_trades = 0
    session.no_trade = True
    session.max_drawdown_bps = 0.0
    result = _summary(
        _CONTROL_PROFILE,
        [session],
        MarketReplayConfig(Path("recording.ibrec"), Path("reports")),
    )
    assert result.no_trade_rate_pct == 100.0
    assert result.score == 0.0


def test_minimum_clamp_is_searched_and_saturation_is_reported() -> None:
    config = MarketReplayConfig(Path("recording.ibrec"), Path("reports"))
    minimums = {profile.min_atr_pct for profile in _coarse_profiles(config, [(14, 60)])}
    assert {0.01, 0.05, 0.10, 0.20}.issubset(minimums)
    profile = AtrProfile(14, 60, 1.5, 0.75, 1.0, 1.0, min_atr_pct=0.10)
    assert _effective_percentage_state(0.01, 1.0, profile, allow_zero=False) == (
        0.10,
        "min",
    )


def test_window_screening_uses_multiple_representative_multiplier_profiles() -> None:
    profiles = _window_screen_profiles(
        MarketReplayConfig(Path("recording.ibrec"), Path("reports")),
        14,
        60,
    )
    assert len(profiles) >= 5
    assert _CONTROL_PROFILE.key() in {profile.key() for profile in profiles}
    assert len({
        (
            profile.initial_drop_multiplier,
            profile.buy_rebound_multiplier,
            profile.minimum_profit_multiplier,
            profile.sell_trail_multiplier,
        )
        for profile in profiles
    }) >= 5


def test_liquidity_and_clamp_instability_reject_changed_candidate() -> None:
    days = [f"2026-07-{day:02d}" for day in range(6, 11)]
    control_sessions = [_session(day, 0.0) for day in days]
    candidate_sessions = [
        _session(day, 25.0, touch_pct=50.0, clamp_pct=100.0) for day in days
    ]
    profile = AtrProfile(14, 60, 1.5, 0.75, 1.0, 0.75)
    config = MarketReplayConfig(
        Path("recording.ibrec"),
        Path("reports"),
        min_touch_liquidity_coverage_pct=80.0,
    )
    evidence, _ = _candidate_robustness(
        _summary(profile, candidate_sessions, config),
        candidate_sessions,
        _summary(_CONTROL_PROFILE, control_sessions, config),
        control_sessions,
        config,
        seed="quality-gates",
    )
    assert evidence["passed"] is False
    text = " ".join(evidence["failure_reasons"])
    assert "top-of-book size" in text
    assert "ATR clamp" in text


def test_changed_component_minimum_clamp_saturation_rejects_candidate() -> None:
    days = [f"2026-07-{day:02d}" for day in range(6, 11)]
    control_sessions = [_session(day, 0.0) for day in days]
    candidate_sessions = [
        _session(
            day,
            25.0,
            clamp_pct=100.0,
            clamp_component="buy_rebound",
        )
        for day in days
    ]
    profile = replace(_CONTROL_PROFILE, buy_rebound_multiplier=0.50)
    evidence, _ = _candidate_robustness(
        _summary(profile, candidate_sessions),
        candidate_sessions,
        _summary(_CONTROL_PROFILE, control_sessions),
        control_sessions,
        seed="component-min-clamp",
    )
    assert "buy_rebound" in evidence["candidate_clamp_saturated_components"]
    assert any("not identifiable" in item for item in evidence["failure_reasons"])


def test_changed_component_maximum_clamp_saturation_rejects_candidate() -> None:
    days = [f"2026-07-{day:02d}" for day in range(6, 11)]
    control_sessions = [_session(day, 0.0) for day in days]
    candidate_sessions = [
        _session(
            day,
            25.0,
            clamp_pct=100.0,
            clamp_component="sell_trail",
            max_clamp=True,
        )
        for day in days
    ]
    profile = replace(_CONTROL_PROFILE, sell_trail_multiplier=1.25)
    evidence, _ = _candidate_robustness(
        _summary(profile, candidate_sessions),
        candidate_sessions,
        _summary(_CONTROL_PROFILE, control_sessions),
        control_sessions,
        seed="component-max-clamp",
    )
    assert "sell_trail" in evidence["candidate_clamp_saturated_components"]
    assert any("not identifiable" in item for item in evidence["failure_reasons"])


def test_saturation_in_unchanged_component_does_not_trigger_component_gate() -> None:
    days = [f"2026-07-{day:02d}" for day in range(6, 11)]
    control_sessions = [_session(day, 0.0) for day in days]
    candidate_sessions = [
        _session(
            day,
            25.0,
            clamp_pct=100.0,
            clamp_component="initial_drop",
        )
        for day in days
    ]
    profile = replace(_CONTROL_PROFILE, sell_trail_multiplier=0.75)
    evidence, _ = _candidate_robustness(
        _summary(profile, candidate_sessions),
        candidate_sessions,
        _summary(_CONTROL_PROFILE, control_sessions),
        control_sessions,
        seed="unchanged-component-clamp",
    )
    assert evidence["candidate_clamp_saturated_components"] == []


def test_notional_below_one_share_cancels_setup_without_fill(tmp_path: Path) -> None:
    config, recording = _loaded(tmp_path)
    ticks = _session_ticks(recording, recording.periods[0])
    profile = AtrProfile(5, 15, 0.75, 0.0, 0.50, 0.0)
    result, trades = _simulate_session(
        ticks,
        recording.periods[0],
        profile,
        _precompute_atr(ticks, profile.period, profile.bar_seconds),
        recording.min_tick,
        MarketReplayConfig(
            config.recording_path,
            config.output_root,
            assumed_trade_notional=0.01,
        ),
        keep_trades=True,
    )
    assert result.completed_trades == 0
    assert trades == []
    assert any("could not buy one whole share" in item for item in result.issues)


def test_clamp_accounting_samples_each_component_once_per_monotonic_second(
    tmp_path: Path,
) -> None:
    config, recording = _loaded(tmp_path)
    period = recording.periods[0]
    ticks = _session_ticks(recording, period)
    duplicated = []
    for tick in ticks:
        duplicated.extend([tick, replace(tick, sequence=tick.sequence + 1_000_000)])
    profile = AtrProfile(5, 15, 0.75, 0.0, 0.50, 0.0)
    original, _ = _simulate_session(
        ticks,
        period,
        profile,
        _precompute_atr(ticks, profile.period, profile.bar_seconds),
        recording.min_tick,
        config,
        keep_trades=False,
    )
    duplicate_atr = [value for value in _precompute_atr(ticks, 5, 15) for _ in (0, 1)]
    repeated, _ = _simulate_session(
        duplicated,
        period,
        profile,
        duplicate_atr,
        recording.min_tick,
        config,
        keep_trades=False,
    )
    unique_seconds = len({tick.elapsed_ns // 1_000_000_000 for tick in duplicated})
    for counts in repeated.clamp_component_counts.values():
        assert sum(counts.values()) <= unique_seconds
    # A raw callback count would be twice the source length. Time-weighted
    # sampling remains bounded by elapsed recorder seconds instead.
    assert repeated.clamp_total_count < len(duplicated)
    assert original.clamp_total_count <= len(ticks)


def test_fewer_than_five_days_skips_expensive_authorization(tmp_path: Path) -> None:
    config, _ = _loaded(tmp_path, sessions=1)
    result = run_market_replay_analysis(config)
    assert result.recommendation.profile.key() == _CONTROL_PROFILE.key()
    assert result.robustness_evidence == []
    assert result.recommendation_leave_one_day_out == []
    assert "No data-supported ATR change" in result.recommendation_reason


def test_report_exports_quality_cost_liquidity_and_clamp_evidence(tmp_path: Path) -> None:
    config, _ = _loaded(tmp_path, sessions=1)
    result = write_market_replay_report(run_market_replay_analysis(config))
    assert (result.output_dir / "session_quality.csv").is_file()
    html = (result.output_dir / "index.html").read_text(encoding="utf-8")
    assert "Session-quality evidence" in html
    assert "execution-cost reserve" in html
    assert "Touch-size sufficiency" in html
    assert "Minimum-clamp" in html
    assert "No data-supported ATR change" in html


def test_missing_touch_size_counts_as_insufficient_liquidity_evidence(
    tmp_path: Path,
) -> None:
    config, recording = _loaded(tmp_path)
    ticks = [
        replace(tick, bid_size=None, ask_size=None)
        for tick in _session_ticks(recording, recording.periods[0])
    ]
    profile = AtrProfile(5, 15, 0.75, 0.0, 0.50, 0.0)
    atr = _precompute_atr(ticks, profile.period, profile.bar_seconds)
    result, trades = _simulate_session(
        ticks,
        recording.periods[0],
        profile,
        atr,
        recording.min_tick,
        MarketReplayConfig(
            config.recording_path,
            config.output_root,
            assumed_trade_notional=1_000.0,
        ),
        keep_trades=True,
    )
    assert result.completed_trades >= 1
    assert result.touch_liquidity_checks >= 2
    assert result.touch_liquidity_sufficient_checks == 0
    assert result.touch_liquidity_coverage_pct == 0.0
    assert trades
    assert trades[0].buy_touch_sufficient is False
    assert trades[0].buy_touch_size is None


def test_zero_multiplier_clamp_evidence_is_reported_as_a_percentage(
    tmp_path: Path,
) -> None:
    config, recording = _loaded(tmp_path)
    ticks = _session_ticks(recording, recording.periods[0])
    profile = AtrProfile(5, 15, 0.75, 0.0, 0.50, 0.0)
    atr = _precompute_atr(ticks, profile.period, profile.bar_seconds)
    result, _ = _simulate_session(
        ticks,
        recording.periods[0],
        profile,
        atr,
        recording.min_tick,
        config,
        keep_trades=False,
    )
    for component in ("buy_rebound", "sell_trail"):
        rates = result.clamp_component_rates_pct[component]
        assert rates["zero"] == 100.0
        assert sum(rates.values()) == 100.0
    summary = _summary(profile, [result], config)
    assert summary.clamp_component_rates_pct["buy_rebound"]["zero"] == 100.0
    assert summary.clamp_component_rates_pct["sell_trail"]["zero"] == 100.0
