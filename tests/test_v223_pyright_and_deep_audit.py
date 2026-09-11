"""Regression coverage for the v2.2.3 Pyright and deep-audit corrections."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import optimizer.market_replay_fast as fast
import optimizer.market_replay_optimization as optimization
from optimizer.ibrec import load_ibrec_set
from optimizer.market_replay_fast import FastTickSequence, PreparedReplayStore
from optimizer.market_replay_models import AtrProfile, MarketReplayConfig
from optimizer.market_replay_optimization import (
    _STATE_NAME,
    _UNAVAILABLE_BASIS_POINTS,
    EffectiveArrayCatalog,
    EffectiveAtrSeries,
    effective_percentage_state_scalar,
    lookup_effective_percentage_state,
)
from tests.market_replay_fixtures import make_ticks, write_v3


def _profile() -> AtrProfile:
    return AtrProfile(14, 60, 1.5, 0.75, 1.0, 1.0)


def _recording(tmp_path: Path):
    rows, periods = make_ticks(points_per_session=4)
    path = write_v3(tmp_path / "recording.ibrec", rows, periods)
    return load_ibrec_set(MarketReplayConfig(path, tmp_path / "reports"))


def test_shared_atr_protocol_accepts_lists_and_numpy_vectors() -> None:
    profile = _profile()
    expected = lookup_effective_percentage_state(
        [0.20], 0, profile.initial_drop_multiplier, profile, allow_zero=False
    )
    compact = np.asarray([0.20], dtype=np.float64)
    assert lookup_effective_percentage_state(
        compact, 0, profile.initial_drop_multiplier, profile, allow_zero=False
    ) == expected
    assert EffectiveAtrSeries(compact, None, 0)[0] == pytest.approx(0.20)


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf")])
def test_atr_value_boundary_rejects_nonfinite_or_boolean_values(value: object) -> None:
    profile = _profile()
    assert lookup_effective_percentage_state(
        [value], 0, profile.initial_drop_multiplier, profile, allow_zero=False
    ) == (None, "unavailable")


def test_available_memory_query_uses_optional_sysconf_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(optimization.os, "sysconf", raising=False)
    assert optimization._available_memory_bytes() is None

    monkeypatch.setattr(
        optimization.os,
        "sysconf",
        lambda name: True if name == "SC_AVPHYS_PAGES" else 4096,
        raising=False,
    )
    assert optimization._available_memory_bytes() is None

    monkeypatch.setattr(
        optimization.os,
        "sysconf",
        lambda name: 100 if name == "SC_AVPHYS_PAGES" else 4096,
        raising=False,
    )
    assert optimization._available_memory_bytes() == 409_600


def test_equivalence_signature_requires_observed_sessions(tmp_path: Path) -> None:
    catalog = EffectiveArrayCatalog(tmp_path)
    try:
        assert catalog.profile_signature(_profile(), session_indices=()) is None
    finally:
        catalog.close()


def test_bounded_scalar_cache_preserves_exact_effective_arrays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atr_path = tmp_path / "atr.npy"
    source = np.asarray([0.01, 0.05, 0.10, 0.20, np.nan], dtype=np.float64)
    np.save(atr_path, source, allow_pickle=False)
    monkeypatch.setattr(optimization, "_MAX_SCALAR_CACHE_ENTRIES", 1)
    profile = _profile()
    catalog = EffectiveArrayCatalog(tmp_path / "effective")
    try:
        paths = catalog._prepare_entry(
            0,
            atr_path,
            profile,
            profile.initial_drop_multiplier,
            allow_zero=False,
        )
        values = np.load(paths.values, allow_pickle=False)
        states = np.load(paths.states, allow_pickle=False)
        for index, raw in enumerate(source):
            atr_pct = None if np.isnan(raw) else float(raw)
            effective, state = effective_percentage_state_scalar(
                atr_pct,
                profile.initial_drop_multiplier,
                profile.min_atr_pct,
                profile.max_atr_pct,
                allow_zero=False,
            )
            stored = int(values[index])
            assert stored == (
                int(_UNAVAILABLE_BASIS_POINTS)
                if effective is None
                else int(round(effective * 100.0))
            )
            assert _STATE_NAME[int(states[index])] == _STATE_NAME[state]
    finally:
        catalog.close()


def test_failed_tick_store_removes_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = _recording(tmp_path)
    target = tmp_path / "ticks.npy"

    def fail_fromiter(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected tick serialization failure")

    monkeypatch.setattr(fast.np, "fromiter", fail_fromiter)
    with pytest.raises(RuntimeError, match="injected tick serialization failure"):
        PreparedReplayStore._write_ticks(target, recording.ticks[:1])
    assert not target.exists()
    assert not target.with_suffix(".partial.npy").exists()


def test_failed_atr_store_removes_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = _recording(tmp_path)
    period = recording.periods[0]
    ticks = [tick for tick in recording.ticks if tick.rth_period_id == period.period_id]
    with PreparedReplayStore.create([(period, ticks)]) as store:
        target = store.atr_path(period, 14, 60)

        def fail_fromiter(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("injected ATR serialization failure")

        monkeypatch.setattr(fast.np, "fromiter", fail_fromiter)
        with pytest.raises(RuntimeError, match="injected ATR serialization failure"):
            store.persist_atr(period, 14, 60, [0.10] * len(ticks))
        assert not target.exists()
        assert not target.with_suffix(".partial.npy").exists()


def test_fast_tick_dtype_failure_closes_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMap:
        closed = False

        def close(self) -> None:
            self.closed = True

    class FakeArray:
        dtype = np.dtype(np.float64)
        _mmap = FakeMap()

    fake = FakeArray()
    monkeypatch.setattr(fast.np, "load", lambda *_args, **_kwargs: fake)
    with pytest.raises(ValueError, match="Unsupported compact replay dtype"):
        FastTickSequence(tmp_path / "wrong.npy")
    assert fake._mmap.closed


def test_compact_timestamp_matches_importer_millisecond_truncation(
    tmp_path: Path,
) -> None:
    rows, periods = make_ticks(points_per_session=4)
    rows[0]["captured_at_utc"] = "2026-01-05T14:30:00.123900Z"
    rows[0]["source_time_utc"] = rows[0]["captured_at_utc"]
    periods[0]["observed_start_utc"] = rows[0]["captured_at_utc"]
    path = write_v3(tmp_path / "submillisecond.ibrec", rows, periods)
    recording = load_ibrec_set(MarketReplayConfig(path, tmp_path / "reports"))
    period = recording.periods[0]
    ticks = [tick for tick in recording.ticks if tick.rth_period_id == period.period_id]
    assert ticks[0].captured_at_utc.endswith(".123Z")
    with PreparedReplayStore.create([(period, ticks)]) as store:
        compact = store.open_ticks(period)
        try:
            assert compact[0].captured_at_utc == ticks[0].captured_at_utc
        finally:
            compact.close()


def test_effective_catalog_rejects_truncated_cache_pair(tmp_path: Path) -> None:
    profile = _profile()
    atr_path = tmp_path / "atr.npy"
    np.save(atr_path, np.asarray([0.1, 0.2, 0.3], dtype=np.float64))
    catalog = EffectiveArrayCatalog(tmp_path / "effective")
    catalog.prepare(
        [profile],
        session_atr_paths={(0, profile.period, profile.bar_seconds): atr_path},
    )
    catalog.close()

    state_path = next((tmp_path / "effective").glob("*-states.npy"))
    np.save(state_path, np.asarray([1, 2], dtype=np.uint8))
    second = EffectiveArrayCatalog(tmp_path / "effective")
    try:
        with pytest.raises(ValueError, match="ATR source length"):
            second.prepare(
                [profile],
                session_atr_paths={(0, profile.period, profile.bar_seconds): atr_path},
            )
    finally:
        second.close()


def test_effective_series_rejects_cache_length_mismatch(tmp_path: Path) -> None:
    profile = _profile()
    atr_path = tmp_path / "atr.npy"
    np.save(atr_path, np.asarray([0.1, 0.2, 0.3], dtype=np.float64))
    catalog = EffectiveArrayCatalog(tmp_path / "effective")
    try:
        catalog.prepare(
            [profile],
            session_atr_paths={(0, profile.period, profile.bar_seconds): atr_path},
        )
        series = EffectiveAtrSeries([0.1, 0.2], catalog, 0)
        with pytest.raises(ValueError, match="ATR series length"):
            series.effective_percentage_state(
                0,
                profile.initial_drop_multiplier,
                profile,
                allow_zero=False,
            )
    finally:
        catalog.close()


def test_effective_cache_pair_publication_is_all_or_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    atr_path = tmp_path / "atr.npy"
    np.save(atr_path, np.asarray([0.1, 0.2, 0.3], dtype=np.float64))
    catalog = EffectiveArrayCatalog(tmp_path / "effective")
    real_replace = optimization.os.replace
    calls = 0

    def fail_second(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("injected second-file publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(optimization.os, "replace", fail_second)
    try:
        with pytest.raises(PermissionError, match="second-file"):
            catalog.prepare(
                [profile],
                session_atr_paths={(0, profile.period, profile.bar_seconds): atr_path},
            )
        assert list((tmp_path / "effective").glob("*.npy")) == []
    finally:
        catalog.close()


def test_effective_series_preserves_negative_index_semantics(tmp_path: Path) -> None:
    profile = _profile()
    atr_path = tmp_path / "atr.npy"
    values = np.asarray([0.1, 0.2, 0.3], dtype=np.float64)
    np.save(atr_path, values)
    catalog = EffectiveArrayCatalog(tmp_path / "effective")
    try:
        catalog.prepare(
            [profile],
            session_atr_paths={(0, profile.period, profile.bar_seconds): atr_path},
        )
        series = EffectiveAtrSeries(values, catalog, 0)
        assert series.effective_percentage_state(
            -1,
            profile.initial_drop_multiplier,
            profile,
            allow_zero=False,
        ) == series.effective_percentage_state(
            2,
            profile.initial_drop_multiplier,
            profile,
            allow_zero=False,
        )
        with pytest.raises(IndexError):
            series.effective_percentage_state(
                -4,
                profile.initial_drop_multiplier,
                profile,
                allow_zero=False,
            )

        fallback_series = EffectiveAtrSeries(values, None, 0)
        with pytest.raises(IndexError):
            fallback_series.effective_percentage_state(
                -4,
                profile.initial_drop_multiplier,
                profile,
                allow_zero=False,
            )
    finally:
        catalog.close()


def test_fast_tick_sequence_rejects_non_vector_storage(tmp_path: Path) -> None:
    path = tmp_path / "matrix.npy"
    np.save(path, np.empty((1, 1), dtype=fast._TICK_DTYPE), allow_pickle=False)
    with pytest.raises(ValueError, match="Unsupported compact replay dtype"):
        FastTickSequence(path)


def test_serial_tick_open_failure_closes_previously_opened_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from optimizer.market_replay_fast import ProfileBatchEvaluator

    rows, periods = make_ticks(sessions=2, points_per_session=4)
    path = write_v3(tmp_path / "two-sessions.ibrec", rows, periods)
    recording = load_ibrec_set(MarketReplayConfig(path, tmp_path / "reports"))
    period_ticks = [
        (
            period,
            [tick for tick in recording.ticks if tick.rth_period_id == period.period_id],
        )
        for period in recording.periods
    ]

    class FirstMapping:
        closed = False

        def close(self) -> None:
            self.closed = True

    first = FirstMapping()
    calls = 0

    def fake_sequence(_path: str) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            return first
        raise RuntimeError("injected second mapping failure")

    with PreparedReplayStore.create(period_ticks) as store:
        evaluator = ProfileBatchEvaluator(
            store,
            min_tick=recording.min_tick,
            config=MarketReplayConfig(path, tmp_path / "reports"),
            requested_workers=1,
        )
        monkeypatch.setattr(fast, "FastTickSequence", fake_sequence)
        try:
            with pytest.raises(RuntimeError, match="second mapping"):
                evaluator._serial_period_ticks()
            assert first.closed
        finally:
            evaluator.close()


def test_effective_lookup_hot_path_avoids_structural_runtime_protocol() -> None:
    source = Path(optimization.__file__).read_text(encoding="utf-8")
    assert 'isinstance(atr_values, EffectiveAtrSeries)' in source
    assert 'method = getattr(atr_values, "effective_percentage_state"' not in source


def _summary(profile: AtrProfile):
    from optimizer.market_replay_models import MarketReplayCandidateSummary

    return MarketReplayCandidateSummary(
        profile=profile,
        score=1.0,
        sessions=1,
        completed_sessions=1,
        sessions_with_trades=1,
        completed_trades=1,
        open_position_sessions=0,
        no_trade_sessions=0,
        median_return_bps=1.0,
        mean_return_bps=1.0,
        worst_return_bps=1.0,
        maximum_drawdown_bps=0.0,
        open_position_rate_pct=0.0,
        no_trade_rate_pct=0.0,
    )


def test_effective_catalog_prepares_reuses_and_reads_exact_arrays(tmp_path: Path) -> None:
    profile = _profile()
    atr_path = tmp_path / "atr.npy"
    np.save(
        atr_path,
        np.asarray([np.nan, 0.01, 0.10, 0.20, 20.0], dtype=np.float64),
        allow_pickle=False,
    )
    catalog = EffectiveArrayCatalog(tmp_path / "catalog")
    try:
        mapping = {(2, profile.period, profile.bar_seconds): atr_path}
        catalog.prepare([profile, profile], session_atr_paths=mapping)
        # A repeated prepare is a no-op and exercises the prepared-entry cache.
        catalog.prepare([profile], session_atr_paths=mapping)
        loaded = catalog.loaded_entry(
            2,
            profile,
            profile.initial_drop_multiplier,
            allow_zero=False,
        )
        assert loaded is not None
        values, states = loaded
        assert values.flags.writeable is False
        assert states.flags.writeable is False
        assert catalog.lookup(
            2,
            0,
            profile,
            profile.initial_drop_multiplier,
            allow_zero=False,
            fallback_atr_pct=99.0,
        ) == (None, "unavailable")
        assert catalog.lookup(
            2,
            2,
            profile,
            profile.initial_drop_multiplier,
            allow_zero=False,
            fallback_atr_pct=None,
        )[0] == pytest.approx(0.15)
        # Out-of-range lookup must use the exact scalar fallback.
        assert catalog.lookup(
            2,
            999,
            profile,
            profile.initial_drop_multiplier,
            allow_zero=False,
            fallback_atr_pct=0.20,
        ) == (0.30, "raw")
        first = catalog.profile_signature(profile, session_indices=[2])
        second = catalog.profile_signature(profile, session_indices=[2])
        assert first and first == second
        # Manual protective values participate directly in the signature.
        manual = AtrProfile(
            profile.period,
            profile.bar_seconds,
            profile.initial_drop_multiplier,
            profile.buy_rebound_multiplier,
            profile.minimum_profit_multiplier,
            profile.sell_trail_multiplier,
            protective_sell_mode="manual",
            protective_sell_value=3.0,
        )
        catalog.prepare([manual], session_atr_paths=mapping)
        assert catalog.profile_signature(manual, session_indices=[2]) != first
    finally:
        catalog.close()
        catalog.close()


def test_effective_atr_series_supports_negative_indices_and_fails_closed(
    tmp_path: Path,
) -> None:
    profile = _profile()
    atr_path = tmp_path / "atr.npy"
    np.save(atr_path, np.asarray([0.10, 0.20], dtype=np.float64), allow_pickle=False)
    catalog = EffectiveArrayCatalog(tmp_path / "catalog")
    try:
        catalog.prepare(
            [profile],
            session_atr_paths={(0, profile.period, profile.bar_seconds): atr_path},
        )
        series = optimization.EffectiveAtrSeries(
            np.asarray([0.10, 0.20], dtype=np.float64), catalog, 0
        )
        assert series[-1] == pytest.approx(0.20)
        assert series.effective_percentage_state(
            -1,
            profile.initial_drop_multiplier,
            profile,
            allow_zero=False,
        )[0] == pytest.approx(0.30)
        with pytest.raises(IndexError):
            series.effective_percentage_state(
                -3,
                profile.initial_drop_multiplier,
                profile,
                allow_zero=False,
            )

        class MisalignedCatalog:
            def loaded_entry(self, *_args: object, **_kwargs: object):
                return (
                    np.asarray([10], dtype=np.int16),
                    np.asarray([optimization._STATE_RAW], dtype=np.uint8),
                )

        bad = optimization.EffectiveAtrSeries(
            np.asarray([0.10, 0.20], dtype=np.float64), MisalignedCatalog(), 0
        )
        with pytest.raises(ValueError, match="do not match"):
            bad.effective_percentage_state(
                0,
                profile.initial_drop_multiplier,
                profile,
                allow_zero=False,
            )

        class UnknownStateCatalog:
            def loaded_entry(self, *_args: object, **_kwargs: object):
                return (
                    np.asarray([10, 20], dtype=np.int16),
                    np.asarray([255, 255], dtype=np.uint8),
                )

        unknown = optimization.EffectiveAtrSeries(
            np.asarray([0.10, 0.20], dtype=np.float64), UnknownStateCatalog(), 0
        )
        with pytest.raises(ValueError, match="Unknown effective clamp-state"):
            unknown.effective_percentage_state(
                0,
                profile.initial_drop_multiplier,
                profile,
                allow_zero=False,
            )
    finally:
        catalog.close()


def test_catalog_validation_rejects_partial_wrong_shape_dtype_and_length(
    tmp_path: Path,
) -> None:
    catalog = EffectiveArrayCatalog(tmp_path / "catalog")
    profile = _profile()
    paths = catalog._paths(0, profile, profile.initial_drop_multiplier, allow_zero=False)
    try:
        np.save(paths.values, np.asarray([1], dtype=np.int16), allow_pickle=False)
        with pytest.raises(ValueError, match="incomplete"):
            catalog._validate_entry_files(paths, expected_length=1)
        paths.values.unlink()

        cases = [
            (
                np.asarray([1], dtype=np.int32),
                np.asarray([optimization._STATE_RAW], dtype=np.uint8),
                "dtype",
            ),
            (
                np.asarray([[1]], dtype=np.int16),
                np.asarray([[optimization._STATE_RAW]], dtype=np.uint8),
                "one-dimensional",
            ),
            (
                np.asarray([1, 2], dtype=np.int16),
                np.asarray([optimization._STATE_RAW], dtype=np.uint8),
                "source length",
            ),
        ]
        for values, states, message in cases:
            np.save(paths.values, values, allow_pickle=False)
            np.save(paths.states, states, allow_pickle=False)
            with pytest.raises(ValueError, match=message):
                catalog._validate_entry_files(paths, expected_length=1)
            paths.values.unlink()
            paths.states.unlink()

        # Nonexistent paired files are valid before preparation.
        catalog._validate_entry_files(paths, expected_length=1)
    finally:
        catalog.close()


def test_catalog_rejects_invalid_atr_arrays_and_changed_source(tmp_path: Path) -> None:
    profile = _profile()
    catalog = EffectiveArrayCatalog(tmp_path / "catalog")
    try:
        bad_dtype = tmp_path / "bad_dtype.npy"
        np.save(bad_dtype, np.asarray([0.1], dtype=np.float32), allow_pickle=False)
        with pytest.raises(ValueError, match="one-dimensional float64"):
            catalog._atr_length(bad_dtype)

        bad_shape = tmp_path / "bad_shape.npy"
        np.save(bad_shape, np.asarray([[0.1]], dtype=np.float64), allow_pickle=False)
        with pytest.raises(ValueError, match="one-dimensional float64"):
            catalog._atr_length(bad_shape)

        atr_path = tmp_path / "atr.npy"
        np.save(atr_path, np.asarray([0.1], dtype=np.float64), allow_pickle=False)
        assert catalog._atr_length(atr_path) == 1
        # The cached expected length detects a source replacement during build.
        np.save(atr_path, np.asarray([0.1, 0.2], dtype=np.float64), allow_pickle=False)
        with pytest.raises(ValueError, match="source changed"):
            catalog._prepare_entry(
                0,
                atr_path,
                profile,
                profile.initial_drop_multiplier,
                allow_zero=False,
            )
    finally:
        catalog.close()


def test_catalog_load_rejects_corrupt_effective_arrays(tmp_path: Path) -> None:
    profile = _profile()
    catalog = EffectiveArrayCatalog(tmp_path / "catalog")
    paths = catalog._paths(0, profile, profile.initial_drop_multiplier, allow_zero=False)
    try:
        assert catalog._load(paths) is None
        cases = [
            (
                np.asarray([1], dtype=np.int32),
                np.asarray([optimization._STATE_RAW], dtype=np.uint8),
                "dtype",
            ),
            (
                np.asarray([[1]], dtype=np.int16),
                np.asarray([[optimization._STATE_RAW]], dtype=np.uint8),
                "one-dimensional",
            ),
            (
                np.asarray([1, 2], dtype=np.int16),
                np.asarray([optimization._STATE_RAW], dtype=np.uint8),
                "not aligned",
            ),
        ]
        for values, states, message in cases:
            np.save(paths.values, values, allow_pickle=False)
            np.save(paths.states, states, allow_pickle=False)
            with pytest.raises(ValueError, match=message):
                catalog._load(paths)
            paths.values.unlink()
            paths.states.unlink()

        np.save(paths.values, np.asarray([1], dtype=np.int16), allow_pickle=False)
        np.save(
            paths.states,
            np.asarray([optimization._STATE_RAW], dtype=np.uint8),
            allow_pickle=False,
        )
        catalog._expected_lengths[str(paths.values)] = 2
        with pytest.raises(ValueError, match="ATR source length"):
            catalog._load(paths)
    finally:
        catalog.close()


def test_profile_expansion_and_validation_fail_closed() -> None:
    profile = _profile()
    other = AtrProfile(14, 60, 1.5, 1.0, 1.0, 1.0)
    representative_results = [(profile.key(), _summary(profile), [])]
    expanded = optimization.expand_evaluation_results(
        [profile, other], [profile], [0, 0], representative_results
    )
    assert [key for key, _summary_value, _sessions in expanded] == [
        profile.key(),
        other.key(),
    ]
    optimization.validate_nominal_profile_results([profile, other], expanded)

    with pytest.raises(RuntimeError, match="mapping"):
        optimization.expand_evaluation_results([profile], [profile], [], representative_results)
    with pytest.raises(RuntimeError, match="incomplete"):
        optimization.expand_evaluation_results([profile], [profile], [0], [])
    with pytest.raises(RuntimeError, match="duplicate"):
        optimization.expand_evaluation_results(
            [profile],
            [profile, profile],
            [0],
            representative_results + representative_results,
        )
    with pytest.raises(RuntimeError, match="invalid representative"):
        optimization.expand_evaluation_results(
            [profile], [profile], [2], representative_results
        )
    with pytest.raises(RuntimeError, match="omitted"):
        optimization.expand_evaluation_results(
            [profile], [profile], [0], [(other.key(), _summary(other), [])]
        )
    with pytest.raises(RuntimeError, match="incomplete"):
        optimization.validate_nominal_profile_results([profile], [])
    with pytest.raises(RuntimeError, match="order"):
        optimization.validate_nominal_profile_results(
            [profile], [(other.key(), _summary(other), [])]
        )


def test_worker_and_batch_selection_boundary_cases() -> None:
    assert optimization.memory_aware_worker_count(
        cpu_count=1,
        available_memory_bytes=None,
        estimated_replay_context_bytes=1,
        pending_profiles=0,
    ) == 1
    assert optimization.memory_aware_worker_count(
        cpu_count=32,
        available_memory_bytes=1,
        estimated_replay_context_bytes=1 << 40,
        pending_profiles=100,
        automatic_ceiling=64,
    ) == 1
    assert optimization.memory_aware_worker_count(
        cpu_count=32,
        available_memory_bytes=64 << 30,
        estimated_replay_context_bytes=128 << 20,
        pending_profiles=100,
        automatic_ceiling=20,
    ) == 20
    assert optimization.adaptive_profile_batch_size(
        total_rows=0, profile_count=1, worker_count=1, configured_maximum=999
    ) == 1
    assert optimization.adaptive_profile_batch_size(
        total_rows=100_000, profile_count=10_000, worker_count=2
    ) == 8
    assert optimization.adaptive_profile_batch_size(
        total_rows=500_000, profile_count=10_000, worker_count=2
    ) == 4
    assert optimization.adaptive_profile_batch_size(
        total_rows=1_500_000, profile_count=10_000, worker_count=2
    ) == 2
