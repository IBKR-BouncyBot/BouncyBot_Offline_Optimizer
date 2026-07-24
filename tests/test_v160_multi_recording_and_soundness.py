from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from optimizer.ibrec import (
    IbrecError,
    combine_ibrec_recordings,
    inspect_ibrec_set,
    load_ibrec,
    load_ibrec_set,
)
from optimizer.market_replay import (
    _candidate_robustness,
    _precompute_atr,
    _refinement_seeds,
    _session_ticks,
    _simulate_session,
    _summary,
    run_market_replay_analysis,
)
from optimizer.market_replay_models import (
    AtrProfile,
    MarketReplayCandidateSummary,
    MarketReplayConfig,
    MarketReplaySessionResult,
)
from optimizer.market_replay_reports import write_market_replay_report
from tests.market_replay_fixtures import make_ticks, write_v3


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _hash(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _daily_recordings(root: Path, *, days: int = 5) -> list[Path]:
    ticks, periods = make_ticks(sessions=days)
    outputs: list[Path] = []
    for index, period in enumerate(periods, start=1):
        period_id = int(period["period_id"])
        source_rows = [row for row in ticks if int(row["rth_period_id"]) == period_id]
        first_elapsed = int(source_rows[0]["elapsed_ns"])
        rows: list[dict[str, Any]] = []
        for sequence, row in enumerate(source_rows, start=1):
            rows.append(
                {
                    **row,
                    "sequence": sequence,
                    "elapsed_ns": int(row["elapsed_ns"]) - first_elapsed,
                    "rth_period_id": 1,
                }
            )
        single_period = {
            **period,
            "period_id": 1,
            "first_tick_sequence": 1,
            "last_tick_sequence": len(rows),
            "tick_count": len(rows),
        }
        outputs.append(
            write_v3(root / f"day_{index:02d}.ibrec", rows, [single_period])
        )
    return outputs


def _session(
    day: str,
    return_bps: float,
    *,
    right_censored: bool = False,
    unmarked: bool = False,
) -> MarketReplaySessionResult:
    return MarketReplaySessionResult(
        session_date=day,
        period_id=int(day[-2:]),
        scheduled_open_utc=f"{day}T13:30:00Z",
        scheduled_close_utc=f"{day}T20:00:00Z",
        observed_start_utc=f"{day}T13:30:00Z",
        observed_end_utc=f"{day}T20:00:00Z",
        ticks=100,
        trades=1,
        completed_trades=0 if right_censored else 1,
        no_trade=False,
        open_position=right_censored,
        realized_return_bps=return_bps,
        marked_return_bps=return_bps,
        conservative_return_bps=return_bps,
        max_drawdown_bps=0.0,
        right_censored=right_censored,
        unmarked_open_position=unmarked,
    )


def _candidate(profile: AtrProfile, score: float) -> MarketReplayCandidateSummary:
    return MarketReplayCandidateSummary(
        profile=profile,
        score=score,
        sessions=5,
        completed_sessions=5,
        sessions_with_trades=5,
        completed_trades=5,
        open_position_sessions=0,
        no_trade_sessions=0,
        median_return_bps=score,
        mean_return_bps=score,
        worst_return_bps=score,
        maximum_drawdown_bps=0.0,
        open_position_rate_pct=0.0,
        no_trade_rate_pct=0.0,
    )


def test_config_accepts_an_immutable_recording_set_and_rejects_duplicates(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.ibrec"
    second = tmp_path / "second.ibrec"
    normalized = MarketReplayConfig(
        (second, first),
        tmp_path / "reports",
    ).normalized()
    assert normalized.recording_paths == tuple(
        sorted((first.absolute(), second.absolute()), key=lambda path: str(path).casefold())
    )
    with pytest.raises(ValueError, match="same Market Replay path"):
        MarketReplayConfig((first, first), tmp_path / "reports").normalized()
    with pytest.raises(ValueError, match="At most 1"):
        MarketReplayConfig(
            (first, second),
            tmp_path / "reports",
            max_recordings=1,
        ).normalized()


def test_five_daily_recordings_combine_and_are_input_order_independent(
    tmp_path: Path,
) -> None:
    paths = _daily_recordings(tmp_path / "recordings")
    first = load_ibrec_set(MarketReplayConfig(tuple(paths), tmp_path / "reports-a"))
    second = load_ibrec_set(
        MarketReplayConfig(tuple(reversed(paths)), tmp_path / "reports-b")
    )
    assert first.sha256 == second.sha256
    assert first.input_components == second.input_components
    assert first.input_recording_count == 5
    assert len(first.periods) == 5
    assert [period.session_date for period in first.periods] == sorted(
        period.session_date for period in first.periods
    )
    assert all(period.source_recording_sha256 for period in first.periods)
    assert first.retained_row_count == len(first.ticks)


def test_duplicate_recording_content_is_rejected(tmp_path: Path) -> None:
    path = _daily_recordings(tmp_path / "recordings", days=1)[0]
    duplicate = tmp_path / "duplicate.ibrec"
    duplicate.write_bytes(path.read_bytes())
    with pytest.raises(IbrecError, match="same Market Replay recording content"):
        load_ibrec_set(
            MarketReplayConfig((path, duplicate), tmp_path / "reports")
        )


def test_recording_set_requires_the_same_instrument_identity(tmp_path: Path) -> None:
    first_ticks, first_periods = make_ticks(symbol="AAPL", con_id=265598)
    second_ticks, second_periods = make_ticks(symbol="MSFT", con_id=272093)
    first = write_v3(tmp_path / "aapl.ibrec", first_ticks, first_periods)
    second = write_v3(tmp_path / "msft.ibrec", second_ticks, second_periods)
    with pytest.raises(IbrecError, match="same instrument"):
        load_ibrec_set(MarketReplayConfig((first, second), tmp_path / "reports"))
    with pytest.raises(IbrecError, match="same instrument identity"):
        inspect_ibrec_set(MarketReplayConfig((first, second), tmp_path / "reports"))


def test_multiple_recordings_require_complete_identity_metadata(tmp_path: Path) -> None:
    paths = _daily_recordings(tmp_path / "recordings", days=2)
    recordings = [
        load_ibrec(MarketReplayConfig(path, tmp_path / f"reports-{index}"))
        for index, path in enumerate(paths)
    ]
    incomplete_contract = dict(recordings[1].contract)
    incomplete_contract.pop("currency", None)
    recordings[1] = replace(recordings[1], contract=incomplete_contract)
    with pytest.raises(IbrecError, match="cannot prove instrument identity"):
        combine_ibrec_recordings(recordings)


def test_overlapping_dates_are_excluded_in_full_instead_of_spliced(
    tmp_path: Path,
) -> None:
    paths = _daily_recordings(tmp_path / "recordings", days=2)
    duplicate_ticks, duplicate_periods = make_ticks(sessions=1)
    duplicate_ticks[0]["bid"] = float(duplicate_ticks[0]["bid"]) - 0.001
    overlap = write_v3(
        tmp_path / "recordings" / "overlap.ibrec",
        duplicate_ticks,
        duplicate_periods,
    )
    recording = load_ibrec_set(
        MarketReplayConfig((paths[0], paths[1], overlap), tmp_path / "reports")
    )
    assert len(recording.periods) == 1
    assert recording.periods[0].session_date == "20260106"
    assert recording.excluded_sessions[0]["session_date"] == "20260105"
    assert "never splice" not in " ".join(recording.issues).lower()
    assert "excluded" in " ".join(recording.issues).lower()


def test_all_overlapping_dates_fail_closed(tmp_path: Path) -> None:
    rows, periods = make_ticks(sessions=1)
    first = write_v3(tmp_path / "first.ibrec", rows, periods)
    changed = [dict(row) for row in rows]
    changed[-1]["ask"] = float(changed[-1]["ask"]) + 0.001
    second = write_v3(tmp_path / "second.ibrec", changed, periods)
    with pytest.raises(IbrecError, match="No unambiguous trading date"):
        load_ibrec_set(MarketReplayConfig((first, second), tmp_path / "reports"))


def test_multi_recording_analysis_and_report_are_deterministic(
    tmp_path: Path,
) -> None:
    paths = _daily_recordings(tmp_path / "recordings")
    first = write_market_replay_report(
        run_market_replay_analysis(
            MarketReplayConfig(tuple(paths), tmp_path / "reports-a")
        )
    )
    second = write_market_replay_report(
        run_market_replay_analysis(
            MarketReplayConfig(tuple(reversed(paths)), tmp_path / "reports-b")
        )
    )
    assert first.analysis_id == second.analysis_id
    assert first.recommendation.profile == second.recommendation.profile
    assert _tree(first.output_dir) == _tree(second.output_dir)
    assert (first.output_dir / "input_recordings.csv").is_file()
    assert (first.output_dir / "excluded_sessions.csv").is_file()
    assert first.recording.input_recording_count == 5


def test_atr_uses_monotonic_elapsed_time_not_receipt_wall_clock(tmp_path: Path) -> None:
    path = _daily_recordings(tmp_path / "recordings", days=1)[0]
    recording = load_ibrec(MarketReplayConfig(path, tmp_path / "reports"))
    ticks = _session_ticks(recording, recording.periods[0])
    shifted = [replace(tick, timestamp=tick.timestamp + 86_400.0) for tick in ticks]
    assert _precompute_atr(ticks, 5, 15) == _precompute_atr(shifted, 5, 15)


def test_atr_requires_fresh_warmup_after_a_long_monotonic_gap(tmp_path: Path) -> None:
    path = _daily_recordings(tmp_path / "recordings", days=1)[0]
    recording = load_ibrec(MarketReplayConfig(path, tmp_path / "reports"))
    ticks = _session_ticks(recording, recording.periods[0])
    split = 40
    gap = 1_000 * 1_000_000_000
    gapped = [
        replace(tick, elapsed_ns=tick.elapsed_ns + (gap if index >= split else 0))
        for index, tick in enumerate(ticks)
    ]
    values = _precompute_atr(gapped, 5, 15)
    assert any(value is not None for value in values[:split])
    assert values[split] is None


def test_missing_same_side_touch_leaves_market_order_pending_and_unmarked(
    tmp_path: Path,
) -> None:
    rows, periods = make_ticks()
    for row in rows:
        row["bid"] = None
    path = write_v3(tmp_path / "no-bids.ibrec", rows, periods)
    recording = load_ibrec(MarketReplayConfig(path, tmp_path / "reports"))
    ticks = _session_ticks(recording, recording.periods[0])
    profile = AtrProfile(5, 15, 0.75, 0.0, 100.0, 1.0)
    session, _ = _simulate_session(
        ticks,
        recording.periods[0],
        profile,
        _precompute_atr(ticks, 5, 15),
        recording.min_tick,
        keep_trades=True,
    )
    assert session.open_position is True
    assert session.right_censored is True
    assert session.unmarked_open_position is True
    assert "could not be marked" in " ".join(session.issues)


def test_pre_entry_bid_is_not_reused_to_mark_a_later_open_position(
    tmp_path: Path,
) -> None:
    rows, periods = make_ticks()
    for index, row in enumerate(rows):
        row["bid"] = 99.0 if index == 0 else None
    path = write_v3(tmp_path / "stale-pre-entry-bid.ibrec", rows, periods)
    recording = load_ibrec(MarketReplayConfig(path, tmp_path / "reports"))
    ticks = _session_ticks(recording, recording.periods[0])
    profile = AtrProfile(5, 15, 0.75, 0.0, 100.0, 1.0)
    session, _ = _simulate_session(
        ticks,
        recording.periods[0],
        profile,
        _precompute_atr(ticks, 5, 15),
        recording.min_tick,
        keep_trades=True,
    )
    assert session.open_position is True
    assert session.unmarked_open_position is True
    assert session.marked_return_bps == session.realized_return_bps


def test_recording_set_enforces_aggregate_row_limit_during_preflight_and_load(
    tmp_path: Path,
) -> None:
    paths = _daily_recordings(tmp_path / "recordings", days=2)
    config = MarketReplayConfig(
        tuple(paths),
        tmp_path / "reports",
        max_rows=200,
    )
    with pytest.raises(IbrecError, match="aggregate limit"):
        inspect_ibrec_set(config)
    with pytest.raises(IbrecError, match="aggregate limit"):
        load_ibrec_set(config)


def test_right_censored_or_unmarked_pairs_cannot_support_a_changed_profile() -> None:
    days = [f"2026-07-{day:02d}" for day in range(6, 11)]
    control_profile = AtrProfile(14, 60, 1.50, 0.75, 1.00, 1.00)
    changed_profile = AtrProfile(14, 60, 1.50, 0.75, 1.00, 0.75)
    control_sessions = [_session(day, 0.0) for day in days]
    candidate_sessions = [_session(day, 20.0) for day in days]
    candidate_sessions[-1] = _session(
        days[-1],
        20.0,
        right_censored=True,
        unmarked=True,
    )
    evidence, _ = _candidate_robustness(
        _summary(changed_profile, candidate_sessions),
        candidate_sessions,
        _summary(control_profile, control_sessions),
        control_sessions,
        seed="right-censored-gate",
    )
    assert evidence["passed"] is False
    reasons = " ".join(evidence["failure_reasons"]).lower()
    assert "right-censored" in reasons
    assert "without a valid bid-side end mark" in reasons


def test_refinement_seeds_are_allocated_across_selected_atr_windows() -> None:
    windows = ((5, 15), (14, 60), (21, 120))
    candidates: list[MarketReplayCandidateSummary] = []
    for window_index, (period, bar_seconds) in enumerate(windows):
        for rank in range(8):
            candidates.append(
                _candidate(
                    AtrProfile(
                        period,
                        bar_seconds,
                        1.50 + rank * 0.01,
                        0.75,
                        1.00,
                        1.00,
                    ),
                    100.0 - window_index * 10.0 - rank,
                )
            )
    seeds = _refinement_seeds(candidates, maximum=6)
    assert len(seeds) == 6
    assert {
        (candidate.profile.period, candidate.profile.bar_seconds)
        for candidate in seeds[:3]
    } == set(windows)


def test_report_inventory_records_physical_components_and_combined_identity(
    tmp_path: Path,
) -> None:
    paths = _daily_recordings(tmp_path / "recordings", days=2)
    recording = combine_ibrec_recordings(
        [
            load_ibrec(MarketReplayConfig(path, tmp_path / f"reports-{index}"))
            for index, path in enumerate(paths)
        ]
    )
    assert recording.input_recording_count == 2
    assert all(component["recording_content_sha256"] for component in recording.input_components)
    assert all(component["recording_index"] in {1, 2} for component in recording.input_components)
    assert len(recording.sha256) == 64
