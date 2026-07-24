from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from optimizer.analysis import (
    _capture_candidates,
    _normal_sell_fields,
    _order_time,
    _setting_profiles,
    run_analysis,
)
from optimizer.atr import clamped_percentage
from optimizer.captures import _price_point
from optimizer.database import DatabaseDataset
from optimizer.models import AnalysisConfig, CandidateSummary, CaptureMeta, ReplayObservation
from optimizer.replay import summarize_observations
from optimizer.reports import write_reports
from optimizer.settings_history import ATR_SETTING_DEFAULTS
from tests.conftest import create_source_fixture


def _dataset(*, orders: list[dict[str, object]] | None = None) -> DatabaseDataset:
    return DatabaseDataset(
        schema={},
        cycles=[],
        orders_by_cycle={"cycle-1": list(orders or [])},
    )


def _capture(name: str, *, order_ref: str, seconds: int) -> CaptureMeta:
    return CaptureMeta(
        path=Path(name),
        ticker="AAPL",
        cycle_id="cycle-1",
        cycle_number=1,
        event_type="BUY_FILL",
        event_time_utc=f"2026-01-01T00:00:{seconds:02d}+00:00",
        order_ref=order_ref,
    )


def test_analysis_config_rejects_boolean_fractional_and_nonfinite_limits(tmp_path: Path) -> None:
    valid = AnalysisConfig(
        source_dir=tmp_path,
        output_root=tmp_path / "reports",
        max_archive_uncompressed_bytes="1000000",
        max_rows_per_capture=100.0,
    ).normalized()
    assert valid.max_archive_uncompressed_bytes == 1_000_000
    assert valid.max_rows_per_capture == 100

    for invalid in (True, 100.5, float("inf"), "not-an-integer"):
        with pytest.raises(ValueError):
            AnalysisConfig(
                source_dir=tmp_path,
                output_root=tmp_path / "reports",
                max_rows_per_capture=invalid,
            ).normalized()

    with pytest.raises(ValueError):
        AnalysisConfig(
            source_dir=tmp_path,
            output_root=tmp_path / "reports",
            hash_capture_files=1,  # type: ignore[arg-type]
        ).normalized()


def test_strategy_price_usable_is_authoritative_over_lower_level_fresh_flags() -> None:
    point = _price_point(
        {
            "captured_at_utc": "2026-01-01T00:00:00+00:00",
            "price": 100.0,
            "fields": {"last": 100.0},
            "market_data_update_consumed": True,
            "api_data_received_in_latest_read": True,
            "strategy_price_usable": False,
        }
    )
    assert point is not None
    assert point.fresh_update is False


def test_capture_selection_prefers_exact_order_reference_over_closer_legacy_capture() -> None:
    cycle = {
        "id": "cycle-1",
        "ticker": "AAPL",
        "cycle_number": 1,
        "buy_order_ref": "BUY-EXPECTED",
    }
    exact = _capture("exact.zip", order_ref="BUY-EXPECTED", seconds=20)
    blank_closer = _capture("blank.zip", order_ref="", seconds=10)
    conflicting = _capture("wrong.zip", order_ref="BUY-OTHER", seconds=10)
    dataset = _dataset()

    selected = _capture_candidates(
        [blank_closer, conflicting, exact],
        dataset,
        cycle,
        "buy",
        "2026-01-01T00:00:10+00:00",
    )

    assert selected == [exact, blank_closer]


def test_capture_selection_uses_time_when_no_order_reference_is_known() -> None:
    cycle = {"id": "cycle-1", "ticker": "AAPL", "cycle_number": 1}
    farther = _capture("farther.zip", order_ref="ANY-1", seconds=20)
    closer = _capture("closer.zip", order_ref="ANY-2", seconds=11)

    selected = _capture_candidates(
        [farther, closer],
        _dataset(),
        cycle,
        "buy",
        "2026-01-01T00:00:10+00:00",
    )

    assert selected == [closer, farther]


def test_order_time_does_not_use_an_unrelated_retry_or_generic_decision_when_exact_ref_is_missing() -> None:
    cycle = {
        "id": "cycle-1",
        "buy_order_ref": "BUY-EXPECTED",
    }
    dataset = _dataset(
        orders=[
            {
                "action": "BUY",
                "order_ref": "BUY-OTHER",
                "created_at": "2026-01-01T00:01:00+00:00",
            }
        ]
    )
    dataset.decisions_by_cycle = {
        "cycle-1": [
            {
                "event_type": "BUY_ORDER_SUBMITTED",
                "created_at": "2026-01-01T00:02:00+00:00",
            }
        ]
    }

    assert _order_time(dataset, cycle, "buy") == ""


def test_protective_fill_mirrored_into_normal_fields_requires_real_normal_execution() -> None:
    cycle = {
        "id": "cycle-1",
        "sell_order_ref": "SELL-CANCELLED",
        "protective_sell_order_ref": "PROTECTIVE-FILLED",
        "sell_filled_at": "2026-01-01T00:05:00+00:00",
        "avg_sell_price": 95.0,
        "sell_filled_qty": 10,
        "protective_sell_filled_at": "2026-01-01T00:05:00+00:00",
        "protective_avg_sell_price": 95.0,
        "protective_sell_filled_qty": 10,
    }
    dataset = _dataset()

    assert _normal_sell_fields(dataset, cycle) == ("", None)

    dataset.executions_by_cycle = {
        "cycle-1": [
            {
                "order_ref": "SELL-CANCELLED",
                "side": "SLD",
                "shares": 4,
                "price": 105.0,
                "executed_at": "2026-01-01T00:04:00+00:00",
            }
        ]
    }
    assert _normal_sell_fields(dataset, cycle) == (
        "2026-01-01T00:04:00+00:00",
        105.0,
    )


def _observation(
    *,
    cycle: int,
    triggered: bool,
    delay: float | None,
    atr_source: str = "capture_reconstructed",
    left_censored: bool = False,
) -> ReplayObservation:
    return ReplayObservation(
        cycle_id=f"cycle-{cycle}",
        cycle_number=cycle,
        ticker="AAPL",
        leg="buy",
        candidate_key="candidate",
        multiplier=0.75,
        minimum_profit_multiplier=None,
        period=14,
        bar_seconds=60,
        effective_pct=0.5 if atr_source != "unavailable" else None,
        triggered=triggered,
        price_improvement_bps=10.0 if triggered else None,
        delay_seconds=delay,
        post_trigger_mfe_bps=20.0 if triggered else None,
        post_trigger_mae_bps=0.0 if triggered else None,
        atr_source=atr_source,
        left_censored=left_censored,
    )


def test_screening_excludes_unavailable_atr_from_miss_rate_and_uses_absolute_delay() -> None:
    summary = summarize_observations(
        [
            _observation(cycle=1, triggered=True, delay=-600.0),
            _observation(cycle=2, triggered=True, delay=600.0),
            _observation(
                cycle=3,
                triggered=False,
                delay=None,
                atr_source="unavailable",
            ),
        ]
    )[0]

    assert summary.observations == 3
    assert summary.scoreable_observations == 2
    assert summary.triggered == 2
    assert summary.scoreable_triggered == 2
    assert summary.trigger_rate_pct == 100.0
    assert summary.median_delay_seconds == 0.0
    assert summary.median_absolute_delay_seconds == 600.0
    assert summary.screening_score == 0.0  # 10 bps improvement - 10 minutes


def test_left_censored_windows_are_disclosed_and_excluded_from_ranking() -> None:
    summary = summarize_observations(
        [
            _observation(
                cycle=1,
                triggered=True,
                delay=0.0,
                left_censored=True,
            ),
            _observation(cycle=2, triggered=True, delay=0.0),
        ]
    )[0]

    assert summary.left_censored_observations == 1
    assert summary.left_censored_rate_pct == 50.0
    assert summary.scoreable_observations == 1
    assert summary.screening_score == 10.0


def test_adaptive_percentage_uses_the_trading_bots_two_decimal_precision() -> None:
    assert clamped_percentage(0.3333, 1.0, 0.1, 20.0) == 0.33
    assert clamped_percentage(0.335, 1.0, 0.1, 20.0) == 0.34


def _summary(key: str, *, leg: str, score: float, multiplier: float) -> CandidateSummary:
    return CandidateSummary(
        leg=leg,
        candidate_key=key,
        multiplier=multiplier,
        minimum_profit_multiplier=1.0 if leg == "sell" else None,
        period=14,
        bar_seconds=60,
        baseline_window=True,
        observations=20,
        scoreable_observations=20,
        triggered=20,
        scoreable_triggered=20,
        candidate_atr_observations=20,
        candidate_atr_coverage_pct=100.0,
        trigger_rate_pct=100.0,
        median_improvement_bps=score,
        median_delay_seconds=0.0,
        median_absolute_delay_seconds=0.0,
        median_mfe_bps=0.0,
        median_mae_bps=0.0,
        left_censored_observations=0,
        left_censored_rate_pct=0.0,
        screening_score=score,
        evidence="strong local-window sample",
        priority="evaluate first",
        rationale="test",
    )


def test_replay_candidate_retains_current_controls_that_replay_did_not_evaluate() -> None:
    historical = dict(ATR_SETTING_DEFAULTS)
    historical.update(
        {
            "atr_block_new_buy_until_ready": False,
            "atr_adapt_protective_sell_enabled": False,
            "atr_protective_sell_multiplier": 3.0,
        }
    )
    current = dict(historical)
    current.update(
        {
            "atr_block_new_buy_until_ready": True,
            "atr_adapt_protective_sell_enabled": True,
            "atr_protective_sell_multiplier": 4.25,
        }
    )
    settings_summary = {
        "historical_median_settings": historical,
        "cycles_with_any_stored_atr_settings": 20,
        "historical_median_default_fallback_fields": [],
        "settings_varied_between_cycles": False,
        "distinct_atr_profiles": 1,
        "historical_median_matches_an_observed_complete_profile": True,
        "current_app_settings": current,
        "current_app_settings_complete_for_replay": True,
        "evaluation_control_settings": current,
        "evaluation_control_label": "Evaluation control: current saved app settings",
        "evaluation_control_source": "app_settings.strategy",
        "evaluation_control_missing_fields_before_fallback": [],
        "evaluation_control_exact_for_replay": True,
    }

    profiles = _setting_profiles(
        cycles=[historical],
        summaries=[
            _summary("buy", leg="buy", score=5.0, multiplier=0.9),
            _summary("sell", leg="sell", score=5.0, multiplier=1.1),
        ],
        coverage={"evidence_level": "strong"},
        settings_summary=settings_summary,
    )
    candidate = next(
        profile for profile in profiles if profile["profile"].startswith("Replay-screened")
    )

    assert candidate["atr_block_new_buy_until_ready"] is True
    assert candidate["atr_adapt_protective_sell_enabled"] is True
    assert candidate["atr_protective_sell_multiplier"] == 4.25
    assert "atr_protective_sell_multiplier" in candidate["retained_unevaluated_fields"]
    assert "atr_buy_rebound_multiplier" in candidate["replay_evaluated_fields"]


def test_candidate_selection_tie_break_never_depends_on_nan_ordering() -> None:
    rows = [
        _summary("b", leg="buy", score=5.0, multiplier=0.8),
        _summary("a", leg="buy", score=5.0, multiplier=0.7),
    ]
    rows[0].candidate_atr_coverage_pct = math.nan
    rows[1].candidate_atr_coverage_pct = math.nan
    settings = dict(ATR_SETTING_DEFAULTS)
    profiles = _setting_profiles(
        cycles=[settings],
        summaries=rows,
        coverage={"evidence_level": "strong"},
        settings_summary={
            "historical_median_settings": settings,
            "cycles_with_any_stored_atr_settings": 1,
            "historical_median_default_fallback_fields": [],
            "settings_varied_between_cycles": False,
            "distinct_atr_profiles": 1,
            "historical_median_matches_an_observed_complete_profile": True,
            "current_app_settings": {},
            "current_app_settings_complete_for_replay": False,
        },
    )
    candidate = next(
        profile for profile in profiles if profile["profile"].startswith("Replay-screened")
    )
    assert candidate["atr_buy_rebound_multiplier"] == 0.7


def test_malformed_actual_control_is_preserved_but_replay_uses_one_disclosed_normalization(
    tmp_path: Path,
) -> None:
    source = create_source_fixture(tmp_path / "bot", cycles=6)
    with closing(sqlite3.connect(source / "bot_state.sqlite")) as connection:
        connection.execute(
            """
            UPDATE cycles
            SET atr_period=1,
                atr_bar_seconds=1,
                atr_initial_drop_multiplier=-2,
                atr_buy_rebound_multiplier=-1,
                atr_minimum_profit_multiplier=0,
                atr_sell_trail_multiplier=-1,
                atr_min_pct=150,
                atr_max_pct=100
            WHERE cycle_number=6
            """
        )
        connection.commit()

    result = write_reports(
        run_analysis(AnalysisConfig(source, tmp_path / "reports"))
    )
    ticker = result.tickers[0]
    summary = ticker.atr_settings_summary
    source_values = summary["evaluation_control_source_values"]
    replay_values = summary["counterfactual_replay_control_settings"]
    adjustments = summary["evaluation_control_normalization_adjustments"]

    assert source_values["atr_period"] == 1
    assert source_values["atr_bar_seconds"] == 1
    assert source_values["atr_min_pct"] == 150.0
    assert source_values["atr_max_pct"] == 100.0
    assert replay_values["atr_period"] == 2
    assert replay_values["atr_bar_seconds"] == 5
    assert replay_values["atr_initial_drop_multiplier"] == 0.01
    assert replay_values["atr_buy_rebound_multiplier"] == 0.0
    assert replay_values["atr_minimum_profit_multiplier"] == 0.01
    assert replay_values["atr_sell_trail_multiplier"] == 0.0
    assert replay_values["atr_min_pct"] == 99.98
    assert replay_values["atr_max_pct"] == 99.99
    assert set(adjustments) >= {
        "atr_period",
        "atr_bar_seconds",
        "atr_initial_drop_multiplier",
        "atr_buy_rebound_multiplier",
        "atr_minimum_profit_multiplier",
        "atr_sell_trail_multiplier",
        "atr_min_pct",
        "atr_max_pct",
    }
    assert summary["evaluation_control_values_preserved_exactly_for_replay"] is False

    control = next(
        row for row in ticker.suggested_settings if row.get("evaluation_control")
    )
    for key, expected in replay_values.items():
        assert control[key] == expected
    assert ticker.primary_evaluation_setting["atr_period"] == 2
    assert ticker.primary_evaluation_setting["atr_bar_seconds"] == 5

    report = (
        result.output_dir / "AAPL" / "AAPL_coverage_and_replay.html"
    ).read_text(encoding="utf-8")
    assert "Saved/source value" in report
    assert "Value used for replay and suggestions" in report
    assert "99.98" in report
    assert "99.99" in report
