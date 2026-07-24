from __future__ import annotations

from datetime import datetime, timedelta, timezone

from optimizer.models import PricePoint, ReplayObservation
from optimizer.replay import (
    BuyCandidate,
    SellCandidate,
    _required_sell_activation_price,
    buy_candidates,
    replay_buy,
    replay_sell,
    sell_candidates,
    summarize_observations,
)


def point(second: int, price: float, atr_pct: float = 1.0) -> PricePoint:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return PricePoint(
        timestamp=(base + timedelta(seconds=second)).timestamp(),
        captured_at_utc=(base + timedelta(seconds=second)).isoformat(),
        price=price,
        trigger_price=price,
        atr_pct=atr_pct,
    )


def test_buy_trail_triggers_on_exact_boundary() -> None:
    points = [point(0, 100.0), point(60, 99.0), point(120, 99.99), point(180, 100.2)]
    candidate = BuyCandidate("buy", multiplier=1.0, period=14, bar_seconds=60)
    observation = replay_buy(
        ticker="AAPL",
        cycle_id="1",
        cycle_number=1,
        points=points,
        candidate=candidate,
        order_time_utc=points[0].captured_at_utc,
        actual_fill_time_utc=points[3].captured_at_utc,
        actual_fill_price=100.2,
        atr_min_pct=0.1,
        atr_max_pct=20.0,
    )
    assert observation.triggered
    assert observation.trigger_price == 99.99
    assert observation.price_improvement_bps is not None and observation.price_improvement_bps > 0
    assert observation.delay_seconds == -60.0


def test_sell_trail_activation_and_exact_stop_boundary() -> None:
    required = 101.0 / 0.99
    points = [point(0, 101.0), point(60, required), point(120, 103.0), point(180, 101.97), point(240, 101.0)]
    candidate = SellCandidate(
        "sell",
        minimum_profit_multiplier=1.0,
        sell_trail_multiplier=1.0,
        period=14,
        bar_seconds=60,
    )
    observation = replay_sell(
        ticker="AAPL",
        cycle_id="1",
        cycle_number=1,
        points=points,
        candidate=candidate,
        reference_time_utc=points[0].captured_at_utc,
        actual_fill_time_utc=points[4].captured_at_utc,
        actual_fill_price=101.0,
        average_buy_price=100.0,
        atr_min_pct=0.1,
        atr_max_pct=20.0,
    )
    assert observation.triggered
    assert observation.trigger_price == 101.97
    assert observation.price_improvement_bps is not None and observation.price_improvement_bps > 0


def test_replay_no_trigger_and_missing_atr_are_explicit() -> None:
    candidate = BuyCandidate("buy", multiplier=1.0, period=14, bar_seconds=60)
    no_atr = [point(0, 100.0, atr_pct=0.0)]
    no_atr[0].atr_pct = None
    observation = replay_buy(
        ticker="AAPL",
        cycle_id="1",
        cycle_number=1,
        points=no_atr,
        candidate=candidate,
        order_time_utc=no_atr[0].captured_at_utc,
        actual_fill_time_utc="",
        actual_fill_price=None,
        atr_min_pct=0.1,
        atr_max_pct=20.0,
    )
    assert not observation.triggered
    assert "No usable ATR" in observation.note


def test_buy_order_after_capture_window_is_not_replayed_from_last_point() -> None:
    points = [point(0, 100.0), point(60, 99.0), point(120, 100.0)]
    observation = replay_buy(
        ticker="AAPL",
        cycle_id="1",
        cycle_number=1,
        points=points,
        candidate=BuyCandidate("buy", 1.0, 14, 60),
        order_time_utc=point(180, 100.0).captured_at_utc,
        actual_fill_time_utc="",
        actual_fill_price=None,
        atr_min_pct=0.1,
        atr_max_pct=20.0,
    )
    assert not observation.triggered
    assert "after the capture window" in observation.note


def test_sell_manual_minimum_profit_is_not_scaled_by_candidate() -> None:
    points = [
        point(0, 103.0),
        point(60, 103.6),
        point(120, 104.0),
        point(180, 102.96),
    ]
    observation = replay_sell(
        ticker="AAPL",
        cycle_id="1",
        cycle_number=1,
        points=points,
        candidate=SellCandidate("manual", 10.0, 1.0, 14, 60),
        reference_time_utc=points[0].captured_at_utc,
        strategy_start_time_utc=points[0].captured_at_utc,
        actual_fill_time_utc=points[-1].captured_at_utc,
        actual_fill_price=102.96,
        average_buy_price=100.0,
        atr_min_pct=0.1,
        atr_max_pct=20.0,
        minimum_profit_adaptive=False,
        manual_minimum_profit_pct=2.5,
    )
    assert observation.triggered
    assert observation.effective_minimum_profit_pct == 2.5
    assert "manual" in observation.note.lower()


def test_sell_activation_uses_atr_available_at_each_candidate_time() -> None:
    points = [
        point(0, 101.0, atr_pct=0.5),
        point(60, 101.1, atr_pct=0.5),
        point(120, 102.0, atr_pct=3.0),
        point(180, 101.49, atr_pct=3.0),
    ]
    observation = replay_sell(
        ticker="AAPL",
        cycle_id="1",
        cycle_number=1,
        points=points,
        candidate=SellCandidate("dynamic", 1.0, 1.0, 14, 60),
        reference_time_utc=points[-1].captured_at_utc,
        strategy_start_time_utc=points[0].captured_at_utc,
        actual_fill_time_utc=points[-1].captured_at_utc,
        actual_fill_price=101.49,
        average_buy_price=100.0,
        atr_min_pct=0.1,
        atr_max_pct=20.0,
    )
    assert observation.triggered
    assert observation.effective_pct == 0.5
    assert observation.effective_minimum_profit_pct == 0.5


def test_sell_with_valid_atr_but_unreached_activation_is_right_censored() -> None:
    points = [
        point(0, 100.0, atr_pct=1.0),
        point(60, 100.5, atr_pct=1.0),
        point(120, 101.0, atr_pct=1.0),
    ]
    miss = replay_sell(
        ticker="AAPL",
        cycle_id="miss",
        cycle_number=1,
        points=points,
        candidate=SellCandidate("same", 1.0, 1.0, 14, 60),
        reference_time_utc=points[0].captured_at_utc,
        strategy_start_time_utc=points[0].captured_at_utc,
        actual_fill_time_utc=points[-1].captured_at_utc,
        actual_fill_price=101.0,
        average_buy_price=100.0,
        atr_min_pct=0.1,
        atr_max_pct=20.0,
    )
    trigger = ReplayObservation(
        cycle_id="trigger",
        cycle_number=2,
        ticker="AAPL",
        leg="sell",
        candidate_key="same",
        multiplier=1.0,
        minimum_profit_multiplier=1.0,
        period=14,
        bar_seconds=60,
        effective_pct=1.0,
        effective_minimum_profit_pct=1.0,
        triggered=True,
        baseline_window=True,
        price_improvement_bps=10.0,
        delay_seconds=0.0,
        post_trigger_mfe_bps=0.0,
        post_trigger_mae_bps=0.0,
        atr_source="bot_captured_fallback",
    )

    assert miss.triggered is False
    assert miss.atr_source == "bot_captured_fallback"
    assert "activation price was not reached" in miss.note
    summary = summarize_observations([miss, trigger])[0]
    assert summary.scoreable_observations == 2
    assert summary.scoreable_triggered == 1
    assert summary.trigger_rate_pct == 50.0
    assert miss.right_censored is True
    assert miss.outcome == "right_censored"
    assert summary.right_censored_observations == 1
    assert summary.screening_score == 10.0


def test_alternate_atr_window_is_not_ranked_without_reconstruction() -> None:
    observations = [
        ReplayObservation(
            cycle_id=str(index),
            cycle_number=index,
            ticker="AAPL",
            leg="buy",
            candidate_key="alternate",
            multiplier=0.75,
            minimum_profit_multiplier=None,
            period=21,
            bar_seconds=120,
            effective_pct=None,
            triggered=False,
            baseline_window=False,
            atr_source="candidate_atr_window_unavailable",
        )
        for index in range(1, 7)
    ]
    summary = summarize_observations(observations)[0]
    assert summary.priority == "insufficient ATR coverage"
    assert summary.candidate_atr_observations == 0
    assert summary.screening_score is None


def test_explicit_confirmed_no_trigger_candidate_receives_formula_miss_penalty() -> None:
    observations = [
        ReplayObservation(
            cycle_id=str(index),
            cycle_number=index,
            ticker="AAPL",
            leg="buy",
            candidate_key="all-miss",
            multiplier=0.75,
            minimum_profit_multiplier=None,
            period=14,
            bar_seconds=60,
            effective_pct=0.5,
            triggered=False,
            outcome="confirmed_no_trigger",
            baseline_window=True,
            atr_source="capture_reconstructed",
        )
        for index in range(1, 7)
    ]

    summary = summarize_observations(observations)[0]

    assert summary.scoreable_observations == 6
    assert summary.scoreable_triggered == 0
    assert summary.trigger_rate_pct == 0.0
    assert summary.screening_score == -200.0
    assert summary.priority == "insufficient evidence"


def test_candidate_grids_and_summary_priorities() -> None:
    buys = buy_candidates(0.75, 14, 60)
    sells = sell_candidates(1.0, 1.0, 14, 60)
    assert any(item.baseline for item in buys)
    assert any(item.baseline for item in sells)
    assert len(buys) >= 10
    assert len(sells) >= 20

    points = [point(0, 100.0), point(60, 99.0), point(120, 100.0), point(180, 101.0)]
    observations = [
        replay_buy(
            ticker="AAPL",
            cycle_id=str(index),
            cycle_number=index,
            points=points,
            candidate=BuyCandidate("same", 1.0, 14, 60),
            order_time_utc=points[0].captured_at_utc,
            actual_fill_time_utc=points[3].captured_at_utc,
            actual_fill_price=101.0,
            atr_min_pct=0.1,
            atr_max_pct=20.0,
        )
        for index in range(1, 7)
    ]
    summaries = summarize_observations(observations)
    assert len(summaries) == 1
    assert summaries[0].observations == 6
    assert summaries[0].priority == "evaluate first"
    assert summaries[0].screening_score is not None


def test_zero_minimum_profit_multiplier_is_normalized_to_gui_minimum_in_sell_grid() -> None:
    candidates = sell_candidates(0.0, 1.0, 14, 60)
    assert min(candidate.minimum_profit_multiplier for candidate in candidates) == 0.01
    assert all(candidate.minimum_profit_multiplier > 0.0 for candidate in candidates)


def test_sell_activation_accepts_the_trading_bots_full_slippage_buffer_range() -> None:
    activation = _required_sell_activation_price(
        average_buy_price=100.0,
        minimum_profit_pct=1.0,
        sell_trail_pct=0.0,
        slippage_buffer_enabled=True,
        slippage_buffer_pct=99.99,
    )

    assert activation == 1_010_000.0


def test_candidate_summary_contains_per_historical_profile_metrics() -> None:
    observations = []
    for index, (profile, improvement, triggered) in enumerate(
        (("ATR-A", 12.0, True), ("ATR-A", 8.0, True), ("ATR-B", -5.0, True), ("ATR-B", None, False)),
        start=1,
    ):
        observations.append(
            ReplayObservation(
                cycle_id=str(index),
                cycle_number=index,
                ticker="AAPL",
                leg="buy",
                candidate_key="candidate",
                multiplier=0.75,
                minimum_profit_multiplier=None,
                period=14,
                bar_seconds=60,
                effective_pct=0.5,
                triggered=triggered,
                baseline_window=True,
                price_improvement_bps=improvement,
                delay_seconds=0.0 if triggered else None,
                post_trigger_mfe_bps=20.0 if triggered else None,
                post_trigger_mae_bps=2.0 if triggered else None,
                atr_source="capture_reconstructed",
                historical_atr_profile_id=profile,
            )
        )

    summary = summarize_observations(observations)[0]
    breakdown = {
        row["historical_atr_profile_id"]: row
        for row in summary.historical_atr_profile_breakdown
    }

    assert summary.historical_atr_profile_observations == {"ATR-A": 2, "ATR-B": 2}
    assert breakdown["ATR-A"]["triggered"] == 2
    assert breakdown["ATR-A"]["median_improvement_bps"] == 10.0
    assert breakdown["ATR-B"]["triggered"] == 1
    assert breakdown["ATR-B"]["trigger_rate_pct"] == 50.0


def test_fill_quote_context_never_uses_a_future_capture_row() -> None:
    points = [point(0, 100.0), point(60, 99.0), point(120, 101.0)]
    points[1].bid = 98.9
    points[1].ask = 99.1
    points[2].bid = 100.9
    points[2].ask = 101.1
    actual_fill_time = point(90, 100.0).captured_at_utc

    observation = replay_buy(
        ticker="AAPL",
        cycle_id="fill-quote",
        cycle_number=1,
        points=points,
        candidate=BuyCandidate("immediate", 0.0, 14, 60),
        order_time_utc=points[0].captured_at_utc,
        actual_fill_time_utc=actual_fill_time,
        actual_fill_price=100.0,
        atr_min_pct=0.1,
        atr_max_pct=20.0,
    )

    assert observation.triggered is True
    assert observation.fill_bid == 98.9
    assert observation.fill_ask == 99.1
    assert observation.fill_reference_price == 99.0
    assert observation.fill_quote_age_seconds == 30.0


def test_fill_context_uses_last_equal_timestamp_quote_update() -> None:
    points = [point(0, 100.0), point(60, 99.0), point(120, 99.99)]
    points[1].bid = 98.8
    points[1].ask = 99.2
    later_same_timestamp = point(60, 99.0)
    later_same_timestamp.bid = 98.95
    later_same_timestamp.ask = 99.05
    points.insert(2, later_same_timestamp)
    candidate = BuyCandidate("buy", multiplier=1.0, period=14, bar_seconds=60)

    observation = replay_buy(
        ticker="AAPL",
        cycle_id="1",
        cycle_number=1,
        points=points,
        candidate=candidate,
        order_time_utc=points[0].captured_at_utc,
        actual_fill_time_utc=later_same_timestamp.captured_at_utc,
        actual_fill_price=99.0,
        atr_min_pct=0.1,
        atr_max_pct=20.0,
    )

    assert observation.fill_bid == 98.95
    assert observation.fill_ask == 99.05
    assert observation.fill_quote_age_seconds == 0.0
