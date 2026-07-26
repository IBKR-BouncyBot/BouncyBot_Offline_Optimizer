from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from optimizer.market_replay import (
    _candidate_robustness,
    _continuity_between,
    _evaluate_period_sequence,
    _paired_bootstrap_units,
    _ReplayCarryState,
    _simulate_session_stateful,
    _summary,
    run_market_replay_analysis,
)
from optimizer.market_replay_calibration import (
    _latest_quote,
    _quote_timelines,
    load_execution_calibration,
)
from optimizer.market_replay_models import (
    AtrProfile,
    IbrecPeriod,
    IbrecRecording,
    IbrecTick,
    MarketReplayConfig,
    MarketReplaySessionResult,
)
from optimizer.market_replay_reports import write_market_replay_report
from optimizer.safety import (
    SourceSafetyError,
    source_paths,
    validate_database_source,
    validate_source,
)
from tests.conftest import create_database
from tests.market_replay_fixtures import make_ticks, write_v3


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _period(day: datetime, period_id: int = 1, *, eligible: bool = True) -> IbrecPeriod:
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
        primary_eligible=eligible,
        source_finalized=True,
    )


def _ticks(day: datetime, prices: list[float], *, sequence_base: int = 0) -> list[IbrecTick]:
    values: list[IbrecTick] = []
    for index, price in enumerate(prices):
        timestamp = day + timedelta(minutes=index)
        values.append(
            IbrecTick(
                sequence=sequence_base + index + 1,
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
        data_start_utc=ticks[0].captured_at_utc,
        data_end_utc=ticks[-1].captured_at_utc,
    )


def _robustness_session(
    day: str,
    period_id: int,
    return_bps: float,
    *,
    carried_in: bool = False,
    carried_out: bool = False,
) -> MarketReplaySessionResult:
    return MarketReplaySessionResult(
        session_date=day,
        period_id=period_id,
        scheduled_open_utc=f"{day}T13:30:00Z",
        scheduled_close_utc=f"{day}T20:00:00Z",
        observed_start_utc=f"{day}T13:30:00Z",
        observed_end_utc=f"{day}T20:00:00Z",
        ticks=100,
        trades=1,
        completed_trades=1,
        no_trade=False,
        open_position=False,
        realized_return_bps=return_bps,
        marked_return_bps=return_bps,
        conservative_return_bps=return_bps,
        max_drawdown_bps=0.0,
        session_max_drawdown_bps=0.0,
        chain_max_drawdown_bps=0.0,
        carried_position_in=carried_in,
        carried_position_out=carried_out,
    )


def _flat_then_drop(day_two: bool = False) -> list[float]:
    if day_two:
        return [100.80] * 36
    return [100.0] * 6 + [99.0] + [99.10] * 29


def test_open_position_is_carried_and_sold_on_the_next_session() -> None:
    day1 = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    day2 = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    profile = AtrProfile(5, 15, 1.5, 0.0, 3.0, 0.0)
    ticks1 = _ticks(day1, _flat_then_drop())
    ticks2 = _ticks(day2, _flat_then_drop(day_two=True))
    result1, trades1, state = _simulate_session_stateful(
        ticks1,
        _period(day1, 1),
        profile,
        [0.5] * len(ticks1),
        0.01,
        keep_trades=True,
        carry_to_next=True,
    )
    assert result1.carried_position_out is True
    assert result1.right_censored is False
    assert result1.terminal_open_position is False
    assert state.has_open_position is True
    assert len(trades1) == 1

    result2, trades2, state2 = _simulate_session_stateful(
        ticks2,
        _period(day2, 2),
        profile,
        [0.5] * len(ticks2),
        0.01,
        keep_trades=True,
        state=state,
        carry_to_next=False,
        continuity_chain_id=1,
    )
    assert result2.carried_position_in is True
    assert result2.completed_trades == 1
    assert result2.open_position is False
    assert result2.right_censored is False
    assert state2.has_open_position is False
    # The trade object is owned by the first session and updated in-place when
    # the carried position exits in the second session.
    assert trades2 == []
    assert trades1[0].sell_session_date == day2.date().isoformat()
    assert trades1[0].overnight_sessions_held == 1
    assert trades1[0].sell_price is not None


def test_active_sell_trail_survives_the_session_boundary() -> None:
    day1 = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    day2 = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    profile = AtrProfile(5, 15, 1.5, 0.0, 0.5, 1.0)
    prices1 = [100.0] * 6 + [99.0, 99.6, 100.2, 101.0] + [100.8] * 26
    ticks1 = _ticks(day1, prices1)
    result1, trades, state = _simulate_session_stateful(
        ticks1,
        _period(day1, 1),
        profile,
        [0.5] * len(ticks1),
        0.01,
        keep_trades=True,
        carry_to_next=True,
    )
    assert result1.carried_sell_trail_out is True
    assert state.stage == "SELL_TRAIL"
    assert state.sell_stop is not None

    ticks2 = _ticks(day2, [100.40] * 36)
    result2, _, _ = _simulate_session_stateful(
        ticks2,
        _period(day2, 2),
        profile,
        [0.5] * len(ticks2),
        0.01,
        keep_trades=True,
        state=state,
        carry_to_next=False,
    )
    assert result2.carried_sell_trail_in is True
    assert result2.completed_trades == 1
    assert trades[0].sell_session_date == day2.date().isoformat()


def test_missing_weekday_or_failed_quality_breaks_continuity() -> None:
    monday = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    tuesday = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    wednesday = datetime(2026, 7, 22, 13, 30, tzinfo=timezone.utc)
    assert _continuity_between(_period(monday, 1), _period(tuesday, 2))[0] is True
    continuous, reason = _continuity_between(_period(monday, 1), _period(wednesday, 3))
    assert continuous is False
    assert "missing trading day" in reason
    continuous, reason = _continuity_between(
        _period(monday, 1),
        _period(tuesday, 2, eligible=False),
    )
    assert continuous is False
    assert "data-quality gate" in reason


def test_friday_to_monday_is_a_continuous_weekday_chain() -> None:
    friday = datetime(2026, 7, 17, 13, 30, tzinfo=timezone.utc)
    monday = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)

    continuous, reason = _continuity_between(
        _period(friday, 1),
        _period(monday, 2),
    )

    assert continuous is True
    assert reason == ""


def test_unmarked_terminal_position_is_not_leaked_into_next_session() -> None:
    day1 = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    day2 = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    ticks1 = [replace(tick, bid=None) for tick in _ticks(day1, _flat_then_drop())]
    ticks2 = _ticks(day2, _flat_then_drop(day_two=True), sequence_base=10_000)
    periods = [_period(day1, 1), _period(day2, 2)]
    recording = _recording(ticks1 + ticks2, periods)

    sessions, _ = _evaluate_period_sequence(
        recording,
        [(periods[0], ticks1), (periods[1], ticks2)],
        AtrProfile(5, 15, 1.5, 0.0, 3.0, 0.0),
        MarketReplayConfig(Path("recording.ibrec"), Path("reports")),
        lambda _period, ticks: [0.5] * len(ticks),
        keep_details=True,
    )

    assert sessions[0].terminal_open_position is True
    assert sessions[0].unmarked_open_position is True
    assert sessions[0].carried_position_out is False
    assert sessions[1].carried_position_in is False


def test_pending_overnight_sell_fills_on_first_valid_next_day_bid() -> None:
    day2 = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    ticks = _ticks(day2, [101.0] * 36)
    state = _ReplayCarryState(
        stage="SELL_FILL_PENDING",
        buy_price=99.0,
        buy_cost_basis=99.0,
        assumed_quantity=100,
        buy_time="2026-07-20T19:00:00Z",
        minimum_profit_pct=1.0,
        sell_trail_pct=0.0,
        sell_fill_reference=101.0,
        capital=1.0,
        last_long_mark=100.5,
        last_session_end_equity=100.5 / 99.0,
    )

    result, _, next_state = _simulate_session_stateful(
        ticks,
        _period(day2, 2),
        AtrProfile(5, 15, 1.5, 0.0, 1.0, 0.0),
        [None] * len(ticks),
        0.01,
        keep_trades=False,
        state=state,
        carry_to_next=False,
    )

    assert result.carried_position_in is True
    assert result.completed_trades == 1
    assert result.open_position is False
    assert next_state.has_open_position is False




def test_continuity_break_terminalizes_the_prior_open_position() -> None:
    monday = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    wednesday = datetime(2026, 7, 22, 13, 30, tzinfo=timezone.utc)
    ticks1 = _ticks(monday, _flat_then_drop(), sequence_base=0)
    ticks2 = _ticks(wednesday, _flat_then_drop(day_two=True), sequence_base=10_000)
    periods = [_period(monday, 1), _period(wednesday, 2)]
    recording = _recording(ticks1 + ticks2, periods)
    profile = AtrProfile(5, 15, 1.5, 0.0, 3.0, 0.0)
    sessions, _ = _evaluate_period_sequence(
        recording,
        [(periods[0], ticks1), (periods[1], ticks2)],
        profile,
        MarketReplayConfig(Path("recording.ibrec"), Path("reports")),
        lambda _period, ticks: [0.5] * len(ticks),
        keep_details=True,
    )
    assert sessions[0].terminal_open_position is True
    assert sessions[0].right_censored is True
    assert sessions[0].carried_position_out is False
    assert sessions[1].continuity_broken_before is True
    assert sessions[1].carried_position_in is False


def test_disabling_overnight_replay_restores_isolated_session_behavior() -> None:
    day1 = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    day2 = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    ticks1 = _ticks(day1, _flat_then_drop(), sequence_base=0)
    ticks2 = _ticks(day2, _flat_then_drop(day_two=True), sequence_base=10_000)
    periods = [_period(day1, 1), _period(day2, 2)]
    recording = _recording(ticks1 + ticks2, periods)
    sessions, _ = _evaluate_period_sequence(
        recording,
        [(periods[0], ticks1), (periods[1], ticks2)],
        AtrProfile(5, 15, 1.5, 0.0, 3.0, 0.0),
        MarketReplayConfig(
            Path("recording.ibrec"),
            Path("reports"),
            continuous_overnight_replay=False,
        ),
        lambda _period, ticks: [0.5] * len(ticks),
        keep_details=True,
    )
    assert sessions[0].terminal_open_position is True
    assert sessions[1].carried_position_in is False
    assert sessions[1].continuity_break_reason.startswith(
        "Continuous overnight replay was disabled"
    )


def test_overnight_hold_rewarms_atr_before_creating_a_sell() -> None:
    day1 = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    day2 = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    profile = AtrProfile(5, 15, 1.5, 0.0, 1.0, 0.0)
    ticks1 = _ticks(day1, _flat_then_drop())
    _, trades, state = _simulate_session_stateful(
        ticks1,
        _period(day1, 1),
        profile,
        [0.5] * len(ticks1),
        0.01,
        keep_trades=True,
        carry_to_next=True,
    )
    ticks2 = _ticks(day2, [101.0] * 36)
    atr = [None] * 5 + [0.5] * 31
    result2, _, _ = _simulate_session_stateful(
        ticks2,
        _period(day2, 2),
        profile,
        atr,
        0.01,
        keep_trades=True,
        state=state,
        carry_to_next=False,
    )
    assert result2.completed_trades == 1
    assert trades[0].sell_time_utc == ticks2[5].captured_at_utc

def _prepare_calibration_database(root: Path, ticks: list[dict[str, object]]) -> Path:
    database = create_database(root, ticker="AAPL", cycles=5)
    connection = sqlite3.connect(database)
    try:
        connection.execute("ALTER TABLE cycles ADD COLUMN con_id INTEGER")
        connection.execute("ALTER TABLE cycles ADD COLUMN currency TEXT")
        connection.execute("UPDATE cycles SET con_id=265598, currency='USD'")
        points_per_day = len(ticks) // 5
        for index in range(5):
            buy_tick = ticks[index * points_per_day + 20]
            sell_tick = ticks[index * points_per_day + 60]
            buy_price = float(buy_tick["ask"]) * 1.0002
            sell_price = float(sell_tick["bid"]) / 1.0003
            connection.execute(
                "UPDATE executions SET executed_at=?, price=?, avg_price=?, commission=? WHERE order_ref=?",
                (buy_tick["captured_at_utc"], buy_price, buy_price, 0.75, f"BUY-{index + 1}"),
            )
            connection.execute(
                "UPDATE executions SET executed_at=?, price=?, avg_price=?, commission=? WHERE order_ref=?",
                (sell_tick["captured_at_utc"], sell_price, sell_price, 0.80, f"SELL-{index + 1}"),
            )
        connection.commit()
    finally:
        connection.close()
    return database


def test_execution_calibration_uses_actual_costs_and_buy_notionals(tmp_path: Path) -> None:
    raw_ticks, raw_periods = make_ticks(sessions=5)
    source = tmp_path / "bot"
    database = _prepare_calibration_database(source, raw_ticks)
    path = write_v3(tmp_path / "recording.ibrec", raw_ticks, raw_periods)
    config = MarketReplayConfig(
        path,
        tmp_path / "reports",
        assumed_trade_notional=10_000.0,
        execution_cost_bps_per_side=1.0,
        calibration_source_dir=source,
        calibration_min_samples=5,
    )
    from optimizer.ibrec import load_ibrec

    recording = load_ibrec(config)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    calibration = load_execution_calibration(recording, config)
    after = hashlib.sha256(database.read_bytes()).hexdigest()
    assert before == after
    assert calibration.enabled is True
    assert calibration.applied is True
    assert calibration.buy_order_samples == 5
    assert calibration.sell_order_samples == 5
    assert calibration.buy_quote_matched_orders == 5
    assert calibration.sell_quote_matched_orders == 5
    assert calibration.used_execution_cost_calibration is True
    assert calibration.effective_execution_cost_bps_per_side > 1.0
    assert calibration.used_trade_notional_calibration is True
    assert calibration.effective_trade_notional < 2_000.0
    assert not (source / "ibkr_trading_bot.lock").exists()


def test_sqlite_only_calibration_does_not_require_debug_captures(
    tmp_path: Path,
) -> None:
    raw_ticks, raw_periods = make_ticks(sessions=1)
    source = tmp_path / "bot"
    create_database(source, ticker="AAPL", cycles=1)
    assert not (source / "debug_captures").exists()

    paths = source_paths(source)
    assert validate_database_source(paths) == []
    assert validate_source(paths) == [
        "Capture directory 'debug_captures' was not found."
    ]

    path = write_v3(tmp_path / "recording.ibrec", raw_ticks, raw_periods)
    from optimizer.ibrec import load_ibrec

    config = MarketReplayConfig(
        path,
        tmp_path / "reports",
        calibration_source_dir=source,
        calibration_min_samples=1,
    )
    calibration = load_execution_calibration(load_ibrec(config), config)

    assert calibration.enabled is True
    assert not any("debug_captures" in warning for warning in calibration.warnings)


def test_cycle_level_commissions_replace_unknown_execution_row_zeros(
    tmp_path: Path,
) -> None:
    raw_ticks, raw_periods = make_ticks(sessions=5)
    source = tmp_path / "bot"
    _prepare_calibration_database(source, raw_ticks)
    connection = sqlite3.connect(source / "bot_state.sqlite")
    try:
        connection.execute("ALTER TABLE cycles ADD COLUMN buy_commission REAL")
        connection.execute("ALTER TABLE cycles ADD COLUMN sell_commission REAL")
        connection.execute("UPDATE executions SET commission=0")
        connection.execute(
            "UPDATE cycles SET buy_commission=1.25, sell_commission=1.50"
        )
        connection.commit()
    finally:
        connection.close()
    path = write_v3(tmp_path / "recording.ibrec", raw_ticks, raw_periods)
    from optimizer.ibrec import load_ibrec

    config = MarketReplayConfig(
        path,
        tmp_path / "reports",
        calibration_source_dir=source,
        calibration_min_samples=5,
    )
    calibration = load_execution_calibration(load_ibrec(config), config)

    assert calibration.cycle_commission_groups_applied == 10
    assert calibration.buy_commission_bps_p75 is not None
    assert calibration.sell_commission_bps_p75 is not None
    assert calibration.buy_commission_bps_p75 > 0
    assert calibration.sell_commission_bps_p75 > 0
    assert calibration.used_execution_cost_calibration is True
    assert any(
        "completed-cycle commission totals" in warning
        for warning in calibration.warnings
    )


def test_execution_calibration_rejects_a_conflicting_contract_identity(
    tmp_path: Path,
) -> None:
    raw_ticks, raw_periods = make_ticks(sessions=1)
    source = tmp_path / "bot"
    create_database(source, ticker="AAPL", cycles=1)
    connection = sqlite3.connect(source / "bot_state.sqlite")
    try:
        connection.execute("ALTER TABLE cycles ADD COLUMN con_id INTEGER")
        connection.execute("ALTER TABLE cycles ADD COLUMN currency TEXT")
        connection.execute("UPDATE cycles SET con_id=999999, currency='USD'")
        connection.commit()
    finally:
        connection.close()
    path = write_v3(tmp_path / "recording.ibrec", raw_ticks, raw_periods)
    from optimizer.ibrec import load_ibrec

    config = MarketReplayConfig(
        path,
        tmp_path / "reports",
        calibration_source_dir=source,
        calibration_min_samples=1,
    )
    calibration = load_execution_calibration(load_ibrec(config), config)

    assert calibration.matched_cycles == 0
    assert calibration.execution_rows_considered == 0
    assert calibration.applied is False
    assert any("no execution rows" in warning.lower() for warning in calibration.warnings)


def test_quote_matching_never_uses_a_future_quote_and_refreshes_same_price_age() -> None:
    day = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
    ticks = _ticks(day, [100.0, 100.0, 100.0])
    refreshed = replace(
        ticks[1],
        ask=ticks[0].ask,
        changed_fields=("ask",),
        full_snapshot=False,
    )
    later = replace(
        ticks[2],
        changed_fields=("last",),
        full_snapshot=False,
    )
    recording = _recording([ticks[0], refreshed, later], [_period(day)])
    _, _, ask_times, asks = _quote_timelines(recording)
    assert _latest_quote(day.timestamp() - 1, ask_times, asks, max_age=120) is None
    # The explicit same-price update at minute one refreshes quote age.
    value = _latest_quote(
        (day + timedelta(minutes=1, seconds=30)).timestamp(),
        ask_times,
        asks,
        max_age=45,
    )
    assert value == ticks[0].ask
    assert _latest_quote(
        (day + timedelta(minutes=2)).timestamp(),
        ask_times,
        asks,
        max_age=30,
    ) is None


def test_calibration_lock_blocks_analysis(tmp_path: Path) -> None:
    ticks, periods = make_ticks(sessions=1)
    source = tmp_path / "bot"
    create_database(source, ticker="AAPL", cycles=1)
    (source / "ibkr_trading_bot.lock").write_text("123", encoding="ascii")
    path = write_v3(tmp_path / "recording.ibrec", ticks, periods)
    from optimizer.ibrec import load_ibrec

    config = MarketReplayConfig(
        path,
        tmp_path / "reports",
        calibration_source_dir=source,
        calibration_min_samples=1,
    )
    recording = load_ibrec(config)
    with pytest.raises(SourceSafetyError, match="lock file exists"):
        load_execution_calibration(recording, config)


def test_calibrated_report_is_path_independent_and_explains_continuity(tmp_path: Path) -> None:
    raw_ticks, raw_periods = make_ticks(sessions=1)
    path = write_v3(tmp_path / "recording.ibrec", raw_ticks, raw_periods)
    source = tmp_path / "private-calibration-folder"
    _prepare_calibration_database(source, raw_ticks * 5)
    # Only one recording session is required for this report-contract test; use
    # one sample threshold so the optional calibration path is exercised.
    config = MarketReplayConfig(
        path,
        tmp_path / "reports",
        calibration_source_dir=source,
        calibration_min_samples=1,
    )
    result = write_market_replay_report(run_market_replay_analysis(config))
    assert (result.output_dir / "execution_calibration.csv").is_file()
    assert (result.output_dir / "continuity_evidence.csv").is_file()
    combined = "\n".join(
        file.read_text(encoding="utf-8-sig")
        for file in result.output_dir.iterdir()
        if file.suffix.lower() in {".html", ".json", ".csv", ".txt"}
    )
    assert str(source.resolve()) not in combined
    assert "overnight replay" in combined.lower()
    assert "execution calibration" in combined.lower()


def test_insufficient_calibration_samples_retain_configured_values(tmp_path: Path) -> None:
    raw_ticks, raw_periods = make_ticks(sessions=1)
    source = tmp_path / "bot"
    create_database(source, ticker="AAPL", cycles=1)
    path = write_v3(tmp_path / "recording.ibrec", raw_ticks, raw_periods)
    from optimizer.ibrec import load_ibrec

    config = MarketReplayConfig(
        path,
        tmp_path / "reports",
        assumed_trade_notional=12_345.0,
        execution_cost_bps_per_side=2.5,
        calibration_source_dir=source,
        calibration_min_samples=5,
    )
    calibration = load_execution_calibration(load_ibrec(config), config)
    assert calibration.applied is False
    assert calibration.effective_trade_notional == 12_345.0
    assert calibration.effective_execution_cost_bps_per_side == 2.5
    assert calibration.warnings


def _report_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_calibrated_analysis_is_deterministic_across_absolute_source_paths(
    tmp_path: Path,
) -> None:
    raw_ticks, raw_periods = make_ticks(sessions=1)
    recording_a = write_v3(tmp_path / "a" / "recording.ibrec", raw_ticks, raw_periods)
    recording_b = tmp_path / "b" / "renamed.ibrec"
    recording_b.parent.mkdir(parents=True)
    recording_b.write_bytes(recording_a.read_bytes())
    source_a = tmp_path / "source-a"
    _prepare_calibration_database(source_a, raw_ticks * 5)
    source_b = tmp_path / "source-b"
    source_b.mkdir()
    (source_b / "bot_state.sqlite").write_bytes(
        (source_a / "bot_state.sqlite").read_bytes()
    )
    first = write_market_replay_report(
        run_market_replay_analysis(
            MarketReplayConfig(
                recording_a,
                tmp_path / "reports-a",
                calibration_source_dir=source_a,
                calibration_min_samples=1,
            )
        )
    )
    second = write_market_replay_report(
        run_market_replay_analysis(
            MarketReplayConfig(
                recording_b,
                tmp_path / "reports-b",
                calibration_source_dir=source_b,
                calibration_min_samples=1,
            )
        )
    )
    assert first.analysis_id == second.analysis_id
    assert _report_hashes(first.output_dir) == _report_hashes(second.output_dir)


def test_previous_close_bid_is_not_reused_as_a_fresh_next_session_mark() -> None:
    day = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    ticks = [replace(tick, bid=None) for tick in _ticks(day, [100.0] * 36)]
    state = _ReplayCarryState(
        stage="HOLD",
        buy_price=99.0,
        buy_cost_basis=99.0,
        assumed_quantity=100,
        buy_time="2026-07-20T19:00:00Z",
        capital=1.0,
        last_long_mark=100.0,
        last_session_end_equity=100.0 / 99.0,
        equity_peak=100.0 / 99.0,
    )

    result, _, next_state = _simulate_session_stateful(
        ticks,
        _period(day, 2),
        AtrProfile(5, 15, 1.5, 0.0, 3.0, 0.0),
        [None] * len(ticks),
        0.01,
        keep_trades=False,
        state=state,
        carry_to_next=True,
    )

    assert result.unmarked_open_position is True
    assert result.terminal_open_position is True
    assert result.carried_position_out is False
    assert next_state.last_long_mark is None


def test_overnight_drawdown_is_measured_from_the_continuity_chain_peak() -> None:
    day = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    ticks = _ticks(day, [90.0] * 36)
    state = _ReplayCarryState(
        stage="HOLD",
        buy_price=100.0,
        buy_cost_basis=100.0,
        assumed_quantity=100,
        buy_time="2026-07-20T19:00:00Z",
        capital=1.0,
        last_long_mark=100.0,
        last_session_end_equity=1.0,
        equity_peak=1.10,
        maximum_drawdown_bps=0.0,
    )

    result, _, next_state = _simulate_session_stateful(
        ticks,
        _period(day, 2),
        AtrProfile(5, 15, 1.5, 0.0, 10.0, 1.0),
        [None] * len(ticks),
        0.01,
        MarketReplayConfig(
            Path("recording.ibrec"),
            Path("reports"),
            execution_cost_bps_per_side=0.0,
        ),
        keep_trades=False,
        state=state,
        carry_to_next=True,
    )

    assert result.session_max_drawdown_bps == pytest.approx(1_001.0)
    assert result.chain_max_drawdown_bps == pytest.approx(1_819.090909, rel=1e-6)
    assert result.max_drawdown_bps == result.chain_max_drawdown_bps
    assert next_state.maximum_drawdown_bps == result.chain_max_drawdown_bps


def test_exact_contract_cycles_take_precedence_over_legacy_ticker_cycles(
    tmp_path: Path,
) -> None:
    raw_ticks, raw_periods = make_ticks(sessions=1)
    source = tmp_path / "bot"
    create_database(source, ticker="AAPL", cycles=2)
    connection = sqlite3.connect(source / "bot_state.sqlite")
    try:
        connection.execute("ALTER TABLE cycles ADD COLUMN con_id INTEGER")
        connection.execute("ALTER TABLE cycles ADD COLUMN currency TEXT")
        connection.execute(
            "UPDATE cycles SET con_id=265598, currency='USD' WHERE id='cycle-1'"
        )
        connection.execute(
            "UPDATE cycles SET con_id=NULL, currency=NULL WHERE id='cycle-2'"
        )
        connection.commit()
    finally:
        connection.close()
    path = write_v3(tmp_path / "recording.ibrec", raw_ticks, raw_periods)
    from optimizer.ibrec import load_ibrec

    config = MarketReplayConfig(
        path,
        tmp_path / "reports",
        calibration_source_dir=source,
        calibration_min_samples=1,
    )
    calibration = load_execution_calibration(load_ibrec(config), config)

    assert calibration.identity_selection_mode == "exact_con_id"
    assert calibration.matched_cycles == 1
    assert calibration.legacy_identity_cycles == 0
    assert calibration.execution_rows_considered == 2


def test_commission_currency_mismatch_is_not_combined_with_trade_notional(
    tmp_path: Path,
) -> None:
    raw_ticks, raw_periods = make_ticks(sessions=1)
    source = tmp_path / "bot"
    create_database(source, ticker="AAPL", cycles=1)
    connection = sqlite3.connect(source / "bot_state.sqlite")
    try:
        connection.execute("ALTER TABLE cycles ADD COLUMN con_id INTEGER")
        connection.execute("ALTER TABLE cycles ADD COLUMN currency TEXT")
        connection.execute("ALTER TABLE executions ADD COLUMN currency TEXT")
        connection.execute("UPDATE cycles SET con_id=265598, currency='USD'")
        buy_tick = raw_ticks[20]
        sell_tick = raw_ticks[60]
        connection.execute(
            "UPDATE executions SET executed_at=?, price=?, avg_price=?, commission=1.0, currency='EUR' WHERE side='BOT'",
            (
                buy_tick["captured_at_utc"],
                float(buy_tick["ask"]) * 1.0002,
                float(buy_tick["ask"]) * 1.0002,
            ),
        )
        connection.execute(
            "UPDATE executions SET executed_at=?, price=?, avg_price=?, commission=0.0, currency='USD' WHERE side='SLD'",
            (
                sell_tick["captured_at_utc"],
                float(sell_tick["bid"]) / 1.0003,
                float(sell_tick["bid"]) / 1.0003,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    path = write_v3(tmp_path / "recording.ibrec", raw_ticks, raw_periods)
    from optimizer.ibrec import load_ibrec

    config = MarketReplayConfig(
        path,
        tmp_path / "reports",
        execution_cost_bps_per_side=0.0,
        calibration_source_dir=source,
        calibration_min_samples=1,
        calibration_use_trade_notional=False,
    )
    calibration = load_execution_calibration(load_ibrec(config), config)

    assert calibration.commission_currency_mismatch_rows == 1
    assert calibration.commission_unavailable_rows == 1
    assert calibration.buy_commission_bps_p75 is None
    assert calibration.sell_commission_bps_p75 is None
    assert calibration.buy_adverse_slippage_bps_p75 is not None
    assert calibration.effective_execution_cost_bps_per_side > 0.0
    assert any("commission currency" in warning for warning in calibration.warnings)


def test_bootstrap_groups_days_linked_by_overnight_exposure() -> None:
    candidate = {
        "2026-07-20": [
            (
                _robustness_session("2026-07-20", 1, 10.0, carried_out=True),
                _robustness_session("2026-07-20", 1, 0.0),
            )
        ],
        "2026-07-21": [
            (
                _robustness_session("2026-07-21", 2, 10.0, carried_in=True),
                _robustness_session("2026-07-21", 2, 0.0),
            )
        ],
        "2026-07-22": [
            (
                _robustness_session("2026-07-22", 3, 10.0),
                _robustness_session("2026-07-22", 3, 0.0),
            )
        ],
    }

    unit_type, units = _paired_bootstrap_units(candidate)

    assert unit_type == "overnight_continuity_block"
    assert units == [("2026-07-20", "2026-07-21"), ("2026-07-22",)]


def test_overnight_dependency_requires_independent_blocks_and_exact_stage_selection() -> None:
    days = [f"2026-07-{day:02d}" for day in range(20, 25)]
    control_sessions = [
        _robustness_session(day, index + 1, 0.0)
        for index, day in enumerate(days)
    ]
    candidate_sessions = [
        _robustness_session(
            day,
            index + 1,
            20.0,
            carried_in=index > 0,
            carried_out=index < len(days) - 1,
        )
        for index, day in enumerate(days)
    ]
    profile = AtrProfile(14, 60, 1.50, 0.75, 1.00, 0.75)
    selection_rows = [
        {
            "omitted_trading_day": day,
            "selected_windows": [{"period": 14, "bar_seconds": 60}],
            "selected_profile_key": profile.key(),
        }
        for day in days
    ]

    evidence, _ = _candidate_robustness(
        _summary(profile, candidate_sessions),
        candidate_sessions,
        _summary(AtrProfile(14, 60, 1.50, 0.75, 1.00, 1.00), control_sessions),
        control_sessions,
        seed="overnight-chain",
        selection_rows=selection_rows,
    )

    assert evidence["bootstrap_unit_type"] == "overnight_continuity_block"
    assert evidence["bootstrap_independent_units"] == 1
    assert evidence["passed"] is False
    combined = " ".join(evidence["failure_reasons"]).lower()
    assert "continuity blocks" in combined
    assert "reuses full-run session summaries" not in combined
