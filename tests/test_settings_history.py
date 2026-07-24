from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from optimizer.analysis import run_analysis
from optimizer.models import AnalysisConfig
from optimizer.settings_history import (
    ATR_SETTING_DEFAULTS,
    build_settings_audit,
    cycle_profile_lookup,
)
from tests.conftest import create_source_fixture


def _cycle(number: int, *, period: int, buy: float, created: str) -> dict[str, object]:
    row = dict(ATR_SETTING_DEFAULTS)
    row.update(
        {
            "id": f"cycle-{number}",
            "cycle_number": number,
            "created_at": created,
            "updated_at": created,
            "atr_period": period,
            "atr_buy_rebound_multiplier": buy,
        }
    )
    return row


def test_settings_audit_preserves_exact_profiles_and_contiguous_regimes() -> None:
    cycles = [
        _cycle(1, period=14, buy=0.75, created="2026-01-01T10:00:00+00:00"),
        _cycle(2, period=21, buy=1.0, created="2026-01-02T10:00:00+00:00"),
        _cycle(3, period=14, buy=0.75, created="2026-01-03T10:00:00+00:00"),
    ]
    summary, profiles, regimes, history = build_settings_audit(
        cycles,
        settings={"strategy": {"ticker": "AAPL", **ATR_SETTING_DEFAULTS}},
        settings_updated_at={"strategy": "2026-01-04T10:00:00+00:00"},
        ticker="AAPL",
        ticker_count=1,
    )

    assert summary["distinct_atr_profiles"] == 2
    assert summary["contiguous_setting_regimes"] == 3
    assert summary["configuration_change_count"] == 2
    assert summary["settings_varied_between_cycles"] is True
    assert len(profiles) == 2
    assert [row["cycle_count"] for row in profiles] == [2, 1]
    assert [row["regime_number"] for row in regimes] == [1, 2, 3]
    assert regimes[0]["profile_id"] == regimes[2]["profile_id"]
    assert regimes[0]["profile_id"] != regimes[1]["profile_id"]
    assert cycle_profile_lookup(history)["cycle-2"] == regimes[1]["profile_id"]
    assert summary["current_app_settings_complete_for_replay"] is True


def test_historical_median_can_be_actual_values_without_being_one_real_profile() -> None:
    cycles = [
        _cycle(1, period=10, buy=0.5, created="2026-01-01T10:00:00+00:00"),
        _cycle(2, period=20, buy=1.5, created="2026-01-02T10:00:00+00:00"),
    ]
    summary, _, _, _ = build_settings_audit(
        cycles,
        settings={},
        settings_updated_at={},
        ticker="AAPL",
        ticker_count=1,
    )

    assert summary["historical_median_settings"]["atr_period"] == 15
    assert summary["historical_median_settings"]["atr_buy_rebound_multiplier"] == 1.0
    assert summary["historical_median_matches_an_observed_complete_profile"] is False
    assert summary["historical_median_label"] == "Historical median baseline (derived from actual stored cycle ATR settings)"


def test_current_settings_are_not_applied_to_the_wrong_ticker() -> None:
    summary, _, _, _ = build_settings_audit(
        [_cycle(1, period=14, buy=0.75, created="2026-01-01T10:00:00+00:00")],
        settings={"strategy": {"ticker": "MSFT", **ATR_SETTING_DEFAULTS}},
        settings_updated_at={"strategy": "2026-01-02T10:00:00+00:00"},
        ticker="AAPL",
        ticker_count=2,
    )
    assert summary["current_app_settings_available"] is False
    assert "MSFT, not AAPL" in summary["current_app_settings_applicability"]


def test_replay_observations_and_summaries_record_historical_profile_provenance(
    tmp_path: Path,
) -> None:
    source = create_source_fixture(tmp_path / "bot", cycles=4)
    with closing(sqlite3.connect(source / "bot_state.sqlite")) as connection:
        connection.execute(
            "UPDATE cycles SET atr_period=21, atr_buy_rebound_multiplier=1.25 WHERE cycle_number IN (3,4)"
        )
        connection.commit()

    result = run_analysis(AnalysisConfig(source, tmp_path / "reports"))
    ticker = result.tickers[0]
    profile_ids = {
        row.historical_atr_profile_id for row in ticker.replay_observations
    }
    assert len(profile_ids) == 2
    assert all(row.historical_atr_profile_count == 2 for row in ticker.candidate_summaries)
    assert all(
        sum(row.historical_atr_profile_observations.values()) == row.observations
        for row in ticker.candidate_summaries
    )
    baseline = ticker.suggested_settings[0]
    assert baseline["profile"].startswith(
        "Evaluation control: latest complete historical ATR snapshot"
    )
    assert baseline["settings_changed_between_cycles"] is True
    assert "exact profile" in baseline["evidence"]
    median_reference = next(
        profile
        for profile in ticker.suggested_settings
        if profile["profile"]
        == "Historical median baseline (derived from actual stored cycle ATR settings)"
    )
    assert "not necessarily one profile that ever ran" in median_reference["evidence"]


def test_counterfactual_candidates_hold_historical_median_clamps_constant(
    tmp_path: Path,
) -> None:
    source = create_source_fixture(tmp_path / "bot-clamps", cycles=2)
    with closing(sqlite3.connect(source / "bot_state.sqlite")) as connection:
        connection.execute(
            "UPDATE cycles SET atr_min_pct=5.0 WHERE cycle_number=1"
        )
        connection.execute(
            "UPDATE cycles SET atr_min_pct=10.0 WHERE cycle_number=2"
        )
        connection.commit()

    result = run_analysis(AnalysisConfig(source, tmp_path / "reports-clamps"))
    ticker = result.tickers[0]
    baseline_rows = [
        row
        for row in ticker.replay_observations
        if row.leg == "buy"
        and row.period == 14
        and row.bar_seconds == 60
        and row.multiplier == 0.75
    ]

    assert len(baseline_rows) == 2
    # Candidate generation uses one exact production-like control rather than a
    # field-wise median assembled from settings that may never have run together.
    # The latest complete cycle has the 10% minimum clamp.
    assert {row.effective_pct for row in baseline_rows} == {10.0}
    assert ticker.atr_settings_summary["counterfactual_replay_atr_min_pct"] == 10.0
    assert "Every candidate uses" in ticker.atr_settings_summary[
        "counterfactual_replay_clamp_policy"
    ]


def test_actual_control_values_are_not_relabelled_after_replay_normalization(
    tmp_path: Path,
) -> None:
    source = create_source_fixture(tmp_path / "bot-legacy-control", cycles=2)
    current = dict(ATR_SETTING_DEFAULTS)
    current.update(
        {
            "ticker": "AAPL",
            "atr_period": 999,
            "atr_bar_seconds": 1,
            "atr_buy_rebound_multiplier": 75.123,
            "atr_min_pct": 120.0,
            "atr_max_pct": -4.0,
        }
    )
    with closing(sqlite3.connect(source / "bot_state.sqlite")) as connection:
        connection.execute(
            "UPDATE app_settings SET value_json=? WHERE key='strategy'",
            (json.dumps(current, sort_keys=True),),
        )
        connection.commit()

    ticker = run_analysis(
        AnalysisConfig(source, tmp_path / "reports-legacy-control")
    ).tickers[0]
    summary = ticker.atr_settings_summary

    assert summary["evaluation_control_settings"]["atr_period"] == 999
    assert summary["evaluation_control_settings"]["atr_bar_seconds"] == 1
    replay = summary["counterfactual_replay_control_settings"]
    assert replay["atr_period"] == 200
    assert replay["atr_bar_seconds"] == 5
    assert replay["atr_buy_rebound_multiplier"] == 50.0
    assert replay["atr_min_pct"] == 99.98
    assert replay["atr_max_pct"] == 99.99
    assert set(summary["evaluation_control_normalization_adjustments"]) >= {
        "atr_period",
        "atr_bar_seconds",
        "atr_buy_rebound_multiplier",
        "atr_min_pct",
        "atr_max_pct",
    }
    control = next(
        row for row in ticker.suggested_settings if row["evaluation_control"]
    )
    actual = next(
        row
        for row in ticker.suggested_settings
        if row.get("actual_settings_control")
        and row.get("evaluation_status")
        == "descriptive actual-settings evidence: normalized before replay"
    )
    assert control["atr_period"] == 200
    assert control["actual_settings_control"] is False
    assert actual["atr_period"] == 999
