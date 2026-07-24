"""ATR reconstruction and candidate generation for capture-window replay."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from .models import PricePoint
from .utils import finite_float, median

# These are the ranges accepted by BouncyBot's ATR controls. Keeping candidate
# windows inside them makes every suggested profile directly reproducible in
# the trading application.
ATR_PERIOD_MIN = 2
ATR_PERIOD_MAX = 200
ATR_BAR_SECONDS_MIN = 5
ATR_BAR_SECONDS_MAX = 3_600


def normalize_atr_multiplier(value: object, *, allow_zero: bool) -> float:
    """Return a finite, two-decimal multiplier accepted by BouncyBot's GUI.

    Historical SQLite rows are evidence and are preserved verbatim elsewhere.
    Counterfactual replay, however, must use a value that a user can actually
    enter in the trading application.  A zero value is meaningful only for the
    BUY and SELL trailing multipliers, where it selects immediate market-style
    behavior.
    """

    number = finite_float(value)
    if number is None:
        number = 0.0 if allow_zero else 0.01
    if allow_zero and number <= 0.0:
        return 0.0
    return round(max(0.01, min(50.0, number)), 2)


def normalize_atr_window(period: object, bar_seconds: object) -> tuple[int, int]:
    """Return a finite, GUI-enterable ATR period and bar duration.

    Saved databases can outlive UI validation rules or contain manually edited
    values. Counterfactual candidates must never recommend an impossible value,
    so malformed and out-of-range inputs are normalized to BouncyBot's current
    control limits. Historical evidence remains exported separately unchanged.
    """

    def integer_or_default(value: object, default: int) -> int:
        number = finite_float(value)
        if number is None:
            return default
        if not number.is_integer():
            return default
        return int(number)

    normalized_period = max(
        ATR_PERIOD_MIN,
        min(ATR_PERIOD_MAX, integer_or_default(period, 14)),
    )
    normalized_bar = max(
        ATR_BAR_SECONDS_MIN,
        min(ATR_BAR_SECONDS_MAX, integer_or_default(bar_seconds, 60)),
    )
    return normalized_period, normalized_bar


def normalize_atr_clamps(lower: object, upper: object) -> tuple[float, float]:
    """Return a finite ATR range that can be entered in BouncyBot's GUI.

    Valid saved values are preserved. BouncyBot treats a missing or zero lower
    clamp as 0.10%, enforces a hard minimum of 0.01%, treats a missing or zero
    upper clamp as 20.00%, and caps the upper value at 99.99%. Its current GUI
    additionally requires the maximum to be strictly greater than the minimum.
    Legacy or manually edited invalid values are therefore repaired to the
    nearest current GUI-enterable pair rather than being presented as an exact
    production setting.
    """

    def finite_or_default(value: object, default: float) -> float:
        number = finite_float(value)
        if number is None:
            return default
        if number == 0.0:
            return default
        return number

    # 99.98 leaves room for a strictly greater 99.99 upper value when malformed
    # historical input supplies an excessive minimum clamp. BouncyBot's normal
    # UI validation prevents this case; the optimizer must still fail safely on
    # manually edited or legacy databases.
    normalized_lower = max(0.01, min(99.98, finite_or_default(lower, 0.10)))
    normalized_upper = finite_or_default(upper, 20.00)
    normalized_upper = max(normalized_lower + 0.01, min(99.99, normalized_upper))
    normalized_upper = min(99.99, normalized_upper)
    return normalized_lower, normalized_upper


@dataclass(slots=True, frozen=True)
class AtrEstimate:
    ready: bool
    atr: float | None
    atr_pct: float | None
    bars_available: int
    bars_required: int
    period: int
    bar_seconds: int
    source: str


def build_bars(points: Iterable[PricePoint], *, bar_seconds: int, before: float | None = None) -> list[dict[str, float]]:
    _, duration = normalize_atr_window(14, bar_seconds)
    clean = [point for point in points if before is None or point.timestamp <= before]
    clean.sort(key=lambda point: point.timestamp)
    bars: list[dict[str, float]] = []
    for point in clean:
        price = float(point.price)
        if not math.isfinite(price) or price <= 0:
            continue
        bucket = int(point.timestamp // duration)
        if not bars or int(bars[-1]["bucket"]) != bucket:
            bars.append(
                {
                    "bucket": float(bucket),
                    "start": float(bucket * duration),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                }
            )
        else:
            bar = bars[-1]
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
    return bars


def atr_estimate(
    points: Iterable[PricePoint],
    *,
    period: int = 14,
    bar_seconds: int = 60,
    before: float | None = None,
) -> AtrEstimate:
    period, bar_seconds = normalize_atr_window(period, bar_seconds)
    bars = build_bars(points, bar_seconds=bar_seconds, before=before)
    required = period + 1
    if len(bars) < required:
        return AtrEstimate(
            ready=False,
            atr=None,
            atr_pct=None,
            bars_available=len(bars),
            bars_required=required,
            period=period,
            bar_seconds=bar_seconds,
            source="capture_reconstructed",
        )
    recent = bars[-required:]
    true_ranges: list[float] = []
    for previous, current in zip(recent, recent[1:]):
        previous_close = float(previous["close"])
        high = float(current["high"])
        low = float(current["low"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    if len(true_ranges) < period:
        return AtrEstimate(
            ready=False,
            atr=None,
            atr_pct=None,
            bars_available=len(bars),
            bars_required=required,
            period=period,
            bar_seconds=bar_seconds,
            source="capture_reconstructed",
        )
    atr = sum(true_ranges[-period:]) / period
    latest_close = float(recent[-1]["close"])
    atr_pct = atr / latest_close * 100.0 if latest_close > 0 else None
    ready = bool(atr_pct is not None and math.isfinite(atr_pct) and atr_pct > 0)
    return AtrEstimate(
        ready=ready,
        atr=atr if ready else None,
        atr_pct=atr_pct if ready else None,
        bars_available=len(bars),
        bars_required=required,
        period=period,
        bar_seconds=bar_seconds,
        source="capture_reconstructed",
    )


def captured_atr_near(points: Iterable[PricePoint], timestamp: float, *, max_distance_seconds: float = 180.0) -> float | None:
    """Return the nearest saved ATR at or before ``timestamp``.

    The fallback represents information the live bot had already calculated at
    the replay decision time.  Looking forward, even by one capture row, would
    leak future volatility into the counterfactual.
    """
    candidates: list[tuple[PricePoint, float]] = []
    for point in points:
        atr_pct = finite_float(point.atr_pct)
        if atr_pct is None or atr_pct <= 0:
            continue
        if 0.0 <= timestamp - point.timestamp <= max_distance_seconds:
            candidates.append((point, atr_pct))
    if not candidates:
        return None
    closest_distance = min(timestamp - point.timestamp for point, _ in candidates)
    close_values = [
        atr_pct
        for point, atr_pct in candidates
        if abs((timestamp - point.timestamp) - closest_distance) < 1e-9
    ]
    return median(close_values)


def choose_atr(
    points: Iterable[PricePoint],
    *,
    timestamp: float,
    period: int,
    bar_seconds: int,
    allow_captured_fallback: bool = True,
) -> tuple[float | None, str, int, int]:
    """Prefer a candidate-specific reconstruction, then an allowed captured ATR.

    A bot-captured ATR value represents the period/bar configuration active in
    the trading bot at capture time. It is therefore a valid fallback only when
    the replay candidate uses that same window. Callers evaluating a different
    ATR window disable the fallback so the report cannot falsely claim that the
    alternate period or bar duration was reconstructed.
    """
    point_list = list(points)
    estimate = atr_estimate(point_list, period=period, bar_seconds=bar_seconds, before=timestamp)
    if estimate.ready:
        return estimate.atr_pct, estimate.source, estimate.bars_available, estimate.bars_required
    if allow_captured_fallback:
        captured = captured_atr_near(point_list, timestamp)
        if captured is not None:
            return captured, "bot_captured_fallback", estimate.bars_available, estimate.bars_required
    elif estimate.bars_available < estimate.bars_required:
        return None, "candidate_atr_window_unavailable", estimate.bars_available, estimate.bars_required
    return None, "unavailable", estimate.bars_available, estimate.bars_required


def clamped_percentage(
    atr_pct: float | None,
    multiplier: float,
    lower: float,
    upper: float,
    *,
    allow_zero: bool = False,
) -> float | None:
    if atr_pct is None or isinstance(atr_pct, bool) or isinstance(multiplier, bool):
        return None
    try:
        normalized_multiplier = float(multiplier)
    except (TypeError, ValueError):
        return None
    normalized_lower, normalized_upper = normalize_atr_clamps(lower, upper)
    if not all(
        math.isfinite(value)
        for value in (
            float(atr_pct),
            normalized_multiplier,
            normalized_lower,
            normalized_upper,
        )
    ):
        return None
    if normalized_multiplier < 0:
        return None
    if allow_zero and normalized_multiplier <= 0.0:
        # BouncyBot treats a zero BUY/SELL trailing multiplier as an explicit
        # immediate market-order mode.  Applying the ATR minimum clamp here
        # would silently replay a different strategy.
        return 0.0
    value = float(atr_pct) * normalized_multiplier
    if not math.isfinite(value):
        return None
    # Match BouncyBot's user-facing adaptive percentages exactly. The trading
    # application rounds the clamped percentage to two decimals before it is
    # used to construct strategy/order values.
    return round(max(normalized_lower, min(normalized_upper, value)), 2)


def period_bar_candidates(period: int, bar_seconds: int) -> list[tuple[str, int, int]]:
    """Return baseline, faster and smoother ATR windows for evaluation."""
    period, bar_seconds = normalize_atr_window(period, bar_seconds)
    faster_period = max(ATR_PERIOD_MIN, min(period, int(round(period * 0.7))))
    faster_bar = max(
        ATR_BAR_SECONDS_MIN,
        min(bar_seconds, int(round(bar_seconds * 0.5))),
    )
    smoother_period = min(
        ATR_PERIOD_MAX,
        max(period + 1, int(round(period * 1.5))),
    )
    smoother_bar = min(
        ATR_BAR_SECONDS_MAX,
        max(bar_seconds + ATR_BAR_SECONDS_MIN, int(round(bar_seconds * 2.0))),
    )
    candidates = [
        ("baseline_window", period, bar_seconds),
        ("faster_window", faster_period, faster_bar),
        ("smoother_window", smoother_period, smoother_bar),
    ]
    deduped: list[tuple[str, int, int]] = []
    seen: set[tuple[int, int]] = set()
    for name, candidate_period, candidate_bar in candidates:
        key = (candidate_period, candidate_bar)
        if key not in seen:
            seen.add(key)
            deduped.append((name, candidate_period, candidate_bar))
    return deduped
