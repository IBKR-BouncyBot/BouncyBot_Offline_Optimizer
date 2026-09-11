"""Regression tests for analysis-wide deterministic process parallelism."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from optimizer.ibrec import load_ibrec_set
from optimizer.market_replay import (
    _assumption_stress_scenario_row,
    _atr_cache_key,
    _atr_phase_task_result,
    _control_profile,
    _fixed_leave_one_out_task_result,
    _leave_one_day_out_selection_row,
    _leave_one_day_out_selection_rows,
    _precompute_atr,
    _selection_aware_bootstrap_replicate_row,
    _session_ticks,
    _walk_forward_fold_row,
    run_market_replay_analysis,
)
from optimizer.market_replay_fast import (
    AnalysisWorkerTask,
    PreparedReplayStore,
    ProfileBatchEvaluator,
)
from optimizer.market_replay_models import AtrProfile, MarketReplayConfig
from tests.market_replay_fixtures import make_ticks, write_v3


def _loaded_fixture(
    tmp_path: Path,
    *,
    sessions: int = 3,
    points_per_session: int = 24,
):
    # Keep process-dispatch tests fast while retaining enough 15-second rows
    # for the shortest ATR window to warm up. The tests validate scheduling
    # equivalence, not the statistical strength of the synthetic sample.
    rows, periods = make_ticks(
        sessions=sessions,
        points_per_session=points_per_session,
    )
    recording_path = write_v3(tmp_path / "recording.ibrec", rows, periods)
    config = MarketReplayConfig(
        recording_path,
        tmp_path / "reports",
        worker_processes=2,
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


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_plain(item) for item in value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def test_one_pool_is_reused_for_profiles_and_every_post_search_task_kind(
    tmp_path: Path,
) -> None:
    """Stage 3 and all expensive validation task types share one worker pool."""

    recording, config, period_ticks = _loaded_fixture(tmp_path)
    control = _control_profile(config)
    candidate = AtrProfile(7, 30, 1.25, 0.75, 1.25, 0.75)
    profiles = [control, candidate]
    atr_cache = _atr_cache(period_ticks, profiles)
    days = tuple(period.session_date for period, _ticks in period_ticks)
    units = tuple((day,) for day in days)
    variant = replace(config, assumed_trade_notional=5_000.0).normalized()
    tasks = [
        AnalysisWorkerTask(
            "loo-selector",
            "leave_one_out_selector",
            (days[0],),
        ),
        AnalysisWorkerTask(
            "fixed-loo",
            "fixed_leave_one_out",
            (days[0], candidate, control),
        ),
        AnalysisWorkerTask(
            "phase",
            "atr_phase",
            (candidate, 5),
        ),
        AnalysisWorkerTask(
            "stress",
            "assumption_stress",
            ("half-notional", variant, candidate, control),
        ),
        AnalysisWorkerTask(
            "walk-forward",
            "walk_forward",
            (1, days[:2], days[2:], candidate),
        ),
        AnalysisWorkerTask(
            "selection-bootstrap",
            "selection_bootstrap",
            (1, (0, 0, 1), units, candidate),
        ),
    ]

    def serial(task: AnalysisWorkerTask) -> Any:
        if task.kind == "leave_one_out_selector":
            return _leave_one_day_out_selection_row(
                recording,
                period_ticks,
                config,
                str(task.payload[0]),
            )
        if task.kind == "fixed_leave_one_out":
            return _fixed_leave_one_out_task_result(
                recording,
                period_ticks,
                config,
                str(task.payload[0]),
                task.payload[1],
                task.payload[2],
            )
        if task.kind == "atr_phase":
            return _atr_phase_task_result(
                recording,
                period_ticks,
                task.payload[0],
                int(task.payload[1]),
                config,
            )
        if task.kind == "assumption_stress":
            return _assumption_stress_scenario_row(
                recording,
                period_ticks,
                str(task.payload[0]),
                task.payload[1],
                task.payload[2],
                task.payload[3],
            )
        if task.kind == "walk_forward":
            return _walk_forward_fold_row(
                recording,
                period_ticks,
                config,
                int(task.payload[0]),
                task.payload[1],
                task.payload[2],
                task.payload[3],
            )
        if task.kind == "selection_bootstrap":
            return _selection_aware_bootstrap_replicate_row(
                recording,
                period_ticks,
                config,
                int(task.payload[0]),
                task.payload[1],
                task.payload[2],
                task.payload[3],
            )
        raise AssertionError(task.kind)

    expected = [_plain(serial(task)) for task in tasks]
    progress: list[tuple[str, int, int]] = []
    with PreparedReplayStore.create(period_ticks) as store:
        store.persist_atr_cache(atr_cache)
        with ProfileBatchEvaluator(
            store,
            min_tick=recording.min_tick,
            config=config,
            requested_workers=2,
            recording=recording,
        ) as evaluator:
            # Start the pool through the Stage-3 profile path first.
            evaluator.evaluate(
                profiles,
                summary_day_weights=None,
                progress=None,
            )
            original_pool = evaluator._pool
            assert original_pool is not None
            actual = evaluator.run_analysis_tasks(
                tasks,
                serial_handler=serial,
                progress=lambda message, current, total: progress.append(
                    (message, current, total)
                ),
                message="Post-Stage 3 integration probe",
            )
            assert evaluator._pool is original_pool

    assert [_plain(value) for value in actual] == expected
    assert progress[0][1:] == (0, len(tasks))
    assert progress[-1][1:] == (len(tasks), len(tasks))
    assert all("2 worker processes" in message for message, _current, _total in progress)
    assert progress[-1][0].endswith("0 pending")


def test_analysis_task_scheduler_fails_closed_on_invalid_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording, config, period_ticks = _loaded_fixture(tmp_path, sessions=1)

    with PreparedReplayStore.create(period_ticks) as store:
        evaluator = ProfileBatchEvaluator(
            store,
            min_tick=recording.min_tick,
            config=config,
            requested_workers=2,
            recording=recording,
        )
        with pytest.raises(ValueError, match="identifiers must be unique"):
            evaluator.run_analysis_tasks(
                [AnalysisWorkerTask("duplicate", "atr_phase"), AnalysisWorkerTask("duplicate", "atr_phase")],
                serial_handler=lambda _task: None,
                progress=None,
                message="probe",
            )

        pending: Future[tuple[str, object]] = Future()

        class MismatchingPool:
            def __init__(self) -> None:
                self.calls = 0

            def submit(self, *_args: Any, **_kwargs: Any) -> Future[tuple[str, object]]:
                self.calls += 1
                if self.calls == 1:
                    future: Future[tuple[str, object]] = Future()
                    future.set_result(("wrong-task-id", None))
                    return future
                return pending

        monkeypatch.setattr(evaluator, "_ensure_pool", lambda: MismatchingPool())
        with pytest.raises(RuntimeError, match="mismatched task identifier"):
            evaluator.run_analysis_tasks(
                [AnalysisWorkerTask("one", "atr_phase"), AnalysisWorkerTask("two", "atr_phase")],
                serial_handler=lambda _task: None,
                progress=None,
                message="probe",
            )
        assert pending.cancelled()
        evaluator.close()
        with pytest.raises(RuntimeError, match="already closed"):
            evaluator.run_analysis_tasks(
                [AnalysisWorkerTask("closed", "atr_phase")],
                serial_handler=lambda _task: None,
                progress=None,
                message="probe",
            )


def test_exact_leave_one_out_selector_parallel_matches_serial(
    tmp_path: Path,
) -> None:
    recording, config, period_ticks = _loaded_fixture(tmp_path)
    serial = _leave_one_day_out_selection_rows(
        recording,
        period_ticks,
        config,
    )
    with PreparedReplayStore.create(period_ticks) as store:
        with ProfileBatchEvaluator(
            store,
            min_tick=recording.min_tick,
            config=config,
            requested_workers=2,
            recording=recording,
        ) as evaluator:
            parallel = _leave_one_day_out_selection_rows(
                recording,
                period_ticks,
                config,
                batch_evaluator=evaluator,
            )
    assert parallel == serial


def test_progress_moves_beyond_completed_boundary_probes(
    tmp_path: Path,
) -> None:
    rows, periods = make_ticks()
    recording_path = write_v3(tmp_path / "recording.ibrec", rows, periods)
    progress: list[tuple[str, int, int]] = []
    run_market_replay_analysis(
        MarketReplayConfig(
            recording_path,
            tmp_path / "reports",
            worker_processes=1,
        ),
        progress=lambda message, current, total: progress.append(
            (message, current, total)
        ),
    )

    stage3_index = next(
        index
        for index, (message, _current, _total) in enumerate(progress)
        if message == "Stage 3 search and boundary probes complete"
    )
    later_messages = [message for message, _current, _total in progress[stage3_index + 1 :]]
    assert any(message.startswith("Post-Stage 3:") for message in later_messages)
    assert later_messages[-1] == "Market Replay analysis complete"
    assert any(
        message == "Post-Stage 3: no stable-region centers require robustness validation"
        for message in later_messages
    )
