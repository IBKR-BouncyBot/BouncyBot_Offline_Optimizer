"""Independent full-recording ATR search for Market Replay Lab data."""

from __future__ import annotations

import hashlib
import itertools
import math
import statistics
import struct
from collections import defaultdict
from typing import Any, Callable, Iterable

from .determinism import canonical_json_bytes
from .ibrec import load_ibrec_set
from .market_replay_models import (
    AtrProfile,
    IbrecPeriod,
    IbrecRecording,
    IbrecTick,
    MarketReplayAnalysisResult,
    MarketReplayCandidateSummary,
    MarketReplayConfig,
    MarketReplaySessionResult,
    MarketReplayTrade,
)
from .version import APP_VERSION


class MarketReplayAnalysisError(RuntimeError):
    """Raised when a recording cannot support a safe deterministic analysis."""


ProgressCallback = Callable[[str, int, int], None]
MARKET_REPLAY_ANALYSIS_CONTRACT_VERSION = 6
_CONTROL_PROFILE = AtrProfile(
    period=14,
    bar_seconds=60,
    initial_drop_multiplier=1.50,
    buy_rebound_multiplier=0.75,
    minimum_profit_multiplier=1.00,
    sell_trail_multiplier=1.00,
)
_STAGE1_FIXED_PERIOD = 14
_STAGE1_BAR_SECONDS = (15, 30, 60, 120)
_STAGE1_KEEP_BAR_DURATIONS = 2
_STAGE2_PERIODS = (5, 7, 10, 14, 21, 28)
_STAGE2_NON_CONTROL_WINDOWS = 2
_COARSE_INITIAL = (0.75, 1.50, 2.25)
_COARSE_BUY = (0.00, 0.75, 1.25)
_COARSE_PROFIT = (0.50, 1.00, 1.50)
_COARSE_SELL = (0.00, 0.75, 1.25)
_REFINEMENT_STEP = 0.25
_REFINEMENT_SEEDS = 12
_BOOTSTRAP_REPLICATES = 2_000
_MIN_ROBUST_TRADING_DAYS = 5
_MIN_BOOTSTRAP_POSITIVE_PCT = 80.0
_MIN_LOO_WINDOW_SELECTION_PCT = 60.0
_ENTRY_OPEN_DELAY_SECONDS = 5 * 60
_ENTRY_CUTOFF_SECONDS = 15 * 60
_BUY_TRAIL_CANCEL_SECONDS = 5 * 60
_MIN_PAIRED_POSITIVE_DAY_PCT = 60.0
_MIN_CONTROL_TRADE_DAY_RETENTION_PCT = 80.0
_MAX_DRAWDOWN_DETERIORATION_BPS = 25.0
_MAX_WORST_RETURN_DETERIORATION_BPS = 25.0
_MIN_PRACTICAL_SCORE_DELTA = 1.0
_ATR_PHASE_STEP_SECONDS = 5


def market_replay_search_contract(config: MarketReplayConfig) -> dict[str, Any]:
    normalized = config.normalized()
    return {
        "contract_version": MARKET_REPLAY_ANALYSIS_CONTRACT_VERSION,
        "optimizer_version": APP_VERSION,
        "supported_ibrec_versions": [2, 3],
        "atr_window_search": {
            "stage_1": {
                "purpose": "compare bar duration while holding ATR period and all multipliers fixed",
                "fixed_period": _STAGE1_FIXED_PERIOD,
                "bar_seconds": list(_STAGE1_BAR_SECONDS),
                "keep_best_bar_durations": _STAGE1_KEEP_BAR_DURATIONS,
                "control_bar_seconds_always_retained": _CONTROL_PROFILE.bar_seconds,
            },
            "stage_2": {
                "purpose": "compare ATR period within the strongest stage-1 bar durations",
                "periods": list(_STAGE2_PERIODS),
                "keep_non_control_windows": _STAGE2_NON_CONTROL_WINDOWS,
                "control_window_always_retained": [
                    _CONTROL_PROFILE.period,
                    _CONTROL_PROFILE.bar_seconds,
                ],
            },
            "stage_3": {
                "purpose": "search entry and exit multipliers only within the narrowed ATR windows",
                "control_window_always_included": [
                    _CONTROL_PROFILE.period,
                    _CONTROL_PROFILE.bar_seconds,
                ],
            },
        },
        "coarse_initial_drop_multipliers": list(_COARSE_INITIAL),
        "coarse_buy_rebound_multipliers": list(_COARSE_BUY),
        "coarse_minimum_profit_multipliers": list(_COARSE_PROFIT),
        "coarse_sell_trail_multipliers": list(_COARSE_SELL),
        "refinement_step": _REFINEMENT_STEP,
        "refinement_seed_count": _REFINEMENT_SEEDS,
        "trading_day_bootstrap_replicates": _BOOTSTRAP_REPLICATES,
        "trading_day_bootstrap_schedule": (
            "all stable-region centers use the same deterministic whole-day resample schedule"
        ),
        "minimum_robust_trading_days": _MIN_ROBUST_TRADING_DAYS,
        "minimum_bootstrap_probability_positive_pct": _MIN_BOOTSTRAP_POSITIVE_PCT,
        "minimum_leave_one_day_out_window_selection_pct": (
            _MIN_LOO_WINDOW_SELECTION_PCT
        ),
        "changed_recommendation_requires": [
            "connected near-best multiplier region",
            "positive score delta versus the unchanged control on identical RTH sessions",
            f"paired score improvement of at least {_MIN_PRACTICAL_SCORE_DELTA:.1f} point",
            "80% trading-day bootstrap interval entirely above zero",
            f"at least {_MIN_BOOTSTRAP_POSITIVE_PCT:.0f}% positive bootstrap replicates",
            "positive leave-one-day-out result for every removable trading day",
            "no leave-one-day-out sign reversal",
            "same ATR period/bar window advances through stage 2 in at least 60% of leave-one-day-out reruns",
            "no paired candidate/control session is right-censored",
            "no paired candidate/control session has an unmarked open long position",
        ],
        "min_atr_pct": normalized.min_atr_pct,
        "max_atr_pct": normalized.max_atr_pct,
        "entry_open_delay_seconds": _ENTRY_OPEN_DELAY_SECONDS,
        "entry_cutoff_seconds": _ENTRY_CUTOFF_SECONDS,
        "buy_trail_cancel_seconds": _BUY_TRAIL_CANCEL_SECONDS,
        "input_recordings": {
            "maximum": normalized.max_recordings,
            "aggregate_row_limit": normalized.max_rows,
            "aggregate_input_byte_limit": normalized.max_input_bytes,
            "same_instrument_required": [
                "symbol",
                "positive conId",
                "currency",
                "security type",
                "exchange time zone",
                "minimum tick",
            ],
            "same_day_overlap_policy": "exclude the complete trading date; never splice fragments",
        },
        "strategy_price_priority": [
            "Last when inside a valid spread",
            "valid bid/ask midpoint",
            "mark",
            "Last",
            "close",
        ],
        "native_trail_trigger": "new Last event only; blank changed_fields is a full snapshot",
        "event_retention": (
            "all rows are integrity-checked; replay retains all Last events, full snapshots, price/feed changes, "
            "first/final rows, and at least one usable state per UTC second"
        ),
        "session_observation_end": (
            "right-censoring uses explicit RTH observed_end metadata plus the final retained event; an unfilled BUY "
            "trail is cancelled when observation reaches the standardized cutoff"
        ),
        "evidence_stability_gates": [
            "synthetic source",
            "delayed-only analyzed session",
            "mixed live/delayed session",
            "frozen feed interruption",
            "receipt-clock reversal",
        ],
        "fill_model": (
            "BUY requires a recorded non-crossed ask and uses the worse of trigger/reference and ask; "
            "SELL requires a recorded non-crossed bid and uses the worse of trigger/reference and bid. "
            "A triggered market order remains pending until its same-side touch is observed."
        ),
        "atr_clock": (
            "OHLC buckets use recorder elapsed_ns (monotonic time). The primary run uses the canonical stored phase; "
            "changed recommendations are stress-tested over 5-second phase offsets because BouncyBot's process-clock phase is not recorded."
        ),
        "scoring": (
            "0.50*median_conservative_return_bps + 0.30*mean_conservative_return_bps "
            "+ 0.20*worst_conservative_return_bps - 0.35*maximum_drawdown_bps "
            "- 25*open_position_session_fraction - 10*no_trade_session_fraction "
            "- 15*right_censored_session_fraction"
        ),
        "additional_changed_recommendation_gates": [
            f"at least {_MIN_PAIRED_POSITIVE_DAY_PCT:.0f}% of paired trading days have a better conservative return",
            f"candidate trades on at least {_MIN_CONTROL_TRADE_DAY_RETENTION_PCT:.0f}% of control trading days",
            f"maximum drawdown cannot deteriorate by more than {_MAX_DRAWDOWN_DETERIORATION_BPS:.0f} bps",
            f"worst-session return cannot deteriorate by more than {_MAX_WORST_RETURN_DETERIORATION_BPS:.0f} bps",
            "candidate remains above control under every tested ATR bar phase and an adverse per-session phase aggregation",
        ],
    }


def _emit(progress: ProgressCallback | None, message: str, current: int, total: int) -> None:
    if progress is not None:
        progress(message, current, total)


def _valid(value: float | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _round_increment(value: float, increment: float, direction: str) -> float:
    step = increment if math.isfinite(increment) and increment > 0 else 0.01
    units = value / step
    if direction == "up":
        rounded = math.ceil(units - 1e-12) * step
    elif direction == "down":
        rounded = math.floor(units + 1e-12) * step
    else:
        rounded = round(units) * step
    decimals = 0
    while decimals < 8 and abs(round(step, decimals) - step) > 1e-12:
        decimals += 1
    return round(max(step, rounded), max(2, decimals))


def _effective_percentage(
    atr_pct: float | None,
    multiplier: float,
    profile: AtrProfile,
    *,
    allow_zero: bool,
) -> float | None:
    if allow_zero and multiplier <= 0:
        return 0.0
    if atr_pct is None or not math.isfinite(atr_pct) or atr_pct <= 0:
        return None
    value = atr_pct * multiplier
    return round(max(profile.min_atr_pct, min(profile.max_atr_pct, value)), 2)


def _profile_distance(left: AtrProfile, right: AtrProfile = _CONTROL_PROFILE) -> float:
    return (
        abs(left.period - right.period) / 10.0
        + abs(left.bar_seconds - right.bar_seconds) / 60.0
        + abs(left.initial_drop_multiplier - right.initial_drop_multiplier)
        + abs(left.buy_rebound_multiplier - right.buy_rebound_multiplier)
        + abs(left.minimum_profit_multiplier - right.minimum_profit_multiplier)
        + abs(left.sell_trail_multiplier - right.sell_trail_multiplier)
    )


def _candidate_sort_key(candidate: MarketReplayCandidateSummary) -> tuple[Any, ...]:
    return (-candidate.score, _profile_distance(candidate.profile), candidate.profile.key())


def _session_ticks(recording: IbrecRecording, period: IbrecPeriod) -> list[IbrecTick]:
    if recording.format_version == 3 or period.source_recording_sha256:
        values = [tick for tick in recording.ticks if tick.rth_period_id == period.period_id]
    else:
        values = [
            tick
            for tick in recording.ticks
            if period.open_timestamp <= tick.timestamp < period.close_timestamp
        ]
        if not values:
            # Legacy observed-bound fallback includes the final retained row.
            values = [
                tick
                for tick in recording.ticks
                if period.open_timestamp <= tick.timestamp <= period.close_timestamp
            ]
    live = [tick for tick in values if tick.market_data_type == 1]
    if live:
        return live
    return [tick for tick in values if tick.market_data_type == 3]


def _session_feed_evidence(
    recording: IbrecRecording,
    period: IbrecPeriod,
) -> tuple[str, bool, bool]:
    """Return selected feed class plus mixed and frozen-feed evidence."""

    if recording.format_version == 3 or period.source_recording_sha256:
        values = [tick for tick in recording.ticks if tick.rth_period_id == period.period_id]
    else:
        values = [
            tick
            for tick in recording.ticks
            if period.open_timestamp <= tick.timestamp <= period.close_timestamp
        ]
    has_live = any(tick.market_data_type == 1 for tick in values)
    has_delayed = any(tick.market_data_type == 3 for tick in values)
    has_frozen = any(tick.market_data_type in {2, 4} for tick in values)
    selected = "live" if has_live else ("delayed" if has_delayed else "none")
    return selected, has_live and has_delayed, has_frozen


def _precompute_atr(
    ticks: list[IbrecTick],
    period: int,
    bar_seconds: int,
    *,
    phase_seconds: int = 0,
) -> list[float | None]:
    """Reconstruct BouncyBot's simple ATR on the recorder monotonic clock.

    BouncyBot buckets observations using ``time.monotonic()``.  Market Replay
    stores the equivalent recorder ``elapsed_ns`` but not the absolute phase of
    BouncyBot's process clock.  ``phase_seconds`` therefore allows the same
    event stream to be stress-tested against alternate fixed bucket phases.
    """

    bars: list[dict[str, float]] = []
    output: list[float | None] = []
    max_age = max(float((period + 4) * bar_seconds), 300.0)
    for tick in ticks:
        monotonic_time = tick.elapsed_ns / 1_000_000_000.0 + float(phase_seconds)
        price = tick.selected_price()
        if price is not None:
            bucket = int(monotonic_time // bar_seconds)
            if not bars or int(bars[-1]["bucket"]) != bucket:
                bars.append(
                    {
                        "bucket": float(bucket),
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "end_ts": monotonic_time,
                    }
                )
            else:
                bars[-1]["high"] = max(bars[-1]["high"], price)
                bars[-1]["low"] = min(bars[-1]["low"], price)
                bars[-1]["close"] = price
                bars[-1]["end_ts"] = monotonic_time
        stale_before = monotonic_time - max_age
        first_recent = 0
        while (
            first_recent < len(bars)
            and float(bars[first_recent].get("end_ts", 0.0)) < stale_before
        ):
            first_recent += 1
        if first_recent:
            del bars[:first_recent]
        if len(bars) < period + 1:
            output.append(None)
            continue
        recent = bars[-(period + 1) :]
        true_ranges: list[float] = []
        for previous, current in zip(recent, recent[1:]):
            high = current["high"]
            low = current["low"]
            previous_close = previous["close"]
            true_ranges.append(
                max(high - low, abs(high - previous_close), abs(low - previous_close))
            )
        latest_close = recent[-1]["close"]
        atr = sum(true_ranges[-period:]) / period
        atr_pct = atr / latest_close * 100.0 if latest_close > 0 else None
        output.append(
            atr_pct
            if atr_pct is not None and math.isfinite(atr_pct) and atr_pct > 0
            else None
        )
    return output


def _max_drawdown(equity_values: Iterable[float]) -> float:
    peak = 0.0
    maximum = 0.0
    for equity in equity_values:
        if not math.isfinite(equity) or equity <= 0:
            continue
        peak = max(peak, equity)
        if peak > 0:
            maximum = max(maximum, (peak - equity) / peak * 10_000.0)
    return maximum


def _simulate_session(
    ticks: list[IbrecTick],
    period: IbrecPeriod,
    profile: AtrProfile,
    atr_values: list[float | None],
    min_tick: float,
    *,
    keep_trades: bool,
) -> tuple[MarketReplaySessionResult, list[MarketReplayTrade]]:
    """Replay one standardized RTH session without inventing executable quotes."""

    if len(ticks) != len(atr_values):
        raise MarketReplayAnalysisError("Internal ATR/tick alignment failure.")
    entry_start = period.open_timestamp + _ENTRY_OPEN_DELAY_SECONDS
    entry_cutoff = period.close_timestamp - _ENTRY_CUTOFF_SECONDS
    buy_cancel = period.close_timestamp - _BUY_TRAIL_CANCEL_SECONDS
    stage = "WAIT_READY"
    anchor: float | None = None
    buy_stop: float | None = None
    buy_running_low: float | None = None
    buy_trail_pct: float | None = None
    buy_placed_sequence = 0
    buy_fill_reference: float | None = None
    buy_price: float | None = None
    buy_time = ""
    minimum_profit_pct: float | None = None
    sell_trail_pct: float | None = None
    sell_stop: float | None = None
    sell_running_high: float | None = None
    sell_placed_sequence = 0
    sell_fill_reference: float | None = None
    capital = 1.0
    cycle_number = 1
    completed_trade_count = 0
    trades: list[MarketReplayTrade] = []
    equity_values: list[float] = [1.0]
    first_entry = ""
    last_exit = ""
    last_long_mark: float | None = None
    issues: list[str] = []

    def append_long_mark(tick: IbrecTick) -> None:
        nonlocal last_long_mark
        if buy_price is None:
            return
        bid = tick.long_mark_price()
        if bid is not None:
            last_long_mark = bid
            equity_values.append(capital * (bid / buy_price))

    def complete_buy(tick: IbrecTick) -> bool:
        nonlocal buy_price, buy_time, first_entry, stage, last_long_mark
        ask = tick.executable_price("BUY")
        if ask is None or buy_fill_reference is None:
            return False
        buy_price = max(buy_fill_reference, ask)
        last_long_mark = None
        buy_time = tick.captured_at_utc
        first_entry = first_entry or buy_time
        stage = "HOLD"
        if keep_trades:
            trades.append(
                MarketReplayTrade(
                    session_date=period.session_date,
                    cycle_number=cycle_number,
                    buy_time_utc=buy_time,
                    buy_price=buy_price,
                    buy_trigger_pct=buy_trail_pct,
                )
            )
        # Record the immediately executable liquidation value.  This captures
        # the bid/ask spread as drawdown on the fill event itself.
        append_long_mark(tick)
        return True

    def complete_sell(tick: IbrecTick) -> bool:
        nonlocal capital, last_exit, stage, anchor, buy_price
        nonlocal minimum_profit_pct, sell_trail_pct, sell_fill_reference
        nonlocal cycle_number, completed_trade_count, last_long_mark
        bid = tick.executable_price("SELL")
        if bid is None or buy_price is None or sell_fill_reference is None:
            return False
        sell_price = min(sell_fill_reference, bid)
        capital *= sell_price / buy_price
        return_bps = (sell_price / buy_price - 1.0) * 10_000.0
        last_exit = tick.captured_at_utc
        if keep_trades:
            trade = trades[-1]
            trade.sell_time_utc = last_exit
            trade.sell_price = sell_price
            trade.return_bps = return_bps
            trade.minimum_profit_pct = minimum_profit_pct
            trade.sell_trigger_pct = sell_trail_pct
        completed_trade_count += 1
        stage = "WAIT_READY"
        anchor = None
        buy_price = None
        minimum_profit_pct = None
        sell_trail_pct = None
        sell_fill_reference = None
        last_long_mark = None
        cycle_number += 1
        equity_values.append(capital)
        return True

    for index, tick in enumerate(ticks):
        selected = tick.selected_price()
        atr_pct = atr_values[index]

        # A native stop becomes a market order when triggered.  The historical
        # Last event proves the trigger, but a fill is not modeled until the
        # recording supplies the executable same-side quote.  Quote-only rows
        # can therefore complete a previously triggered order.
        if stage == "BUY_FILL_PENDING":
            complete_buy(tick)
            if stage == "BUY_FILL_PENDING":
                equity_values.append(capital)
            continue
        if stage == "SELL_FILL_PENDING":
            append_long_mark(tick)
            complete_sell(tick)
            continue

        if selected is None:
            if buy_price is not None:
                append_long_mark(tick)
            continue
        can_enter = entry_start <= tick.timestamp <= entry_cutoff
        if stage == "WAIT_READY":
            if can_enter and atr_pct is not None:
                anchor = selected
                stage = "WAIT_DROP"
                equity_values.append(capital)
            continue

        if stage == "WAIT_DROP":
            if not can_enter or atr_pct is None:
                equity_values.append(capital)
                continue
            anchor = selected if anchor is None else max(anchor, selected)
            drop_pct = _effective_percentage(
                atr_pct,
                profile.initial_drop_multiplier,
                profile,
                allow_zero=False,
            )
            buy_pct = _effective_percentage(
                atr_pct,
                profile.buy_rebound_multiplier,
                profile,
                allow_zero=True,
            )
            if drop_pct is None or buy_pct is None:
                equity_values.append(capital)
                continue
            trigger = anchor * (1.0 - drop_pct / 100.0)
            if selected > trigger:
                equity_values.append(capital)
                continue
            buy_trail_pct = buy_pct
            if buy_pct <= 0:
                buy_fill_reference = selected
                stage = "BUY_FILL_PENDING"
                if not complete_buy(tick):
                    equity_values.append(capital)
            else:
                reference_values = [
                    value
                    for value in (
                        selected,
                        tick.valid_ask(),
                        _valid(tick.last),
                        _valid(tick.mark_price),
                    )
                    if value is not None
                ]
                reference = max(reference_values) if reference_values else selected
                buy_stop = _round_increment(
                    reference * (1.0 + buy_pct / 100.0),
                    min_tick,
                    "up",
                )
                buy_running_low = _valid(tick.last) or selected
                buy_placed_sequence = tick.sequence
                stage = "BUY_TRAIL"
                equity_values.append(capital)
            continue

        if stage == "BUY_TRAIL":
            if tick.timestamp >= buy_cancel:
                stage = "WAIT_DROP"
                anchor = None
                buy_stop = None
                buy_running_low = None
                buy_trail_pct = None
                issues.append(
                    "An unfilled BUY trailing setup was treated as cancelled at the standardized five-minute-before-close cutoff."
                )
                equity_values.append(capital)
                continue
            if tick.sequence <= buy_placed_sequence or not tick.has_last_event():
                equity_values.append(capital)
                continue
            last = _valid(tick.last)
            if last is None or buy_trail_pct is None or buy_stop is None:
                equity_values.append(capital)
                continue
            buy_running_low = last if buy_running_low is None else min(buy_running_low, last)
            calculated = buy_running_low * (1.0 + buy_trail_pct / 100.0)
            buy_stop = min(buy_stop, calculated)
            if last < buy_stop:
                equity_values.append(capital)
                continue
            buy_fill_reference = last
            stage = "BUY_FILL_PENDING"
            if not complete_buy(tick):
                equity_values.append(capital)
            continue

        if buy_price is None:
            raise MarketReplayAnalysisError("Internal position state is missing a BUY price.")
        append_long_mark(tick)

        if stage == "HOLD":
            if atr_pct is None:
                continue
            profit_pct = _effective_percentage(
                atr_pct,
                profile.minimum_profit_multiplier,
                profile,
                allow_zero=False,
            )
            trail_pct = _effective_percentage(
                atr_pct,
                profile.sell_trail_multiplier,
                profile,
                allow_zero=True,
            )
            if profit_pct is None or trail_pct is None:
                continue
            minimum_stop = buy_price * (1.0 + profit_pct / 100.0)
            minimum_profit_pct = profit_pct
            sell_trail_pct = trail_pct
            if trail_pct <= 0:
                if selected < minimum_stop:
                    continue
                sell_fill_reference = selected
                stage = "SELL_FILL_PENDING"
                complete_sell(tick)
                continue
            required_price = minimum_stop / (1.0 - trail_pct / 100.0)
            if selected < required_price:
                continue
            reference_values = [
                value
                for value in (
                    selected,
                    tick.valid_bid(),
                    _valid(tick.last),
                    _valid(tick.mark_price),
                )
                if value is not None
            ]
            reference = min(reference_values) if reference_values else selected
            sell_stop = _round_increment(
                reference * (1.0 - trail_pct / 100.0),
                min_tick,
                "down",
            )
            minimum_stop_rounded = _round_increment(minimum_stop, min_tick, "up")
            if sell_stop + 1e-9 < minimum_stop_rounded:
                # BouncyBot's controller rejects a normalized normal-SELL stop
                # that no longer protects the configured minimum-profit floor.
                continue
            sell_running_high = _valid(tick.last) or selected
            sell_placed_sequence = tick.sequence
            stage = "SELL_TRAIL"
            continue

        if stage == "SELL_TRAIL":
            if tick.sequence <= sell_placed_sequence or not tick.has_last_event():
                continue
            last = _valid(tick.last)
            if last is None or sell_trail_pct is None or sell_stop is None:
                continue
            sell_running_high = last if sell_running_high is None else max(sell_running_high, last)
            calculated = sell_running_high * (1.0 - sell_trail_pct / 100.0)
            sell_stop = max(sell_stop, calculated)
            if last > sell_stop:
                continue
            sell_fill_reference = last
            stage = "SELL_FILL_PENDING"
            complete_sell(tick)

    observation_end = max(
        ticks[-1].timestamp if ticks else period.observed_start_timestamp,
        period.observed_end_timestamp,
    )
    if stage == "BUY_TRAIL" and observation_end >= buy_cancel:
        stage = "WAIT_DROP"
        buy_stop = None
        buy_running_low = None
        buy_trail_pct = None
        issues.append(
            "An unfilled BUY trailing setup was treated as cancelled at the standardized five-minute-before-close cutoff."
        )
    open_entry_price = buy_price if stage in {"HOLD", "SELL_TRAIL", "SELL_FILL_PENDING"} else None
    open_position = open_entry_price is not None
    open_entry_setup = stage in {"BUY_TRAIL", "BUY_FILL_PENDING"}
    future_entry_possible = (
        stage in {"WAIT_READY", "WAIT_DROP"} and observation_end < entry_cutoff
    )
    right_censored = open_position or open_entry_setup or future_entry_possible
    realized_return = (capital - 1.0) * 10_000.0
    marked_return = realized_return
    unmarked_open_position = False
    if open_entry_price is not None:
        if last_long_mark is not None:
            marked_return = (
                capital * (last_long_mark / open_entry_price) - 1.0
            ) * 10_000.0
        else:
            unmarked_open_position = True
            issues.append(
                "No valid bid was recorded after the open BUY, so the unresolved long position could not be marked conservatively."
            )
        if keep_trades and trades:
            trades[-1].open_at_end = True
        issues.append(
            "A position remained open when the recorded session ended; its outcome is right-censored."
        )
    elif open_entry_setup:
        issues.append(
            "A BUY trailing or triggered market setup remained unresolved when the recording ended; its entry outcome is right-censored."
        )
    elif future_entry_possible:
        issues.append(
            "The recording ended before the standardized entry cutoff; a later setup or additional cycle remains unknown."
        )
    conservative = min(realized_return, marked_return) if open_position else realized_return
    trades_count = completed_trade_count + (1 if open_position else 0)
    return (
        MarketReplaySessionResult(
            session_date=period.session_date,
            period_id=period.period_id,
            scheduled_open_utc=period.schedule_open_utc,
            scheduled_close_utc=period.schedule_close_utc,
            observed_start_utc=period.observed_start_utc,
            observed_end_utc=period.observed_end_utc,
            ticks=len(ticks),
            trades=trades_count,
            completed_trades=completed_trade_count,
            no_trade=trades_count == 0,
            open_position=open_position,
            realized_return_bps=realized_return,
            marked_return_bps=marked_return,
            conservative_return_bps=conservative,
            max_drawdown_bps=_max_drawdown(equity_values),
            first_entry_utc=first_entry,
            last_exit_utc=last_exit,
            open_entry_setup=open_entry_setup,
            right_censored=right_censored,
            unmarked_open_position=unmarked_open_position,
            issues=issues,
        ),
        trades,
    )

def _summary(
    profile: AtrProfile,
    sessions: list[MarketReplaySessionResult],
) -> MarketReplayCandidateSummary:
    returns = [item.conservative_return_bps for item in sessions]
    session_count = len(sessions)
    if not returns:
        return MarketReplayCandidateSummary(
            profile=profile,
            score=-math.inf,
            sessions=0,
            completed_sessions=0,
            sessions_with_trades=0,
            completed_trades=0,
            open_position_sessions=0,
            no_trade_sessions=0,
            median_return_bps=0.0,
            mean_return_bps=0.0,
            worst_return_bps=0.0,
            maximum_drawdown_bps=0.0,
            open_position_rate_pct=0.0,
            no_trade_rate_pct=0.0,
            right_censored_sessions=0,
            right_censored_rate_pct=0.0,
            unmarked_open_position_sessions=0,
            unmarked_open_position_rate_pct=0.0,
            instability_reasons=["No analyzable RTH session was available."],
        )
    median_return = float(statistics.median(returns))
    mean_return = float(statistics.fmean(returns))
    worst_return = min(returns)
    max_drawdown = max(item.max_drawdown_bps for item in sessions)
    open_count = sum(1 for item in sessions if item.open_position)
    no_trade_count = sum(1 for item in sessions if item.no_trade)
    right_censored_count = sum(1 for item in sessions if item.right_censored)
    unmarked_open_count = sum(
        1 for item in sessions if item.unmarked_open_position
    )
    open_fraction = open_count / session_count
    no_trade_fraction = no_trade_count / session_count
    right_censored_fraction = right_censored_count / session_count
    score = (
        0.50 * median_return
        + 0.30 * mean_return
        + 0.20 * worst_return
        - 0.35 * max_drawdown
        - 25.0 * open_fraction
        - 10.0 * no_trade_fraction
        - 15.0 * right_censored_fraction
    )
    return MarketReplayCandidateSummary(
        profile=profile,
        score=score,
        sessions=session_count,
        completed_sessions=sum(1 for item in sessions if not item.right_censored),
        sessions_with_trades=sum(1 for item in sessions if not item.no_trade),
        completed_trades=sum(item.completed_trades for item in sessions),
        open_position_sessions=open_count,
        no_trade_sessions=no_trade_count,
        median_return_bps=median_return,
        mean_return_bps=mean_return,
        worst_return_bps=worst_return,
        maximum_drawdown_bps=max_drawdown,
        open_position_rate_pct=open_fraction * 100.0,
        no_trade_rate_pct=no_trade_fraction * 100.0,
        right_censored_sessions=right_censored_count,
        right_censored_rate_pct=right_censored_fraction * 100.0,
        unmarked_open_position_sessions=unmarked_open_count,
        unmarked_open_position_rate_pct=unmarked_open_count / session_count * 100.0,
    )


def _control_profile(config: MarketReplayConfig) -> AtrProfile:
    normalized = config.normalized()
    return AtrProfile(
        period=_CONTROL_PROFILE.period,
        bar_seconds=_CONTROL_PROFILE.bar_seconds,
        initial_drop_multiplier=_CONTROL_PROFILE.initial_drop_multiplier,
        buy_rebound_multiplier=_CONTROL_PROFILE.buy_rebound_multiplier,
        minimum_profit_multiplier=_CONTROL_PROFILE.minimum_profit_multiplier,
        sell_trail_multiplier=_CONTROL_PROFILE.sell_trail_multiplier,
        min_atr_pct=normalized.min_atr_pct,
        max_atr_pct=normalized.max_atr_pct,
    )


def _window_profile(
    config: MarketReplayConfig,
    period: int,
    bar_seconds: int,
) -> AtrProfile:
    control = _control_profile(config)
    return AtrProfile(
        period=period,
        bar_seconds=bar_seconds,
        initial_drop_multiplier=control.initial_drop_multiplier,
        buy_rebound_multiplier=control.buy_rebound_multiplier,
        minimum_profit_multiplier=control.minimum_profit_multiplier,
        sell_trail_multiplier=control.sell_trail_multiplier,
        min_atr_pct=control.min_atr_pct,
        max_atr_pct=control.max_atr_pct,
    )


def _coarse_profiles(
    config: MarketReplayConfig,
    windows: Iterable[tuple[int, int]] | None = None,
) -> list[AtrProfile]:
    normalized = config.normalized()
    selected_windows = tuple(sorted(set(windows or ((_CONTROL_PROFILE.period, _CONTROL_PROFILE.bar_seconds),))))
    profiles = {
        AtrProfile(
            period=period,
            bar_seconds=bar_seconds,
            initial_drop_multiplier=initial,
            buy_rebound_multiplier=buy,
            minimum_profit_multiplier=profit,
            sell_trail_multiplier=sell,
            min_atr_pct=normalized.min_atr_pct,
            max_atr_pct=normalized.max_atr_pct,
        )
        for (period, bar_seconds), initial, buy, profit, sell in itertools.product(
            selected_windows,
            _COARSE_INITIAL,
            _COARSE_BUY,
            _COARSE_PROFIT,
            _COARSE_SELL,
        )
    }
    profiles.add(_control_profile(config))
    return sorted(profiles, key=lambda profile: profile.key())


def _neighbor_values(value: float, *, allow_zero: bool) -> tuple[float, ...]:
    minimum = 0.0 if allow_zero else 0.25
    values = {
        round(max(minimum, min(5.0, value + offset)), 2)
        for offset in (-_REFINEMENT_STEP, 0.0, _REFINEMENT_STEP)
    }
    return tuple(sorted(values))


def _refined_profiles(
    seeds: list[MarketReplayCandidateSummary],
    config: MarketReplayConfig,
) -> list[AtrProfile]:
    normalized = config.normalized()
    profiles: set[AtrProfile] = set()
    for seed in seeds:
        profile = seed.profile
        for initial, buy, profit, sell in itertools.product(
            _neighbor_values(profile.initial_drop_multiplier, allow_zero=False),
            _neighbor_values(profile.buy_rebound_multiplier, allow_zero=True),
            _neighbor_values(profile.minimum_profit_multiplier, allow_zero=False),
            _neighbor_values(profile.sell_trail_multiplier, allow_zero=True),
        ):
            profiles.add(
                AtrProfile(
                    period=profile.period,
                    bar_seconds=profile.bar_seconds,
                    initial_drop_multiplier=initial,
                    buy_rebound_multiplier=buy,
                    minimum_profit_multiplier=profit,
                    sell_trail_multiplier=sell,
                    min_atr_pct=normalized.min_atr_pct,
                    max_atr_pct=normalized.max_atr_pct,
                )
            )
    return sorted(profiles, key=lambda profile: profile.key())


def _refinement_seeds(
    candidates: Iterable[MarketReplayCandidateSummary],
    *,
    maximum: int = _REFINEMENT_SEEDS,
) -> list[MarketReplayCandidateSummary]:
    """Allocate refinement seeds across the selected ATR windows.

    A pure top-N selection can spend every refinement slot inside one ATR
    window and leave the other windows at coarse multiplier resolution.  This
    round-robin allocation gives each selected period/bar window an equal first
    opportunity, then fills remaining slots by within-window rank.
    """

    groups: dict[tuple[int, int], list[MarketReplayCandidateSummary]] = defaultdict(list)
    for candidate in sorted(candidates, key=_candidate_sort_key):
        groups[(candidate.profile.period, candidate.profile.bar_seconds)].append(candidate)
    windows = sorted(
        groups,
        key=lambda window: (
            _candidate_sort_key(groups[window][0]),
            window,
        ),
    )
    selected: list[MarketReplayCandidateSummary] = []
    depth = 0
    while len(selected) < maximum:
        added = False
        for window in windows:
            values = groups[window]
            if depth < len(values):
                selected.append(values[depth])
                added = True
                if len(selected) >= maximum:
                    break
        if not added:
            break
        depth += 1
    return selected


def _quantile(values: Iterable[float], probability: float) -> float | None:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    probability = max(0.0, min(1.0, float(probability)))
    position = probability * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _deterministic_bootstrap_indices(
    seed: str,
    *,
    replicates: int,
    draws: int,
    size: int,
) -> Iterable[tuple[int, ...]]:
    """Yield deterministic bootstrap draws from one reproducible hash stream."""

    if replicates <= 0 or draws <= 0 or size <= 0:
        return
    stream = hashlib.shake_256(
        (
            "market-replay-bootstrap-v1|"
            f"{seed}|{replicates}|{draws}|{size}"
        ).encode("utf-8")
    ).digest(replicates * draws * 8)
    values = struct.iter_unpack(">Q", stream)
    for _ in range(replicates):
        yield tuple(next(values)[0] % size for _ in range(draws))


def _session_index(
    sessions: Iterable[MarketReplaySessionResult],
) -> dict[tuple[str, int], MarketReplaySessionResult]:
    indexed: dict[tuple[str, int], MarketReplaySessionResult] = {}
    for session in sessions:
        key = (session.session_date, session.period_id)
        if key in indexed:
            raise MarketReplayAnalysisError(
                f"Duplicate Market Replay session evidence for {session.session_date} period {session.period_id}."
            )
        indexed[key] = session
    return indexed


def _paired_sessions_by_day(
    candidate_sessions: Iterable[MarketReplaySessionResult],
    control_sessions: Iterable[MarketReplaySessionResult],
) -> dict[str, list[tuple[MarketReplaySessionResult, MarketReplaySessionResult]]]:
    candidate_index = _session_index(candidate_sessions)
    control_index = _session_index(control_sessions)
    if set(candidate_index) != set(control_index):
        raise MarketReplayAnalysisError(
            "Candidate and control do not contain the same RTH session identities."
        )
    pairs: dict[str, list[tuple[MarketReplaySessionResult, MarketReplaySessionResult]]] = defaultdict(list)
    for key in sorted(candidate_index):
        day = key[0]
        pairs[day].append((candidate_index[key], control_index[key]))
    return dict(sorted(pairs.items()))


def _score_delta_for_pairs(
    candidate_profile: AtrProfile,
    control_profile: AtrProfile,
    pairs: Iterable[tuple[MarketReplaySessionResult, MarketReplaySessionResult]],
) -> float | None:
    materialized = list(pairs)
    if not materialized:
        return None
    candidate = _summary(candidate_profile, [left for left, _ in materialized])
    control = _summary(control_profile, [right for _, right in materialized])
    delta = candidate.score - control.score
    return delta if math.isfinite(delta) else None


def _candidate_robustness(
    candidate: MarketReplayCandidateSummary,
    candidate_sessions: list[MarketReplaySessionResult],
    control: MarketReplayCandidateSummary,
    control_sessions: list[MarketReplaySessionResult],
    *,
    seed: str,
    selection_rows: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    pairs_by_day = _paired_sessions_by_day(candidate_sessions, control_sessions)
    days = sorted(pairs_by_day)
    all_pairs = [pair for day in days for pair in pairs_by_day[day]]
    observed_delta = _score_delta_for_pairs(candidate.profile, control.profile, all_pairs)
    paired_return_deltas = [
        float(statistics.fmean(left.conservative_return_bps for left, _ in pairs_by_day[day]))
        - float(statistics.fmean(right.conservative_return_bps for _, right in pairs_by_day[day]))
        for day in days
    ]
    paired_median_return_delta = (
        float(statistics.median(paired_return_deltas))
        if paired_return_deltas
        else None
    )
    paired_positive_day_pct = (
        sum(1 for value in paired_return_deltas if value > 0.0)
        / len(paired_return_deltas)
        * 100.0
        if paired_return_deltas
        else None
    )
    candidate_trade_days = sum(
        1
        for day in days
        if any(not left.no_trade for left, _ in pairs_by_day[day])
    )
    control_trade_days = sum(
        1
        for day in days
        if any(not right.no_trade for _, right in pairs_by_day[day])
    )
    control_trade_day_retention_pct = (
        candidate_trade_days / control_trade_days * 100.0
        if control_trade_days
        else 100.0
    )
    candidate_paired_summary = _summary(
        candidate.profile,
        [left for left, _ in all_pairs],
    ) if all_pairs else None
    control_paired_summary = _summary(
        control.profile,
        [right for _, right in all_pairs],
    ) if all_pairs else None
    maximum_drawdown_delta = (
        candidate_paired_summary.maximum_drawdown_bps
        - control_paired_summary.maximum_drawdown_bps
        if candidate_paired_summary is not None and control_paired_summary is not None
        else None
    )
    worst_return_delta = (
        candidate_paired_summary.worst_return_bps
        - control_paired_summary.worst_return_bps
        if candidate_paired_summary is not None and control_paired_summary is not None
        else None
    )

    bootstrap_values: list[float] = []
    if len(days) >= 3:
        for sampled_indices in _deterministic_bootstrap_indices(
            seed,
            replicates=_BOOTSTRAP_REPLICATES,
            draws=len(days),
            size=len(days),
        ):
            sample: list[tuple[MarketReplaySessionResult, MarketReplaySessionResult]] = []
            for sampled_index in sampled_indices:
                selected_day = days[sampled_index]
                sample.extend(pairs_by_day[selected_day])
            delta = _score_delta_for_pairs(candidate.profile, control.profile, sample)
            if delta is not None:
                bootstrap_values.append(delta)

    leave_one_out_rows: list[dict[str, Any]] = []
    if len(days) >= 3:
        for omitted_day in days:
            remaining = [
                pair
                for day in days
                if day != omitted_day
                for pair in pairs_by_day[day]
            ]
            delta = _score_delta_for_pairs(candidate.profile, control.profile, remaining)
            leave_one_out_rows.append(
                {
                    "candidate_profile_key": candidate.profile.key(),
                    "control_profile_key": control.profile.key(),
                    "omitted_trading_day": omitted_day,
                    "remaining_trading_days": len(days) - 1,
                    "score_delta": delta,
                }
            )

    loo_values = [
        float(row["score_delta"])
        for row in leave_one_out_rows
        if row["score_delta"] is not None and math.isfinite(float(row["score_delta"]))
    ]
    influential_day = ""
    largest_change: float | None = None
    if observed_delta is not None:
        for row in leave_one_out_rows:
            value = row["score_delta"]
            if value is None:
                continue
            change = abs(float(value) - observed_delta)
            day = str(row["omitted_trading_day"])
            if largest_change is None or change > largest_change or (
                math.isclose(change, largest_change) and day < influential_day
            ):
                influential_day = day
                largest_change = change
    observed_sign = 0 if observed_delta is None or math.isclose(observed_delta, 0.0) else (1 if observed_delta > 0 else -1)
    sign_reversals = sum(
        1
        for value in loo_values
        if observed_sign and not math.isclose(value, 0.0) and (1 if value > 0 else -1) != observed_sign
    )

    ci80_low = _quantile(bootstrap_values, 0.10)
    ci80_high = _quantile(bootstrap_values, 0.90)
    ci95_low = _quantile(bootstrap_values, 0.025)
    ci95_high = _quantile(bootstrap_values, 0.975)
    probability_positive = (
        sum(1 for value in bootstrap_values if value > 0.0)
        / len(bootstrap_values)
        * 100.0
        if bootstrap_values
        else None
    )
    loo_min = min(loo_values) if loo_values else None
    loo_median = float(statistics.median(loo_values)) if loo_values else None
    loo_max = max(loo_values) if loo_values else None
    loo_positive = (
        sum(1 for value in loo_values if value > 0.0) / len(loo_values) * 100.0
        if loo_values
        else None
    )

    selection = _selection_stability(candidate.profile, selection_rows or [])
    selection_by_day = {
        str(row.get("omitted_trading_day")): row
        for row in selection_rows or []
    }
    for row in leave_one_out_rows:
        selection_row = selection_by_day.get(str(row["omitted_trading_day"]))
        if selection_row is None:
            continue
        row.update(
            {
                "selected_stage1_bar_seconds": selection_row.get(
                    "selected_stage1_bar_seconds"
                ),
                "selected_windows": selection_row.get("selected_windows"),
                "selected_profile_key": selection_row.get("selected_profile_key"),
                "selected_period": selection_row.get("selected_period"),
                "selected_profile_bar_seconds": selection_row.get(
                    "selected_profile_bar_seconds"
                ),
                "selected_score": selection_row.get("selected_score"),
                "selected_score_delta_vs_control": selection_row.get(
                    "selected_score_delta_vs_control"
                ),
                "selection_reason": selection_row.get("selection_reason"),
            }
        )

    failure_reasons: list[str] = []
    if len(days) < _MIN_ROBUST_TRADING_DAYS:
        failure_reasons.append(
            f"Fewer than {_MIN_ROBUST_TRADING_DAYS} paired RTH trading days were available."
        )
    if observed_delta is None or observed_delta < _MIN_PRACTICAL_SCORE_DELTA:
        failure_reasons.append(
            "The candidate's paired full-sample score improvement is below the minimum practical threshold."
        )
    if paired_median_return_delta is None or paired_median_return_delta <= 0:
        failure_reasons.append(
            "The median same-day conservative return is not above the unchanged control."
        )
    if (
        candidate_paired_summary is None
        or candidate_paired_summary.median_return_bps <= 0
        or candidate_paired_summary.mean_return_bps <= 0
    ):
        failure_reasons.append(
            "The candidate does not have both a positive median and positive mean conservative session return."
        )
    if (
        candidate_paired_summary is None
        or control_paired_summary is None
        or candidate_paired_summary.unmarked_open_position_sessions
        or control_paired_summary.unmarked_open_position_sessions
    ):
        failure_reasons.append(
            "At least one paired candidate/control session contains an open long position without a valid bid-side end mark."
        )
    if (
        candidate_paired_summary is None
        or control_paired_summary is None
        or candidate_paired_summary.right_censored_sessions
        or control_paired_summary.right_censored_sessions
    ):
        failure_reasons.append(
            "At least one paired candidate/control session is right-censored; a changed profile requires fully observed outcomes on every paired session."
        )
    if (
        paired_positive_day_pct is None
        or paired_positive_day_pct < _MIN_PAIRED_POSITIVE_DAY_PCT
    ):
        failure_reasons.append(
            "The candidate improves conservative return on fewer than "
            f"{_MIN_PAIRED_POSITIVE_DAY_PCT:.0f}% of paired trading days."
        )
    if control_trade_day_retention_pct < _MIN_CONTROL_TRADE_DAY_RETENTION_PCT:
        failure_reasons.append(
            "The candidate retains trades on fewer than "
            f"{_MIN_CONTROL_TRADE_DAY_RETENTION_PCT:.0f}% of the control's trading days."
        )
    if (
        maximum_drawdown_delta is None
        or maximum_drawdown_delta > _MAX_DRAWDOWN_DETERIORATION_BPS
    ):
        failure_reasons.append(
            "Maximum drawdown deteriorates beyond the allowed candidate-versus-control tolerance."
        )
    if (
        worst_return_delta is None
        or worst_return_delta < -_MAX_WORST_RETURN_DETERIORATION_BPS
    ):
        failure_reasons.append(
            "The worst session deteriorates beyond the allowed candidate-versus-control tolerance."
        )
    if ci80_low is None or ci80_low <= 0:
        failure_reasons.append(
            "The trading-day bootstrap 80% interval does not remain entirely above zero."
        )
    if probability_positive is None or probability_positive < _MIN_BOOTSTRAP_POSITIVE_PCT:
        failure_reasons.append(
            "Fewer than "
            f"{_MIN_BOOTSTRAP_POSITIVE_PCT:.0f}% of trading-day bootstrap replicates favor the candidate."
        )
    if len(loo_values) != len(days):
        failure_reasons.append(
            "Leave-one-day-out evidence is incomplete for one or more trading days."
        )
    if loo_min is None or loo_min <= 0:
        failure_reasons.append(
            "At least one leave-one-day-out score delta is zero, negative, or unavailable."
        )
    if sign_reversals:
        failure_reasons.append(
            "Removing one trading day reverses the sign of the candidate-versus-control result."
        )
    if selection_rows is not None:
        if selection["selection_runs"] != len(days):
            failure_reasons.append(
                "Leave-one-day-out three-stage selection evidence is incomplete."
            )
        window_pct = selection["same_atr_window_selection_pct"]
        if (
            window_pct is None
            or window_pct < _MIN_LOO_WINDOW_SELECTION_PCT
        ):
            failure_reasons.append(
                "The same ATR period/bar window advances through stage 2 in fewer than "
                f"{_MIN_LOO_WINDOW_SELECTION_PCT:.0f}% of leave-one-day-out reruns."
            )

    evidence = {
        "candidate_profile_key": candidate.profile.key(),
        "control_profile_key": control.profile.key(),
        "paired_sessions": len(all_pairs),
        "paired_trading_days": len(days),
        "observed_score_delta": observed_delta,
        "paired_median_return_delta_bps": paired_median_return_delta,
        "paired_positive_day_pct": paired_positive_day_pct,
        "candidate_trade_days": candidate_trade_days,
        "control_trade_days": control_trade_days,
        "control_trade_day_retention_pct": control_trade_day_retention_pct,
        "maximum_drawdown_delta_bps": maximum_drawdown_delta,
        "worst_return_delta_bps": worst_return_delta,
        "candidate_paired_median_return_bps": (
            candidate_paired_summary.median_return_bps
            if candidate_paired_summary is not None
            else None
        ),
        "candidate_paired_mean_return_bps": (
            candidate_paired_summary.mean_return_bps
            if candidate_paired_summary is not None
            else None
        ),
        "bootstrap_replicates": len(bootstrap_values),
        "bootstrap_ci80_low": ci80_low,
        "bootstrap_ci80_high": ci80_high,
        "bootstrap_ci95_low": ci95_low,
        "bootstrap_ci95_high": ci95_high,
        "bootstrap_probability_positive_pct": probability_positive,
        "leave_one_day_out_estimates": len(loo_values),
        "leave_one_day_out_min_delta": loo_min,
        "leave_one_day_out_median_delta": loo_median,
        "leave_one_day_out_max_delta": loo_max,
        "leave_one_day_out_positive_pct": loo_positive,
        "leave_one_day_out_sign_reversals": sign_reversals,
        "leave_one_day_out_most_influential_day": influential_day,
        "leave_one_day_out_largest_change": largest_change,
        **selection,
        "passed": not failure_reasons,
        "failure_reasons": failure_reasons,
    }
    return evidence, leave_one_out_rows


def _market_replay_bootstrap_seed(
    recording_sha256: str,
    control_profile: AtrProfile,
) -> str:
    """Return one candidate-independent resample seed for the recording.

    Every stable-region center is compared on the same deterministic sequence
    of whole-day resamples. Giving each candidate a different resample schedule
    would add avoidable Monte Carlo noise to the decision about which profile
    survives the robustness gate.
    """

    return (
        f"market-replay-v{MARKET_REPLAY_ANALYSIS_CONTRACT_VERSION}|"
        f"{recording_sha256}|{control_profile.key()}|shared-trading-day-bootstrap"
    )


def _apply_robustness(
    candidate: MarketReplayCandidateSummary,
    evidence: dict[str, Any],
) -> None:
    candidate.robustness_evaluated = True
    candidate.robustness_passed = bool(evidence.get("passed"))
    candidate.control_score_delta = evidence.get("observed_score_delta")
    candidate.paired_trading_days = int(evidence.get("paired_trading_days") or 0)
    candidate.bootstrap_replicates = int(evidence.get("bootstrap_replicates") or 0)
    candidate.bootstrap_ci80_low = evidence.get("bootstrap_ci80_low")
    candidate.bootstrap_ci80_high = evidence.get("bootstrap_ci80_high")
    candidate.bootstrap_ci95_low = evidence.get("bootstrap_ci95_low")
    candidate.bootstrap_ci95_high = evidence.get("bootstrap_ci95_high")
    candidate.bootstrap_probability_positive_pct = evidence.get(
        "bootstrap_probability_positive_pct"
    )
    candidate.leave_one_day_out_estimates = int(
        evidence.get("leave_one_day_out_estimates") or 0
    )
    candidate.leave_one_day_out_min_delta = evidence.get(
        "leave_one_day_out_min_delta"
    )
    candidate.leave_one_day_out_median_delta = evidence.get(
        "leave_one_day_out_median_delta"
    )
    candidate.leave_one_day_out_max_delta = evidence.get(
        "leave_one_day_out_max_delta"
    )
    candidate.leave_one_day_out_positive_pct = evidence.get(
        "leave_one_day_out_positive_pct"
    )
    candidate.leave_one_day_out_sign_reversals = int(
        evidence.get("leave_one_day_out_sign_reversals") or 0
    )
    candidate.leave_one_day_out_most_influential_day = str(
        evidence.get("leave_one_day_out_most_influential_day") or ""
    )
    candidate.leave_one_day_out_largest_change = evidence.get(
        "leave_one_day_out_largest_change"
    )
    candidate.leave_one_day_out_selection_runs = int(
        evidence.get("selection_runs") or 0
    )
    candidate.leave_one_day_out_exact_profile_selections = int(
        evidence.get("exact_profile_selections") or 0
    )
    candidate.leave_one_day_out_exact_profile_selection_pct = evidence.get(
        "exact_profile_selection_pct"
    )
    candidate.leave_one_day_out_same_window_selections = int(
        evidence.get("same_atr_window_selections") or 0
    )
    candidate.leave_one_day_out_same_window_selection_pct = evidence.get(
        "same_atr_window_selection_pct"
    )
    candidate.paired_median_return_delta_bps = evidence.get(
        "paired_median_return_delta_bps"
    )
    candidate.paired_positive_day_pct = evidence.get("paired_positive_day_pct")
    candidate.control_trade_day_retention_pct = evidence.get(
        "control_trade_day_retention_pct"
    )
    candidate.maximum_drawdown_delta_bps = evidence.get(
        "maximum_drawdown_delta_bps"
    )
    candidate.worst_return_delta_bps = evidence.get("worst_return_delta_bps")
    candidate.atr_phase_cases = int(evidence.get("atr_phase_cases") or 0)
    candidate.atr_phase_min_score_delta = evidence.get(
        "atr_phase_min_score_delta"
    )
    candidate.atr_phase_adverse_score_delta = evidence.get(
        "atr_phase_adverse_score_delta"
    )
    candidate.instability_reasons.extend(
        str(reason) for reason in evidence.get("failure_reasons") or []
    )


def _adjacent(left: AtrProfile, right: AtrProfile) -> bool:
    if (left.period, left.bar_seconds) != (right.period, right.bar_seconds):
        return False
    differences = [
        abs(left.initial_drop_multiplier - right.initial_drop_multiplier),
        abs(left.buy_rebound_multiplier - right.buy_rebound_multiplier),
        abs(left.minimum_profit_multiplier - right.minimum_profit_multiplier),
        abs(left.sell_trail_multiplier - right.sell_trail_multiplier),
    ]
    changed = [value for value in differences if value > 1e-9]
    return len(changed) == 1 and changed[0] <= _REFINEMENT_STEP + 1e-9


def _stable_region_centers(
    candidates: list[MarketReplayCandidateSummary],
    *,
    sessions: int,
) -> list[tuple[MarketReplayCandidateSummary, str]]:
    ranked = sorted(candidates, key=_candidate_sort_key)
    if not ranked or all(candidate.sessions_with_trades == 0 for candidate in ranked):
        return []
    best = ranked[0]
    tolerance = max(2.0, abs(best.score) * 0.10)
    near = [candidate for candidate in ranked if candidate.score >= best.score - tolerance]
    for candidate in near:
        candidate.near_best = True

    # Build the grid graph without comparing every candidate with every other
    # candidate. The previous breadth-first implementation scanned all remaining
    # profiles for each visited profile, which became quadratic when a broad
    # near-best plateau contained hundreds of refined candidates. Grouping by
    # the three unchanged multiplier dimensions gives exactly the same edges as
    # ``_adjacent`` while keeping the operation close to O(n log n).
    by_key: dict[str, int] = {}
    for index, candidate in enumerate(near):
        key = candidate.profile.key()
        if key in by_key:
            raise MarketReplayAnalysisError(
                f"Duplicate Market Replay candidate profile in stable-region search: {key}"
            )
        by_key[key] = index

    parent = list(range(len(near)))
    rank = [0] * len(near)

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if rank[left_root] < rank[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        if rank[left_root] == rank[right_root]:
            rank[left_root] += 1

    dimensions = (
        "initial_drop_multiplier",
        "buy_rebound_multiplier",
        "minimum_profit_multiplier",
        "sell_trail_multiplier",
    )
    for dimension in dimensions:
        grouped: dict[tuple[Any, ...], list[tuple[float, int]]] = defaultdict(list)
        other_dimensions = tuple(name for name in dimensions if name != dimension)
        for index, candidate in enumerate(near):
            profile = candidate.profile
            signature = (
                profile.period,
                profile.bar_seconds,
                *(round(float(getattr(profile, name)), 9) for name in other_dimensions),
            )
            grouped[signature].append((float(getattr(profile, dimension)), index))
        for values in grouped.values():
            values.sort(key=lambda item: (item[0], near[item[1]].profile.key()))
            for left_position, (left_value, left_index) in enumerate(values):
                for right_value, right_index in values[left_position + 1 :]:
                    difference = right_value - left_value
                    if difference > _REFINEMENT_STEP + 1e-9:
                        break
                    if difference > 1e-9:
                        union(left_index, right_index)

    component_map: dict[int, list[MarketReplayCandidateSummary]] = defaultdict(list)
    for index, candidate in enumerate(near):
        component_map[find(index)].append(candidate)
    components = [
        sorted(component, key=lambda item: item.profile.key())
        for component in component_map.values()
    ]

    stable_components = [component for component in components if len(component) >= 3]
    stable_components.sort(
        key=lambda component: (
            -min(item.score for item in component),
            -float(statistics.median(item.score for item in component)),
            -len(component),
            min(_profile_distance(item.profile) for item in component),
            min(item.profile.key() for item in component),
        )
    )
    selections: list[tuple[MarketReplayCandidateSummary, str]] = []
    for component in stable_components:
        region_id = "REGION-" + hashlib.sha256(
            "|".join(sorted(item.profile.key() for item in component)).encode("utf-8")
        ).hexdigest()[:10].upper()
        centers = {
            name: float(statistics.median(getattr(item.profile, name) for item in component))
            for name in dimensions
        }
        ordered_component = sorted(
            component,
            key=lambda item: (
                sum(abs(getattr(item.profile, name) - centers[name]) for name in dimensions),
                -item.score,
                _profile_distance(item.profile),
                item.profile.key(),
            ),
        )
        selected = ordered_component[0]
        for item in component:
            item.stable_region_id = region_id
            item.stable_region_size = len(component)
        selected.stable_region_center = True
        selected.evidence_stable = (
            sessions >= 5
            and selected.completed_trades >= 3
            and selected.sessions_with_trades >= 3
            and selected.right_censored_rate_pct <= 20.0
            and selected.unmarked_open_position_sessions == 0
        )
        if sessions < 5:
            selected.instability_reasons.append(
                "Fewer than five recorded RTH sessions were available; the region is locally stable but not a robust multi-day result."
            )
        if selected.completed_trades < 3:
            selected.instability_reasons.append(
                "The selected region produced fewer than three completed simulated trades."
            )
        if selected.sessions_with_trades < 3:
            selected.instability_reasons.append(
                "The selected region produced trades in fewer than three recorded RTH sessions."
            )
        if selected.right_censored_rate_pct > 20.0:
            selected.instability_reasons.append(
                "More than 20% of the selected profile's session outcomes are right-censored."
            )
        if selected.unmarked_open_position_sessions:
            selected.instability_reasons.append(
                "At least one open long position lacked a valid bid-side end mark."
            )
        selections.append(
            (
                selected,
                f"Selected the deterministic center of {region_id}, a connected near-best region containing "
                f"{len(component)} adjacent profiles.",
            )
        )
    return selections


def _stable_recommendation(
    candidates: list[MarketReplayCandidateSummary],
    *,
    sessions: int,
    control_profile: AtrProfile,
) -> tuple[MarketReplayCandidateSummary, str]:
    ranked = sorted(candidates, key=_candidate_sort_key)
    if not ranked:
        raise MarketReplayAnalysisError("The Market Replay candidate search returned no results.")
    if all(candidate.sessions_with_trades == 0 for candidate in ranked):
        control = next(
            (candidate for candidate in ranked if candidate.profile.key() == control_profile.key()),
            ranked[0],
        )
        control.instability_reasons.extend(
            [
                "No candidate produced a simulated trade in the recorded data.",
                "The unchanged BouncyBot default profile is shown as a fallback, not as an optimized result.",
            ]
        )
        if control.right_censored_rate_pct > 20.0:
            control.instability_reasons.append(
                "More than 20% of the fallback profile's session outcomes are right-censored."
            )
        return control, (
            "The recording did not contain enough warm-up and price movement for any candidate to complete a trade. "
            "The unchanged BouncyBot default profile is therefore the single fallback setting to evaluate."
        )
    selections = _stable_region_centers(candidates, sessions=sessions)
    if selections:
        return selections[0]
    best = ranked[0]
    best.instability_reasons.append(
        "The highest-scoring profile was an isolated grid point; no supported adjacent near-best region was found."
    )
    if sessions < 5:
        best.instability_reasons.append("Fewer than five recorded RTH sessions were available.")
    if best.right_censored_rate_pct > 20.0:
        best.instability_reasons.append(
            "More than 20% of the highest-scoring profile's session outcomes are right-censored."
        )
    return best, (
        "No adjacent near-best parameter region was supported, so the highest-scoring bounded-grid profile is shown "
        "with an explicit instability warning."
    )


def _evaluate_profile(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    atr_cache: dict[tuple[int, int, int], list[float | None]],
    profile: AtrProfile,
    *,
    keep_details: bool,
) -> tuple[MarketReplayCandidateSummary, list[MarketReplaySessionResult], list[MarketReplayTrade]]:
    sessions: list[MarketReplaySessionResult] = []
    trades: list[MarketReplayTrade] = []
    for session_index, (period, ticks) in enumerate(period_ticks):
        atr_values = atr_cache[(session_index, profile.period, profile.bar_seconds)]
        result, session_trades = _simulate_session(
            ticks,
            period,
            profile,
            atr_values,
            recording.min_tick,
            keep_trades=keep_details,
        )
        sessions.append(result)
        if keep_details:
            trades.extend(session_trades)
    return _summary(profile, sessions), sessions, trades


def _evaluate_profile_phase(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    profile: AtrProfile,
    phase_seconds: int,
) -> list[MarketReplaySessionResult]:
    sessions: list[MarketReplaySessionResult] = []
    for period, ticks in period_ticks:
        atr_values = _precompute_atr(
            ticks,
            profile.period,
            profile.bar_seconds,
            phase_seconds=phase_seconds,
        )
        result, _ = _simulate_session(
            ticks,
            period,
            profile,
            atr_values,
            recording.min_tick,
            keep_trades=False,
        )
        sessions.append(result)
    return sessions


def _phase_values(bar_seconds: int) -> tuple[int, ...]:
    return tuple(range(0, max(_ATR_PHASE_STEP_SECONDS, bar_seconds), _ATR_PHASE_STEP_SECONDS))


def _atr_phase_robustness(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    candidate: AtrProfile,
    control: AtrProfile,
    cache: dict[tuple[str, int], list[MarketReplaySessionResult]],
) -> dict[str, Any]:
    """Stress candidate/control ATR buckets across plausible monotonic phases."""

    def sessions(profile: AtrProfile, phase: int) -> list[MarketReplaySessionResult]:
        key = (profile.key(), phase)
        if key not in cache:
            cache[key] = _evaluate_profile_phase(
                recording,
                period_ticks,
                profile,
                phase,
            )
        return cache[key]

    candidate_phases = _phase_values(candidate.bar_seconds)
    control_phases = _phase_values(control.bar_seconds)
    cases: list[dict[str, Any]] = []
    session_adverse: dict[tuple[str, int], tuple[MarketReplaySessionResult, MarketReplaySessionResult, float]] = {}
    for candidate_phase in candidate_phases:
        candidate_sessions = sessions(candidate, candidate_phase)
        for control_phase in control_phases:
            control_sessions = sessions(control, control_phase)
            pairs_by_day = _paired_sessions_by_day(candidate_sessions, control_sessions)
            pairs = [pair for day in sorted(pairs_by_day) for pair in pairs_by_day[day]]
            delta = _score_delta_for_pairs(candidate, control, pairs)
            cases.append(
                {
                    "candidate_phase_seconds": candidate_phase,
                    "control_phase_seconds": control_phase,
                    "score_delta": delta,
                }
            )
            for left, right in pairs:
                key = (left.session_date, left.period_id)
                return_delta = left.conservative_return_bps - right.conservative_return_bps
                previous = session_adverse.get(key)
                if previous is None or return_delta < previous[2]:
                    session_adverse[key] = (left, right, return_delta)
    finite = [
        float(case["score_delta"])
        for case in cases
        if case["score_delta"] is not None and math.isfinite(float(case["score_delta"]))
    ]
    adverse_delta = _score_delta_for_pairs(
        candidate,
        control,
        [(left, right) for left, right, _ in session_adverse.values()],
    )
    failure_reasons: list[str] = []
    expected_cases = len(candidate_phases) * len(control_phases)
    if len(finite) != expected_cases:
        failure_reasons.append("ATR bar-phase stress evidence is incomplete.")
    if not finite or min(finite) <= 0:
        failure_reasons.append(
            "At least one tested candidate/control ATR bar-phase combination is not above the control."
        )
    if adverse_delta is None or adverse_delta <= 0:
        failure_reasons.append(
            "The independently adverse per-session ATR phase aggregation is not above the control."
        )
    return {
        "atr_phase_cases": expected_cases,
        "atr_phase_finite_cases": len(finite),
        "atr_phase_min_score_delta": min(finite) if finite else None,
        "atr_phase_median_score_delta": (
            float(statistics.median(finite)) if finite else None
        ),
        "atr_phase_max_score_delta": max(finite) if finite else None,
        "atr_phase_adverse_score_delta": adverse_delta,
        "atr_phase_candidate_offsets": list(candidate_phases),
        "atr_phase_control_offsets": list(control_phases),
        "atr_phase_failure_reasons": failure_reasons,
        "atr_phase_passed": not failure_reasons,
    }


def _ensure_atr_windows(
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    atr_cache: dict[tuple[int, int, int], list[float | None]],
    windows: Iterable[tuple[int, int]],
    *,
    progress: ProgressCallback | None,
    message: str,
) -> None:
    missing = [
        (session_index, period, bar_seconds, ticks)
        for session_index, (_, ticks) in enumerate(period_ticks)
        for period, bar_seconds in sorted(set(windows))
        if (session_index, period, bar_seconds) not in atr_cache
    ]
    total = len(missing)
    for index, (session_index, period, bar_seconds, ticks) in enumerate(missing, start=1):
        atr_cache[(session_index, period, bar_seconds)] = _precompute_atr(
            ticks,
            period,
            bar_seconds,
        )
        _emit(progress, message, index, total)


def _evaluate_profiles(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    atr_cache: dict[tuple[int, int, int], list[float | None]],
    profiles: Iterable[AtrProfile],
    summaries: dict[str, MarketReplayCandidateSummary],
    sessions_by_profile: dict[str, list[MarketReplaySessionResult]],
    *,
    progress: ProgressCallback | None,
    message: str,
) -> None:
    pending = [profile for profile in profiles if profile.key() not in summaries]
    total = len(pending)
    for index, profile in enumerate(pending, start=1):
        summary, sessions, _ = _evaluate_profile(
            recording,
            period_ticks,
            atr_cache,
            profile,
            keep_details=False,
        )
        summaries[profile.key()] = summary
        sessions_by_profile[profile.key()] = sessions
        _emit(progress, message, index, total)


def _window_search_rows(
    stage: str,
    candidates: list[MarketReplayCandidateSummary],
    selected_windows: set[tuple[int, int]],
    explanation: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rank, candidate in enumerate(sorted(candidates, key=_candidate_sort_key), start=1):
        window = (candidate.profile.period, candidate.profile.bar_seconds)
        rows.append(
            {
                "stage": stage,
                "rank": rank,
                "period": candidate.profile.period,
                "bar_seconds": candidate.profile.bar_seconds,
                "selected_for_next_stage": window in selected_windows,
                "score": candidate.score,
                "completed_trades": candidate.completed_trades,
                "sessions_with_trades": candidate.sessions_with_trades,
                "right_censored_rate_pct": candidate.right_censored_rate_pct,
                "profile_key": candidate.profile.key(),
                "selection_basis": explanation,
            }
        )
    return rows


def _select_stage1_bars(
    candidates: Iterable[MarketReplayCandidateSummary],
) -> list[int]:
    """Select the strongest bar durations while always retaining the control bar."""

    selected = {
        candidate.profile.bar_seconds
        for candidate in sorted(candidates, key=_candidate_sort_key)[
            :_STAGE1_KEEP_BAR_DURATIONS
        ]
    }
    selected.add(_CONTROL_PROFILE.bar_seconds)
    return sorted(selected)


def _select_stage2_windows(
    candidates: Iterable[MarketReplayCandidateSummary],
) -> list[tuple[int, int]]:
    """Select the strongest non-control windows plus the unchanged control."""

    control_window = (_CONTROL_PROFILE.period, _CONTROL_PROFILE.bar_seconds)
    selected: list[tuple[int, int]] = [control_window]
    for candidate in sorted(candidates, key=_candidate_sort_key):
        window = (candidate.profile.period, candidate.profile.bar_seconds)
        if window == control_window or window in selected:
            continue
        selected.append(window)
        if len(selected) >= _STAGE2_NON_CONTROL_WINDOWS + 1:
            break
    return selected


def _select_windows(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    config: MarketReplayConfig,
    atr_cache: dict[tuple[int, int, int], list[float | None]],
    summaries: dict[str, MarketReplayCandidateSummary],
    sessions_by_profile: dict[str, list[MarketReplaySessionResult]],
    *,
    progress: ProgressCallback | None,
) -> tuple[
    list[tuple[int, int]],
    list[dict[str, Any]],
    list[AtrProfile],
    list[AtrProfile],
]:
    control_window = (_CONTROL_PROFILE.period, _CONTROL_PROFILE.bar_seconds)

    stage1_windows = [
        (_STAGE1_FIXED_PERIOD, bar_seconds)
        for bar_seconds in _STAGE1_BAR_SECONDS
    ]
    _ensure_atr_windows(
        period_ticks,
        atr_cache,
        stage1_windows,
        progress=progress,
        message="Stage 1 of 3: reconstructing bar-duration ATR windows",
    )
    stage1_profiles = [
        _window_profile(config, period, bar_seconds)
        for period, bar_seconds in stage1_windows
    ]
    _evaluate_profiles(
        recording,
        period_ticks,
        atr_cache,
        stage1_profiles,
        summaries,
        sessions_by_profile,
        progress=progress,
        message="Stage 1 of 3: comparing ATR bar durations",
    )
    stage1_candidates = [summaries[profile.key()] for profile in stage1_profiles]
    selected_bars = _select_stage1_bars(stage1_candidates)
    rows = _window_search_rows(
        "1_bar_duration",
        stage1_candidates,
        {(_STAGE1_FIXED_PERIOD, value) for value in selected_bars},
        "Period and all four strategy multipliers were fixed at the BouncyBot control values; the strongest bar durations advanced.",
    )

    # Precompute every period/bar pair so leave-one-day-out reruns can select a
    # different stage-1 bar without silently removing its stage-2 competitors.
    all_stage2_windows = {
        (period, bar_seconds)
        for bar_seconds in _STAGE1_BAR_SECONDS
        for period in _STAGE2_PERIODS
    }
    all_stage2_windows.add(control_window)
    _ensure_atr_windows(
        period_ticks,
        atr_cache,
        all_stage2_windows,
        progress=progress,
        message="Stage 2 of 3: reconstructing ATR period candidates",
    )
    all_stage2_profiles = [
        _window_profile(config, period, bar_seconds)
        for period, bar_seconds in sorted(all_stage2_windows)
    ]
    _evaluate_profiles(
        recording,
        period_ticks,
        atr_cache,
        all_stage2_profiles,
        summaries,
        sessions_by_profile,
        progress=progress,
        message="Stage 2 of 3: comparing ATR periods",
    )
    stage2_profiles = [
        profile
        for profile in all_stage2_profiles
        if profile.bar_seconds in selected_bars
        or (profile.period, profile.bar_seconds) == control_window
    ]
    stage2_candidates = [summaries[profile.key()] for profile in stage2_profiles]
    selected_windows = _select_stage2_windows(stage2_candidates)
    selected_window_set = set(selected_windows)
    rows.extend(
        _window_search_rows(
            "2_period",
            stage2_candidates,
            selected_window_set,
            "Periods were compared only inside the strongest stage-1 bar durations; the unchanged 14×60 control window was always retained.",
        )
    )
    rows.extend(
        _window_search_rows(
            "3_multiplier_search_windows",
            [
                summaries[_window_profile(config, period, bar_seconds).key()]
                for period, bar_seconds in selected_windows
            ],
            selected_window_set,
            "Only these narrowed ATR windows entered the full entry/exit multiplier grid and local multiplier refinement.",
        )
    )
    return selected_windows, rows, stage1_profiles, all_stage2_profiles


def _subset_candidate(
    profile: AtrProfile,
    sessions_by_profile: dict[str, list[MarketReplaySessionResult]],
    omitted_day: str,
) -> MarketReplayCandidateSummary | None:
    sessions = [
        session
        for session in sessions_by_profile.get(profile.key(), [])
        if session.session_date != omitted_day
    ]
    return _summary(profile, sessions) if sessions else None


def _leave_one_day_out_selection_rows(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    atr_cache: dict[tuple[int, int, int], list[float | None]],
    config: MarketReplayConfig,
    stage1_profiles: list[AtrProfile],
    stage2_profiles: list[AtrProfile],
    sessions_by_profile: dict[str, list[MarketReplaySessionResult]],
    control_profile: AtrProfile,
) -> list[dict[str, Any]]:
    """Rerun all three search stages after removing each trading day."""

    control_sessions = sessions_by_profile.get(control_profile.key(), [])
    days = sorted({session.session_date for session in control_sessions})
    if len(days) < 3:
        return []
    control_window = (control_profile.period, control_profile.bar_seconds)
    rows: list[dict[str, Any]] = []

    def sessions_for(profile: AtrProfile) -> list[MarketReplaySessionResult]:
        key = profile.key()
        if key not in sessions_by_profile:
            _, full_sessions, _ = _evaluate_profile(
                recording,
                period_ticks,
                atr_cache,
                profile,
                keep_details=False,
            )
            sessions_by_profile[key] = full_sessions
        return sessions_by_profile[key]

    for omitted_day in days:
        stage1_candidates = [
            candidate
            for profile in stage1_profiles
            if (
                candidate := _subset_candidate(
                    profile,
                    sessions_by_profile,
                    omitted_day,
                )
            )
            is not None
        ]
        selected_bars = _select_stage1_bars(stage1_candidates)

        stage2_candidates = [
            candidate
            for profile in stage2_profiles
            if (
                profile.bar_seconds in selected_bars
                or (profile.period, profile.bar_seconds) == control_window
            )
            and (
                candidate := _subset_candidate(
                    profile,
                    sessions_by_profile,
                    omitted_day,
                )
            )
            is not None
        ]
        selected_windows = _select_stage2_windows(stage2_candidates)
        selected_window_set = set(selected_windows)

        coarse_profiles = _coarse_profiles(config, selected_windows)
        coarse_candidates: list[MarketReplayCandidateSummary] = []
        for profile in coarse_profiles:
            sessions_for(profile)
            candidate = _subset_candidate(profile, sessions_by_profile, omitted_day)
            if candidate is not None:
                coarse_candidates.append(candidate)
        seeds = _refinement_seeds(coarse_candidates)
        refined_profiles = _refined_profiles(seeds, config)
        stage3_candidates = list(coarse_candidates)
        existing = {candidate.profile.key() for candidate in stage3_candidates}
        for profile in refined_profiles:
            sessions_for(profile)
            candidate = _subset_candidate(profile, sessions_by_profile, omitted_day)
            if candidate is not None and profile.key() not in existing:
                stage3_candidates.append(candidate)
                existing.add(profile.key())
        control_candidate = _subset_candidate(
            control_profile,
            sessions_by_profile,
            omitted_day,
        )
        if control_candidate is not None and all(
            candidate.profile.key() != control_profile.key()
            for candidate in stage3_candidates
        ):
            stage3_candidates.append(control_candidate)
        remaining_days = {
            session.session_date
            for session in control_sessions
            if session.session_date != omitted_day
        }
        selected, reason = _stable_recommendation(
            stage3_candidates,
            sessions=len(remaining_days),
            control_profile=control_profile,
        )
        control_score = control_candidate.score if control_candidate is not None else None
        rows.append(
            {
                "omitted_trading_day": omitted_day,
                "omitted_period_ids": sorted(
                    session.period_id
                    for session in control_sessions
                    if session.session_date == omitted_day
                ),
                "selected_stage1_bar_seconds": selected_bars,
                "selected_windows": [
                    {"period": period, "bar_seconds": bar_seconds}
                    for period, bar_seconds in selected_windows
                ],
                "available_stage3_windows": [
                    {"period": period, "bar_seconds": bar_seconds}
                    for period, bar_seconds in sorted(
                        {
                            (
                                candidate.profile.period,
                                candidate.profile.bar_seconds,
                            )
                            for candidate in stage3_candidates
                        }
                    )
                ],
                "selected_profile_key": selected.profile.key(),
                "selected_period": selected.profile.period,
                "selected_profile_bar_seconds": selected.profile.bar_seconds,
                "selected_score": selected.score,
                "selected_score_delta_vs_control": (
                    selected.score - control_score
                    if control_score is not None
                    else None
                ),
                "stage3_search_scope": (
                    "Rebuilt the complete stage-3 coarse multiplier grid for the omitted-day windows and "
                    "performed omission-specific local multiplier refinement."
                ),
                "selection_reason": reason,
            }
        )
    return rows


def _selection_stability(
    profile: AtrProfile,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    runs = len(rows)
    exact = sum(
        1 for row in rows if row.get("selected_profile_key") == profile.key()
    )
    same_window = 0
    for row in rows:
        selected_windows = row.get("selected_windows")
        values = selected_windows if isinstance(selected_windows, list) else []
        windows = {
            (item.get("period"), item.get("bar_seconds"))
            for item in values
            if isinstance(item, dict)
        }
        if (profile.period, profile.bar_seconds) in windows:
            same_window += 1
    return {
        "selection_runs": runs,
        "exact_profile_selections": exact,
        "exact_profile_selection_pct": exact / runs * 100.0 if runs else None,
        "same_atr_window_selections": same_window,
        "same_atr_window_selection_pct": (
            same_window / runs * 100.0 if runs else None
        ),
    }


def _copy_selection_evidence(
    source: MarketReplayCandidateSummary,
    target: MarketReplayCandidateSummary,
) -> None:
    target.stable_region_id = source.stable_region_id
    target.stable_region_size = source.stable_region_size
    target.stable_region_center = source.stable_region_center
    target.near_best = source.near_best
    target.evidence_stable = source.evidence_stable
    target.instability_reasons = list(source.instability_reasons)
    target.robustness_evaluated = source.robustness_evaluated
    target.robustness_passed = source.robustness_passed
    target.control_score_delta = source.control_score_delta
    target.paired_trading_days = source.paired_trading_days
    target.bootstrap_replicates = source.bootstrap_replicates
    target.bootstrap_ci80_low = source.bootstrap_ci80_low
    target.bootstrap_ci80_high = source.bootstrap_ci80_high
    target.bootstrap_ci95_low = source.bootstrap_ci95_low
    target.bootstrap_ci95_high = source.bootstrap_ci95_high
    target.bootstrap_probability_positive_pct = source.bootstrap_probability_positive_pct
    target.leave_one_day_out_estimates = source.leave_one_day_out_estimates
    target.leave_one_day_out_min_delta = source.leave_one_day_out_min_delta
    target.leave_one_day_out_median_delta = source.leave_one_day_out_median_delta
    target.leave_one_day_out_max_delta = source.leave_one_day_out_max_delta
    target.leave_one_day_out_positive_pct = source.leave_one_day_out_positive_pct
    target.leave_one_day_out_sign_reversals = source.leave_one_day_out_sign_reversals
    target.leave_one_day_out_most_influential_day = (
        source.leave_one_day_out_most_influential_day
    )
    target.leave_one_day_out_largest_change = source.leave_one_day_out_largest_change
    target.leave_one_day_out_selection_runs = (
        source.leave_one_day_out_selection_runs
    )
    target.leave_one_day_out_exact_profile_selections = (
        source.leave_one_day_out_exact_profile_selections
    )
    target.leave_one_day_out_exact_profile_selection_pct = (
        source.leave_one_day_out_exact_profile_selection_pct
    )
    target.leave_one_day_out_same_window_selections = (
        source.leave_one_day_out_same_window_selections
    )
    target.leave_one_day_out_same_window_selection_pct = (
        source.leave_one_day_out_same_window_selection_pct
    )
    target.paired_median_return_delta_bps = source.paired_median_return_delta_bps
    target.paired_positive_day_pct = source.paired_positive_day_pct
    target.control_trade_day_retention_pct = source.control_trade_day_retention_pct
    target.maximum_drawdown_delta_bps = source.maximum_drawdown_delta_bps
    target.worst_return_delta_bps = source.worst_return_delta_bps
    target.atr_phase_cases = source.atr_phase_cases
    target.atr_phase_min_score_delta = source.atr_phase_min_score_delta
    target.atr_phase_adverse_score_delta = source.atr_phase_adverse_score_delta


def run_market_replay_analysis(
    config: MarketReplayConfig,
    *,
    progress: ProgressCallback | None = None,
) -> MarketReplayAnalysisResult:
    """Analyze one or more Market Replay v2/v3 recordings without bot SQLite data."""

    normalized = config.normalized()
    recording = load_ibrec_set(normalized, progress=progress)
    period_ticks = [
        (period, ticks)
        for period in recording.periods
        if (ticks := _session_ticks(recording, period))
    ]
    if not period_ticks:
        raise MarketReplayAnalysisError(
            "Recording has no live or delayed market-data rows inside an analyzable session."
        )

    control_profile = _control_profile(normalized)
    atr_cache: dict[tuple[int, int, int], list[float | None]] = {}
    summaries: dict[str, MarketReplayCandidateSummary] = {}
    sessions_by_profile: dict[str, list[MarketReplaySessionResult]] = {}
    selected_windows, window_search, stage1_profiles, stage2_profiles = _select_windows(
        recording,
        period_ticks,
        normalized,
        atr_cache,
        summaries,
        sessions_by_profile,
        progress=progress,
    )

    coarse_profiles = _coarse_profiles(normalized, selected_windows)
    _evaluate_profiles(
        recording,
        period_ticks,
        atr_cache,
        coarse_profiles,
        summaries,
        sessions_by_profile,
        progress=progress,
        message="Stage 3 of 3: evaluating multiplier profiles",
    )
    stage3_keys = {profile.key() for profile in coarse_profiles}
    coarse_candidates = [summaries[key] for key in sorted(stage3_keys)]
    seeds = _refinement_seeds(coarse_candidates)
    refined_profiles = [
        profile
        for profile in _refined_profiles(seeds, normalized)
        if profile.key() not in stage3_keys
    ]
    _evaluate_profiles(
        recording,
        period_ticks,
        atr_cache,
        refined_profiles,
        summaries,
        sessions_by_profile,
        progress=progress,
        message="Stage 3 of 3: refining multiplier profiles",
    )
    stage3_keys.update(profile.key() for profile in refined_profiles)
    candidates = sorted(
        (summaries[key] for key in stage3_keys),
        key=_candidate_sort_key,
    )
    control_summary = summaries[control_profile.key()]
    control_sessions = sessions_by_profile[control_profile.key()]
    leave_one_day_out_selection_rows = _leave_one_day_out_selection_rows(
        recording,
        period_ticks,
        atr_cache,
        normalized,
        stage1_profiles,
        stage2_profiles,
        sessions_by_profile,
        control_profile,
    )

    selected_feed_evidence = [
        _session_feed_evidence(recording, period)
        for period, _ in period_ticks
    ]
    trading_day_count = len({period.session_date for period, _ in period_ticks})
    global_instability: list[str] = []
    if recording.is_synthetic:
        global_instability.append(
            "The manifest identifies this as synthetic sample data. Analysis is supported for software validation, "
            "but a changed ATR recommendation cannot be treated as stable live-market evidence."
        )
    if any(selected == "delayed" for selected, _, _ in selected_feed_evidence):
        global_instability.append(
            "At least one analyzed session used delayed market data because no live rows were available for that session."
        )
    if any(mixed for _, mixed, _ in selected_feed_evidence):
        global_instability.append(
            "At least one analyzed session contains both live and delayed rows. Live rows were preferred, but the feed transition reduces comparability."
        )
    if any(has_frozen for _, _, has_frozen in selected_feed_evidence):
        global_instability.append(
            "At least one analyzed session contains frozen or delayed-frozen rows. Those rows were excluded, but the feed interruption reduces evidence stability."
        )
    if any("captured_at_utc moved backwards" in issue for issue in recording.issues):
        global_instability.append(
            "The recorder receipt clock moved backwards. Monotonic event order was preserved, but UTC bar/session timing is less reliable."
        )

    region_centers = _stable_region_centers(candidates, sessions=trading_day_count)
    robustness_evidence: list[dict[str, Any]] = []
    leave_one_out_by_key: dict[str, list[dict[str, Any]]] = {}
    phase_cache: dict[tuple[str, int], list[MarketReplaySessionResult]] = {}
    eligible_centers: list[
        tuple[MarketReplayCandidateSummary, str, dict[str, Any]]
    ] = []
    for center, region_reason in region_centers:
        evidence, leave_one_out = _candidate_robustness(
            center,
            sessions_by_profile[center.profile.key()],
            control_summary,
            control_sessions,
            seed=_market_replay_bootstrap_seed(
                recording.sha256,
                control_summary.profile,
            ),
            selection_rows=leave_one_day_out_selection_rows,
        )
        phase_evidence = _atr_phase_robustness(
            recording,
            period_ticks,
            center.profile,
            control_summary.profile,
            phase_cache,
        )
        evidence.update(phase_evidence)
        evidence["failure_reasons"] = [
            *list(evidence.get("failure_reasons") or []),
            *list(phase_evidence.get("atr_phase_failure_reasons") or []),
        ]
        evidence["passed"] = bool(evidence.get("passed")) and bool(
            phase_evidence.get("atr_phase_passed")
        )
        _apply_robustness(center, evidence)
        raw_region_stable = center.evidence_stable
        changed_profile = center.profile.key() != control_summary.profile.key()
        eligible = (
            changed_profile
            and raw_region_stable
            and center.robustness_passed
            and not global_instability
        )
        center.evidence_stable = eligible
        if global_instability:
            center.instability_reasons.extend(global_instability)
        evidence = {
            **evidence,
            "stable_region_id": center.stable_region_id,
            "stable_region_size": center.stable_region_size,
            "raw_region_stable": raw_region_stable,
            "changed_profile": changed_profile,
            "global_instability_reasons": list(global_instability),
            "eligible_for_changed_recommendation": eligible,
            "region_selection_reason": region_reason,
        }
        robustness_evidence.append(evidence)
        leave_one_out_by_key[center.profile.key()] = leave_one_out
        if eligible:
            eligible_centers.append((center, region_reason, evidence))

    def robust_order(
        item: tuple[MarketReplayCandidateSummary, str, dict[str, Any]],
    ) -> tuple[Any, ...]:
        center, _, evidence = item

        def finite_or_negative(value: Any) -> float:
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return -math.inf
            return number if math.isfinite(number) else -math.inf

        return (
            -finite_or_negative(evidence.get("bootstrap_ci80_low")),
            -finite_or_negative(evidence.get("leave_one_day_out_min_delta")),
            -finite_or_negative(evidence.get("atr_phase_adverse_score_delta")),
            -finite_or_negative(evidence.get("atr_phase_min_score_delta")),
            -finite_or_negative(evidence.get("observed_score_delta")),
            -center.score,
            -center.stable_region_size,
            _profile_distance(center.profile),
            center.profile.key(),
        )

    eligible_centers.sort(key=robust_order)
    chosen: MarketReplayCandidateSummary | None = None
    chosen_reason = ""
    if eligible_centers:
        chosen, region_reason, _ = eligible_centers[0]
        chosen_reason = (
            f"{region_reason} The candidate passed the whole-trading-day bootstrap, full leave-one-day-out "
            "search, paired day/tail-risk checks, and all tested ATR bar-phase stress cases against the "
            "unchanged BouncyBot control."
        )

    if chosen is None:
        chosen = control_summary
        chosen.evidence_stable = False
        control_selection = _selection_stability(
            control_profile,
            leave_one_day_out_selection_rows,
        )
        chosen.leave_one_day_out_selection_runs = int(
            control_selection.get("selection_runs") or 0
        )
        chosen.leave_one_day_out_exact_profile_selections = int(
            control_selection.get("exact_profile_selections") or 0
        )
        chosen.leave_one_day_out_exact_profile_selection_pct = (
            control_selection.get("exact_profile_selection_pct")
        )
        chosen.leave_one_day_out_same_window_selections = int(
            control_selection.get("same_atr_window_selections") or 0
        )
        chosen.leave_one_day_out_same_window_selection_pct = (
            control_selection.get("same_atr_window_selection_pct")
        )
        if all(candidate.sessions_with_trades == 0 for candidate in candidates):
            chosen_reason = (
                "No searched profile completed a simulated trade. The unchanged BouncyBot control is therefore "
                "the single fallback profile to evaluate."
            )
            chosen.instability_reasons.append(
                "No candidate produced a simulated trade in the recorded data."
            )
        elif global_instability:
            chosen_reason = (
                "A changed profile cannot be recommended because the recording failed one or more source-quality "
                "stability gates. The unchanged BouncyBot control remains the single profile to evaluate."
            )
        elif region_centers:
            chosen_reason = (
                "No changed stable-region center passed every paired-day, complete-outcome, tail-risk, trading-day "
                "bootstrap, leave-one-day-out, ATR-window-selection, and ATR bar-phase gate. The unchanged "
                "BouncyBot control remains the single profile to evaluate."
            )
        else:
            chosen_reason = (
                "No supported adjacent near-best multiplier region was found. The unchanged BouncyBot control "
                "remains the single profile to evaluate."
            )
        chosen.instability_reasons.extend(
            [
                "No changed profile passed every default robustness gate.",
                *global_instability,
            ]
        )
        chosen.instability_reasons = sorted(set(chosen.instability_reasons))
        recommendation_leave_one_out = [
            {
                "candidate_profile_key": control_profile.key(),
                "control_profile_key": control_profile.key(),
                "omitted_trading_day": row["omitted_trading_day"],
                "remaining_trading_days": max(
                    0,
                    len(leave_one_day_out_selection_rows) - 1,
                ),
                "score_delta": 0.0,
                **row,
            }
            for row in leave_one_day_out_selection_rows
        ]
    else:
        recommendation_leave_one_out = leave_one_out_by_key.get(
            chosen.profile.key(),
            [],
        )

    if chosen.profile.key() == control_profile.key():
        control_detail, detailed_control_sessions, control_trades = _evaluate_profile(
            recording,
            period_ticks,
            atr_cache,
            control_profile,
            keep_details=True,
        )
        _copy_selection_evidence(chosen, control_detail)
        summaries[control_profile.key()] = control_detail
        recommendation = control_detail
        recommended_sessions = detailed_control_sessions
        recommended_trades = control_trades
    else:
        recommendation, recommended_sessions, recommended_trades = _evaluate_profile(
            recording,
            period_ticks,
            atr_cache,
            chosen.profile,
            keep_details=True,
        )
        _copy_selection_evidence(chosen, recommendation)
        summaries[chosen.profile.key()] = recommendation

        control_detail, detailed_control_sessions, _ = _evaluate_profile(
            recording,
            period_ticks,
            atr_cache,
            control_profile,
            keep_details=True,
        )
        _copy_selection_evidence(control_summary, control_detail)
        summaries[control_profile.key()] = control_detail

    candidates = sorted(
        (summaries[key] for key in stage3_keys),
        key=_candidate_sort_key,
    )
    contract = market_replay_search_contract(normalized)
    fingerprint_payload = {
        "input_components": recording.input_components,
        "search_contract": contract,
    }
    analysis_id = hashlib.sha256(canonical_json_bytes(fingerprint_payload)).hexdigest()
    output_dir = normalized.output_root / f"market_replay_{analysis_id[:16]}"
    issues = list(recording.issues)
    observed_durations = [
        max(
            0.0,
            (ticks[-1].elapsed_ns - ticks[0].elapsed_ns) / 1_000_000_000.0,
        )
        for _, ticks in period_ticks
        if ticks
    ]
    shortest_warmup = min(
        (period + 1) * bar_seconds
        for period in _STAGE2_PERIODS
        for bar_seconds in _STAGE1_BAR_SECONDS
    )
    if not observed_durations or max(observed_durations) < shortest_warmup:
        issues.append(
            "Every analyzable session is shorter than the smallest configured ATR warm-up window; no data-derived ATR trade can be expected."
        )
    if trading_day_count < _MIN_ROBUST_TRADING_DAYS:
        issues.append(
            f"Fewer than {_MIN_ROBUST_TRADING_DAYS} analyzable RTH trading days are present; a changed profile cannot pass the default day-level robustness gate."
        )
    issues.extend(global_instability)
    _emit(progress, "Market Replay analysis complete", 1, 1)
    return MarketReplayAnalysisResult(
        run_id=f"market-replay-{analysis_id[:16]}",
        analysis_id=analysis_id,
        output_dir=output_dir,
        generated_at_utc=recording.data_end_utc,
        recording=recording,
        recommendation=recommendation,
        recommendation_reason=chosen_reason,
        candidates=candidates,
        recommended_sessions=recommended_sessions,
        recommended_trades=recommended_trades,
        control=control_detail,
        control_sessions=detailed_control_sessions,
        global_issues=sorted(set(issues)),
        search_contract=contract,
        window_search=window_search,
        robustness_evidence=robustness_evidence,
        recommendation_leave_one_day_out=recommendation_leave_one_out,
    )
