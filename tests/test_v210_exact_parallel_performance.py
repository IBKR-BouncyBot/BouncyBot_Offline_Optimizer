"""Regression tests for the v2.1 exact compact and parallel replay engine."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pytest

from optimizer.ibrec import load_ibrec_set
from optimizer.market_replay import (
    _atr_cache_key,
    _control_profile,
    _evaluate_profile,
    _precompute_atr,
    _session_ticks,
    market_replay_search_contract,
    run_market_replay_analysis,
)
from optimizer.market_replay_fast import (
    PreparedReplayStore,
    ProfileBatchEvaluator,
    packaged_spawn_smoke_test,
)
from optimizer.market_replay_models import AtrProfile, MarketReplayConfig
from optimizer.market_replay_reports import write_market_replay_report
from tests.market_replay_fixtures import make_ticks, write_v3


def _loaded_fixture(tmp_path: Path):
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


def _atr_cache(period_ticks, profiles):
    cache = {}
    for period, ticks in period_ticks:
        for profile in profiles:
            key = _atr_cache_key(period, profile.period, profile.bar_seconds)
            if key not in cache:
                cache[key] = _precompute_atr(
                    ticks,
                    profile.period,
                    profile.bar_seconds,
                )
    return cache


def _serialized_result(summary, sessions):
    return asdict(summary), [asdict(session) for session in sessions]


def test_compact_tick_store_preserves_every_replay_relevant_field(
    tmp_path: Path,
) -> None:
    recording, _config, period_ticks = _loaded_fixture(tmp_path)
    period, ticks = period_ticks[0]
    with PreparedReplayStore.create(period_ticks) as store:
        compact = store.open_ticks(period)
        try:
            assert len(compact) == len(ticks)
            for index in (0, len(ticks) // 2, len(ticks) - 1):
                expected = ticks[index]
                actual = compact[index]
                assert actual.sequence == expected.sequence
                assert actual.timestamp == expected.timestamp
                assert actual.captured_at_utc == expected.captured_at_utc
                assert actual.elapsed_ns == expected.elapsed_ns
                assert actual.selected_price() == expected.selected_price()
                assert actual.valid_bid() == expected.valid_bid()
                assert actual.valid_ask() == expected.valid_ask()
                assert actual.last == expected.last
                assert actual.mark_price == expected.mark_price
                assert actual.bid_size == expected.bid_size
                assert actual.ask_size == expected.ask_size
                assert actual.full_snapshot == expected.full_snapshot
                assert actual.has_last_event() == expected.has_last_event()
                assert actual.has_bid_event() == expected.has_bid_event()
                assert actual.has_ask_event() == expected.has_ask_event()
        finally:
            compact.close()
    assert recording.ticks  # The compact store never mutates source evidence.


def test_compact_serial_and_spawned_parallel_evaluation_match_reference_exactly(
    tmp_path: Path,
) -> None:
    recording, config, period_ticks = _loaded_fixture(tmp_path)
    profiles = [
        _control_profile(config),
        AtrProfile(
            period=7,
            bar_seconds=30,
            initial_drop_multiplier=1.25,
            buy_rebound_multiplier=0.75,
            minimum_profit_multiplier=1.25,
            sell_trail_multiplier=0.75,
        ),
    ]
    cache = _atr_cache(period_ticks, profiles)
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
        reference[profile.key()] = _serialized_result(summary, sessions)

    with PreparedReplayStore.create(period_ticks) as store:
        store.persist_atr_cache(cache)
        with ProfileBatchEvaluator(
            store,
            min_tick=recording.min_tick,
            config=config,
            requested_workers=1,
        ) as serial:
            serial_results = {
                key: _serialized_result(summary, sessions)
                for key, summary, sessions in serial.evaluate(
                    profiles,
                    summary_day_weights=None,
                    progress=None,
                )
            }
        with ProfileBatchEvaluator(
            store,
            min_tick=recording.min_tick,
            config=config,
            requested_workers=2,
        ) as parallel:
            parallel_results = {
                key: _serialized_result(summary, sessions)
                for key, summary, sessions in parallel.evaluate(
                    profiles,
                    summary_day_weights=None,
                    progress=None,
                )
            }

    assert serial_results == reference
    assert parallel_results == reference


def test_compact_parallel_matches_reference_for_multisession_policy_and_quote_edges(
    tmp_path: Path,
) -> None:
    rows, periods = make_ticks(sessions=3)
    # Exercise cached-Last quote updates, one-sided quotes, and crossed quotes.
    # These are the cases where precomputed event flags and executable touches
    # must remain exactly equivalent to the source object methods.
    rows[20]["changed_fields"] = "bid,ask"
    rows[21]["ask"] = None
    rows[21]["changed_fields"] = "bid"
    rows[22]["bid"] = 101.0
    rows[22]["ask"] = 100.0
    rows[22]["changed_fields"] = "bid,ask,last"
    recording_path = write_v3(tmp_path / "multi.ibrec", rows, periods)
    config = MarketReplayConfig(
        recording_path,
        tmp_path / "reports",
        worker_processes=1,
        continuous_overnight_replay=True,
        execution_cost_overrides=(("2026-01-06", 2.5, 3.5),),
        trade_notional_overrides=(("20260107", 7_500.0),),
    ).normalized()
    recording = load_ibrec_set(config)
    period_ticks = [
        (period, ticks)
        for period in recording.periods
        if (ticks := _session_ticks(recording, period))
    ]
    profiles = [
        _control_profile(config),
        AtrProfile(7, 30, 1.25, 0.0, 1.25, 0.0),
        AtrProfile(14, 60, 1.5, 0.75, 1.0, 1.0, protective_sell_mode="manual", protective_sell_value=3.0),
        AtrProfile(14, 60, 1.5, 0.75, 1.0, 1.0, protective_sell_mode="atr", protective_sell_value=2.5),
        AtrProfile(21, 120, 2.0, 1.25, 1.5, 1.25, min_atr_pct=0.05),
    ]
    cache = _atr_cache(period_ticks, profiles)
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
        reference[profile.key()] = _serialized_result(summary, sessions)

    with PreparedReplayStore.create(period_ticks) as store:
        store.persist_atr_cache(cache)
        with ProfileBatchEvaluator(
            store,
            min_tick=recording.min_tick,
            config=config,
            requested_workers=2,
        ) as evaluator:
            parallel = {
                key: _serialized_result(summary, sessions)
                for key, summary, sessions in evaluator.evaluate(
                    profiles,
                    summary_day_weights=None,
                    progress=None,
                )
            }

    assert parallel == reference


def test_worker_count_changes_execution_only_not_analysis_identity_or_results(
    tmp_path: Path,
) -> None:
    rows, periods = make_ticks()
    recording_path = write_v3(tmp_path / "recording.ibrec", rows, periods)
    one = run_market_replay_analysis(
        MarketReplayConfig(
            recording_path,
            tmp_path / "one",
            worker_processes=1,
        )
    )
    two = run_market_replay_analysis(
        MarketReplayConfig(
            recording_path,
            tmp_path / "two",
            worker_processes=2,
        )
    )

    assert one.analysis_id == two.analysis_id
    assert one.search_contract == two.search_contract
    assert one.recommendation.to_dict() == two.recommendation.to_dict()
    assert [candidate.to_dict() for candidate in one.candidates] == [
        candidate.to_dict() for candidate in two.candidates
    ]

    one_report = write_market_replay_report(one)
    two_report = write_market_replay_report(two)
    one_files = {
        path.relative_to(one_report.output_dir): path.read_bytes()
        for path in one_report.output_dir.rglob("*")
        if path.is_file()
    }
    two_files = {
        path.relative_to(two_report.output_dir): path.read_bytes()
        for path in two_report.output_dir.rglob("*")
        if path.is_file()
    }
    assert one_files == two_files


def test_worker_failure_aborts_instead_of_returning_partial_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording, config, period_ticks = _loaded_fixture(tmp_path)
    profiles = [
        _control_profile(config),
        AtrProfile(
            period=7,
            bar_seconds=30,
            initial_drop_multiplier=1.25,
            buy_rebound_multiplier=0.75,
            minimum_profit_multiplier=1.25,
            sell_trail_multiplier=0.75,
        ),
    ]
    cache = _atr_cache(period_ticks, profiles)

    class FailingPool:
        def submit(self, *_args, **_kwargs):
            future: Future[object] = Future()
            future.set_exception(RuntimeError("simulated worker failure"))
            return future

    with PreparedReplayStore.create(period_ticks) as store:
        store.persist_atr_cache(cache)
        with ProfileBatchEvaluator(
            store,
            min_tick=recording.min_tick,
            config=config,
            requested_workers=2,
        ) as evaluator:
            monkeypatch.setattr(evaluator, "_ensure_pool", lambda: FailingPool())
            with pytest.raises(RuntimeError, match="simulated worker failure"):
                evaluator.evaluate(
                    profiles,
                    summary_day_weights=None,
                    progress=None,
                )


def test_worker_setting_is_validated_and_excluded_from_search_contract(
    tmp_path: Path,
) -> None:
    path = tmp_path / "recording.ibrec"
    auto = MarketReplayConfig(path, tmp_path / "reports", worker_processes=0).normalized()
    single = MarketReplayConfig(path, tmp_path / "reports", worker_processes=1).normalized()
    maximum = MarketReplayConfig(path, tmp_path / "reports", worker_processes=64).normalized()
    assert auto.worker_processes == 0
    assert single.worker_processes == 1
    assert maximum.worker_processes == 64
    assert auto.normalized() == auto
    assert replace(auto, worker_processes=2).normalized().worker_processes == 2
    assert market_replay_search_contract(auto) == market_replay_search_contract(single)

    for invalid in (-1, 65, True, 1.5, "two"):
        with pytest.raises(ValueError, match="worker_processes"):
            MarketReplayConfig(
                path,
                tmp_path / "reports",
                worker_processes=invalid,  # type: ignore[arg-type]
            ).normalized()


def test_frozen_entrypoint_initializes_multiprocessing_before_application_main() -> None:
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    assert "multiprocessing.freeze_support()" in source
    assert source.index("multiprocessing.freeze_support()") < source.index(
        "raise SystemExit(main())"
    )


def test_spawn_smoke_probe_works_from_source() -> None:
    assert packaged_spawn_smoke_test() is True


def test_numpy_is_pinned_for_reproducible_windows_release() -> None:
    root = Path(__file__).parents[1]
    assert '"numpy>=2.3,<3"' in (root / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert "numpy>=2.3,<3" in (root / "requirements.txt").read_text(
        encoding="utf-8"
    )
    assert "numpy==2.3.5" in (
        root / "requirements-release-win64.lock"
    ).read_text(encoding="utf-8")
    build = (root / "scripts/build_windows.ps1").read_text(encoding="utf-8-sig")
    assert "--packaged-multiprocessing-smoke-test" in build


def test_automatic_small_analysis_keeps_reference_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Small Automatic runs must avoid compact-array and spawn overhead."""

    rows, periods = make_ticks()
    recording_path = write_v3(tmp_path / "small.ibrec", rows, periods)

    def unexpected_create(*_args, **_kwargs):
        raise AssertionError("Automatic small analysis unexpectedly prepared compact arrays")

    monkeypatch.setattr(PreparedReplayStore, "create", unexpected_create)
    result = run_market_replay_analysis(
        MarketReplayConfig(
            recording_path,
            tmp_path / "reports",
            worker_processes=0,
        )
    )
    assert result.candidates


def test_compact_atr_storage_preserves_missing_and_finite_values(
    tmp_path: Path,
) -> None:
    recording, _config, period_ticks = _loaded_fixture(tmp_path)
    period, _ticks = period_ticks[0]
    values = [None, 0.25, float("nan"), 1.5]
    with PreparedReplayStore.create(period_ticks) as store:
        path = store.persist_atr(period, 99, 17, values)
        restored = np.load(path, mmap_mode="r", allow_pickle=False)
        try:
            assert np.isnan(restored[0])
            assert restored[1] == pytest.approx(0.25)
            assert np.isnan(restored[2])
            assert restored[3] == pytest.approx(1.5)
        finally:
            mmap_object = getattr(restored, "_mmap", None)
            if mmap_object is not None:
                mmap_object.close()
    assert recording.min_tick > 0
