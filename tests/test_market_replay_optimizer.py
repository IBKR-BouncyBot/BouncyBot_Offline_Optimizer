from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from optimizer.ibrec import load_ibrec
from optimizer.market_replay import (
    _precompute_atr,
    _session_ticks,
    _simulate_session,
    _stable_recommendation,
    run_market_replay_analysis,
)
from optimizer.market_replay_models import (
    AtrProfile,
    MarketReplayCandidateSummary,
    MarketReplayConfig,
)
from optimizer.market_replay_reports import write_market_replay_report
from tests.market_replay_fixtures import make_ticks, write_v2, write_v3


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): file_hash(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def load_fixture(tmp_path: Path, *, version: int = 3, sessions: int = 1):
    ticks, periods = make_ticks(sessions=sessions)
    path = (
        write_v3(tmp_path / "recording.ibrec", ticks, periods)
        if version == 3
        else write_v2(tmp_path / "recording.ibrec", ticks)
    )
    recording = load_ibrec(MarketReplayConfig(path, tmp_path / "reports"))
    return path, recording


def test_full_cycle_replay_completes_buy_and_sell(tmp_path: Path) -> None:
    _, recording = load_fixture(tmp_path)
    ticks = _session_ticks(recording, recording.periods[0])
    profile = AtrProfile(5, 15, 1.50, 0.75, 1.00, 1.00)
    atr = _precompute_atr(ticks, profile.period, profile.bar_seconds)
    session, trades = _simulate_session(
        ticks,
        recording.periods[0],
        profile,
        atr,
        recording.min_tick,
        keep_trades=True,
    )
    assert session.completed_trades >= 1
    assert session.open_position is False
    assert trades[0].sell_price is not None
    assert trades[0].buy_trigger_pct and trades[0].buy_trigger_pct > 0
    assert trades[0].sell_trigger_pct and trades[0].sell_trigger_pct > 0


def test_open_position_is_right_censored_and_profit_cannot_improve_score(tmp_path: Path) -> None:
    _, recording = load_fixture(tmp_path)
    ticks = _session_ticks(recording, recording.periods[0])
    profile = AtrProfile(5, 15, 0.75, 0.0, 0.50, 0.0)
    session, trades = _simulate_session(
        ticks,
        recording.periods[0],
        profile,
        _precompute_atr(ticks, 5, 15),
        recording.min_tick,
        keep_trades=True,
    )
    assert session.open_position is True
    assert session.right_censored is True
    assert "right-censored" in session.issues[0]
    assert session.conservative_return_bps == min(
        session.realized_return_bps,
        session.marked_return_bps,
    )
    assert trades[-1].open_at_end is True


def test_recording_ending_before_entry_cutoff_is_right_censored(tmp_path: Path) -> None:
    rows, periods = make_ticks(points_per_session=20)
    path = write_v3(tmp_path / "short.ibrec", rows, periods)
    recording = load_ibrec(MarketReplayConfig(path, tmp_path / "reports"))
    ticks = _session_ticks(recording, recording.periods[0])
    profile = AtrProfile(5, 15, 1.50, 0.75, 1.00, 1.00)
    session, _ = _simulate_session(
        ticks,
        recording.periods[0],
        profile,
        _precompute_atr(ticks, profile.period, profile.bar_seconds),
        recording.min_tick,
        keep_trades=True,
    )
    assert session.no_trade is True
    assert session.open_position is False
    assert session.open_entry_setup is False
    assert session.right_censored is True
    assert "entry cutoff" in " ".join(session.issues)


def test_quiet_but_fully_observed_session_is_not_right_censored(tmp_path: Path) -> None:
    rows, periods = make_ticks(points_per_session=20)
    periods[0]["observed_end_utc"] = periods[0]["schedule_close_utc"]
    path = write_v3(tmp_path / "quiet-complete.ibrec", rows, periods)
    recording = load_ibrec(MarketReplayConfig(path, tmp_path / "reports"))
    ticks = _session_ticks(recording, recording.periods[0])
    profile = AtrProfile(5, 15, 1.50, 0.75, 1.00, 1.00)
    session, _ = _simulate_session(
        ticks,
        recording.periods[0],
        profile,
        _precompute_atr(ticks, profile.period, profile.bar_seconds),
        recording.min_tick,
        keep_trades=True,
    )
    assert session.no_trade is True
    assert session.right_censored is False
    assert "entry cutoff" not in " ".join(session.issues)


def test_buy_trail_is_cancelled_when_observation_continues_past_cutoff(
    tmp_path: Path,
) -> None:
    rows, periods = make_ticks()
    for row in rows:
        row["changed_fields"] = "bid,ask"
    periods[0]["observed_end_utc"] = periods[0]["schedule_close_utc"]
    path = write_v3(tmp_path / "buy-cancel.ibrec", rows, periods)
    recording = load_ibrec(MarketReplayConfig(path, tmp_path / "reports"))
    ticks = _session_ticks(recording, recording.periods[0])
    profile = AtrProfile(5, 15, 1.50, 0.75, 1.00, 1.00)
    session, _ = _simulate_session(
        ticks,
        recording.periods[0],
        profile,
        _precompute_atr(ticks, profile.period, profile.bar_seconds),
        recording.min_tick,
        keep_trades=True,
    )
    assert session.open_entry_setup is False
    assert session.right_censored is False
    assert "treated as cancelled" in " ".join(session.issues)


def test_positive_native_trail_requires_a_new_last_event(tmp_path: Path) -> None:
    rows, periods = make_ticks()
    for row in rows:
        row["changed_fields"] = "bid,ask"
    path = write_v3(tmp_path / "quotes.ibrec", rows, periods)
    recording = load_ibrec(MarketReplayConfig(path, tmp_path / "reports"))
    ticks = _session_ticks(recording, recording.periods[0])
    positive = AtrProfile(5, 15, 1.50, 0.75, 1.00, 1.00)
    immediate = AtrProfile(5, 15, 0.75, 0.0, 0.50, 0.0)
    positive_result, _ = _simulate_session(
        ticks,
        recording.periods[0],
        positive,
        _precompute_atr(ticks, 5, 15),
        recording.min_tick,
        keep_trades=True,
    )
    immediate_result, _ = _simulate_session(
        ticks,
        recording.periods[0],
        immediate,
        _precompute_atr(ticks, 5, 15),
        recording.min_tick,
        keep_trades=True,
    )
    assert positive_result.trades == 0
    assert immediate_result.trades > 0


def test_live_rows_are_preferred_over_delayed_rows_within_a_session(tmp_path: Path) -> None:
    rows, periods = make_ticks()
    rows[0]["market_data_type"] = 3
    rows[1]["market_data_type"] = 3
    path = write_v3(tmp_path / "mixed.ibrec", rows, periods)
    recording = load_ibrec(MarketReplayConfig(path, tmp_path / "reports"))
    selected = _session_ticks(recording, recording.periods[0])
    assert selected
    assert all(tick.market_data_type == 1 for tick in selected)
    assert len(selected) == len(rows) - 2


def test_synthetic_recording_cannot_produce_stable_recommendation(tmp_path: Path) -> None:
    rows, periods = make_ticks(sessions=5)
    path = write_v3(
        tmp_path / "synthetic.ibrec",
        rows,
        periods,
        source_overrides={"type": "synthetic_sample"},
        notes="Deterministic synthetic fixture.",
    )
    result = run_market_replay_analysis(MarketReplayConfig(path, tmp_path / "reports"))
    assert result.recording.is_synthetic is True
    assert result.recommendation.evidence_stable is False
    assert "synthetic" in " ".join(result.recommendation.instability_reasons).lower()


def test_delayed_or_mixed_feed_cannot_produce_stable_recommendation(tmp_path: Path) -> None:
    delayed_rows, delayed_periods = make_ticks(sessions=5, market_data_type=3)
    delayed_path = write_v3(tmp_path / "delayed.ibrec", delayed_rows, delayed_periods)
    delayed = run_market_replay_analysis(
        MarketReplayConfig(delayed_path, tmp_path / "delayed-reports")
    )
    assert delayed.recommendation.evidence_stable is False
    assert "delayed market data" in " ".join(delayed.recommendation.instability_reasons)

    mixed_rows, mixed_periods = make_ticks(sessions=5)
    points_per_session = len(mixed_rows) // 5
    for session_index in range(5):
        mixed_rows[session_index * points_per_session]["market_data_type"] = 3
    mixed_path = write_v3(tmp_path / "mixed-evidence.ibrec", mixed_rows, mixed_periods)
    mixed = run_market_replay_analysis(
        MarketReplayConfig(mixed_path, tmp_path / "mixed-reports")
    )
    assert mixed.recommendation.evidence_stable is False
    assert "both live and delayed" in " ".join(mixed.recommendation.instability_reasons)


def test_custom_clamps_are_preserved_when_no_candidate_trades(tmp_path: Path) -> None:
    rows, periods = make_ticks(points_per_session=20)
    path = write_v3(tmp_path / "short.ibrec", rows, periods)
    result = run_market_replay_analysis(
        MarketReplayConfig(path, tmp_path / "reports", min_atr_pct=0.2, max_atr_pct=10.0)
    )
    assert result.recommendation.profile.min_atr_pct == 0.2
    assert result.recommendation.profile.max_atr_pct == 10.0
    assert result.recommendation.evidence_stable is False


def candidate(profile: AtrProfile, score: float, trades: int = 5) -> MarketReplayCandidateSummary:
    return MarketReplayCandidateSummary(
        profile=profile,
        score=score,
        sessions=6,
        completed_sessions=6,
        sessions_with_trades=6,
        completed_trades=trades,
        open_position_sessions=0,
        no_trade_sessions=0,
        median_return_bps=score,
        mean_return_bps=score,
        worst_return_bps=score,
        maximum_drawdown_bps=0.0,
        open_position_rate_pct=0.0,
        no_trade_rate_pct=0.0,
    )


def test_stable_region_center_beats_an_isolated_peak() -> None:
    control = AtrProfile(14, 60, 1.5, 0.75, 1.0, 1.0)
    isolated = candidate(AtrProfile(14, 60, 2.5, 2.5, 2.5, 2.5), 100.0)
    plateau = [
        candidate(AtrProfile(14, 60, 1.25 + offset, 0.75, 1.0, 1.0), 95.0 - abs(offset) * 2)
        for offset in (0.0, 0.25, 0.50)
    ]
    selected, reason = _stable_recommendation(
        [candidate(control, 80.0), isolated, *plateau],
        sessions=6,
        control_profile=control,
    )
    assert selected.profile in {item.profile for item in plateau}
    assert selected.stable_region_size == 3
    assert selected.stable_region_center is True
    assert "connected near-best region" in reason


def test_end_to_end_v2_and_v3_each_generate_one_complete_profile(tmp_path: Path) -> None:
    for version in (2, 3):
        version_root = tmp_path / f"v{version}"
        version_root.mkdir()
        path, _ = load_fixture(version_root, version=version)
        result = run_market_replay_analysis(MarketReplayConfig(path, version_root / "reports"))
        assert result.recording.format_version == version
        assert result.recommendation.profile.period >= 2
        assert result.recommendation.profile.bar_seconds >= 5
        assert len([candidate for candidate in result.candidates if candidate.profile == result.recommendation.profile]) == 1
        assert result.search_contract["supported_ibrec_versions"] == [2, 3]


def test_reports_are_deterministic_across_paths_and_do_not_modify_input(tmp_path: Path) -> None:
    rows, periods = make_ticks()
    source_a = write_v3(tmp_path / "one" / "recording.ibrec", rows, periods)
    source_a.parent.mkdir(exist_ok=True)
    source_b = tmp_path / "two" / "renamed.ibrec"
    source_b.parent.mkdir()
    source_b.write_bytes(source_a.read_bytes())
    before_a = file_hash(source_a)
    before_b = file_hash(source_b)

    result_a = write_market_replay_report(
        run_market_replay_analysis(MarketReplayConfig(source_a, tmp_path / "reports-a"))
    )
    result_b = write_market_replay_report(
        run_market_replay_analysis(MarketReplayConfig(source_b, tmp_path / "reports-b"))
    )
    assert result_a.analysis_id == result_b.analysis_id
    assert tree_hashes(result_a.output_dir) == tree_hashes(result_b.output_dir)
    assert file_hash(source_a) == before_a
    assert file_hash(source_b) == before_b


def test_report_contains_explanations_and_all_evidence_files(tmp_path: Path) -> None:
    path, _ = load_fixture(tmp_path)
    result = write_market_replay_report(
        run_market_replay_analysis(MarketReplayConfig(path, tmp_path / "reports"))
    )
    expected = {
        "index.html",
        "market_replay_analysis.json",
        "input_recordings.csv",
        "excluded_sessions.csv",
        "atr_window_search.csv",
        "candidate_results.csv",
        "robustness_evidence.csv",
        "recommendation_leave_one_day_out.csv",
        "recommended_atr_settings.csv",
        "recommended_session_results.csv",
        "recommended_simulated_trades.csv",
        "control_session_results.csv",
        "data_quality_issues.csv",
        "README_REPORT.txt",
        "analysis_manifest.json",
        "SHA256SUMS.txt",
    }
    assert {path.name for path in result.files_written} == expected
    text = (result.output_dir / "index.html").read_text(encoding="utf-8")
    assert "One complete ATR profile to evaluate" in text
    assert "Format version 2" in text or "format version" in text.lower()
    assert "right-censored" in text
    assert "not a mathematical optimum" in text
    assert "changed_fields" in text
    assert "simple mean" in text
    assert "full-cycle counterfactual replay" in text.lower()
    assert "three-stage two-dimensional" in text.lower()
    assert "trading-day bootstrap" in text.lower()
    assert "leave-one-day-out" in text.lower()


def test_report_refuses_to_replace_different_content_at_same_analysis_id(
    tmp_path: Path,
) -> None:
    path, _ = load_fixture(tmp_path)
    result = write_market_replay_report(
        run_market_replay_analysis(MarketReplayConfig(path, tmp_path / "reports"))
    )
    (result.output_dir / "index.html").write_text("tampered", encoding="utf-8")
    with pytest.raises(FileExistsError, match="different report"):
        write_market_replay_report(
            run_market_replay_analysis(MarketReplayConfig(path, tmp_path / "reports"))
        )
