from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import optimizer.market_replay as market_replay
from optimizer.market_replay import (
    MARKET_REPLAY_ANALYSIS_CONTRACT_VERSION,
    _bootstrap_units_across_profiles,
    _boundary_extension_profiles,
    _continuity_block_comparison,
    _copy_selection_evidence,
    _leave_one_day_out_selection_rows,
    _paired_session_list,
    _rebase_validation_drawdown,
    _resolve_boundary_evidence,
    _selection_aware_bootstrap,
    _summary,
    market_replay_search_contract,
)
from optimizer.market_replay_models import (
    AtrProfile,
    IbrecPeriod,
    MarketReplayCandidateSummary,
    MarketReplayConfig,
    MarketReplaySessionResult,
)
from optimizer.market_replay_reports import _FILE_NAMES, _balanced_score_formula
from optimizer.market_replay_validation import (
    BALANCED_SCORE_POLICY,
    COST_STRESSED_SCORE_POLICY,
    continuity_block_metrics,
    deterministic_moving_block_indices,
    expanding_walk_forward_folds,
    moving_block_length,
    pareto_dominates,
    pareto_frontier,
    recommendation_gate_rows,
    score_policy_comparison,
    score_policy_contract,
    score_sessions,
)


def _profile(**changes: Any) -> AtrProfile:
    base = AtrProfile(14, 60, 1.50, 0.75, 1.00, 1.00)
    return replace(base, **changes)


def _session(
    day: str,
    period_id: int,
    return_bps: float,
    *,
    drawdown_bps: float = 10.0,
    trades: int = 1,
    chain_id: int = 1,
    start_equity: float = 1.0,
    end_equity: float | None = None,
    carried_in: bool = False,
    carried_out: bool = False,
    right_censored: bool = False,
    open_position: bool = False,
    execution_cost_bps: float = 2.0,
) -> MarketReplaySessionResult:
    final_equity = (
        float(end_equity)
        if end_equity is not None
        else start_equity * (1.0 + return_bps / 10_000.0)
    )
    return MarketReplaySessionResult(
        session_date=day,
        period_id=period_id,
        scheduled_open_utc=f"{day}T13:30:00+00:00",
        scheduled_close_utc=f"{day}T20:00:00+00:00",
        observed_start_utc=f"{day}T13:30:00+00:00",
        observed_end_utc=f"{day}T20:00:00+00:00",
        ticks=100,
        trades=trades,
        completed_trades=trades,
        no_trade=trades == 0,
        open_position=open_position,
        realized_return_bps=return_bps,
        marked_return_bps=return_bps,
        conservative_return_bps=return_bps,
        max_drawdown_bps=drawdown_bps,
        session_max_drawdown_bps=drawdown_bps,
        chain_max_drawdown_bps=drawdown_bps,
        right_censored=right_censored,
        terminal_open_position=open_position,
        total_execution_cost_bps=execution_cost_bps,
        continuity_chain_id=chain_id,
        carried_position_in=carried_in,
        carried_position_out=carried_out,
        session_start_equity=start_equity,
        session_end_equity=final_equity,
        cumulative_end_equity=final_equity,
    )


def _candidate(profile: AtrProfile, *, score: float, return_bps: float = 10.0) -> MarketReplayCandidateSummary:
    return MarketReplayCandidateSummary(
        profile=profile,
        score=score,
        sessions=5,
        completed_sessions=5,
        sessions_with_trades=5,
        completed_trades=5,
        open_position_sessions=0,
        no_trade_sessions=0,
        median_return_bps=return_bps,
        mean_return_bps=return_bps,
        worst_return_bps=return_bps - 2.0,
        maximum_drawdown_bps=10.0,
        open_position_rate_pct=0.0,
        no_trade_rate_pct=0.0,
        average_completed_trades_per_session=1.0,
        total_execution_cost_bps=2.0,
    )


def test_analysis_contract_is_v13_and_single_sources_score_policies(tmp_path: Path) -> None:
    contract = market_replay_search_contract(
        MarketReplayConfig(tmp_path / "sample.ibrec", tmp_path / "reports")
    )
    assert MARKET_REPLAY_ANALYSIS_CONTRACT_VERSION == 13
    assert contract["contract_version"] == 13
    assert contract["score_policies"] == score_policy_contract()
    assert any(
        "selection-aware out-of-bag bootstrap" in item
        for item in contract["changed_recommendation_requires"]
    )


def test_balanced_score_has_no_direct_no_trade_penalty() -> None:
    no_trade = _session("2026-07-01", 1, 0.0, trades=0, drawdown_bps=0.0)
    result = score_sessions(
        [no_trade], turnover_penalty_bps_per_completed_trade=10.0
    )
    assert result["score"] == pytest.approx(0.0)
    assert result["average_completed_trades"] == 0.0
    assert result["turnover_penalty_points"] == 0.0


def test_summary_uses_the_same_balanced_score_policy() -> None:
    sessions = [
        _session("2026-07-01", 1, 20.0, drawdown_bps=5.0),
        _session("2026-07-02", 2, -5.0, drawdown_bps=15.0),
    ]
    config = MarketReplayConfig(Path("sample.ibrec"), Path("reports"))
    expected = score_sessions(
        sessions,
        turnover_penalty_bps_per_completed_trade=(
            config.normalized().turnover_penalty_bps_per_completed_trade
        ),
        policy=BALANCED_SCORE_POLICY,
    )
    assert _summary(_profile(), sessions, config).score == pytest.approx(
        expected["score"]
    )


def test_cost_stressed_policy_cannot_improve_a_costly_profile() -> None:
    sessions = [_session("2026-07-01", 1, 20.0, execution_cost_bps=12.0)]
    balanced = score_sessions(
        sessions,
        turnover_penalty_bps_per_completed_trade=1.0,
        policy=BALANCED_SCORE_POLICY,
    )
    stressed = score_sessions(
        sessions,
        turnover_penalty_bps_per_completed_trade=1.0,
        policy=COST_STRESSED_SCORE_POLICY,
    )
    assert stressed["score"] < balanced["score"]


def test_score_policy_comparison_reports_every_policy() -> None:
    candidate = [_session("2026-07-01", 1, 30.0)]
    control = [_session("2026-07-01", 1, 0.0)]
    rows = score_policy_comparison(
        candidate,
        control,
        turnover_penalty_bps_per_completed_trade=0.0,
    )
    assert {row["policy_key"] for row in rows} == {
        "balanced",
        "drawdown_focused",
        "return_focused",
        "cost_stressed",
    }
    assert all(row["passed"] for row in rows)


def test_pareto_frontier_excludes_fully_dominated_profile() -> None:
    strong = _candidate(_profile(sell_trail_multiplier=0.75), score=20.0)
    weak = _candidate(_profile(sell_trail_multiplier=1.25), score=10.0, return_bps=5.0)
    weak.maximum_drawdown_bps = 20.0
    weak.right_censored_rate_pct = 10.0
    assert pareto_dominates(strong, weak)
    assert pareto_frontier([strong, weak]) == {strong.profile.key()}


def test_continuity_block_metrics_compound_linked_sessions() -> None:
    first = _session(
        "2026-07-01",
        1,
        100.0,
        chain_id=7,
        start_equity=1.0,
        end_equity=1.01,
        carried_out=True,
    )
    second = _session(
        "2026-07-02",
        2,
        100.0,
        chain_id=7,
        start_equity=1.01,
        end_equity=1.0201,
        carried_in=True,
    )
    rows = continuity_block_metrics([first, second])
    assert len(rows) == 1
    assert rows[0]["session_count"] == 2
    assert rows[0]["overnight_boundaries"] == 1
    assert rows[0]["block_return_bps"] == pytest.approx(201.0)


def test_continuity_block_comparison_rejects_extra_open_position() -> None:
    control = [_session("2026-07-01", 1, 0.0, chain_id=1)]
    candidate = [
        _session(
            "2026-07-01",
            1,
            10.0,
            chain_id=1,
            right_censored=True,
            open_position=True,
        )
    ]
    evidence = _continuity_block_comparison(candidate, control)
    assert evidence["passed"] is False
    assert any("open" in reason.lower() for reason in evidence["failure_reasons"])


@pytest.mark.parametrize(
    ("count", "expected"),
    [(14, None), (15, 2), (29, 2), (30, 3), (60, 4)],
)
def test_moving_block_length_policy(count: int, expected: int | None) -> None:
    assert moving_block_length(count) == expected


def test_moving_block_samples_are_deterministic_and_complete() -> None:
    first = list(
        deterministic_moving_block_indices(
            "seed", replicates=4, unit_count=17, block_length=2
        )
    )
    second = list(
        deterministic_moving_block_indices(
            "seed", replicates=4, unit_count=17, block_length=2
        )
    )
    assert first == second
    assert all(len(sample) == 17 for sample in first)
    assert all(0 <= index < 17 for sample in first for index in sample)


def test_expanding_walk_forward_folds_never_leak_validation_into_training() -> None:
    dates = [f"2026-07-{day:02d}" for day in range(1, 31)]
    folds = expanding_walk_forward_folds(
        dates, minimum_training_days=15, validation_days=5
    )
    assert [len(train) for train, _ in folds] == [15, 20, 25]
    assert all(set(train).isdisjoint(validation) for train, validation in folds)
    assert all(max(train) < min(validation) for train, validation in folds)


def test_rebase_validation_drawdown_discards_training_chain_peak() -> None:
    inherited = _session("2026-07-20", 20, 5.0, drawdown_bps=12.0)
    inherited.chain_max_drawdown_bps = 500.0
    inherited.max_drawdown_bps = 500.0
    rebased = _rebase_validation_drawdown([inherited])
    assert rebased[0].chain_max_drawdown_bps == pytest.approx(12.0)
    assert rebased[0].max_drawdown_bps == pytest.approx(12.0)


def test_paired_session_list_uses_identity_not_input_order() -> None:
    left = [
        _session("2026-07-02", 2, 2.0),
        _session("2026-07-01", 1, 1.0),
    ]
    right = [
        _session("2026-07-01", 1, 0.0),
        _session("2026-07-02", 2, 0.0),
    ]
    pairs = _paired_session_list(left, right)
    assert [(a.session_date, b.session_date) for a, b in pairs] == [
        ("2026-07-01", "2026-07-01"),
        ("2026-07-02", "2026-07-02"),
    ]


def test_paired_session_list_rejects_different_session_identity() -> None:
    with pytest.raises(market_replay.MarketReplayAnalysisError):
        _paired_session_list(
            [_session("2026-07-01", 1, 1.0)],
            [_session("2026-07-02", 2, 0.0)],
        )


def test_bootstrap_units_use_overnight_dependency_from_any_profile() -> None:
    flat = [
        _session("2026-07-01", 1, 0.0, chain_id=1),
        _session("2026-07-02", 2, 0.0, chain_id=2),
    ]
    linked = [
        _session("2026-07-01", 1, 0.0, chain_id=1, carried_out=True),
        _session("2026-07-02", 2, 0.0, chain_id=1, carried_in=True),
    ]
    unit_type, units = _bootstrap_units_across_profiles([flat, linked])
    assert unit_type == "overnight_continuity_block"
    assert units == [("2026-07-01", "2026-07-02")]


def test_boundary_extension_probes_multiplier_edge() -> None:
    center = _candidate(_profile(sell_trail_multiplier=1.25), score=20.0)
    center.clamp_min_rate_pct = 0.0
    candidates = [
        _candidate(_profile(sell_trail_multiplier=value), score=20.0)
        for value in (0.75, 1.0, 1.25)
    ]
    probes, rows = _boundary_extension_profiles(center, candidates)
    assert any(
        row["dimension"] == "sell_trail_multiplier"
        and row["direction"] == "upper"
        for row in rows
    )
    assert any(profile.sell_trail_multiplier == 1.50 for profile in probes)


def test_boundary_evidence_remains_unresolved_when_probe_is_near_best() -> None:
    center = _candidate(_profile(sell_trail_multiplier=1.25), score=100.0)
    probe = _candidate(_profile(sell_trail_multiplier=1.50), score=95.0)
    rows = [
        {
            "profile_key": center.profile.key(),
            "dimension": "sell_trail_multiplier",
            "direction": "upper",
            "boundary_value": 1.25,
            "probe_value": 1.50,
            "probe_profile_key": probe.profile.key(),
        }
    ]
    evidence = _resolve_boundary_evidence(
        center, rows, {probe.profile.key(): probe}
    )
    assert evidence["resolved"] is False
    assert evidence["unresolved_dimensions"] == ["sell_trail_multiplier"]


def test_boundary_evidence_resolves_when_probe_falls_outside_plateau() -> None:
    center = _candidate(_profile(sell_trail_multiplier=1.25), score=100.0)
    probe = _candidate(_profile(sell_trail_multiplier=1.50), score=70.0)
    rows = [
        {
            "profile_key": center.profile.key(),
            "dimension": "sell_trail_multiplier",
            "direction": "upper",
            "boundary_value": 1.25,
            "probe_value": 1.50,
            "probe_profile_key": probe.profile.key(),
        }
    ]
    assert _resolve_boundary_evidence(
        center, rows, {probe.profile.key(): probe}
    )["resolved"]


def test_recommendation_gates_are_sorted_and_required_by_default() -> None:
    rows = recommendation_gate_rows(
        [
            {"gate_key": "b", "passed": True},
            {"gate_key": "a", "passed": False, "detail": "failed"},
        ]
    )
    assert [row["gate_key"] for row in rows] == ["a", "b"]
    assert all(row["required"] for row in rows)


def test_copy_selection_evidence_copies_new_v190_fields() -> None:
    source = _candidate(_profile(), score=1.0)
    target = _candidate(_profile(), score=1.0)
    source.moving_block_replicates = 2000
    source.walk_forward_folds = 3
    source.score_policy_all_positive = True
    source.pareto_frontier = True
    source.boundary_resolved = True
    source.assumption_stress_all_positive = True
    source.recommendation_gates = [{"gate_key": "a", "passed": True}]
    _copy_selection_evidence(source, target)
    assert target.moving_block_replicates == 2000
    assert target.walk_forward_folds == 3
    assert target.score_policy_all_positive
    assert target.pareto_frontier
    assert target.boundary_resolved
    assert target.assumption_stress_all_positive
    assert target.recommendation_gates == source.recommendation_gates


def test_leave_one_day_out_rebuilds_raw_chronology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    periods = [
        (
            IbrecPeriod(
                period_id=index,
                session_date=f"2026-07-0{index}",
                schedule_open_utc="",
                schedule_close_utc="",
                open_timestamp=float(index),
                close_timestamp=float(index + 1),
                observed_start_utc="",
                observed_end_utc="",
                observed_start_timestamp=float(index),
                observed_end_timestamp=float(index + 1),
                status="closed",
                close_reason="normal",
                tick_count=0,
            ),
            [],
        )
        for index in range(1, 4)
    ]
    calls: list[tuple[str, ...]] = []
    selected = _candidate(_profile(), score=2.0)
    control = _candidate(_profile(), score=1.0)

    def fake_selector(
        _recording: Any,
        subset: list[tuple[IbrecPeriod, list[Any]]],
        _config: MarketReplayConfig,
        **_kwargs: Any,
    ) -> Any:
        calls.append(tuple(period.session_date for period, _ in subset))
        return SimpleNamespace(
            selected=selected,
            control=control,
            selected_windows=[(14, 60)],
            candidates=[selected, control],
            reason="test",
        )

    monkeypatch.setattr(market_replay, "_run_bounded_selector", fake_selector)
    rows = _leave_one_day_out_selection_rows(
        SimpleNamespace(),
        periods,
        MarketReplayConfig(Path("sample.ibrec"), Path("reports")),
    )
    assert len(rows) == 3
    assert all(len(call) == 2 for call in calls)
    assert all(row["selection_mode"] == "exact_chronology_rebuilt" for row in rows)


def test_selection_aware_bootstrap_is_deterministic_with_mocked_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(market_replay, "_MIN_ADVANCED_VALIDATION_DAYS", 5)
    monkeypatch.setattr(market_replay, "_SELECTION_BOOTSTRAP_REPLICATES", 8)
    profile = _profile(sell_trail_multiplier=0.75)
    days = [f"2026-07-{day:02d}" for day in range(1, 7)]
    candidate_sessions = [
        _session(day, index, 10.0, chain_id=index)
        for index, day in enumerate(days, start=1)
    ]
    control_sessions = [
        _session(day, index, 0.0, chain_id=index)
        for index, day in enumerate(days, start=1)
    ]
    period_ticks = [
        (
            IbrecPeriod(
                period_id=index,
                session_date=day,
                schedule_open_utc="",
                schedule_close_utc="",
                open_timestamp=float(index),
                close_timestamp=float(index + 1),
                observed_start_utc="",
                observed_end_utc="",
                observed_start_timestamp=float(index),
                observed_end_timestamp=float(index + 1),
                status="closed",
                close_reason="normal",
                tick_count=0,
            ),
            [],
        )
        for index, day in enumerate(days, start=1)
    ]

    def fake_selector(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(selected=SimpleNamespace(profile=profile))

    def fake_eval(
        _recording: Any,
        subset: list[tuple[IbrecPeriod, list[Any]]],
        selected_profile: AtrProfile,
        _config: MarketReplayConfig,
    ) -> tuple[MarketReplayCandidateSummary, list[MarketReplaySessionResult]]:
        source = candidate_sessions if selected_profile.key() == profile.key() else control_sessions
        dates = {period.session_date for period, _ in subset}
        sessions = [item for item in source if item.session_date in dates]
        return _summary(selected_profile, sessions), sessions

    monkeypatch.setattr(market_replay, "_run_bounded_selector", fake_selector)
    monkeypatch.setattr(market_replay, "_evaluate_profile_fresh", fake_eval)
    config = MarketReplayConfig(Path("sample.ibrec"), Path("reports"))
    first = _selection_aware_bootstrap(
        SimpleNamespace(),
        period_ticks,
        profile,
        candidate_sessions,
        control_sessions,
        config,
        seed="deterministic",
    )
    second = _selection_aware_bootstrap(
        SimpleNamespace(),
        period_ticks,
        profile,
        candidate_sessions,
        control_sessions,
        config,
        seed="deterministic",
    )
    assert first == second


def test_report_contract_includes_all_v190_evidence_files() -> None:
    required = {
        "continuity_block_evidence.csv",
        "score_policy_evidence.csv",
        "moving_block_evidence.csv",
        "selection_bootstrap_evidence.csv",
        "walk_forward_evidence.csv",
        "pareto_frontier.csv",
        "search_boundary_evidence.csv",
        "assumption_stress_evidence.csv",
        "recommendation_quality_gates.csv",
    }
    assert required <= set(_FILE_NAMES)


def test_score_formula_is_rendered_from_contract() -> None:
    contract = {
        "score_policies": score_policy_contract(),
    }
    text = _balanced_score_formula(SimpleNamespace(search_contract=contract))
    assert "0.50 × median" in text
    assert "0.35 × maximum drawdown" in text
    assert "no-trade" not in text.lower()
