from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from optimizer.evidence import apply_execution_model, enrich_candidate_evidence
from optimizer.models import ReplayObservation
from optimizer.replay import summarize_observations


def _observation(
    *,
    cycle: int,
    candidate: str,
    multiplier: float,
    improvement: float | None,
    control: bool = False,
    day: int | None = None,
    triggered: bool = True,
    observed_seconds: float = 120.0,
    delay_seconds: float | None = None,
) -> ReplayObservation:
    actual_day = day if day is not None else cycle
    observation_start = datetime(2026, 1, actual_day, 14, 0, tzinfo=UTC)
    observation_end = observation_start + timedelta(seconds=observed_seconds)
    return ReplayObservation(
        cycle_id=f"cycle-{cycle}",
        cycle_number=cycle,
        ticker="AAPL",
        leg="buy",
        candidate_key=candidate,
        multiplier=multiplier,
        minimum_profit_multiplier=None,
        period=14,
        bar_seconds=60,
        effective_pct=0.5,
        triggered=triggered,
        baseline_window=True,
        control_candidate=control,
        outcome="triggered" if triggered else "right_censored",
        right_censored=not triggered,
        observation_start_utc=observation_start.isoformat(),
        observation_end_utc=observation_end.isoformat(),
        observed_seconds=observed_seconds,
        time_to_trigger_seconds=observed_seconds if triggered else None,
        trading_day_utc=f"2026-01-{actual_day:02d}",
        actual_fill_price=100.0,
        actual_fill_time_utc=f"2026-01-{actual_day:02d}T15:00:00+00:00",
        trigger_price=99.0 if triggered else None,
        price_improvement_bps=improvement,
        adjusted_price_improvement_bps=improvement,
        execution_model_samples=5,
        delay_seconds=(
            (0.0 if delay_seconds is None else delay_seconds)
            if triggered
            else None
        ),
        post_trigger_mfe_bps=20.0 if triggered else None,
        post_trigger_mae_bps=2.0 if triggered else None,
        atr_source="capture_reconstructed",
    )


def _enriched(observations: list[ReplayObservation]):
    summaries = summarize_observations(observations)
    methodology = enrich_candidate_evidence(
        observations,
        summaries,
        {
            "buy": {
                "touch_residual_samples": 10,
                "trigger_to_fill_samples": 10,
            },
            "sell": {},
        },
    )
    return {row.candidate_key: row for row in summaries}, methodology


def test_paired_comparison_uses_only_identical_cycle_intersection() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        observations.extend(
            [
                _observation(
                    cycle=cycle,
                    candidate="control",
                    multiplier=0.75,
                    improvement=0.0,
                    control=True,
                ),
                _observation(
                    cycle=cycle,
                    candidate="candidate",
                    multiplier=0.60,
                    improvement=10.0,
                ),
            ]
        )
    observations.append(
        _observation(
            cycle=6,
            candidate="candidate",
            multiplier=0.60,
            improvement=500.0,
        )
    )

    summaries, _ = _enriched(observations)
    paired = summaries["candidate"].paired_evidence

    assert paired["paired_windows"] == 5
    assert paired["paired_both_triggered"] == 5
    assert paired["paired_execution_adjusted_median_bps"] == 10.0
    assert paired["independent_trading_days"] == 5


def test_right_censored_windows_are_not_misses_and_km_requires_horizon_support() -> None:
    observations = [
        _observation(
            cycle=1,
            candidate="candidate",
            multiplier=0.60,
            improvement=10.0,
            triggered=True,
            observed_seconds=60.0,
        ),
        _observation(
            cycle=2,
            candidate="candidate",
            multiplier=0.60,
            improvement=None,
            triggered=False,
            observed_seconds=120.0,
        ),
        _observation(
            cycle=3,
            candidate="candidate",
            multiplier=0.60,
            improvement=None,
            triggered=False,
            observed_seconds=30.0,
        ),
    ]

    summary = summarize_observations(observations)[0]

    assert summary.right_censored_observations == 2
    assert summary.unavailable_observations == 0
    assert summary.screening_score == pytest.approx(9.9)
    assert summary.km_trigger_probability_1m_pct == 50.0
    assert summary.km_trigger_probability_5m_pct is None
    assert summary.km_trigger_probability_15m_pct is None


def test_cluster_bootstrap_and_pairing_are_input_order_independent() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        observations.extend(
            [
                _observation(
                    cycle=cycle,
                    candidate="control",
                    multiplier=0.75,
                    improvement=0.0,
                    control=True,
                ),
                _observation(
                    cycle=cycle,
                    candidate="candidate-a",
                    multiplier=0.60,
                    improvement=10.0 + cycle,
                ),
                _observation(
                    cycle=cycle,
                    candidate="candidate-b",
                    multiplier=0.65,
                    improvement=9.0 + cycle,
                ),
            ]
        )

    first, first_method = _enriched(deepcopy(observations))
    second, second_method = _enriched(list(reversed(deepcopy(observations))))

    assert first["candidate-a"].paired_evidence == second["candidate-a"].paired_evidence
    assert first["candidate-a"].stable_region == second["candidate-a"].stable_region
    assert first_method == second_method


def test_leave_one_day_out_detects_a_single_cluster_that_can_reverse_result() -> None:
    observations: list[ReplayObservation] = []
    cycle = 1
    for day in range(1, 5):
        observations.extend(
            [
                _observation(
                    cycle=cycle,
                    day=day,
                    candidate="control",
                    multiplier=0.75,
                    improvement=0.0,
                    control=True,
                ),
                _observation(
                    cycle=cycle,
                    day=day,
                    candidate="candidate",
                    multiplier=0.60,
                    improvement=10.0,
                ),
            ]
        )
        cycle += 1
    for _ in range(3):
        observations.extend(
            [
                _observation(
                    cycle=cycle,
                    day=5,
                    candidate="control",
                    multiplier=0.75,
                    improvement=0.0,
                    control=True,
                ),
                _observation(
                    cycle=cycle,
                    day=5,
                    candidate="candidate",
                    multiplier=0.60,
                    improvement=-100.0,
                ),
            ]
        )
        cycle += 1

    summaries, _ = _enriched(observations)
    candidate = summaries["candidate"]
    influence = candidate.paired_evidence["leave_one_day_out"]

    assert candidate.paired_evidence["paired_execution_adjusted_median_bps"] == 10.0
    assert influence["minimum_bps"] < 0.0
    assert influence["sign_reversals"] >= 1
    assert candidate.evidence_stable is False
    assert any("leave-one-day-out" in reason for reason in candidate.instability_reasons)


def test_stable_region_requires_true_grid_adjacency_not_subset_adjacency() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        observations.extend(
            [
                _observation(
                    cycle=cycle,
                    candidate="control",
                    multiplier=0.75,
                    improvement=0.0,
                    control=True,
                ),
                _observation(
                    cycle=cycle,
                    candidate="far-low",
                    multiplier=0.50,
                    improvement=10.0,
                ),
                _observation(
                    cycle=cycle,
                    candidate="far-high",
                    multiplier=1.00,
                    improvement=9.5,
                ),
            ]
        )

    summaries, _ = _enriched(observations)

    assert summaries["far-low"].stable_region["size"] == 1
    assert summaries["far-high"].stable_region["size"] == 1
    assert summaries["far-low"].evidence_stable is False
    assert summaries["far-high"].evidence_stable is False


def test_adjacent_near_best_candidates_produce_one_stable_region_center() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        observations.extend(
            [
                _observation(
                    cycle=cycle,
                    candidate="control",
                    multiplier=0.75,
                    improvement=0.0,
                    control=True,
                ),
                _observation(
                    cycle=cycle,
                    candidate="candidate-a",
                    multiplier=0.60,
                    improvement=10.0,
                ),
                _observation(
                    cycle=cycle,
                    candidate="candidate-b",
                    multiplier=0.65,
                    improvement=9.5,
                ),
            ]
        )

    summaries, methodology = _enriched(observations)
    stable = [
        row.candidate_key for row in summaries.values() if row.evidence_stable
    ]

    assert stable == ["candidate-a"]
    assert summaries["candidate-a"].stable_region["size"] == 2
    assert summaries["candidate-a"].stable_region["supported"] is True
    assert summaries["candidate-a"].stable_region["is_center"] is True
    assert methodology["stable_candidate_keys"] == ["candidate-a"]


def test_zero_pair_execution_samples_are_not_replaced_by_aggregate_count() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        control = _observation(
            cycle=cycle,
            candidate="control",
            multiplier=0.75,
            improvement=0.0,
            control=True,
        )
        candidate = _observation(
            cycle=cycle,
            candidate="candidate",
            multiplier=0.60,
            improvement=10.0,
        )
        control.execution_model_samples = 0
        candidate.execution_model_samples = 0
        observations.extend([control, candidate])

    summaries = summarize_observations(observations)
    enrich_candidate_evidence(
        observations,
        summaries,
        {
            "buy": {
                "touch_residual_samples": 20,
                "trigger_to_fill_samples": 20,
            },
            "sell": {},
        },
    )
    changed = next(row for row in summaries if row.candidate_key == "candidate")

    assert changed.paired_evidence["minimum_execution_model_samples"] == 0
    assert any("Only 0 empirical execution samples" in reason for reason in changed.instability_reasons)
    assert changed.evidence_stable is False


def test_execution_model_uses_spread_and_leave_one_cycle_out_slippage() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 4):
        observations.append(
            ReplayObservation(
                cycle_id=f"cycle-{cycle}",
                cycle_number=cycle,
                ticker="AAPL",
                leg="buy",
                candidate_key="control",
                multiplier=0.75,
                minimum_profit_multiplier=None,
                period=14,
                bar_seconds=60,
                effective_pct=0.5,
                triggered=True,
                control_candidate=True,
                outcome="triggered",
                trigger_price=99.0,
                trigger_bid=99.0,
                trigger_ask=99.1,
                actual_fill_price=100.2,
                fill_reference_price=100.0,
                fill_bid=99.9,
                fill_ask=100.0,
                fill_spread_bps=10.0,
                fill_quote_age_seconds=0.5,
                trading_day_utc=f"2026-01-{cycle:02d}",
                observed_seconds=60.0,
                atr_source="capture_reconstructed",
            )
        )

    model = apply_execution_model(observations)
    row = observations[0]

    assert model["buy"]["touch_residual_samples"] == 3
    assert row.execution_model_leave_one_out is True
    assert row.execution_model_samples == 2
    assert row.estimated_fill_price == pytest.approx(99.2982)
    assert row.execution_adjustment_bps > 0.0
    assert row.adjusted_price_improvement_bps > 0.0


def test_duplicate_candidate_cycle_rows_are_excluded_and_make_evidence_unstable() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        observations.extend(
            [
                _observation(
                    cycle=cycle,
                    candidate="control",
                    multiplier=0.75,
                    improvement=0.0,
                    control=True,
                ),
                _observation(
                    cycle=cycle,
                    candidate="candidate",
                    multiplier=0.60,
                    improvement=10.0,
                ),
            ]
        )
    observations.append(
        _observation(
            cycle=3,
            candidate="candidate",
            multiplier=0.60,
            improvement=12.0,
        )
    )

    summaries, _ = _enriched(observations)
    candidate = summaries["candidate"]

    assert candidate.paired_evidence["paired_cycles"] == 4
    assert candidate.paired_evidence[
        "candidate_duplicate_cycle_keys_excluded"
    ] == ["cycle-3"]
    assert candidate.evidence_stable is False
    assert any("Duplicate candidate observations" in reason for reason in candidate.instability_reasons)


def test_candidate_control_context_mismatch_is_excluded_fail_closed() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        control = _observation(
            cycle=cycle,
            candidate="control",
            multiplier=0.75,
            improvement=0.0,
            control=True,
        )
        candidate = _observation(
            cycle=cycle,
            candidate="candidate",
            multiplier=0.60,
            improvement=10.0,
        )
        if cycle == 2:
            candidate.actual_fill_price = 101.0
        observations.extend([control, candidate])

    summaries, _ = _enriched(observations)
    candidate = summaries["candidate"]

    assert candidate.paired_evidence["paired_cycles"] == 4
    assert candidate.paired_evidence[
        "context_mismatch_cycle_keys_excluded"
    ] == {"cycle-2": "actual fill price"}
    assert any("immutable cycle context" in reason for reason in candidate.instability_reasons)
    assert candidate.evidence_stable is False


def test_candidate_control_execution_context_mismatch_is_excluded_fail_closed() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        control = _observation(
            cycle=cycle,
            candidate="control",
            multiplier=0.75,
            improvement=0.0,
            control=True,
        )
        candidate = _observation(
            cycle=cycle,
            candidate="candidate",
            multiplier=0.60,
            improvement=10.0,
        )
        control.fill_reference_price = 100.0
        candidate.fill_reference_price = 100.0
        if cycle == 2:
            candidate.fill_reference_price = None
        observations.extend([control, candidate])

    summaries, _ = _enriched(observations)
    candidate = summaries["candidate"]

    assert candidate.paired_evidence["paired_cycles"] == 4
    assert candidate.paired_evidence[
        "context_mismatch_cycle_keys_excluded"
    ] == {"cycle-2": "fill reference price"}
    assert candidate.evidence_stable is False


def test_multiple_control_candidates_make_all_changed_candidates_unstable() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        observations.extend(
            [
                _observation(
                    cycle=cycle,
                    candidate="control-a",
                    multiplier=0.75,
                    improvement=0.0,
                    control=True,
                ),
                _observation(
                    cycle=cycle,
                    candidate="control-b",
                    multiplier=0.80,
                    improvement=0.0,
                    control=True,
                ),
                _observation(
                    cycle=cycle,
                    candidate="candidate",
                    multiplier=0.60,
                    improvement=10.0,
                ),
            ]
        )

    summaries, _ = _enriched(observations)
    candidate = summaries["candidate"]

    assert candidate.paired_evidence["paired_cycles"] == 0
    assert any("More than one evaluation-control" in reason for reason in candidate.instability_reasons)
    assert candidate.evidence_stable is False


def test_execution_model_prefers_complete_cycle_sample_and_excludes_stale_fill_context() -> None:
    incomplete = _observation(
        cycle=1,
        candidate="control",
        multiplier=0.75,
        improvement=0.0,
        control=True,
    )
    incomplete.trigger_price = 99.0
    incomplete.actual_fill_price = 100.2
    incomplete.fill_reference_price = 100.0
    incomplete.fill_quote_age_seconds = 1.0

    complete = deepcopy(incomplete)
    complete.candidate_key = "candidate"
    complete.control_candidate = False
    complete.fill_bid = 99.9
    complete.fill_ask = 100.0
    complete.fill_spread_bps = 10.0

    stale = _observation(
        cycle=2,
        candidate="control",
        multiplier=0.75,
        improvement=0.0,
        control=True,
    )
    stale.trigger_price = 99.0
    stale.actual_fill_price = 100.2
    stale.fill_reference_price = 100.0
    stale.fill_bid = 99.9
    stale.fill_ask = 100.0
    stale.fill_spread_bps = 10.0
    stale.fill_quote_age_seconds = 10.0

    model = apply_execution_model([incomplete, complete, stale])

    assert model["buy"]["fills_considered"] == 2
    assert model["buy"]["touch_residual_samples"] == 1
    assert model["buy"]["trigger_to_fill_samples"] == 1
    assert model["buy"]["fresh_touch_quote_samples"] == 1
    assert model["buy"]["fresh_reference_quote_samples"] == 1
    assert model["buy"]["stale_or_unknown_touch_quote_samples"] == 1
    assert model["buy"]["stale_or_unknown_reference_quote_samples"] == 1


def test_conflicting_fill_context_for_one_cycle_is_excluded_fail_closed() -> None:
    control = _observation(
        cycle=1,
        candidate="control",
        multiplier=0.75,
        improvement=0.0,
        control=True,
    )
    candidate = deepcopy(control)
    candidate.candidate_key = "candidate"
    candidate.control_candidate = False
    control.trigger_price = 99.0
    candidate.trigger_price = 99.0
    control.actual_fill_price = 100.0
    candidate.actual_fill_price = 101.0
    for row in (control, candidate):
        row.fill_reference_price = 99.5
        row.fill_bid = 99.4
        row.fill_ask = 99.5
        row.fill_spread_bps = 10.05
        row.fill_quote_age_seconds = 0.5

    model = apply_execution_model([control, candidate])

    assert model["buy"]["fills_considered"] == 0
    assert model["buy"]["conflicting_cycle_contexts_excluded"] == 1
    assert model["buy"]["conflicting_cycle_context_fields"] == {
        "cycle-1": ["actual fill price"]
    }
    assert control.execution_model_samples == 0
    assert candidate.execution_model_samples == 0

    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        observations.extend(
            [
                _observation(
                    cycle=cycle,
                    candidate="control",
                    multiplier=0.75,
                    improvement=0.0,
                    control=True,
                ),
                _observation(
                    cycle=cycle,
                    candidate="candidate",
                    multiplier=0.60,
                    improvement=10.0,
                ),
            ]
        )
    summaries = summarize_observations(observations)
    enrich_candidate_evidence(observations, summaries, model)
    changed = next(row for row in summaries if row.candidate_key == "candidate")
    assert any(
        "execution model excluded conflicting" in reason
        for reason in changed.instability_reasons
    )
    assert changed.evidence_stable is False


def test_crossed_fill_quote_is_excluded_and_trigger_quote_falls_back_to_last() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 7):
        row = _observation(
            cycle=cycle,
            candidate="control",
            multiplier=0.75,
            improvement=0.0,
            control=True,
        )
        row.trigger_price = 99.0
        row.trigger_bid = 100.1
        row.trigger_ask = 100.0
        row.actual_fill_price = 100.2
        row.fill_reference_price = 100.0
        row.fill_bid = 100.1 if cycle == 1 else 99.9
        row.fill_ask = 100.0
        row.fill_spread_bps = 10.0
        row.fill_quote_age_seconds = 0.5
        observations.append(row)

    model = apply_execution_model(observations)

    assert model["buy"]["crossed_touch_quotes_excluded"] == 1
    assert model["buy"]["fresh_touch_quote_samples"] == 5
    assert model["buy"]["stale_or_unknown_touch_quote_samples"] == 0
    assert observations[0].execution_model_method.startswith("Last trigger")
    assert "ask touch" not in observations[0].execution_model_method


def test_longest_shared_km_horizon_can_fall_back_to_five_minutes() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 7):
        triggered = cycle < 6
        observations.extend(
            [
                _observation(
                    cycle=cycle,
                    candidate="control",
                    multiplier=0.75,
                    improvement=0.0 if triggered else None,
                    control=True,
                    triggered=triggered,
                    observed_seconds=300.0,
                ),
                _observation(
                    cycle=cycle,
                    candidate="candidate",
                    multiplier=0.60,
                    improvement=10.0 if triggered else None,
                    triggered=triggered,
                    observed_seconds=300.0,
                ),
            ]
        )

    summaries, _ = _enriched(observations)
    candidate = summaries["candidate"]

    assert candidate.paired_evidence["selected_km_horizon"] == "5m_pct"
    assert candidate.paired_evidence["selected_km_horizon_seconds"] == 300.0
    assert not any(
        "follow-up through at least five minutes" in reason
        for reason in candidate.instability_reasons
    )


def test_only_one_minute_shared_follow_up_is_unstable() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 7):
        triggered = cycle < 6
        observations.extend(
            [
                _observation(
                    cycle=cycle,
                    candidate="control",
                    multiplier=0.75,
                    improvement=0.0 if triggered else None,
                    control=True,
                    triggered=triggered,
                    observed_seconds=60.0,
                ),
                _observation(
                    cycle=cycle,
                    candidate="candidate",
                    multiplier=0.60,
                    improvement=10.0 if triggered else None,
                    triggered=triggered,
                    observed_seconds=60.0,
                ),
            ]
        )

    summaries, _ = _enriched(observations)
    candidate = summaries["candidate"]

    assert candidate.paired_evidence["selected_km_horizon"] == "1m_pct"
    assert any(
        "follow-up through at least five minutes" in reason
        for reason in candidate.instability_reasons
    )


def test_large_median_timing_deterioration_is_unstable() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        observations.extend(
            [
                _observation(
                    cycle=cycle,
                    candidate="control",
                    multiplier=0.75,
                    improvement=0.0,
                    control=True,
                    observed_seconds=900.0,
                    delay_seconds=0.0,
                ),
                _observation(
                    cycle=cycle,
                    candidate="candidate",
                    multiplier=0.60,
                    improvement=10.0,
                    observed_seconds=900.0,
                    delay_seconds=301.0,
                ),
            ]
        )

    summaries, _ = _enriched(observations)
    candidate = summaries["candidate"]

    assert candidate.paired_evidence["median_absolute_delay_delta_seconds"] == 301.0
    assert any(
        "timing error increases" in reason
        for reason in candidate.instability_reasons
    )


def test_sell_execution_adjustment_uses_bid_touch_and_adverse_residual() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        observations.append(
            ReplayObservation(
                cycle_id=f"cycle-{cycle}",
                cycle_number=cycle,
                ticker="AAPL",
                leg="sell",
                candidate_key="control",
                multiplier=1.0,
                minimum_profit_multiplier=1.0,
                period=14,
                bar_seconds=60,
                effective_pct=1.0,
                triggered=True,
                control_candidate=True,
                outcome="triggered",
                trigger_price=100.0,
                trigger_bid=99.9,
                trigger_ask=100.0,
                actual_fill_price=99.5,
                fill_reference_price=100.0,
                fill_bid=99.8,
                fill_ask=100.0,
                fill_spread_bps=20.02,
                fill_quote_age_seconds=0.5,
                trading_day_utc=f"2026-02-{cycle:02d}",
                observed_seconds=60.0,
                atr_source="capture_reconstructed",
            )
        )

    model = apply_execution_model(observations)
    row = observations[0]

    assert model["sell"]["touch_residual_samples"] == 5
    assert row.execution_model_leave_one_out is True
    assert row.execution_model_samples == 4
    assert row.estimated_fill_price is not None
    assert row.estimated_fill_price < row.trigger_price
    assert row.execution_adjustment_bps > 0.0
    assert "bid touch" in row.execution_model_method


def test_stable_lower_plateau_is_preferred_over_higher_isolated_peak() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        observations.extend(
            [
                _observation(
                    cycle=cycle,
                    candidate="control",
                    multiplier=0.75,
                    improvement=0.0,
                    control=True,
                ),
                _observation(
                    cycle=cycle,
                    candidate="isolated-peak",
                    multiplier=0.50,
                    improvement=20.0,
                ),
                _observation(
                    cycle=cycle,
                    candidate="valley",
                    multiplier=0.55,
                    improvement=-5.0,
                ),
                _observation(
                    cycle=cycle,
                    candidate="plateau-a",
                    multiplier=0.60,
                    improvement=15.0,
                ),
                _observation(
                    cycle=cycle,
                    candidate="plateau-b",
                    multiplier=0.65,
                    improvement=14.0,
                ),
            ]
        )

    summaries, methodology = _enriched(observations)

    assert summaries["isolated-peak"].evidence_stable is False
    assert summaries["plateau-a"].evidence_stable is True
    assert summaries["plateau-a"].stable_region["preferred"] is True
    assert summaries["plateau-a"].stable_region["minimum_paired_delta_bps"] == 14.0
    assert methodology["stable_candidate_keys"] == ["plateau-a"]


def test_trigger_probability_deficit_rejects_otherwise_positive_candidate() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        observations.append(
            _observation(
                cycle=cycle,
                candidate="control",
                multiplier=0.75,
                improvement=0.0,
                control=True,
                triggered=True,
                observed_seconds=900.0,
            )
        )
        for candidate, multiplier in (("candidate-a", 0.60), ("candidate-b", 0.65)):
            observations.append(
                _observation(
                    cycle=cycle,
                    candidate=candidate,
                    multiplier=multiplier,
                    improvement=10.0 if cycle < 5 else None,
                    triggered=cycle < 5,
                    observed_seconds=900.0,
                )
            )

    summaries, _ = _enriched(observations)
    candidate = summaries["candidate-a"]

    assert candidate.paired_evidence["candidate_km_trigger_probability_15m_pct"] == 80.0
    assert candidate.paired_evidence["control_km_trigger_probability_15m_pct"] == 100.0
    assert candidate.evidence_stable is False
    assert any("trigger probability" in reason for reason in candidate.instability_reasons)


def test_pairing_rejects_different_observation_origins() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        control = _observation(
            cycle=cycle,
            candidate="control",
            multiplier=0.75,
            improvement=0.0,
            control=True,
        )
        candidate = _observation(
            cycle=cycle,
            candidate="candidate",
            multiplier=0.60,
            improvement=10.0,
        )
        common_start = f"2026-01-{cycle:02d}T14:55:00+00:00"
        common_end = f"2026-01-{cycle:02d}T14:57:00+00:00"
        control.observation_start_utc = common_start
        control.observation_end_utc = common_end
        candidate.observation_start_utc = common_start
        candidate.observation_end_utc = common_end
        observations.extend((control, candidate))
    observations[-1].observation_start_utc = "2026-01-05T14:56:00+00:00"
    observations[-1].observation_end_utc = "2026-01-05T14:58:00+00:00"

    summaries, _ = _enriched(observations)
    evidence = summaries["candidate"].paired_evidence

    assert evidence["paired_cycles"] == 4
    assert evidence["context_mismatch_cycle_keys_excluded"] == {
        "cycle-5": "observation start"
    }


def test_missing_timing_or_adverse_excursion_evidence_is_unstable() -> None:
    observations: list[ReplayObservation] = []
    for cycle in range(1, 6):
        for row in (
            _observation(
                cycle=cycle,
                candidate="control",
                multiplier=0.75,
                improvement=0.0,
                control=True,
            ),
            _observation(
                cycle=cycle,
                candidate="candidate",
                multiplier=0.60,
                improvement=10.0,
            ),
        ):
            row.delay_seconds = None
            row.post_trigger_mae_bps = None
            observations.append(row)

    summaries, _ = _enriched(observations)
    candidate = summaries["candidate"]

    assert candidate.evidence_stable is False
    assert any(
        "trigger-timing evidence is unavailable" in reason
        for reason in candidate.instability_reasons
    )
    assert any(
        "adverse-excursion evidence is unavailable" in reason
        for reason in candidate.instability_reasons
    )


def test_execution_sample_tie_prefers_more_complete_context() -> None:
    sparse = _observation(
        cycle=1,
        candidate="control",
        multiplier=0.75,
        improvement=0.0,
        control=True,
    )
    sparse.trading_day_utc = ""
    sparse.trigger_price = 99.0
    sparse.actual_fill_price = 100.2
    sparse.fill_reference_price = 100.0
    sparse.fill_quote_age_seconds = None

    contextual = deepcopy(sparse)
    contextual.candidate_key = "candidate"
    contextual.control_candidate = False
    contextual.trading_day_utc = "2026-01-01"
    contextual.fill_quote_age_seconds = 1.0

    model = apply_execution_model([sparse, contextual])

    assert model["buy"]["fills_considered"] == 1
    assert model["buy"]["trading_days"] == 1
    assert model["buy"]["median_fill_quote_age_seconds"] == 1.0


def test_invalid_execution_leg_is_rejected_fail_closed() -> None:
    row = _observation(
        cycle=1,
        candidate="candidate",
        multiplier=0.75,
        improvement=0.0,
    )
    row.leg = "hold"
    row.trigger_price = 99.0
    row.actual_fill_price = 100.2
    row.fill_reference_price = 100.0
    row.fill_bid = 99.9
    row.fill_ask = 100.0
    row.fill_quote_age_seconds = 0.5

    model = apply_execution_model([row])

    assert model["buy"]["fills_considered"] == 0
    assert model["sell"]["fills_considered"] == 0
    assert row.execution_model_method == ""


def test_overlapping_secondary_region_recomputes_its_actual_metadata() -> None:
    observations: list[ReplayObservation] = []
    candidates = (
        ("peak-a", 0.50, 20.0),
        ("a-neighbor", 0.55, 17.0),
        ("bridge", 0.60, 16.0),
        ("secondary-low", 0.65, 15.0),
        ("peak-b", 0.70, 18.0),
    )
    for cycle in range(1, 6):
        observations.append(
            _observation(
                cycle=cycle,
                candidate="control",
                multiplier=0.75,
                improvement=0.0,
                control=True,
            )
        )
        observations.extend(
            _observation(
                cycle=cycle,
                candidate=key,
                multiplier=multiplier,
                improvement=improvement,
            )
            for key, multiplier, improvement in candidates
        )

    summaries, _ = _enriched(observations)
    secondary = summaries["secondary-low"].stable_region

    assert secondary["preferred"] is False
    assert secondary["size"] == 2
    assert secondary["peak_candidate_key"] == "peak-b"
    assert secondary["minimum_paired_delta_bps"] == 15.0
    assert secondary["median_paired_delta_bps"] == 16.5
    assert secondary["best_paired_delta_bps"] == 18.0
