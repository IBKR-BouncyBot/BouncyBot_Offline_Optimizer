"""Capture-window counterfactual replay for native BUY and SELL trails."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from .atr import (
    choose_atr,
    clamped_percentage,
    normalize_atr_multiplier,
    normalize_atr_window,
    period_bar_candidates,
)
from .evidence import kaplan_meier_trigger_probability
from .models import CandidateSummary, PricePoint, ReplayObservation
from .utils import iso_from_timestamp, median, parse_datetime, timestamp_seconds

_USABLE_ATR_SOURCES = {"capture_reconstructed", "bot_captured_fallback"}
_MAX_FILL_QUOTE_AGE_SECONDS = 60.0


def _normalized_outcome(row: ReplayObservation) -> str:
    """Return a backward-compatible outcome classification.

    v1.3 replay code always writes an explicit outcome. Older serialized or
    test-created observations may only contain ``triggered`` and ``atr_source``.
    Treat such a non-trigger with usable ATR as a confirmed legacy miss rather
    than silently dropping it; capture-end results produced by v1.3 explicitly
    set ``right_censored`` and remain censored.
    """

    if row.triggered:
        return "triggered"
    if row.right_censored or row.outcome == "right_censored":
        return "right_censored"
    if row.outcome == "confirmed_no_trigger":
        return "confirmed_no_trigger"
    if row.outcome not in {"", "unavailable"}:
        return row.outcome
    if row.atr_source in _USABLE_ATR_SOURCES:
        return "confirmed_no_trigger"
    return "unavailable"


@dataclass(slots=True, frozen=True)
class BuyCandidate:
    key: str
    multiplier: float
    period: int
    bar_seconds: int
    baseline: bool = False
    baseline_window: bool = True


@dataclass(slots=True, frozen=True)
class SellCandidate:
    key: str
    minimum_profit_multiplier: float
    sell_trail_multiplier: float
    period: int
    bar_seconds: int
    baseline: bool = False
    baseline_window: bool = True


def _required_sell_activation_price(
    *,
    average_buy_price: float,
    minimum_profit_pct: float,
    sell_trail_pct: float,
    slippage_buffer_enabled: bool,
    slippage_buffer_pct: float,
) -> float:
    """Mirror BouncyBot's minimum-profit activation calculation.

    The app rounds the planned minimum stop and final activation price to four
    decimals.  Optional slippage buffering raises the minimum stop so a modeled
    fill below that stop can still preserve the configured gross-profit floor.
    """

    profit_pct = max(0.01, float(minimum_profit_pct))
    stop_price = float(average_buy_price) * (1.0 + profit_pct / 100.0)
    if slippage_buffer_enabled:
        try:
            slip_pct = float(slippage_buffer_pct)
        except (TypeError, ValueError):
            slip_pct = 0.0
        if not math.isfinite(slip_pct):
            slip_pct = 0.0
        # BouncyBot's GUI accepts a slippage buffer through 99.99%.  Preserve
        # that reproducible range rather than silently narrowing it to 99.00%.
        slip = max(0.0, min(99.99, slip_pct)) / 100.0
        stop_price /= max(1e-12, 1.0 - slip)
    stop_price = round(stop_price, 4)
    trail_fraction = max(0.0, float(sell_trail_pct)) / 100.0
    if trail_fraction >= 1.0:
        return math.inf
    return round(stop_price / (1.0 - trail_fraction), 4)


def _scaled_values(
    base: float,
    factors: Iterable[float],
    *,
    minimum: float = 0.01,
    maximum: float = 50.0,
    allow_zero: bool = False,
) -> list[float]:
    """Return GUI-enterable multiplier candidates.

    BouncyBot's multiplier controls use two decimal places and cap values at
    50.00.  Producing four-decimal or out-of-range recommendations makes a
    counterfactual impossible to reproduce in the trading app, so candidate
    generation applies the same entry precision and limits here.
    """

    normalized_base = normalize_atr_multiplier(base, allow_zero=allow_zero)
    if allow_zero and normalized_base == 0.0:
        # Preserve the actual immediate-market control and add conservative
        # positive experiments. Multiplying zero by factors would otherwise
        # produce no trail candidates at all.
        values = {0.0, 0.05, 0.1, 0.25}
    else:
        values = {
            round(max(minimum, min(maximum, normalized_base * float(factor))), 2)
            for factor in factors
        }
        values.add(round(max(minimum, min(maximum, normalized_base)), 2))
    return sorted(values)


def buy_candidates(base_multiplier: float, period: int, bar_seconds: int) -> list[BuyCandidate]:
    baseline_period, baseline_bar = normalize_atr_window(period, bar_seconds)
    values = _scaled_values(
        base_multiplier,
        (0.75, 0.9, 1.0, 1.1, 1.25),
        allow_zero=True,
    )
    result: list[BuyCandidate] = []
    for window_name, candidate_period, candidate_bar in period_bar_candidates(
        baseline_period,
        baseline_bar,
    ):
        is_baseline_window = (
            candidate_period == baseline_period and candidate_bar == baseline_bar
        )
        for multiplier in values:
            key = f"buy_{window_name}_p{candidate_period}_b{candidate_bar}_m{multiplier:.2f}"
            result.append(
                BuyCandidate(
                    key=key,
                    multiplier=multiplier,
                    period=candidate_period,
                    bar_seconds=candidate_bar,
                    baseline=bool(
                        is_baseline_window
                        and abs(
                            multiplier
                            - normalize_atr_multiplier(
                                base_multiplier,
                                allow_zero=True,
                            )
                        )
                        < 1e-8
                    ),
                    baseline_window=is_baseline_window,
                )
            )
    return result


def sell_candidates(
    base_minimum_profit_multiplier: float,
    base_sell_trail_multiplier: float,
    period: int,
    bar_seconds: int,
    *,
    vary_minimum_profit: bool = True,
) -> list[SellCandidate]:
    baseline_period, baseline_bar = normalize_atr_window(period, bar_seconds)
    normalized_profit_base = normalize_atr_multiplier(
        base_minimum_profit_multiplier,
        allow_zero=False,
    )
    normalized_trail_base = normalize_atr_multiplier(
        base_sell_trail_multiplier,
        allow_zero=True,
    )
    profit_values = (
        _scaled_values(
            normalized_profit_base,
            (0.85, 1.0, 1.15),
            allow_zero=False,
        )
        if vary_minimum_profit
        else [
            normalized_profit_base
        ]
    )
    trail_values = _scaled_values(
        base_sell_trail_multiplier,
        (0.75, 1.0, 1.25),
        allow_zero=True,
    )
    result: list[SellCandidate] = []
    for window_name, candidate_period, candidate_bar in period_bar_candidates(
        baseline_period,
        baseline_bar,
    ):
        is_baseline_window = (
            candidate_period == baseline_period and candidate_bar == baseline_bar
        )
        for profit in profit_values:
            for trail in trail_values:
                key = (
                    f"sell_{window_name}_p{candidate_period}_b{candidate_bar}"
                    f"_profit{profit:.2f}_trail{trail:.2f}"
                )
                result.append(
                    SellCandidate(
                        key=key,
                        minimum_profit_multiplier=profit,
                        sell_trail_multiplier=trail,
                        period=candidate_period,
                        bar_seconds=candidate_bar,
                        baseline=bool(
                            is_baseline_window
                            and abs(profit - normalized_profit_base) < 1e-8
                            and abs(trail - normalized_trail_base) < 1e-8
                        ),
                        baseline_window=is_baseline_window,
                    )
                )
    return result


def _points_from(points: Iterable[PricePoint], start: float | None) -> tuple[list[PricePoint], bool]:
    ordered = sorted(points, key=lambda point: point.timestamp)
    if not ordered:
        return [], False
    if start is None:
        return ordered, True
    left_censored = start < ordered[0].timestamp
    if start > ordered[-1].timestamp:
        return [], False
    selected = [point for point in ordered if point.timestamp >= start]
    return selected, left_censored


def _future_excursions(points: list[PricePoint], trigger_index: int, trigger_price: float, *, leg: str) -> tuple[float | None, float | None]:
    future = points[trigger_index:]
    if not future or trigger_price <= 0:
        return None, None
    prices = [point.trigger_price for point in future if point.trigger_price > 0]
    if not prices:
        return None, None
    if leg == "buy":
        mfe = (max(prices) / trigger_price - 1.0) * 10_000.0
        mae = (1.0 - min(prices) / trigger_price) * 10_000.0
    else:
        mfe = (1.0 - min(prices) / trigger_price) * 10_000.0
        mae = (max(prices) / trigger_price - 1.0) * 10_000.0
    return max(0.0, mfe), max(0.0, mae)


def _spread_bps(point: PricePoint | None) -> float | None:
    if point is None or point.bid is None or point.ask is None:
        return None
    if point.bid <= 0 or point.ask <= 0 or point.ask < point.bid:
        return None
    midpoint = (point.bid + point.ask) / 2.0
    if midpoint <= 0:
        return None
    return (point.ask - point.bid) / midpoint * 10_000.0


def _latest_point_at_or_before(
    points: list[PricePoint],
    target: float | None,
    *,
    maximum_age_seconds: float,
) -> tuple[PricePoint | None, float | None]:
    """Return a non-future quote close to ``target``.

    A quote after an actual fill would leak future market state into the
    empirical execution model.  The helper therefore accepts only rows at or
    before the fill timestamp and reports the quote age explicitly.
    """

    if target is None or not points:
        return None, None
    candidates = [point for point in points if point.timestamp <= target]
    if not candidates:
        return None, None
    # Capture rows with identical timestamps retain archive order. Prefer the
    # last such row because it represents the newest quote state observed at
    # that timestamp; ``max(..., key=timestamp)`` would incorrectly keep the
    # first equal-timestamp row.
    _, latest = max(
        enumerate(candidates),
        key=lambda item: (item[1].timestamp, item[0]),
    )
    age = target - latest.timestamp
    if age > maximum_age_seconds:
        return None, age
    return latest, age


def _trading_day(actual_fill_time_utc: str, fallback_timestamp: float | None) -> str:
    parsed = parse_datetime(actual_fill_time_utc)
    if parsed is not None:
        return parsed.date().isoformat()
    parsed = parse_datetime(iso_from_timestamp(fallback_timestamp))
    return parsed.date().isoformat() if parsed is not None else ""


def _observation_context(
    *,
    all_points: list[PricePoint],
    selected: list[PricePoint],
    origin_timestamp: float | None,
    actual_fill_time_utc: str,
    trigger: PricePoint | None,
    outcome: str,
) -> dict[str, Any]:
    """Build censoring, timing, and quote context for one replay result."""

    start_timestamp: float | None = None
    end_timestamp: float | None = None
    if selected:
        first = selected[0].timestamp
        last = selected[-1].timestamp
        if origin_timestamp is None or origin_timestamp < first:
            start_timestamp = first
        elif origin_timestamp <= last:
            start_timestamp = origin_timestamp
        if trigger is not None:
            end_timestamp = trigger.timestamp
        elif start_timestamp is not None:
            end_timestamp = last
    observed_seconds = None
    if start_timestamp is not None and end_timestamp is not None:
        observed_seconds = max(0.0, end_timestamp - start_timestamp)

    fill_point, fill_quote_age = _latest_point_at_or_before(
        all_points,
        timestamp_seconds(actual_fill_time_utc),
        maximum_age_seconds=_MAX_FILL_QUOTE_AGE_SECONDS,
    )
    return {
        "right_censored": outcome == "right_censored",
        "outcome": outcome,
        "observation_start_utc": iso_from_timestamp(start_timestamp),
        "observation_end_utc": iso_from_timestamp(end_timestamp),
        "observed_seconds": observed_seconds,
        "time_to_trigger_seconds": observed_seconds if trigger is not None else None,
        "trading_day_utc": _trading_day(actual_fill_time_utc, start_timestamp),
        "trigger_bid": trigger.bid if trigger is not None else None,
        "trigger_ask": trigger.ask if trigger is not None else None,
        "trigger_spread_bps": _spread_bps(trigger),
        "fill_reference_price": (
            fill_point.trigger_price if fill_point is not None else None
        ),
        "fill_bid": fill_point.bid if fill_point is not None else None,
        "fill_ask": fill_point.ask if fill_point is not None else None,
        "fill_spread_bps": _spread_bps(fill_point),
        "fill_quote_age_seconds": (
            fill_quote_age if fill_point is not None else None
        ),
    }


def replay_buy(
    *,
    ticker: str,
    cycle_id: str,
    cycle_number: int | None,
    points: list[PricePoint],
    candidate: BuyCandidate,
    order_time_utc: str,
    actual_fill_time_utc: str,
    actual_fill_price: float | None,
    atr_min_pct: float,
    atr_max_pct: float,
    allow_captured_atr_fallback: bool = True,
) -> ReplayObservation:
    order_time = timestamp_seconds(order_time_utc)
    selected, left_censored = _points_from(points, order_time)
    reference_time = selected[0].timestamp if selected else (order_time or 0.0)
    atr_pct, atr_source, bars_available, bars_required = choose_atr(
        points,
        timestamp=reference_time,
        period=candidate.period,
        bar_seconds=candidate.bar_seconds,
        allow_captured_fallback=allow_captured_atr_fallback,
    )
    trail_pct = clamped_percentage(
        atr_pct,
        candidate.multiplier,
        atr_min_pct,
        atr_max_pct,
        allow_zero=True,
    )
    note_parts: list[str] = []
    if bars_available < bars_required:
        note_parts.append(f"ATR reconstruction had {bars_available}/{bars_required} bars")
    if left_censored:
        note_parts.append("BUY order began before the capture window")
    if order_time is not None and points and order_time > max(point.timestamp for point in points):
        note_parts.append("BUY order began after the capture window")
    if not selected or trail_pct is None:
        return ReplayObservation(
            cycle_id=cycle_id,
            cycle_number=cycle_number,
            ticker=ticker,
            leg="buy",
            candidate_key=candidate.key,
            multiplier=candidate.multiplier,
            minimum_profit_multiplier=None,
            period=candidate.period,
            bar_seconds=candidate.bar_seconds,
            effective_pct=trail_pct,
            triggered=False,
            control_candidate=candidate.baseline,
            baseline_window=candidate.baseline_window,
            actual_fill_price=actual_fill_price,
            actual_fill_time_utc=actual_fill_time_utc,
            left_censored=left_censored,
            **_observation_context(
                all_points=points,
                selected=selected,
                origin_timestamp=order_time,
                actual_fill_time_utc=actual_fill_time_utc,
                trigger=None,
                outcome="unavailable",
            ),
            atr_source=atr_source,
            note="; ".join(note_parts + ["No usable ATR or price window"]),
        )

    running_low = selected[0].trigger_price
    trigger_index: int | None = None
    for index, point in enumerate(selected):
        running_low = min(running_low, point.trigger_price)
        stop = running_low * (1.0 + trail_pct / 100.0)
        if point.trigger_price >= stop:
            trigger_index = index
            break
    if trigger_index is None:
        return ReplayObservation(
            cycle_id=cycle_id,
            cycle_number=cycle_number,
            ticker=ticker,
            leg="buy",
            candidate_key=candidate.key,
            multiplier=candidate.multiplier,
            minimum_profit_multiplier=None,
            period=candidate.period,
            bar_seconds=candidate.bar_seconds,
            effective_pct=trail_pct,
            triggered=False,
            control_candidate=candidate.baseline,
            baseline_window=candidate.baseline_window,
            actual_fill_price=actual_fill_price,
            actual_fill_time_utc=actual_fill_time_utc,
            left_censored=left_censored,
            **_observation_context(
                all_points=points,
                selected=selected,
                origin_timestamp=order_time,
                actual_fill_time_utc=actual_fill_time_utc,
                trigger=None,
                outcome="right_censored",
            ),
            atr_source=atr_source,
            note="; ".join(
                note_parts
                + [
                    "The saved window ended before a counterfactual BUY trigger was observed; "
                    "this is right-censored, not a confirmed miss"
                ]
            ),
        )

    trigger = selected[trigger_index]
    actual_time = timestamp_seconds(actual_fill_time_utc)
    improvement = None
    if actual_fill_price is not None and actual_fill_price > 0:
        improvement = (actual_fill_price - trigger.trigger_price) / actual_fill_price * 10_000.0
    delay = trigger.timestamp - actual_time if actual_time is not None else None
    mfe, mae = _future_excursions(selected, trigger_index, trigger.trigger_price, leg="buy")
    return ReplayObservation(
        cycle_id=cycle_id,
        cycle_number=cycle_number,
        ticker=ticker,
        leg="buy",
        candidate_key=candidate.key,
        multiplier=candidate.multiplier,
        minimum_profit_multiplier=None,
        period=candidate.period,
        bar_seconds=candidate.bar_seconds,
        effective_pct=trail_pct,
        triggered=True,
        control_candidate=candidate.baseline,
        baseline_window=candidate.baseline_window,
        trigger_time_utc=iso_from_timestamp(trigger.timestamp),
        trigger_price=trigger.trigger_price,
        actual_fill_price=actual_fill_price,
        actual_fill_time_utc=actual_fill_time_utc,
        price_improvement_bps=improvement,
        delay_seconds=delay,
        post_trigger_mfe_bps=mfe,
        post_trigger_mae_bps=mae,
        left_censored=left_censored,
        **_observation_context(
            all_points=points,
            selected=selected,
            origin_timestamp=order_time,
            actual_fill_time_utc=actual_fill_time_utc,
            trigger=trigger,
            outcome="triggered",
        ),
        atr_source=atr_source,
        note="; ".join(note_parts),
    )


def replay_sell(
    *,
    ticker: str,
    cycle_id: str,
    cycle_number: int | None,
    points: list[PricePoint],
    candidate: SellCandidate,
    reference_time_utc: str,
    actual_fill_time_utc: str,
    actual_fill_price: float | None,
    average_buy_price: float | None,
    atr_min_pct: float,
    atr_max_pct: float,
    strategy_start_time_utc: str = "",
    minimum_profit_adaptive: bool = True,
    manual_minimum_profit_pct: float | None = None,
    slippage_buffer_enabled: bool = False,
    slippage_buffer_pct: float = 0.0,
    allow_captured_atr_fallback: bool = True,
) -> ReplayObservation:
    ordered = sorted(points, key=lambda point: point.timestamp)
    explicit_strategy_start = timestamp_seconds(strategy_start_time_utc)
    fallback_reference = timestamp_seconds(reference_time_utc)
    strategy_start = (
        explicit_strategy_start
        if explicit_strategy_start is not None
        else fallback_reference
    )
    selected, boundary_censored = _points_from(ordered, strategy_start)
    # A SELL activation replay needs the path from the BUY fill onward.  A SELL
    # order/fill timestamp is only a useful parsing fallback; it cannot prove
    # that the earlier minimum-profit activation context was captured.
    left_censored = boundary_censored or explicit_strategy_start is None
    if not selected or average_buy_price is None or average_buy_price <= 0:
        return ReplayObservation(
            cycle_id=cycle_id,
            cycle_number=cycle_number,
            ticker=ticker,
            leg="sell",
            candidate_key=candidate.key,
            multiplier=candidate.sell_trail_multiplier,
            minimum_profit_multiplier=candidate.minimum_profit_multiplier,
            period=candidate.period,
            bar_seconds=candidate.bar_seconds,
            effective_pct=None,
            triggered=False,
            control_candidate=candidate.baseline,
            baseline_window=candidate.baseline_window,
            actual_fill_price=actual_fill_price,
            actual_fill_time_utc=actual_fill_time_utc,
            left_censored=left_censored or strategy_start is None,
            **_observation_context(
                all_points=points,
                selected=selected,
                origin_timestamp=strategy_start,
                actual_fill_time_utc=actual_fill_time_utc,
                trigger=None,
                outcome="unavailable",
            ),
            atr_source="unavailable",
            note="No usable SELL capture or average BUY price",
        )
    note_parts: list[str] = []
    if explicit_strategy_start is None:
        note_parts.append("BUY/strategy start time is unavailable; SELL activation replay is left-censored")
    elif boundary_censored:
        note_parts.append("SELL activation context began before the capture window")
    if not minimum_profit_adaptive:
        note_parts.append("Minimum profit was manual for this cycle; only the SELL trail is ATR-derived")
    if slippage_buffer_enabled:
        try:
            note_slippage = float(slippage_buffer_pct or 0.0)
        except (TypeError, ValueError):
            note_slippage = 0.0
        if not math.isfinite(note_slippage):
            note_slippage = 0.0
        note_parts.append(
            f"SELL activation includes the cycle's {max(0.0, note_slippage):.4f}% slippage buffer"
        )
    active = False
    running_high = 0.0
    trigger_index: int | None = None
    locked_trail_pct: float | None = None
    locked_profit_pct: float | None = None
    locked_required_price: float | None = None
    latest_trail_pct: float | None = None
    latest_profit_pct: float | None = None
    latest_required_price: float | None = None
    atr_source = "unavailable"
    max_bars_available = 0
    bars_required = max(2, int(candidate.period)) + 1
    for index, point in enumerate(selected):
        # BouncyBot decides whether to submit the native SELL trail using the
        # selected app strategy price.  Once submitted, the simulated native
        # trail follows Last/trigger price.  Keeping those two streams distinct
        # avoids activating a counterfactual on a Last print that the strategy
        # itself would not have accepted.
        activation_price = point.price
        trigger_price = point.trigger_price
        if not active:
            atr_pct, point_atr_source, bars_available, point_bars_required = choose_atr(
                ordered,
                timestamp=point.timestamp,
                period=candidate.period,
                bar_seconds=candidate.bar_seconds,
                allow_captured_fallback=allow_captured_atr_fallback,
            )
            max_bars_available = max(max_bars_available, bars_available)
            bars_required = point_bars_required
            trail_pct = clamped_percentage(
                atr_pct,
                candidate.sell_trail_multiplier,
                atr_min_pct,
                atr_max_pct,
                allow_zero=True,
            )
            if minimum_profit_adaptive:
                profit_pct = clamped_percentage(
                    atr_pct,
                    candidate.minimum_profit_multiplier,
                    atr_min_pct,
                    atr_max_pct,
                )
            else:
                try:
                    profit_pct = (
                        float(manual_minimum_profit_pct)
                        if manual_minimum_profit_pct is not None
                        else None
                    )
                except (TypeError, ValueError):
                    profit_pct = None
                if profit_pct is not None and math.isfinite(profit_pct):
                    profit_pct = round(max(0.0, min(99.99, profit_pct)), 2)
                else:
                    profit_pct = None
            if trail_pct is None or profit_pct is None or trail_pct >= 100.0:
                continue
            # Valid candidate ATR is evidence even when the activation price is
            # not reached before the capture ends. Retain the latest valid
            # values so the result is represented as right-censored rather
            # than disappearing as ATR-unavailable or being called a miss.
            atr_source = point_atr_source
            required_price = _required_sell_activation_price(
                average_buy_price=average_buy_price,
                minimum_profit_pct=profit_pct,
                sell_trail_pct=trail_pct,
                slippage_buffer_enabled=slippage_buffer_enabled,
                slippage_buffer_pct=slippage_buffer_pct,
            )
            latest_trail_pct = trail_pct
            latest_profit_pct = profit_pct
            latest_required_price = required_price
            if activation_price < required_price:
                continue
            active = True
            running_high = trigger_price
            locked_trail_pct = trail_pct
            locked_profit_pct = profit_pct
            locked_required_price = required_price
            if trail_pct <= 0:
                trigger_index = index
                break
            continue
        running_high = max(running_high, trigger_price)
        if locked_trail_pct is None:
            break
        stop = running_high * (1.0 - locked_trail_pct / 100.0)
        if trigger_price <= stop:
            trigger_index = index
            break
    if max_bars_available < bars_required:
        note_parts.append(
            f"ATR reconstruction had at most {max_bars_available}/{bars_required} bars"
        )
    if trigger_index is None:
        reason = "No usable ATR for the SELL candidate"
        if atr_source != "unavailable":
            if locked_required_price is None:
                reason = "SELL activation price was not reached inside the saved window"
                if latest_required_price is not None:
                    reason += f" (estimated activation price {latest_required_price:.4f})"
            else:
                reason = "No SELL trigger inside the saved window"
                reason += f" after the estimated activation price {locked_required_price:.4f}"
        outcome = (
            "right_censored"
            if atr_source in _USABLE_ATR_SOURCES
            and latest_trail_pct is not None
            and latest_profit_pct is not None
            else "unavailable"
        )
        if outcome == "right_censored":
            reason += (
                "; the capture ended before the candidate outcome was known, "
                "so this is not a confirmed miss"
            )
        return ReplayObservation(
            cycle_id=cycle_id,
            cycle_number=cycle_number,
            ticker=ticker,
            leg="sell",
            candidate_key=candidate.key,
            multiplier=candidate.sell_trail_multiplier,
            minimum_profit_multiplier=candidate.minimum_profit_multiplier,
            period=candidate.period,
            bar_seconds=candidate.bar_seconds,
            effective_pct=(
                locked_trail_pct
                if locked_trail_pct is not None
                else latest_trail_pct
            ),
            triggered=False,
            control_candidate=candidate.baseline,
            baseline_window=candidate.baseline_window,
            effective_minimum_profit_pct=(
                locked_profit_pct
                if locked_profit_pct is not None
                else latest_profit_pct
            ),
            actual_fill_price=actual_fill_price,
            actual_fill_time_utc=actual_fill_time_utc,
            left_censored=left_censored,
            **_observation_context(
                all_points=points,
                selected=selected,
                origin_timestamp=strategy_start,
                actual_fill_time_utc=actual_fill_time_utc,
                trigger=None,
                outcome=outcome,
            ),
            atr_source=atr_source,
            note="; ".join(note_parts + [reason]),
        )

    trigger = selected[trigger_index]
    assert locked_trail_pct is not None
    assert locked_profit_pct is not None
    assert locked_required_price is not None
    actual_time = timestamp_seconds(actual_fill_time_utc)
    improvement = None
    if actual_fill_price is not None and actual_fill_price > 0:
        improvement = (trigger.trigger_price - actual_fill_price) / actual_fill_price * 10_000.0
    delay = trigger.timestamp - actual_time if actual_time is not None else None
    mfe, mae = _future_excursions(selected, trigger_index, trigger.trigger_price, leg="sell")
    return ReplayObservation(
        cycle_id=cycle_id,
        cycle_number=cycle_number,
        ticker=ticker,
        leg="sell",
        candidate_key=candidate.key,
        multiplier=candidate.sell_trail_multiplier,
        minimum_profit_multiplier=candidate.minimum_profit_multiplier,
        period=candidate.period,
        bar_seconds=candidate.bar_seconds,
        effective_pct=locked_trail_pct,
        triggered=True,
        control_candidate=candidate.baseline,
        baseline_window=candidate.baseline_window,
        effective_minimum_profit_pct=locked_profit_pct,
        trigger_time_utc=iso_from_timestamp(trigger.timestamp),
        trigger_price=trigger.trigger_price,
        actual_fill_price=actual_fill_price,
        actual_fill_time_utc=actual_fill_time_utc,
        price_improvement_bps=improvement,
        delay_seconds=delay,
        post_trigger_mfe_bps=mfe,
        post_trigger_mae_bps=mae,
        left_censored=left_censored,
        **_observation_context(
            all_points=points,
            selected=selected,
            origin_timestamp=strategy_start,
            actual_fill_time_utc=actual_fill_time_utc,
            trigger=trigger,
            outcome="triggered",
        ),
        atr_source=atr_source,
        note="; ".join(
            note_parts
            + [
                f"Estimated activation price {locked_required_price:.4f}; "
                f"minimum-profit {locked_profit_pct:.4f}%"
            ]
        ),
    )


def summarize_observations(observations: Iterable[ReplayObservation]) -> list[CandidateSummary]:
    grouped: dict[tuple[str, str], list[ReplayObservation]] = {}
    for observation in observations:
        grouped.setdefault((observation.leg, observation.candidate_key), []).append(observation)
    summaries: list[CandidateSummary] = []
    for (leg, key), rows in grouped.items():
        first = rows[0]
        baseline_window = all(row.baseline_window for row in rows)
        metrics = _observation_metrics(rows, baseline_window=baseline_window)
        count = int(metrics["observations"])
        scoreable_count = int(metrics["scoreable_observations"])
        triggered = int(metrics["triggered"])
        scoreable_triggered = int(metrics["scoreable_triggered"])
        candidate_atr_count = int(metrics["candidate_atr_observations"])
        candidate_atr_coverage = metrics["candidate_atr_coverage_pct"]
        candidate_window_eligible = bool(metrics["candidate_window_eligible"])
        rate = metrics["trigger_rate_pct"]
        improvement = metrics["median_improvement_bps"]
        delay = metrics["median_delay_seconds"]
        absolute_delay = metrics["median_absolute_delay_seconds"]
        mfe = metrics["median_mfe_bps"]
        mae = metrics["median_mae_bps"]
        left_censored = int(metrics["left_censored_observations"])
        left_censored_rate = metrics["left_censored_rate_pct"]
        right_censored = int(metrics["right_censored_observations"])
        right_censored_rate = metrics["right_censored_rate_pct"]
        unavailable = int(metrics["unavailable_observations"])
        km_1m = metrics["km_trigger_probability_1m_pct"]
        km_5m = metrics["km_trigger_probability_5m_pct"]
        km_15m = metrics["km_trigger_probability_15m_pct"]
        adjusted_improvement = metrics["median_adjusted_improvement_bps"]
        score = metrics["screening_score"]
        if scoreable_count >= 20 and scoreable_triggered >= 16:
            evidence = "strong local-window sample"
        elif scoreable_count >= 5 and scoreable_triggered >= 3:
            evidence = "moderate local-window sample"
        else:
            evidence = "limited local-window sample"
        if not candidate_window_eligible:
            complete_context_count = count - left_censored
            evidence += (
                "; insufficient candidate-specific ATR reconstruction "
                f"({candidate_atr_count}/{complete_context_count} complete-context windows)"
            )
        elif any(
            not row.left_censored and row.atr_source == "bot_captured_fallback"
            for row in rows
        ):
            evidence += "; bot-captured ATR fallback was used only for matching historical ATR windows"
        else:
            complete_context_count = count - left_censored
            evidence += (
                f"; candidate ATR reconstructed in {candidate_atr_count}/{complete_context_count} "
                "complete-context windows"
            )
        if left_censored:
            evidence += (
                f"; {left_censored}/{count} left-censored window(s) remain descriptive and are excluded from ranking"
            )
        if right_censored:
            evidence += (
                f"; {right_censored}/{count} right-censored window(s) ended before the counterfactual outcome was known"
            )
        rationale = (
            "The legacy local screening score uses execution-adjusted trigger-price improvement when available, "
            "absolute delay, and adverse excursion on complete-context triggered windows. Capture-window exhaustion "
            "is right-censoring and is not treated as a failed trade. The primary v1.3 recommendation is based on "
            "paired control comparisons, clustered uncertainty, influence diagnostics, stable-region support, and "
            "execution-cost adjustment rather than this standalone score."
        )
        profile_counts: dict[str, int] = {}
        for row in rows:
            profile = row.historical_atr_profile_id or "UNKNOWN"
            profile_counts[profile] = profile_counts.get(profile, 0) + 1
        profile_breakdown: list[dict[str, Any]] = []
        for profile in sorted(profile_counts):
            profile_rows = [
                row
                for row in rows
                if (row.historical_atr_profile_id or "UNKNOWN") == profile
            ]
            profile_metrics = _observation_metrics(
                profile_rows,
                baseline_window=baseline_window,
            )
            profile_metrics.pop("candidate_window_eligible")
            profile_breakdown.append(
                {
                    "historical_atr_profile_id": profile,
                    **profile_metrics,
                }
            )
        summaries.append(
            CandidateSummary(
                leg=leg,
                candidate_key=key,
                multiplier=first.multiplier,
                minimum_profit_multiplier=first.minimum_profit_multiplier,
                period=first.period,
                bar_seconds=first.bar_seconds,
                baseline_window=baseline_window,
                observations=count,
                scoreable_observations=scoreable_count,
                triggered=triggered,
                scoreable_triggered=scoreable_triggered,
                candidate_atr_observations=candidate_atr_count,
                candidate_atr_coverage_pct=candidate_atr_coverage,
                trigger_rate_pct=rate,
                median_improvement_bps=improvement,
                median_delay_seconds=delay,
                median_absolute_delay_seconds=absolute_delay,
                median_mfe_bps=mfe,
                median_mae_bps=mae,
                left_censored_observations=left_censored,
                left_censored_rate_pct=left_censored_rate,
                control_candidate=all(row.control_candidate for row in rows),
                right_censored_observations=right_censored,
                right_censored_rate_pct=right_censored_rate,
                unavailable_observations=unavailable,
                km_trigger_probability_1m_pct=km_1m,
                km_trigger_probability_5m_pct=km_5m,
                km_trigger_probability_15m_pct=km_15m,
                median_adjusted_improvement_bps=adjusted_improvement,
                screening_score=score,
                evidence=evidence,
                priority="unranked" if candidate_window_eligible else "insufficient ATR coverage",
                rationale=rationale,
                historical_atr_profile_count=len(profile_counts),
                historical_atr_profiles=sorted(profile_counts),
                historical_atr_profile_observations=dict(sorted(profile_counts.items())),
                historical_atr_profile_breakdown=profile_breakdown,
            )
        )
    def finite_or(value: float | None, fallback: float) -> float:
        return value if value is not None and math.isfinite(value) else fallback

    for leg in ("buy", "sell"):
        leg_rows = [
            row
            for row in summaries
            if row.leg == leg
            and row.screening_score is not None
            and row.priority != "insufficient ATR coverage"
        ]
        leg_rows.sort(
            key=lambda row: (
                -finite_or(row.screening_score, -math.inf),
                -finite_or(row.trigger_rate_pct, 0.0),
                -finite_or(row.candidate_atr_coverage_pct, 0.0),
                row.candidate_key,
            ),
        )
        for index, row in enumerate(leg_rows):
            if row.evidence.startswith("limited local-window sample"):
                row.priority = "insufficient evidence"
            elif index < 3:
                row.priority = "evaluate first"
            elif index < 8:
                row.priority = "secondary evaluation"
            else:
                row.priority = "low priority"
    return sorted(
        summaries,
        key=lambda row: (
            row.leg,
            0 if row.priority == "evaluate first" else 1,
            -finite_or(row.screening_score, -math.inf),
            row.candidate_key,
        ),
    )


def _observation_metrics(
    rows: list[ReplayObservation],
    *,
    baseline_window: bool,
) -> dict[str, Any]:
    """Calculate aggregate fields while keeping censored evidence descriptive.

    A left-censored capture begins after the order or SELL-activation context.
    Its prior running low/high is unknowable, so it remains in coverage and raw
    replay output but cannot fairly rank candidate settings. Headline screening
    metrics therefore exclude left-censored observations. Right-censored
    observations remain valid exposure for trigger-rate survival estimates.
    """

    complete_context_rows = [row for row in rows if not row.left_censored]
    scoreable_rows = [
        row
        for row in complete_context_rows
        if row.atr_source in _USABLE_ATR_SOURCES
        and _normalized_outcome(row) != "unavailable"
    ]
    triggered_rows = [row for row in rows if row.triggered]
    scoreable_triggered_rows = [row for row in scoreable_rows if row.triggered]
    scoreable_right_censored_rows = [
        row
        for row in scoreable_rows
        if _normalized_outcome(row) == "right_censored"
    ]
    confirmed_no_trigger_rows = [
        row
        for row in scoreable_rows
        if _normalized_outcome(row) == "confirmed_no_trigger"
    ]
    count = len(rows)
    scoreable_count = len(scoreable_rows)
    triggered = len(triggered_rows)
    scoreable_triggered = len(scoreable_triggered_rows)
    complete_context_count = len(complete_context_rows)
    candidate_atr_count = scoreable_count
    candidate_atr_coverage = (
        candidate_atr_count / complete_context_count * 100.0
        if complete_context_count
        else None
    )
    candidate_window_eligible = bool(
        scoreable_count
        and (
            baseline_window
            or (
                candidate_atr_count >= 3
                and candidate_atr_coverage is not None
                and candidate_atr_coverage >= 50.0
            )
        )
    )
    rate = (
        scoreable_triggered / scoreable_count * 100.0
        if scoreable_count
        else None
    )
    improvement = median(
        row.price_improvement_bps for row in scoreable_triggered_rows
    )
    adjusted_improvement = median(
        row.adjusted_price_improvement_bps
        for row in scoreable_triggered_rows
    )
    delay = median(row.delay_seconds for row in scoreable_triggered_rows)
    absolute_delay = median(
        abs(row.delay_seconds)
        for row in scoreable_triggered_rows
        if row.delay_seconds is not None
    )
    mfe = median(row.post_trigger_mfe_bps for row in scoreable_triggered_rows)
    mae = median(row.post_trigger_mae_bps for row in scoreable_triggered_rows)
    left_censored = count - complete_context_count
    left_censored_rate = left_censored / count * 100.0 if count else None
    right_censored = len(scoreable_right_censored_rows)
    right_censored_rate = (
        right_censored / scoreable_count * 100.0 if scoreable_count else None
    )
    unavailable = sum(
        1 for row in rows if _normalized_outcome(row) == "unavailable"
    )
    km_1m = kaplan_meier_trigger_probability(scoreable_rows, 60.0)
    km_5m = kaplan_meier_trigger_probability(scoreable_rows, 300.0)
    km_15m = kaplan_meier_trigger_probability(scoreable_rows, 900.0)
    score = None
    if candidate_window_eligible and not scoreable_triggered_rows and confirmed_no_trigger_rows:
        score = -200.0
    elif (
        candidate_window_eligible
        and scoreable_triggered_rows
        and improvement is not None
    ):
        # A signed median can hide timing error when equally early and late
        # triggers cancel. Penalize the median absolute error while retaining the
        # signed median as a descriptive result.
        delay_penalty = (absolute_delay or 0.0) / 60.0
        adverse_penalty = max(0.0, mae or 0.0) * 0.05
        score = (
            (adjusted_improvement if adjusted_improvement is not None else improvement)
            - delay_penalty
            - adverse_penalty
        )
    return {
        "observations": count,
        "scoreable_observations": scoreable_count,
        "triggered": triggered,
        "scoreable_triggered": scoreable_triggered,
        "candidate_atr_observations": candidate_atr_count,
        "candidate_atr_coverage_pct": candidate_atr_coverage,
        "candidate_window_eligible": candidate_window_eligible,
        "trigger_rate_pct": rate,
        "median_improvement_bps": improvement,
        "median_adjusted_improvement_bps": adjusted_improvement,
        "median_delay_seconds": delay,
        "median_absolute_delay_seconds": absolute_delay,
        "median_mfe_bps": mfe,
        "median_mae_bps": mae,
        "left_censored_observations": left_censored,
        "left_censored_rate_pct": left_censored_rate,
        "right_censored_observations": right_censored,
        "right_censored_rate_pct": right_censored_rate,
        "unavailable_observations": unavailable,
        "km_trigger_probability_1m_pct": km_1m,
        "km_trigger_probability_5m_pct": km_5m,
        "km_trigger_probability_15m_pct": km_15m,
        "screening_score": score,
    }
