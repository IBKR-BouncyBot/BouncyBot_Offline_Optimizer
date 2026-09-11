"""Regression tests for exact v2.2 refinement acceleration."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np

from optimizer.ibrec import load_ibrec_set
from optimizer.market_replay import (
    _atr_cache_key,
    _control_profile,
    _evaluate_profile,
    _precompute_atr,
    _session_ticks,
)
from optimizer.market_replay_fast import PreparedReplayStore, ProfileBatchEvaluator
from optimizer.market_replay_models import AtrProfile, MarketReplayConfig
from optimizer.market_replay_optimization import (
    _STATE_MAXIMUM,
    _STATE_MINIMUM,
    _STATE_RAW,
    _STATE_ZERO,
    EffectiveArrayCatalog,
    _collapse_profiles,
    _effective_scalar,
    adaptive_profile_batch_size,
    effective_percentage_state_scalar,
    exact_equivalence_group_count,
    expand_evaluation_results,
    lookup_effective_percentage_state,
    memory_aware_worker_count,
    set_runtime_effective_catalog,
)
from tests.market_replay_fixtures import make_ticks, write_v3


def _fixture(tmp_path: Path):
    rows, periods = make_ticks()
    recording_path = write_v3(tmp_path / "recording.ibrec", rows, periods)
    config = MarketReplayConfig(
        recording_path,
        tmp_path / "reports",
        worker_processes=1,
    ).normalized()
    recording = load_ibrec_set(config)
    period_ticks = [
        (period, ticks)
        for period in recording.periods
        if (ticks := _session_ticks(recording, period))
    ]
    return recording, config, period_ticks


def _cache(period_ticks, profiles):
    values = {}
    for period, ticks in period_ticks:
        for profile in profiles:
            key = _atr_cache_key(period, profile.period, profile.bar_seconds)
            if key not in values:
                values[key] = _precompute_atr(
                    ticks,
                    profile.period,
                    profile.bar_seconds,
                )
    return values


def test_effective_scalar_matches_bouncybot_round_clamp_semantics() -> None:
    profile = AtrProfile(14, 60, 1.5, 0.75, 1.0, 1.0, 0.10, 20.0)
    assert effective_percentage_state_scalar(
        0.01,
        1.0,
        0.10,
        20.0,
        allow_zero=False,
    ) == (0.10, _STATE_MINIMUM)
    assert effective_percentage_state_scalar(
        30.0,
        1.0,
        0.10,
        20.0,
        allow_zero=False,
    ) == (20.0, _STATE_MAXIMUM)
    assert effective_percentage_state_scalar(
        0.127,
        1.0,
        0.10,
        20.0,
        allow_zero=False,
    ) == (0.13, _STATE_RAW)
    assert _effective_scalar(None, 0.0, profile, allow_zero=True) == (
        0.0,
        _STATE_ZERO,
    )


def test_prepared_arrays_match_scalar_values_and_are_read_only(tmp_path: Path) -> None:
    recording, config, period_ticks = _fixture(tmp_path)
    profile = _control_profile(config)
    cache = _cache(period_ticks, [profile])
    period, _ticks = period_ticks[0]
    with PreparedReplayStore.create(period_ticks) as store:
        store.persist_atr_cache(cache)
        catalog = EffectiveArrayCatalog(store.effective_root)
        try:
            catalog.prepare(
                [profile],
                session_atr_paths={
                    (
                        store.session_spec(period).session_index,
                        profile.period,
                        profile.bar_seconds,
                    ): store.atr_path(period, profile.period, profile.bar_seconds)
                },
            )
            base = np.load(
                store.atr_path(period, profile.period, profile.bar_seconds),
                mmap_mode="r",
                allow_pickle=False,
            )
            from optimizer.market_replay_optimization import EffectiveAtrSeries

            series = EffectiveAtrSeries(
                base,
                catalog,
                store.session_spec(period).session_index,
            )
            for index in (0, len(series) // 2, len(series) - 1):
                expected_value, expected_state = _effective_scalar(
                    series[index],
                    profile.initial_drop_multiplier,
                    profile,
                    allow_zero=False,
                )
                actual_value, actual_state = lookup_effective_percentage_state(
                    series,
                    index,
                    profile.initial_drop_multiplier,
                    profile,
                    allow_zero=False,
                )
                state_names = {
                    0: "unavailable",
                    _STATE_MINIMUM: "min",
                    _STATE_MAXIMUM: "max",
                    _STATE_RAW: "raw",
                    _STATE_ZERO: "zero",
                }
                assert actual_value == expected_value
                assert actual_state == state_names[expected_state]
            loaded = next(iter(catalog._loaded.values()))
            assert loaded[0].flags.writeable is False
            assert loaded[1].flags.writeable is False
        finally:
            catalog.close()
            mmap_object = getattr(base, "_mmap", None)
            if mmap_object is not None:
                mmap_object.close()
    assert recording.min_tick > 0


def test_equivalence_collapses_only_exact_effective_behaviors(tmp_path: Path) -> None:
    _recording, config, period_ticks = _fixture(tmp_path)
    base = _control_profile(config)
    control = AtrProfile(
        base.period,
        base.bar_seconds,
        base.initial_drop_multiplier,
        0.75,
        base.minimum_profit_multiplier,
        base.sell_trail_multiplier,
        1.00,
        base.max_atr_pct,
    )
    # The fixture ATR remains below 1.00%, so both BUY multipliers are exactly
    # pinned to the same 1.00% minimum-clamp path while retaining distinct
    # nominal profile keys. A different minimum clamp must remain separate.
    alias = AtrProfile(
        control.period,
        control.bar_seconds,
        control.initial_drop_multiplier,
        1.0,
        control.minimum_profit_multiplier,
        control.sell_trail_multiplier,
        control.min_atr_pct,
        control.max_atr_pct,
    )
    distinct = AtrProfile(
        control.period,
        control.bar_seconds,
        control.initial_drop_multiplier,
        control.buy_rebound_multiplier,
        control.minimum_profit_multiplier,
        control.sell_trail_multiplier,
        1.20,
        control.max_atr_pct,
    )
    profiles = [control, alias, distinct]
    cache = _cache(period_ticks, profiles)
    with PreparedReplayStore.create(period_ticks) as store:
        store.persist_atr_cache(cache)
        catalog = EffectiveArrayCatalog(store.effective_root)
        try:
            paths = {
                (session.session_index, profile.period, profile.bar_seconds): store.atr_path(
                    session.period,
                    profile.period,
                    profile.bar_seconds,
                )
                for session in store.sessions
                for profile in profiles
            }
            catalog.prepare(profiles, session_atr_paths=paths)
            representatives, mapping = _collapse_profiles(
                profiles,
                catalog,
                session_indices=[session.session_index for session in store.sessions],
            )
            assert mapping[0] == mapping[1]
            assert mapping[2] != mapping[0]
            assert exact_equivalence_group_count(
                profiles,
                catalog,
                session_indices=[session.session_index for session in store.sessions],
            ) == len(representatives) == 2
        finally:
            catalog.close()


def test_expansion_restores_nominal_profiles_and_duplicate_cardinality(
    tmp_path: Path,
) -> None:
    recording, config, period_ticks = _fixture(tmp_path)
    control = _control_profile(config)
    profiles = [control, control]
    cache = _cache(period_ticks, profiles)
    summary, sessions, _ = _evaluate_profile(
        recording,
        period_ticks,
        cache,
        control,
        config,
        keep_details=False,
    )
    expanded = expand_evaluation_results(
        profiles,
        [control],
        [0, 0],
        [(control.key(), summary, sessions)],
    )
    assert len(expanded) == 2
    assert [row[0] for row in expanded] == [control.key(), control.key()]
    assert expanded[0][1] is not expanded[1][1]
    assert asdict(expanded[0][1]) == asdict(expanded[1][1])


def test_adaptive_batches_get_smaller_for_large_recordings() -> None:
    assert adaptive_profile_batch_size(
        total_rows=10_000,
        profile_count=256,
        worker_count=8,
    ) <= 16
    assert adaptive_profile_batch_size(
        total_rows=600_000,
        profile_count=256,
        worker_count=8,
    ) <= 4
    assert adaptive_profile_batch_size(
        total_rows=2_000_000,
        profile_count=256,
        worker_count=8,
    ) <= 2
    assert adaptive_profile_batch_size(
        total_rows=2_000_000,
        profile_count=2,
        worker_count=16,
    ) == 1


def test_automatic_worker_count_can_exceed_eight_but_honors_memory() -> None:
    assert memory_aware_worker_count(
        cpu_count=24,
        available_memory_bytes=64 << 30,
        estimated_replay_context_bytes=1 << 30,
        pending_profiles=1_000,
        automatic_ceiling=16,
    ) == 16
    assert memory_aware_worker_count(
        cpu_count=24,
        available_memory_bytes=2 << 30,
        estimated_replay_context_bytes=3 << 30,
        pending_profiles=1_000,
        automatic_ceiling=16,
    ) == 1


def test_runtime_catalog_setter_restores_previous_catalog(tmp_path: Path) -> None:
    first = EffectiveArrayCatalog(tmp_path / "first")
    second = EffectiveArrayCatalog(tmp_path / "second")
    try:
        assert set_runtime_effective_catalog(first) is None
        assert set_runtime_effective_catalog(second) is first
        assert set_runtime_effective_catalog(None) is second
    finally:
        first.close()
        second.close()


def test_serial_and_parallel_refinement_results_remain_exact(tmp_path: Path) -> None:
    recording, config, period_ticks = _fixture(tmp_path)
    profiles = [
        _control_profile(config),
        AtrProfile(14, 60, 1.5, 1.0, 1.0, 1.0),
        AtrProfile(7, 30, 1.25, 0.75, 1.25, 0.75),
    ]
    cache = _cache(period_ticks, profiles)
    reference = {}
    for profile in profiles:
        summary, sessions, _ = _evaluate_profile(
            recording,
            period_ticks,
            cache,
            profile,
            config,
            keep_details=False,
        )
        reference[profile.key()] = (asdict(summary), [asdict(row) for row in sessions])

    with PreparedReplayStore.create(period_ticks) as store:
        store.persist_atr_cache(cache)
        with ProfileBatchEvaluator(
            store,
            min_tick=recording.min_tick,
            config=config,
            requested_workers=2,
        ) as evaluator:
            actual = {
                key: (asdict(summary), [asdict(row) for row in sessions])
                for key, summary, sessions in evaluator.evaluate(
                    profiles,
                    summary_day_weights=None,
                    progress=None,
                )
            }
    assert actual == reference
