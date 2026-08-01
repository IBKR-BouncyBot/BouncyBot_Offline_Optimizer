"""Regression tests for protective SELL policy optimization and replay."""

from __future__ import annotations

import csv
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import optimizer.market_replay as market_replay
from optimizer.market_replay import (
    _coarse_profiles_for_policy_windows,
    _enrich_protective_trade_diagnostics,
    _protective_policy_center,
    _protective_policy_components,
    _refinement_seeds,
    _select_protective_policy,
    _simulate_session_stateful,
    _summary,
    market_replay_search_contract,
    run_market_replay_analysis,
)
from optimizer.market_replay_models import (
    AtrProfile,
    IbrecPeriod,
    IbrecRecording,
    IbrecTick,
    MarketReplayCandidateSummary,
    MarketReplayConfig,
    MarketReplaySessionResult,
)
from optimizer.market_replay_reports import write_market_replay_report
from tests.market_replay_fixtures import make_ticks, write_v3


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _period(day: datetime, period_id: int = 1) -> IbrecPeriod:
    close = day + timedelta(minutes=35)
    return IbrecPeriod(
        period_id=period_id,
        session_date=day.date().isoformat(),
        schedule_open_utc=_iso(day),
        schedule_close_utc=_iso(close),
        open_timestamp=day.timestamp(),
        close_timestamp=close.timestamp(),
        observed_start_utc=_iso(day),
        observed_end_utc=_iso(close),
        observed_start_timestamp=day.timestamp(),
        observed_end_timestamp=close.timestamp(),
        status="closed",
        close_reason="complete",
        tick_count=36,
        source_recording_sha256=f"recording-{period_id}",
        primary_eligible=True,
        source_finalized=True,
    )


def _ticks(day: datetime, prices: list[float]) -> list[IbrecTick]:
    values: list[IbrecTick] = []
    for index, price in enumerate(prices):
        timestamp = day + timedelta(minutes=index)
        values.append(
            IbrecTick(
                sequence=index + 1,
                captured_at_utc=_iso(timestamp),
                timestamp=timestamp.timestamp(),
                elapsed_ns=index * 60 * 1_000_000_000,
                symbol="AAPL",
                con_id=265598,
                source_time_utc=_iso(timestamp),
                bid=round(price - 0.01, 4),
                bid_size=1_000.0,
                ask=round(price + 0.01, 4),
                ask_size=1_000.0,
                last=price,
                last_size=100.0,
                open=prices[0],
                high=max(prices[: index + 1]),
                low=min(prices[: index + 1]),
                close=price,
                volume=float(10_000 + index),
                mark_price=price,
                market_data_type=1,
                changed_fields=("bid", "ask", "last", "mark_price"),
                full_snapshot=index == 0,
                rth_period_id=1,
            )
        )
    return values


def _recording(ticks: list[IbrecTick], periods: list[IbrecPeriod]) -> IbrecRecording:
    return IbrecRecording(
        path=Path("recording.ibrec"),
        sha256="a" * 64,
        size_bytes=1,
        input_components=[],
        container_format="sqlite",
        format_version=3,
        manifest={"status": "complete"},
        contract={
            "symbol": "AAPL",
            "con_id": 265598,
            "currency": "USD",
            "sec_type": "STK",
            "min_tick": 0.01,
        },
        ticks=ticks,
        periods=periods,
        issues=[],
        feed_counts={"live": len(ticks)},
        raw_row_count=len(ticks),
        retained_row_count=len(ticks),
        data_start_utc=ticks[0].captured_at_utc if ticks else "",
        data_end_utc=ticks[-1].captured_at_utc if ticks else "",
    )


def _profile(
    *,
    mode: str = "disabled",
    value: float = 0.0,
    minimum_profit: float = 1.0,
    sell_trail: float = 1.0,
) -> AtrProfile:
    return AtrProfile(
        period=5,
        bar_seconds=15,
        initial_drop_multiplier=1.5,
        buy_rebound_multiplier=0.0,
        minimum_profit_multiplier=minimum_profit,
        sell_trail_multiplier=sell_trail,
        min_atr_pct=0.10,
        max_atr_pct=20.0,
        protective_sell_mode=mode,
        protective_sell_value=value,
    )


def _protective_path() -> list[float]:
    return [100.0] * 6 + [99.0, 100.0, 98.9] + [98.8] * 27


def _simulate(
    profile: AtrProfile,
    ticks: list[IbrecTick],
    period: IbrecPeriod,
    *,
    state: market_replay._ReplayCarryState | None = None,
    carry_to_next: bool = False,
) -> tuple[MarketReplaySessionResult, list[Any], market_replay._ReplayCarryState]:
    return _simulate_session_stateful(
        ticks,
        period,
        profile,
        [0.5] * len(ticks),
        0.01,
        MarketReplayConfig(
            Path("recording.ibrec"),
            Path("reports"),
            execution_cost_bps_per_side=0.0,
            turnover_penalty_bps_per_completed_trade=0.0,
            min_touch_liquidity_coverage_pct=0.0,
        ),
        keep_trades=True,
        state=state,
        carry_to_next=carry_to_next,
    )


def test_protective_profile_validation_key_and_json_contract() -> None:
    disabled = _profile()
    manual = _profile(mode="manual", value=3.0)
    adaptive = _profile(mode="atr", value=3.0)

    assert disabled.protective_policy_label == "Disabled"
    assert manual.protective_policy_label == "Manual 3.00% trail"
    assert adaptive.protective_policy_label == "ATR-adaptive 3.00x trail"
    assert len({disabled.key(), manual.key(), adaptive.key()}) == 3
    assert manual.to_dict()["protective_sell_trailing_stop_pct"] == 3.0
    assert adaptive.to_dict()["atr_protective_sell_multiplier"] == 3.0

    with pytest.raises(ValueError, match="protective_sell_mode"):
        replace(disabled, protective_sell_mode="unknown")
    with pytest.raises(ValueError, match="between 0.01% and 99.99%"):
        replace(disabled, protective_sell_mode="manual", protective_sell_value=0.0)
    with pytest.raises(ValueError, match="between 0.01 and 50"):
        replace(disabled, protective_sell_mode="atr", protective_sell_value=51.0)


def test_manual_protective_trail_uses_broker_style_stop_and_exits() -> None:
    day = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    session, trades, state = _simulate(
        _profile(mode="manual", value=1.0),
        _ticks(day, _protective_path()),
        _period(day),
    )

    assert session.protective_exits == 1
    assert session.completed_trades == 1
    assert session.open_position is False
    assert state.has_open_position is False
    trade = trades[0]
    assert trade.exit_type == "protective"
    assert trade.protective_sell_pct == pytest.approx(1.0)
    assert trade.protective_initial_stop_price == pytest.approx(98.0)
    assert trade.protective_trigger_price == pytest.approx(98.9)
    assert trade.sell_price == pytest.approx(98.89)


def test_atr_adaptive_protective_trail_uses_atr_and_records_clamp_state() -> None:
    day = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    prices = [100.0] * 6 + [99.0, 100.0, 98.4] + [98.3] * 27
    session, trades, _state = _simulate(
        _profile(mode="atr", value=3.0),
        _ticks(day, prices),
        _period(day),
    )

    assert session.protective_exits == 1
    assert trades[0].protective_sell_pct == pytest.approx(1.5)
    assert session.clamp_component_counts["protective_sell"]["raw"] >= 1


def test_quote_only_cached_last_cannot_trigger_protective_native_trail() -> None:
    day = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    ticks = _ticks(day, _protective_path())
    for index in range(8, len(ticks)):
        ticks[index] = replace(
            ticks[index],
            changed_fields=("bid", "ask"),
            full_snapshot=False,
        )
    session, trades, state = _simulate(
        _profile(mode="manual", value=1.0),
        ticks,
        _period(day),
        carry_to_next=True,
    )

    assert session.protective_exits == 0
    assert session.open_position is True
    assert state.has_open_position is True
    assert trades[0].sell_price is None


def test_triggered_protective_market_sell_waits_for_fresh_bid() -> None:
    day = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    prices = [100.0] * 6 + [99.0, 100.0, 98.9, 98.8] + [98.8] * 26
    ticks = _ticks(day, prices)
    ticks[8] = replace(
        ticks[8],
        bid=None,
        changed_fields=("ask", "last", "mark_price"),
        full_snapshot=False,
    )
    ticks[9] = replace(
        ticks[9],
        changed_fields=("bid", "ask"),
        full_snapshot=False,
    )
    session, trades, state = _simulate(
        _profile(mode="manual", value=1.0),
        ticks,
        _period(day),
    )

    assert session.protective_exits == 1
    assert session.protective_trigger_pending_at_end is False
    assert state.has_open_position is False
    assert trades[0].protective_trigger_price == pytest.approx(98.9)
    assert trades[0].sell_price == pytest.approx(98.79)


def test_normal_profit_exit_cancels_protective_order_before_replacement() -> None:
    day = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    prices = [100.0] * 6 + [99.0, 99.5] + [99.5] * 28
    session, trades, _state = _simulate(
        _profile(
            mode="manual",
            value=5.0,
            minimum_profit=0.5,
            sell_trail=0.0,
        ),
        _ticks(day, prices),
        _period(day),
    )

    assert session.protective_cancellations == 1
    assert session.protective_exits == 0
    assert session.completed_trades == 1
    assert trades[0].exit_type == "normal"


def test_active_protective_trail_survives_overnight_and_can_exit_next_day() -> None:
    day1 = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    day2 = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    profile = _profile(
        mode="manual",
        value=1.0,
        minimum_profit=10.0,
        sell_trail=1.0,
    )
    first_prices = [100.0] * 6 + [99.0, 99.5] + [99.5] * 28
    result1, trades, state = _simulate(
        profile,
        _ticks(day1, first_prices),
        _period(day1, 1),
        carry_to_next=True,
    )
    assert result1.carried_protective_trail_out is True
    assert state.has_open_position is True

    second_prices = [98.4] + [98.3] * 35
    result2, later_trades, state2 = _simulate(
        profile,
        _ticks(day2, second_prices),
        _period(day2, 2),
        state=state,
    )
    assert result2.carried_protective_trail_in is True
    assert result2.protective_exits == 1
    assert later_trades == []
    assert state2.has_open_position is False
    assert trades[0].sell_session_date == day2.date().isoformat()
    assert trades[0].overnight_sessions_held == 1


def test_triggered_protective_market_sell_can_wait_for_next_day_bid() -> None:
    day1 = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    day2 = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    profile = _profile(
        mode="manual",
        value=1.0,
        minimum_profit=10.0,
    )
    prices = [100.0] * 6 + [99.0, 100.0, 98.9] + [98.8] * 27
    ticks = _ticks(day1, prices)
    for index in range(8, len(ticks)):
        ticks[index] = replace(
            ticks[index],
            bid=None,
            changed_fields=("ask", "last", "mark_price"),
            full_snapshot=False,
        )
    first, trades, state = _simulate(
        profile,
        ticks,
        _period(day1, 1),
        carry_to_next=True,
    )
    assert first.protective_trigger_pending_at_end is True
    assert first.carried_protective_trail_out is True
    assert state.stage == "PROTECTIVE_FILL_PENDING"

    second_prices = [98.7] * 36
    second, later_trades, final_state = _simulate(
        profile,
        _ticks(day2, second_prices),
        _period(day2, 2),
        state=state,
    )
    assert second.carried_protective_trail_in is True
    assert second.protective_exits == 1
    assert later_trades == []
    assert final_state.has_open_position is False
    assert trades[0].sell_session_date == day2.date().isoformat()


def test_protective_exit_uses_current_event_normal_activation_threshold() -> None:
    day = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    ticks = _ticks(day, _protective_path())
    atr_values = [0.5] * len(ticks)
    atr_values[8] = 1.0
    _session, trades, _state = _simulate_session_stateful(
        ticks,
        _period(day),
        _profile(mode="manual", value=1.0),
        atr_values,
        0.01,
        MarketReplayConfig(
            Path("recording.ibrec"),
            Path("reports"),
            execution_cost_bps_per_side=0.0,
            turnover_penalty_bps_per_completed_trade=0.0,
            min_touch_liquidity_coverage_pct=0.0,
        ),
        keep_trades=True,
    )
    trade = trades[0]
    expected = trade.buy_price * 1.01 / 0.99
    assert trade.exit_type == "protective"
    assert trade.normal_activation_price_at_protective_exit == pytest.approx(expected)


def test_overnight_hold_does_not_reuse_prior_day_normal_activation() -> None:
    day1 = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    day2 = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    profile = _profile(mode="manual", value=1.0)
    first_prices = [100.0] * 6 + [99.0, 99.5] + [99.5] * 28
    first, trades, state = _simulate_session_stateful(
        _ticks(day1, first_prices),
        _period(day1, 1),
        profile,
        [0.5] * len(first_prices),
        0.01,
        MarketReplayConfig(Path("recording.ibrec"), Path("reports")),
        keep_trades=True,
        carry_to_next=True,
    )
    assert first.carried_position_out is True
    assert state.protective_normal_activation_price is not None

    second_prices = [98.4] + [98.3] * 35
    second, _later, _final = _simulate_session_stateful(
        _ticks(day2, second_prices),
        _period(day2, 2),
        profile,
        [None] * len(second_prices),
        0.01,
        MarketReplayConfig(Path("recording.ibrec"), Path("reports")),
        keep_trades=True,
        state=state,
    )
    assert second.protective_exits == 1
    assert trades[0].normal_activation_price_at_protective_exit is None


def test_protective_exit_diagnostics_quantify_recovery_and_further_loss() -> None:
    day = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    prices = [100.0] * 6 + [99.0, 100.0, 98.9, 97.0, 99.5] + [99.5] * 25
    ticks = _ticks(day, prices)
    period = _period(day)
    session, trades, _state = _simulate(
        _profile(mode="manual", value=1.0, minimum_profit=10.0),
        ticks,
        period,
    )
    assert session.protective_exits == 1

    _enrich_protective_trade_diagnostics(
        [(period, ticks)],
        [session],
        trades,
    )
    trade = trades[0]
    assert trade.protective_recovered_to_buy is True
    assert trade.protective_loss_avoided_bps is not None
    assert trade.protective_loss_avoided_bps > 0
    assert trade.protective_regret_bps is not None
    assert trade.protective_regret_bps > 0
    assert trade.protective_observation_end_utc


def _candidate(
    profile: AtrProfile,
    score: float,
    *,
    protective_exits: int = 0,
) -> MarketReplayCandidateSummary:
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
        maximum_drawdown_bps=10.0,
        open_position_rate_pct=0.0,
        no_trade_rate_pct=0.0,
        protective_exits=protective_exits,
    )


def _policy_session(day: int, *, protective_exits: int) -> MarketReplaySessionResult:
    date_text = f"2026-07-{day:02d}"
    return MarketReplaySessionResult(
        session_date=date_text,
        period_id=day,
        scheduled_open_utc=f"{date_text}T13:30:00Z",
        scheduled_close_utc=f"{date_text}T20:00:00Z",
        observed_start_utc=f"{date_text}T13:30:00Z",
        observed_end_utc=f"{date_text}T20:00:00Z",
        ticks=100,
        trades=1,
        completed_trades=1,
        no_trade=False,
        open_position=False,
        realized_return_bps=10.0,
        marked_return_bps=10.0,
        conservative_return_bps=10.0,
        max_drawdown_bps=10.0,
        protective_exits=protective_exits,
    )


def test_protective_policy_components_require_adjacent_region_and_center() -> None:
    candidates = [
        _candidate(_profile(mode="manual", value=1.0), 10.0, protective_exits=5),
        _candidate(_profile(mode="manual", value=2.0), 12.0, protective_exits=5),
        _candidate(_profile(mode="manual", value=3.0), 11.0, protective_exits=5),
        _candidate(_profile(mode="manual", value=5.0), -20.0, protective_exits=5),
    ]
    components = _protective_policy_components(candidates)
    assert len(components) == 1
    assert [item.profile.protective_sell_value for item in components[0]] == [
        1.0,
        2.0,
        3.0,
    ]
    assert _protective_policy_center(components[0]).profile.protective_sell_value == 2.0


def test_policy_selector_advances_only_supported_region_center(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    day = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    period_ticks = [(_period(day + timedelta(days=index), index + 1), []) for index in range(5)]
    recording = _recording(_ticks(day, [100.0]), [item[0] for item in period_ticks])
    config = MarketReplayConfig(tmp_path / "probe.ibrec", tmp_path / "reports")

    monkeypatch.setattr(market_replay, "_ensure_atr_windows", lambda *args, **kwargs: None)

    def fake_evaluate(
        _recording: Any,
        _period_ticks: Any,
        _atr_cache: Any,
        profiles: list[AtrProfile],
        summaries: dict[str, MarketReplayCandidateSummary],
        sessions_by_profile: dict[str, list[MarketReplaySessionResult]],
        _config: Any,
        **_kwargs: Any,
    ) -> None:
        for profile in profiles:
            score = 0.0
            exits = 0
            if profile.protective_sell_mode == "manual":
                score = {1.0: 10.0, 2.0: 12.0, 3.0: 11.0}.get(
                    profile.protective_sell_value,
                    -20.0,
                )
                exits = 5
            elif profile.protective_sell_mode == "atr":
                score = -30.0
                exits = 5
            summary = _candidate(profile, score, protective_exits=exits)
            summaries[profile.key()] = summary
            sessions_by_profile[profile.key()] = [
                _policy_session(index, protective_exits=1 if exits else 0)
                for index in range(1, 6)
            ]

    monkeypatch.setattr(market_replay, "_evaluate_profiles", fake_evaluate)
    policies, evidence, reason = _select_protective_policy(
        recording,
        period_ticks,
        config,
        {},
        {},
        {},
        progress=None,
    )

    assert [(item.protective_sell_mode, item.protective_sell_value) for item in policies] == [
        ("disabled", 0.0),
        ("manual", 2.0),
    ]
    selected = [row for row in evidence if row["selected_for_atr_search"]]
    assert {row["label"] for row in selected} == {
        "Disabled",
        "Manual 2.00% trail",
    }
    assert "strongest supported" in reason


def test_isolated_protective_policy_winner_does_not_advance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    day = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    period_ticks = [(_period(day + timedelta(days=index), index + 1), []) for index in range(5)]
    recording = _recording(_ticks(day, [100.0]), [item[0] for item in period_ticks])
    config = MarketReplayConfig(tmp_path / "probe.ibrec", tmp_path / "reports")
    monkeypatch.setattr(market_replay, "_ensure_atr_windows", lambda *args, **kwargs: None)

    def fake_evaluate(
        _recording: Any,
        _period_ticks: Any,
        _atr_cache: Any,
        profiles: list[AtrProfile],
        summaries: dict[str, MarketReplayCandidateSummary],
        sessions_by_profile: dict[str, list[MarketReplaySessionResult]],
        _config: Any,
        **_kwargs: Any,
    ) -> None:
        for profile in profiles:
            score = (
                20.0
                if profile.protective_sell_mode == "manual"
                and profile.protective_sell_value == 2.0
                else 0.0
            )
            exits = 5 if profile.protective_sell_enabled else 0
            summaries[profile.key()] = _candidate(
                profile,
                score,
                protective_exits=exits,
            )
            sessions_by_profile[profile.key()] = [
                _policy_session(index, protective_exits=1 if exits else 0)
                for index in range(1, 6)
            ]

    monkeypatch.setattr(market_replay, "_evaluate_profiles", fake_evaluate)
    policies, _evidence, reason = _select_protective_policy(
        recording,
        period_ticks,
        config,
        {},
        {},
        {},
        progress=None,
    )
    assert [(item.protective_sell_mode, item.protective_sell_value) for item in policies] == [
        ("disabled", 0.0)
    ]
    assert "No enabled protective SELL policy passed" in reason


def test_disabled_protective_policy_search_evaluates_only_control(
    tmp_path: Path,
) -> None:
    day = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    period_ticks = [(_period(day, 1), _ticks(day, [100.0] * 36))]
    recording = _recording(period_ticks[0][1], [period_ticks[0][0]])
    config = MarketReplayConfig(
        tmp_path / "probe.ibrec",
        tmp_path / "reports",
        protective_policy_search_enabled=False,
    )
    profiles, evidence, reason = _select_protective_policy(
        recording,
        period_ticks,
        config,
        {},
        {},
        {},
        progress=None,
    )
    assert len(profiles) == 1
    assert profiles[0].protective_sell_mode == "disabled"
    assert evidence == [
        {
            "mode": "disabled",
            "value": 0.0,
            "label": "Disabled",
            "profile_key": profiles[0].key(),
            "selected_for_atr_search": True,
            "eligible": True,
            "decision": "Protective-policy search was disabled by configuration.",
        }
    ]
    assert "disabled" in reason.lower()


def test_stage3_does_not_cross_atr_windows_between_protective_policies(
    tmp_path: Path,
) -> None:
    config = MarketReplayConfig(tmp_path / "probe.ibrec", tmp_path / "reports")
    disabled = _profile()
    manual = _profile(mode="manual", value=3.0)
    profiles = _coarse_profiles_for_policy_windows(
        config,
        [
            (disabled, [(14, 60)]),
            (manual, [(7, 30)]),
        ],
        search_clamps=False,
    )
    windows_by_policy: dict[tuple[str, float], set[tuple[int, int]]] = {}
    for profile in profiles:
        windows_by_policy.setdefault(
            (profile.protective_sell_mode, profile.protective_sell_value),
            set(),
        ).add((profile.period, profile.bar_seconds))
    assert windows_by_policy[("disabled", 0.0)] == {(14, 60)}
    assert windows_by_policy[("manual", 3.0)] == {(7, 30)}


def test_refinement_seed_allocation_keeps_protective_policies_separate() -> None:
    candidates = [
        _candidate(_profile(), 20.0),
        _candidate(_profile(mode="manual", value=3.0), 19.0),
        _candidate(
            replace(_profile(), initial_drop_multiplier=2.0),
            18.0,
        ),
        _candidate(
            replace(
                _profile(mode="manual", value=3.0),
                initial_drop_multiplier=2.0,
            ),
            17.0,
        ),
    ]
    seeds = _refinement_seeds(candidates, maximum=2)
    assert {
        (seed.profile.protective_sell_mode, seed.profile.protective_sell_value)
        for seed in seeds
    } == {("disabled", 0.0), ("manual", 3.0)}


def test_summary_reports_protective_exit_and_cancellation_rates() -> None:
    profile = _profile(mode="manual", value=3.0)
    sessions = [
        _policy_session(1, protective_exits=1),
        replace(
            _policy_session(2, protective_exits=0),
            protective_cancellations=1,
        ),
    ]
    summary = _summary(profile, sessions)
    assert summary.protective_exits == 1
    assert summary.protective_cancellations == 1
    assert summary.protective_exit_rate_pct == pytest.approx(50.0)


def test_protective_search_flag_requires_real_boolean(tmp_path: Path) -> None:
    config = MarketReplayConfig(
        tmp_path / "recording.ibrec",
        tmp_path / "reports",
        protective_policy_search_enabled=False,
    ).normalized()
    assert config.protective_policy_search_enabled is False
    with pytest.raises(ValueError, match="protective_policy_search_enabled"):
        MarketReplayConfig(
            tmp_path / "recording.ibrec",
            tmp_path / "reports",
            protective_policy_search_enabled="false",  # type: ignore[arg-type]
        ).normalized()


def test_market_replay_contract_documents_protective_policy_search(
    tmp_path: Path,
) -> None:
    contract = market_replay_search_contract(
        MarketReplayConfig(tmp_path / "recording.ibrec", tmp_path / "reports")
    )
    assert contract["contract_version"] == 15
    policy = contract["protective_sell_policy_search"]
    assert policy["enabled"] is True
    assert policy["manual_trailing_percentages"] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert policy["atr_adaptive_multipliers"] == [
        1.5,
        2.0,
        2.5,
        3.0,
        3.5,
        4.0,
        4.5,
    ]
    assert "cancel" in policy["normal_sell_replacement"].lower()


def test_wider_protective_trail_cannot_trigger_before_narrower_on_same_last_path() -> None:
    day = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    prices = [100.0] * 6 + [99.0, 101.0, 99.8, 98.8, 97.8] + [97.8] * 25
    ticks = _ticks(day, prices)
    period = _period(day)
    _narrow_session, narrow_trades, _ = _simulate(
        _profile(mode="manual", value=1.0, minimum_profit=10.0),
        ticks,
        period,
    )
    _wide_session, wide_trades, _ = _simulate(
        _profile(mode="manual", value=3.0, minimum_profit=10.0),
        ticks,
        period,
    )
    narrow_time = datetime.fromisoformat(narrow_trades[0].sell_time_utc.replace("Z", "+00:00"))
    wide_time = datetime.fromisoformat(wide_trades[0].sell_time_utc.replace("Z", "+00:00"))
    assert wide_time >= narrow_time
    assert all(math.isfinite(value) for value in (narrow_trades[0].sell_price, wide_trades[0].sell_price) if value is not None)


def test_end_to_end_report_emits_one_complete_profile_and_policy_evidence(
    tmp_path: Path,
) -> None:
    """Insufficient evidence must retain one disabled reference profile.

    This exercises the public analysis and report path rather than only the
    policy helper.  A one-session recording cannot authorize a changed stop
    policy, but every bounded policy must remain visible and auditable.
    """

    rows, periods = make_ticks()
    recording = write_v3(tmp_path / "AAPL.ibrec", rows, periods)
    result = write_market_replay_report(
        run_market_replay_analysis(
            MarketReplayConfig(recording, tmp_path / "reports")
        )
    )

    assert result.recommendation.profile.protective_sell_mode == "disabled"
    assert result.recommendation.profile.protective_sell_value == 0.0
    assert len(result.protective_policy_evidence) == 13
    assert sum(bool(row.get("selected_policy")) for row in result.protective_policy_evidence) == 1
    assert sum(
        bool(row.get("selected_for_atr_search"))
        for row in result.protective_policy_evidence
    ) == 1

    with (result.output_dir / "recommended_atr_settings.csv").open(
        encoding="utf-8-sig",
        newline="",
    ) as stream:
        recommendations = list(csv.DictReader(stream))
    assert len(recommendations) == 1
    assert recommendations[0]["protective_sell_mode"] == "disabled"
    assert float(recommendations[0]["protective_sell_value"]) == 0.0

    with (result.output_dir / "protective_sell_policy_comparison.csv").open(
        encoding="utf-8-sig",
        newline="",
    ) as stream:
        policies = list(csv.DictReader(stream))
    assert len(policies) == 13
    assert sum(row["selected_policy"] == "True" for row in policies) == 1
    assert {
        row["mode"] for row in policies
    } == {"disabled", "manual", "atr"}

    diagnostics = result.output_dir / "protective_sell_trade_diagnostics.csv"
    assert diagnostics.exists()
    assert "protective_sell_mode" in diagnostics.read_text(encoding="utf-8-sig")


def test_diagnostic_enrichment_does_not_change_candidate_score() -> None:
    """Hindsight recovery evidence is descriptive and cannot affect ranking."""

    day = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    prices = [100.0] * 6 + [99.0, 100.0, 98.9, 97.0, 100.5] + [100.5] * 25
    ticks = _ticks(day, prices)
    period = _period(day)
    session, trades, _state = _simulate(
        _profile(mode="manual", value=1.0, minimum_profit=10.0),
        ticks,
        period,
    )
    before = _summary(_profile(mode="manual", value=1.0), [session]).score

    _enrich_protective_trade_diagnostics([(period, ticks)], [session], trades)
    after = _summary(_profile(mode="manual", value=1.0), [session]).score

    assert trades[0].protective_regret_bps is not None
    assert before == after
