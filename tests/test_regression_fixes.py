from __future__ import annotations

from pathlib import Path

from optimizer.analysis import _cycle_match, _setting_profiles, _stored_atr_window
from optimizer.atr import clamped_percentage
from optimizer.models import CandidateSummary, CaptureMeta
from optimizer.replay import buy_candidates, sell_candidates
from optimizer.settings_history import ATR_SETTING_DEFAULTS


def test_conflicting_explicit_capture_cycle_id_is_never_matched_by_number() -> None:
    meta = CaptureMeta(
        path=Path("capture.zip"),
        ticker="AAPL",
        cycle_id="another-install-cycle",
        cycle_number=7,
    )
    cycle = {"id": "local-cycle", "ticker": "AAPL", "cycle_number": 7}
    assert _cycle_match(meta, cycle) is False


def test_captured_atr_fallback_requires_the_exact_stored_window() -> None:
    meta = CaptureMeta(
        path=Path("capture.zip"),
        event={"strategy": {"atr_period": 21, "atr_bar_seconds": 120}},
    )

    # The durable cycle snapshot wins when both exact fields are present.
    assert _stored_atr_window(
        {"atr_period": 14, "atr_bar_seconds": 60},
        meta,
    ) == (14, 60)

    # Older cycle rows may use an exact capture-event strategy snapshot.
    assert _stored_atr_window({}, meta) == (21, 120)

    # A partial or invalid snapshot is unknown; it must not be replaced by a
    # ticker median and then mislabelled as candidate-specific ATR evidence.
    assert _stored_atr_window(
        {},
        CaptureMeta(
            path=Path("partial.zip"),
            event={"strategy": {"atr_period": 21}},
        ),
    ) is None
    assert _stored_atr_window(
        {"atr_period": 0, "atr_bar_seconds": 60},
        CaptureMeta(path=Path("invalid.zip"), event={}),
    ) is None


def test_zero_trailing_multipliers_preserve_immediate_market_mode() -> None:
    buy = buy_candidates(0.0, 14, 60)
    sell = sell_candidates(1.0, 0.0, 14, 60)
    assert any(row.baseline and row.multiplier == 0.0 for row in buy)
    assert any(row.baseline and row.sell_trail_multiplier == 0.0 for row in sell)
    assert clamped_percentage(0.8, 0.0, 0.1, 20.0, allow_zero=True) == 0.0
    assert clamped_percentage(0.8, 0.0, 0.1, 20.0) == 0.1


def _summary(key: str, score: float, multiplier: float) -> CandidateSummary:
    return CandidateSummary(
        leg="buy",
        candidate_key=key,
        multiplier=multiplier,
        minimum_profit_multiplier=None,
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


def test_zero_screening_score_is_ranked_above_negative_score() -> None:
    settings = dict(ATR_SETTING_DEFAULTS)
    settings_summary = {
        "historical_median_settings": settings,
        "cycles_with_any_stored_atr_settings": 20,
        "historical_median_default_fallback_fields": [],
        "settings_varied_between_cycles": False,
        "distinct_atr_profiles": 1,
        "historical_median_matches_an_observed_complete_profile": True,
        "current_app_settings": {},
        "current_app_settings_complete_for_replay": False,
    }
    profiles = _setting_profiles(
        cycles=[settings],
        summaries=[_summary("zero", 0.0, 0.9), _summary("negative", -1.0, 1.1)],
        coverage={"evidence_level": "strong"},
        settings_summary=settings_summary,
    )
    screened = next(row for row in profiles if row["profile"].startswith("Replay-screened"))
    assert screened["atr_buy_rebound_multiplier"] == 0.9
