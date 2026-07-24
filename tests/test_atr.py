from __future__ import annotations

from optimizer.atr import (
    ATR_BAR_SECONDS_MAX,
    ATR_PERIOD_MAX,
    atr_estimate,
    build_bars,
    captured_atr_near,
    choose_atr,
    clamped_percentage,
    normalize_atr_multiplier,
    normalize_atr_window,
    period_bar_candidates,
)
from optimizer.models import PricePoint


def test_multiplier_normalization_matches_gui_precision_and_zero_semantics() -> None:
    assert normalize_atr_multiplier(0, allow_zero=True) == 0.0
    assert normalize_atr_multiplier(-5, allow_zero=True) == 0.0
    assert normalize_atr_multiplier(0, allow_zero=False) == 0.01
    assert normalize_atr_multiplier(0.755, allow_zero=True) == 0.76
    assert normalize_atr_multiplier(500, allow_zero=False) == 50.0
    assert normalize_atr_multiplier(float("nan"), allow_zero=False) == 0.01
    assert normalize_atr_multiplier(True, allow_zero=False) == 0.01
    assert normalize_atr_multiplier(True, allow_zero=True) == 0.0


def points(count: int = 20) -> list[PricePoint]:
    return [
        PricePoint(
            timestamp=float(index * 60),
            captured_at_utc="",
            price=100.0 + index * 0.2 + (0.1 if index % 2 else 0.0),
            trigger_price=100.0 + index * 0.2,
            atr_pct=0.7,
        )
        for index in range(count)
    ]


def test_build_bars_and_atr_estimate() -> None:
    bars = build_bars(points(), bar_seconds=60)
    assert len(bars) == 20
    estimate = atr_estimate(points(), period=14, bar_seconds=60)
    assert estimate.ready
    assert estimate.bars_required == 15
    assert estimate.atr is not None and estimate.atr > 0
    assert estimate.atr_pct is not None and estimate.atr_pct > 0


def test_atr_falls_back_to_captured_value_when_window_is_short() -> None:
    value, source, available, required = choose_atr(points(3), timestamp=120.0, period=14, bar_seconds=60)
    assert value == 0.7
    assert source == "bot_captured_fallback"
    assert available < required


def test_alternate_window_does_not_reuse_bot_captured_atr() -> None:
    value, source, available, required = choose_atr(
        points(3),
        timestamp=120.0,
        period=21,
        bar_seconds=120,
        allow_captured_fallback=False,
    )
    assert value is None
    assert source == "candidate_atr_window_unavailable"
    assert available < required


def test_captured_atr_fallback_never_reads_a_future_row() -> None:
    rows = [
        PricePoint(90.0, "", 100.0, 100.0, atr_pct=0.4),
        PricePoint(110.0, "", 100.0, 100.0, atr_pct=1.8),
    ]
    assert captured_atr_near(rows, 100.0) == 0.4
    assert captured_atr_near(rows[1:], 100.0) is None


def test_clamp_and_candidate_windows() -> None:
    assert clamped_percentage(0.5, 2.0, 0.1, 20.0) == 1.0
    assert clamped_percentage(0.01, 1.0, 0.1, 20.0) == 0.1
    assert clamped_percentage(None, 1.0, 0.1, 20.0) is None
    assert clamped_percentage(0.5, -1.0, 0.1, 20.0) is None
    # BouncyBot normalizes an upper clamp below the lower clamp up to the lower.
    assert clamped_percentage(0.5, 1.0, 2.0, 1.0) == 2.0
    assert clamped_percentage(float("nan"), 1.0, 0.1, 20.0) is None
    assert clamped_percentage(True, 1.0, 0.1, 20.0) is None
    assert clamped_percentage(1.0, True, 0.1, 20.0) is None
    candidates = period_bar_candidates(14, 60)
    assert candidates[0] == ("baseline_window", 14, 60)
    assert len({(period, bar) for _, period, bar in candidates}) == len(candidates)


def test_candidate_window_names_remain_directionally_correct_at_boundaries() -> None:
    small = period_bar_candidates(2, 5)
    assert small[0] == ("baseline_window", 2, 5)
    assert all(period <= 2 and bar <= 5 for name, period, bar in small if name == "faster_window")
    assert all(period > 2 and bar > 5 for name, period, bar in small if name == "smoother_window")

    large = period_bar_candidates(50, 600)
    smoother = next(row for row in large if row[0] == "smoother_window")
    assert smoother[1] > 50
    assert smoother[2] > 600


def test_candidate_windows_are_always_enterable_in_the_trading_app() -> None:
    assert normalize_atr_window(999, 99_999) == (
        ATR_PERIOD_MAX,
        ATR_BAR_SECONDS_MAX,
    )
    assert normalize_atr_window(3.5, float("nan")) == (14, 60)
    assert normalize_atr_window(True, False) == (14, 60)
    candidates = period_bar_candidates(999, 99_999)
    assert candidates[0] == (
        "baseline_window",
        ATR_PERIOD_MAX,
        ATR_BAR_SECONDS_MAX,
    )
    assert all(2 <= period <= ATR_PERIOD_MAX for _, period, _ in candidates)
    assert all(5 <= bar <= ATR_BAR_SECONDS_MAX for _, _, bar in candidates)


def test_malformed_excessive_atr_clamps_are_normalized_to_valid_gui_values() -> None:
    value = clamped_percentage(200.0, 1.0, 150.0, 200.0)
    assert value == 99.99
