"""Deterministic paired evidence, censoring, and execution-cost analysis.

The replay engine produces one observation per candidate, cycle, and leg.  This
module turns those raw local windows into conservative candidate-versus-control
evidence without introducing network data or non-deterministic statistics.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict, deque
from typing import Any, Iterable

from .models import CandidateSummary, ReplayObservation
from .utils import median, timestamp_seconds

_USABLE_ATR_SOURCES = {"capture_reconstructed", "bot_captured_fallback"}
_BOOTSTRAP_REPLICATES = 2_000
_MIN_PAIRED_WINDOWS = 5
_MIN_BOTH_TRIGGERED = 3
_MIN_TRADING_DAYS = 5
_MIN_EXECUTION_SAMPLES = 5
_MIN_BOOTSTRAP_PROBABILITY_PCT = 80.0
_MAX_TRIGGER_PROBABILITY_DROP_PCT = 5.0
_MAX_MEDIAN_MAE_INCREASE_BPS = 25.0
_MAX_MEDIAN_ABSOLUTE_DELAY_INCREASE_SECONDS = 300.0
_MIN_SHARED_KM_HORIZON_SECONDS = 300.0
_MAX_EXECUTION_RESIDUAL_BPS = 1_000.0
_MAX_TOUCH_QUOTE_AGE_SECONDS = 5.0
_MAX_REFERENCE_QUOTE_AGE_SECONDS = 5.0
EVIDENCE_CONTRACT_VERSION = 2


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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


def _cycle_key(row: ReplayObservation) -> str:
    if row.cycle_id:
        return row.cycle_id
    if row.cycle_number is not None:
        return f"cycle-number:{row.cycle_number}"
    return f"unknown:{row.ticker}:{row.leg}:{row.actual_fill_time_utc}"


def _scoreable(row: ReplayObservation) -> bool:
    start = timestamp_seconds(row.observation_start_utc)
    end = timestamp_seconds(row.observation_end_utc)
    duration = _finite(row.observed_seconds)
    valid_window = bool(
        start is not None
        and end is not None
        and duration is not None
        and duration >= 0.0
        and end >= start
        and math.isclose(
            end - start,
            duration,
            rel_tol=0.0,
            abs_tol=1e-6,
        )
    )
    outcome = str(row.outcome or "").strip().lower()
    valid_outcome = bool(
        (
            row.triggered
            and not row.right_censored
            and outcome == "triggered"
        )
        or (
            not row.triggered
            and row.right_censored
            and outcome == "right_censored"
        )
        or (
            not row.triggered
            and not row.right_censored
            and outcome == "confirmed_no_trigger"
        )
    )
    return bool(
        not row.left_censored
        and valid_window
        and valid_outcome
        and row.atr_source in _USABLE_ATR_SOURCES
    )


def _adverse_residual(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    if abs(value) > _MAX_EXECUTION_RESIDUAL_BPS:
        return None
    return max(0.0, value)


def _valid_touch(*, bid: Any, ask: Any, leg: str) -> float | None:
    """Return the executable quote side and reject impossible crossed quotes.

    Older captures can contain only one quote side. That single side remains
    usable for the corresponding BUY or SELL estimate. If both sides exist and
    ask is below bid, neither side is trusted for execution modeling.
    """

    if leg not in {"buy", "sell"}:
        return None

    bid_value = _finite(bid)
    ask_value = _finite(ask)
    if bid_value is not None and bid_value <= 0:
        bid_value = None
    if ask_value is not None and ask_value <= 0:
        ask_value = None
    if (
        bid_value is not None
        and ask_value is not None
        and ask_value < bid_value
    ):
        return None
    return ask_value if leg == "buy" else bid_value


def _crossed_quote(*, bid: Any, ask: Any) -> bool:
    """Return whether two positive finite quote sides are crossed."""

    bid_value = _finite(bid)
    ask_value = _finite(ask)
    return bool(
        bid_value is not None
        and ask_value is not None
        and bid_value > 0
        and ask_value > 0
        and ask_value < bid_value
    )


def _execution_sample(row: ReplayObservation) -> dict[str, Any] | None:
    if row.leg not in {"buy", "sell"}:
        return None
    fill = _finite(row.actual_fill_price)
    reference = _finite(row.fill_reference_price)
    if fill is None or fill <= 0:
        return None
    beyond_touch = None
    total = None
    quote_age = _finite(row.fill_quote_age_seconds)
    touch = _valid_touch(bid=row.fill_bid, ask=row.fill_ask, leg=row.leg)
    crossed_touch_quote = _crossed_quote(bid=row.fill_bid, ask=row.fill_ask)
    touch_quote_usable = bool(
        quote_age is not None
        and 0.0 <= quote_age <= _MAX_TOUCH_QUOTE_AGE_SECONDS
        and touch is not None
    )
    reference_quote_usable = bool(
        quote_age is not None
        and 0.0 <= quote_age <= _MAX_REFERENCE_QUOTE_AGE_SECONDS
        and reference is not None
        and reference > 0
    )
    if row.leg == "buy":
        if touch_quote_usable and touch is not None:
            beyond_touch = (fill - touch) / touch * 10_000.0
        if reference_quote_usable and reference is not None:
            total = (fill - reference) / reference * 10_000.0
    else:
        if touch_quote_usable and touch is not None:
            beyond_touch = (touch - fill) / touch * 10_000.0
        if reference_quote_usable and reference is not None:
            total = (reference - fill) / reference * 10_000.0
    return {
        "cycle_key": _cycle_key(row),
        "trading_day_utc": row.trading_day_utc,
        "beyond_touch_bps": _adverse_residual(beyond_touch),
        "trigger_to_fill_bps": _adverse_residual(total),
        "spread_bps": (
            _finite(row.fill_spread_bps) if touch_quote_usable else None
        ),
        "quote_age_seconds": quote_age,
        "touch_quote_usable": touch_quote_usable,
        "reference_quote_usable": reference_quote_usable,
        "crossed_touch_quote": crossed_touch_quote,
    }


def _execution_context_conflicts(rows: list[ReplayObservation]) -> list[str]:
    """Return immutable fill-context fields that disagree within one cycle.

    Counterfactual candidates for the same cycle are expected to carry the
    exact same historical fill and quote context. Missing legacy fields may be
    completed by another row, but two incompatible non-missing values are an
    invariant violation. Such a cycle is excluded from the empirical model so
    candidate ordering cannot choose which historical reality to trust.
    """

    conflicts: list[str] = []

    def numeric_conflict(label: str, values: Iterable[Any]) -> None:
        finite = [number for value in values if (number := _finite(value)) is not None]
        if not finite:
            return
        reference = finite[0]
        if any(
            not math.isclose(value, reference, rel_tol=1e-10, abs_tol=1e-10)
            for value in finite[1:]
        ):
            conflicts.append(label)

    def text_conflict(label: str, values: Iterable[Any]) -> None:
        present = [str(value).strip() for value in values if str(value or "").strip()]
        if len(set(present)) > 1:
            conflicts.append(label)

    numeric_conflict("actual fill price", (row.actual_fill_price for row in rows))
    timestamps = [
        timestamp_seconds(row.actual_fill_time_utc)
        for row in rows
        if str(row.actual_fill_time_utc or "").strip()
    ]
    numeric_conflict("actual fill time", timestamps)
    text_conflict("trading day", (row.trading_day_utc for row in rows))
    numeric_conflict("fill reference price", (row.fill_reference_price for row in rows))
    numeric_conflict("fill bid", (row.fill_bid for row in rows))
    numeric_conflict("fill ask", (row.fill_ask for row in rows))
    numeric_conflict("fill spread", (row.fill_spread_bps for row in rows))
    numeric_conflict("fill quote age", (row.fill_quote_age_seconds for row in rows))
    return conflicts


def _unique_execution_samples(
    observations: Iterable[ReplayObservation],
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, list[str]]],
]:
    grouped: dict[tuple[str, str], list[ReplayObservation]] = defaultdict(list)
    for row in observations:
        grouped[(row.leg, _cycle_key(row))].append(row)

    selected: dict[tuple[str, str], ReplayObservation] = {}
    conflicts: dict[str, dict[str, list[str]]] = {"buy": {}, "sell": {}}
    for key, rows in sorted(grouped.items()):
        leg, cycle = key
        fields = _execution_context_conflicts(rows)
        if fields:
            conflicts.setdefault(leg, {})[cycle] = fields
            continue

        # Compatible legacy rows may differ only because one omits context that
        # another row preserves. Prefer the most complete sample, then the
        # explicit control, then a stable candidate key.
        def sample_quality(row: ReplayObservation) -> tuple[int, int, int, str]:
            sample = _execution_sample(row)
            execution_completeness = 0
            context_completeness = 0
            if sample is not None:
                execution_completeness = sum(
                    sample.get(field) is not None
                    for field in (
                        "beyond_touch_bps",
                        "trigger_to_fill_bps",
                        "spread_bps",
                    )
                )
                context_completeness = sum(
                    (
                        bool(sample.get("trading_day_utc")),
                        sample.get("quote_age_seconds") is not None,
                        bool(sample.get("touch_quote_usable")),
                        bool(sample.get("reference_quote_usable")),
                    )
                )
            return (
                -execution_completeness,
                -context_completeness,
                0 if row.control_candidate else 1,
                row.candidate_key,
            )

        selected[key] = sorted(rows, key=sample_quality)[0]
    result: dict[str, list[dict[str, Any]]] = {"buy": [], "sell": []}
    for (leg, _), row in sorted(selected.items()):
        sample = _execution_sample(row)
        if sample is not None:
            result.setdefault(leg, []).append(sample)
    return result, conflicts


def _execution_model_for_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    touch = [
        float(row["beyond_touch_bps"])
        for row in samples
        if row.get("beyond_touch_bps") is not None
    ]
    total = [
        float(row["trigger_to_fill_bps"])
        for row in samples
        if row.get("trigger_to_fill_bps") is not None
    ]
    spreads = [
        float(row["spread_bps"])
        for row in samples
        if row.get("spread_bps") is not None
        and float(row["spread_bps"]) >= 0
    ]
    quote_ages = [
        float(row["quote_age_seconds"])
        for row in samples
        if row.get("quote_age_seconds") is not None
        and float(row["quote_age_seconds"]) >= 0
    ]
    fresh_touch_samples = sum(
        1 for row in samples if bool(row.get("touch_quote_usable"))
    )
    fresh_reference_samples = sum(
        1 for row in samples if bool(row.get("reference_quote_usable"))
    )
    crossed_touch_samples = sum(
        1 for row in samples if bool(row.get("crossed_touch_quote"))
    )
    days = sorted(
        {
            str(row.get("trading_day_utc") or "")
            for row in samples
            if row.get("trading_day_utc")
        }
    )
    sample_count = max(len(touch), len(total))
    if sample_count >= 20 and len(days) >= 10:
        evidence = "strong"
    elif sample_count >= 5 and len(days) >= 3:
        evidence = "moderate"
    else:
        evidence = "limited"
    return {
        "fills_considered": len(samples),
        "touch_residual_samples": len(touch),
        "trigger_to_fill_samples": len(total),
        "trading_days": len(days),
        "fresh_touch_quote_samples": fresh_touch_samples,
        "fresh_reference_quote_samples": fresh_reference_samples,
        "crossed_touch_quotes_excluded": crossed_touch_samples,
        "stale_or_unknown_touch_quote_samples": max(
            0,
            len(samples) - fresh_touch_samples - crossed_touch_samples,
        ),
        "stale_or_unknown_reference_quote_samples": max(
            0,
            len(samples) - fresh_reference_samples,
        ),
        "median_fill_quote_age_seconds": _quantile(quote_ages, 0.5),
        "maximum_touch_quote_age_seconds": _MAX_TOUCH_QUOTE_AGE_SECONDS,
        "maximum_reference_quote_age_seconds": _MAX_REFERENCE_QUOTE_AGE_SECONDS,
        "median_spread_bps": _quantile(spreads, 0.5),
        "p75_adverse_beyond_touch_bps": _quantile(touch, 0.75),
        "p75_adverse_trigger_to_fill_bps": _quantile(total, 0.75),
        "evidence": evidence,
        "policy": (
            "Candidate fills use the contemporaneous bid/ask touch plus the "
            "75th-percentile non-negative residual beyond that touch. If a "
            "candidate trigger quote is unavailable, the 75th-percentile "
            "adverse Last-to-fill residual is used. The candidate cycle is "
            "excluded from its own empirical model whenever another sample exists. "
            "Fill-time bid/ask and Last-reference evidence older than five seconds "
            "is not used for execution residuals. Crossed quotes are rejected rather than "
            "being treated as executable top-of-book evidence. Conflicting fill "
            "or quote context across candidate rows for one cycle is excluded fail-closed."
        ),
    }


def apply_execution_adjustments(
    observations: list[ReplayObservation],
) -> dict[str, Any]:
    """Estimate conservative fills and mutate triggered observations in place."""

    samples_by_leg, conflicts_by_leg = _unique_execution_samples(observations)
    aggregate: dict[str, Any] = {}
    for leg in ("buy", "sell"):
        model = _execution_model_for_samples(samples_by_leg.get(leg, []))
        conflicts = dict(sorted((conflicts_by_leg.get(leg) or {}).items()))
        model["conflicting_cycle_contexts_excluded"] = len(conflicts)
        model["conflicting_cycle_context_fields"] = conflicts
        aggregate[leg] = model
    for row in observations:
        if row.leg not in {"buy", "sell"} or not row.triggered:
            continue
        trigger = _finite(row.trigger_price)
        actual = _finite(row.actual_fill_price)
        if trigger is None or trigger <= 0 or actual is None or actual <= 0:
            continue
        cycle = _cycle_key(row)
        all_samples = samples_by_leg.get(row.leg, [])
        own_sample_present = any(
            sample.get("cycle_key") == cycle for sample in all_samples
        )
        leave_one_out = [
            sample for sample in all_samples if sample.get("cycle_key") != cycle
        ]
        use_leave_one_out = own_sample_present and bool(leave_one_out)
        model_samples = leave_one_out if use_leave_one_out else all_samples
        model = _execution_model_for_samples(model_samples)
        used_leave_one_out = use_leave_one_out

        residual: float | None
        residual_samples: int
        method: str
        if row.leg == "buy":
            touch = _valid_touch(
                bid=row.trigger_bid,
                ask=row.trigger_ask,
                leg=row.leg,
            )
            if touch is not None:
                base = max(trigger, touch)
                residual = _finite(model.get("p75_adverse_beyond_touch_bps"))
                residual_samples = int(model.get("touch_residual_samples") or 0)
                method = "ask touch"
                if residual is not None:
                    method += " plus empirical p75 beyond-touch residual"
            else:
                base = trigger
                residual = _finite(model.get("p75_adverse_trigger_to_fill_bps"))
                residual_samples = int(
                    model.get("trigger_to_fill_samples") or 0
                )
                method = "Last trigger"
                if residual is not None:
                    method += " plus empirical p75 trigger-to-fill residual"
            residual = max(0.0, residual or 0.0)
            estimated = base * (1.0 + residual / 10_000.0)
            adjustment = (estimated - trigger) / trigger * 10_000.0
            adjusted_improvement = (actual - estimated) / actual * 10_000.0
        else:
            touch = _valid_touch(
                bid=row.trigger_bid,
                ask=row.trigger_ask,
                leg=row.leg,
            )
            if touch is not None:
                base = min(trigger, touch)
                residual = _finite(model.get("p75_adverse_beyond_touch_bps"))
                residual_samples = int(model.get("touch_residual_samples") or 0)
                method = "bid touch"
                if residual is not None:
                    method += " minus empirical p75 beyond-touch residual"
            else:
                base = trigger
                residual = _finite(model.get("p75_adverse_trigger_to_fill_bps"))
                residual_samples = int(
                    model.get("trigger_to_fill_samples") or 0
                )
                method = "Last trigger"
                if residual is not None:
                    method += " minus empirical p75 trigger-to-fill residual"
            residual = max(0.0, residual or 0.0)
            estimated = base * (1.0 - residual / 10_000.0)
            adjustment = (trigger - estimated) / trigger * 10_000.0
            adjusted_improvement = (estimated - actual) / actual * 10_000.0

        row.estimated_fill_price = round(max(0.0, estimated), 8)
        row.execution_adjustment_bps = round(adjustment, 8)
        row.adjusted_price_improvement_bps = round(adjusted_improvement, 8)
        if residual_samples == 0:
            method += "; no empirical residual sample was available"
        row.execution_model_method = method
        row.execution_model_samples = residual_samples
        row.execution_model_leave_one_out = used_leave_one_out
    return aggregate


def kaplan_meier_trigger_probability(
    rows: list[ReplayObservation],
    horizon_seconds: float,
) -> float | None:
    durations = [
        (float(row.observed_seconds), bool(row.triggered))
        for row in rows
        if row.observed_seconds is not None
        and math.isfinite(float(row.observed_seconds))
        and float(row.observed_seconds) >= 0
    ]
    if not durations:
        return None
    survival = 1.0
    event_times = sorted(
        {
            duration
            for duration, event in durations
            if event and duration <= horizon_seconds
        }
    )
    for event_time in event_times:
        at_risk = sum(1 for duration, _ in durations if duration >= event_time)
        events = sum(
            1
            for duration, event in durations
            if event
            and math.isclose(duration, event_time, rel_tol=0.0, abs_tol=1e-9)
        )
        if at_risk:
            survival *= max(0.0, 1.0 - events / at_risk)
    if survival > 0.0 and not any(
        duration >= horizon_seconds for duration, _ in durations
    ):
        return None
    return (1.0 - survival) * 100.0


def _deterministic_index(seed: str, replicate: int, draw: int, size: int) -> int:
    payload = f"{seed}|{replicate}|{draw}".encode("utf-8")
    value = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    return value % size


def _cluster_bootstrap(
    values_by_day: dict[str, list[float]],
    *,
    seed: str,
) -> dict[str, float | int | None]:
    days = sorted(day for day, values in values_by_day.items() if values)
    observed = [value for day in days for value in values_by_day[day]]
    result: dict[str, float | int | None] = {
        "replicates": 0,
        "trading_days": len(days),
        "median_delta_bps": median(observed),
        "ci80_low_bps": None,
        "ci80_high_bps": None,
        "ci95_low_bps": None,
        "ci95_high_bps": None,
        # The longer names are the public/report-facing contract.  The short
        # aliases remain for backward-compatible JSON consumers introduced
        # during the v1.3 development cycle.
        "ci80_lower_bps": None,
        "ci80_upper_bps": None,
        "ci95_lower_bps": None,
        "ci95_upper_bps": None,
        "probability_positive_pct": None,
    }
    if len(days) < 3 or len(observed) < 3:
        return result
    statistics: list[float] = []
    for replicate in range(_BOOTSTRAP_REPLICATES):
        sample: list[float] = []
        for draw in range(len(days)):
            day = days[_deterministic_index(seed, replicate, draw, len(days))]
            sample.extend(values_by_day[day])
        statistic = median(sample)
        if statistic is not None and math.isfinite(statistic):
            statistics.append(statistic)
    if not statistics:
        return result
    ci80_lower = _quantile(statistics, 0.10)
    ci80_upper = _quantile(statistics, 0.90)
    ci95_lower = _quantile(statistics, 0.025)
    ci95_upper = _quantile(statistics, 0.975)
    result.update(
        {
            "replicates": len(statistics),
            "ci80_low_bps": ci80_lower,
            "ci80_high_bps": ci80_upper,
            "ci95_low_bps": ci95_lower,
            "ci95_high_bps": ci95_upper,
            "ci80_lower_bps": ci80_lower,
            "ci80_upper_bps": ci80_upper,
            "ci95_lower_bps": ci95_lower,
            "ci95_upper_bps": ci95_upper,
            "probability_positive_pct": (
                sum(1 for value in statistics if value > 0.0)
                / len(statistics)
                * 100.0
            ),
        }
    )
    return result


def _leave_one_day_out(values_by_day: dict[str, list[float]]) -> dict[str, Any]:
    days = sorted(day for day, values in values_by_day.items() if values)
    full_values = [value for day in days for value in values_by_day[day]]
    full = median(full_values)
    estimates: list[float] = []
    influential_day = ""
    largest_change = -1.0
    if len(days) >= 3 and full is not None:
        for omitted in days:
            remaining = [
                value
                for day in days
                if day != omitted
                for value in values_by_day[day]
            ]
            estimate = median(remaining)
            if estimate is None:
                continue
            estimates.append(estimate)
            change = abs(estimate - full)
            if change > largest_change or (
                math.isclose(change, largest_change) and omitted < influential_day
            ):
                influential_day = omitted
                largest_change = change
    sign_reversals = 0
    if full is not None and not math.isclose(full, 0.0, abs_tol=1e-12):
        sign = 1 if full > 0 else -1
        sign_reversals = sum(
            1
            for estimate in estimates
            if estimate != 0.0 and (1 if estimate > 0 else -1) != sign
        )
    minimum = min(estimates) if estimates else None
    median_estimate = median(estimates)
    maximum = max(estimates) if estimates else None
    return {
        "trading_days": len(days),
        "estimates": len(estimates),
        "min_delta_bps": minimum,
        "median_delta_bps": median_estimate,
        "max_delta_bps": maximum,
        "minimum_bps": minimum,
        "median_bps": median_estimate,
        "maximum_bps": maximum,
        "sign_reversals": sign_reversals,
        "positive_pct": (
            sum(1 for value in estimates if value > 0.0) / len(estimates) * 100.0
            if estimates
            else None
        ),
        "most_influential_day": influential_day,
        "largest_absolute_change_bps": (
            largest_change if largest_change >= 0 else None
        ),
    }


def _paired_evidence(
    candidate_rows: list[ReplayObservation],
    control_rows: list[ReplayObservation],
    *,
    seed: str,
) -> dict[str, Any]:
    def index_rows(
        rows: list[ReplayObservation],
    ) -> tuple[dict[str, ReplayObservation], list[str]]:
        grouped: dict[str, list[ReplayObservation]] = defaultdict(list)
        for row in sorted(
            (item for item in rows if _scoreable(item)),
            key=lambda item: (
                _cycle_key(item),
                item.observation_start_utc,
                item.observation_end_utc,
                item.candidate_key,
            ),
        ):
            key = _cycle_key(row)
            grouped[key].append(row)
        duplicate_keys = sorted(
            key for key, values in grouped.items() if len(values) != 1
        )
        # Exclude duplicate cycle/candidate rows rather than choosing one by
        # sort order. A duplicate is an invariant violation and must never be
        # able to improve the evidence for a setting.
        indexed = {
            key: values[0]
            for key, values in sorted(grouped.items())
            if len(values) == 1
        }
        return indexed, duplicate_keys

    candidate_by_cycle, candidate_duplicates = index_rows(candidate_rows)
    control_by_cycle, control_duplicates = index_rows(control_rows)
    cycle_keys = sorted(set(candidate_by_cycle) & set(control_by_cycle))

    def pair_context_issue(
        candidate: ReplayObservation,
        control: ReplayObservation,
    ) -> str:
        issues: list[str] = []

        def compare_optional_number(
            label: str,
            candidate_value: Any,
            control_value: Any,
            *,
            relative_tolerance: float = 1e-10,
            absolute_tolerance: float = 1e-10,
        ) -> None:
            candidate_number = _finite(candidate_value)
            control_number = _finite(control_value)
            if (candidate_number is None) != (control_number is None):
                issues.append(label)
                return
            if (
                candidate_number is not None
                and control_number is not None
                and not math.isclose(
                    candidate_number,
                    control_number,
                    rel_tol=relative_tolerance,
                    abs_tol=absolute_tolerance,
                )
            ):
                issues.append(label)

        if candidate.ticker != control.ticker:
            issues.append("ticker")
        if candidate.leg != control.leg:
            issues.append("leg")
        compare_optional_number(
            "actual fill price",
            candidate.actual_fill_price,
            control.actual_fill_price,
        )
        candidate_time = timestamp_seconds(candidate.actual_fill_time_utc)
        control_time = timestamp_seconds(control.actual_fill_time_utc)
        if (candidate_time is None) != (control_time is None) or (
            candidate_time is not None
            and control_time is not None
            and not math.isclose(
                candidate_time,
                control_time,
                rel_tol=0.0,
                abs_tol=1e-6,
            )
        ):
            issues.append("actual fill time")
        if candidate.trading_day_utc != control.trading_day_utc:
            issues.append("trading day")
        candidate_start = timestamp_seconds(candidate.observation_start_utc)
        control_start = timestamp_seconds(control.observation_start_utc)
        if (candidate_start is None) != (control_start is None) or (
            candidate_start is not None
            and control_start is not None
            and not math.isclose(
                candidate_start,
                control_start,
                rel_tol=0.0,
                abs_tol=1e-6,
            )
        ):
            issues.append("observation start")
        candidate_profile = candidate.historical_atr_profile_id
        control_profile = control.historical_atr_profile_id
        if candidate_profile != control_profile:
            issues.append("historical ATR profile")
        for label, candidate_value, control_value in (
            (
                "fill reference price",
                candidate.fill_reference_price,
                control.fill_reference_price,
            ),
            ("fill bid", candidate.fill_bid, control.fill_bid),
            ("fill ask", candidate.fill_ask, control.fill_ask),
            (
                "fill quote age",
                candidate.fill_quote_age_seconds,
                control.fill_quote_age_seconds,
            ),
        ):
            compare_optional_number(label, candidate_value, control_value)
        return ", ".join(issues)

    pairs: list[tuple[ReplayObservation, ReplayObservation]] = []
    context_mismatches: dict[str, str] = {}
    for key in cycle_keys:
        pair = (candidate_by_cycle[key], control_by_cycle[key])
        issue = pair_context_issue(*pair)
        if issue:
            context_mismatches[key] = issue
        else:
            pairs.append(pair)
    both_triggered = [
        pair for pair in pairs if pair[0].triggered and pair[1].triggered
    ]
    candidate_only = sum(
        1 for candidate, control in pairs if candidate.triggered and not control.triggered
    )
    control_only = sum(
        1 for candidate, control in pairs if control.triggered and not candidate.triggered
    )
    both_censored = sum(
        1
        for candidate, control in pairs
        if candidate.right_censored and control.right_censored
    )

    values_by_day: dict[str, list[float]] = defaultdict(list)
    all_deltas: list[float] = []
    dated_execution_sample_counts: list[int] = []
    no_day_cycle_keys: list[str] = []
    delay_deltas: list[float] = []
    mae_deltas: list[float] = []
    adjusted_pairs = 0
    execution_sample_counts: list[int] = []
    for candidate, control in both_triggered:
        candidate_value = _finite(candidate.adjusted_price_improvement_bps)
        control_value = _finite(control.adjusted_price_improvement_bps)
        if candidate_value is not None and control_value is not None:
            adjusted_pairs += 1
            delta = candidate_value - control_value
            all_deltas.append(delta)
            day = candidate.trading_day_utc or control.trading_day_utc
            if day:
                values_by_day[day].append(delta)
                dated_execution_sample_counts.append(
                    min(
                        int(candidate.execution_model_samples or 0),
                        int(control.execution_model_samples or 0),
                    )
                )
            else:
                no_day_cycle_keys.append(_cycle_key(candidate))
            execution_sample_counts.append(
                min(
                    int(candidate.execution_model_samples or 0),
                    int(control.execution_model_samples or 0),
                )
            )
        candidate_delay = _finite(candidate.delay_seconds)
        control_delay = _finite(control.delay_seconds)
        if candidate_delay is not None and control_delay is not None:
            delay_deltas.append(abs(candidate_delay) - abs(control_delay))
        candidate_mae = _finite(candidate.post_trigger_mae_bps)
        control_mae = _finite(control.post_trigger_mae_bps)
        if candidate_mae is not None and control_mae is not None:
            mae_deltas.append(candidate_mae - control_mae)

    bootstrap = _cluster_bootstrap(values_by_day, seed=seed)
    influence = _leave_one_day_out(values_by_day)
    candidate_paired_rows = [row for row, _ in pairs]
    control_paired_rows = [row for _, row in pairs]
    candidate_km_by_horizon = {
        "1m_pct": kaplan_meier_trigger_probability(candidate_paired_rows, 60.0),
        "5m_pct": kaplan_meier_trigger_probability(candidate_paired_rows, 300.0),
        "15m_pct": kaplan_meier_trigger_probability(candidate_paired_rows, 900.0),
    }
    control_km_by_horizon = {
        "1m_pct": kaplan_meier_trigger_probability(control_paired_rows, 60.0),
        "5m_pct": kaplan_meier_trigger_probability(control_paired_rows, 300.0),
        "15m_pct": kaplan_meier_trigger_probability(control_paired_rows, 900.0),
    }
    candidate_km = candidate_km_by_horizon["15m_pct"]
    control_km = control_km_by_horizon["15m_pct"]
    probability_delta = (
        candidate_km - control_km
        if candidate_km is not None and control_km is not None
        else None
    )
    selected_horizon_label = ""
    selected_horizon_seconds: float | None = None
    selected_candidate_probability: float | None = None
    selected_control_probability: float | None = None
    for label, seconds in (("15m_pct", 900.0), ("5m_pct", 300.0), ("1m_pct", 60.0)):
        candidate_probability = candidate_km_by_horizon[label]
        control_probability = control_km_by_horizon[label]
        if candidate_probability is None or control_probability is None:
            continue
        selected_horizon_label = label
        selected_horizon_seconds = seconds
        selected_candidate_probability = candidate_probability
        selected_control_probability = control_probability
        break
    selected_probability_delta = (
        selected_candidate_probability - selected_control_probability
        if selected_candidate_probability is not None
        and selected_control_probability is not None
        else None
    )
    dated_deltas = [
        value for day in sorted(values_by_day) for value in values_by_day[day]
    ]
    median_delta = median(dated_deltas)
    independent_days = len(values_by_day)
    return {
        "control_candidate_key": (
            control_rows[0].candidate_key if control_rows else ""
        ),
        "candidate_duplicate_cycle_keys_excluded": candidate_duplicates,
        "control_duplicate_cycle_keys_excluded": control_duplicates,
        "context_mismatch_cycle_keys_excluded": dict(sorted(context_mismatches.items())),
        "paired_cycles": len(pairs),
        "paired_windows": len(pairs),
        "both_triggered_cycles": len(both_triggered),
        "paired_both_triggered": len(both_triggered),
        "paired_execution_adjusted_cycles": adjusted_pairs,
        "paired_execution_adjusted_dated_cycles": sum(
            len(values) for values in values_by_day.values()
        ),
        "paired_price_delta_cycle_keys_without_trading_day": sorted(no_day_cycle_keys),
        "minimum_execution_model_samples": (
            min(dated_execution_sample_counts)
            if dated_execution_sample_counts
            else 0
        ),
        "minimum_execution_model_samples_all_adjusted_pairs": (
            min(execution_sample_counts) if execution_sample_counts else 0
        ),
        "candidate_only_triggered_cycles": candidate_only,
        "control_only_triggered_cycles": control_only,
        "both_right_censored_cycles": both_censored,
        "paired_trading_days": len(values_by_day),
        "independent_trading_days": independent_days,
        "median_execution_adjusted_delta_bps": median_delta,
        "paired_execution_adjusted_median_bps": median_delta,
        "paired_execution_adjusted_median_all_pairs_bps": median(all_deltas),
        "median_absolute_delay_delta_seconds": median(delay_deltas),
        "median_mae_delta_bps": median(mae_deltas),
        "candidate_km_trigger_probability_15m_pct": candidate_km,
        "control_km_trigger_probability_15m_pct": control_km,
        "candidate_km_trigger_probability": candidate_km_by_horizon,
        "control_km_trigger_probability": control_km_by_horizon,
        "km_trigger_probability_delta_15m_pct": probability_delta,
        "selected_km_horizon": selected_horizon_label,
        "selected_km_horizon_seconds": selected_horizon_seconds,
        "selected_candidate_km_trigger_probability_pct": selected_candidate_probability,
        "selected_control_km_trigger_probability_pct": selected_control_probability,
        "selected_km_trigger_probability_delta_pct": selected_probability_delta,
        "bootstrap": bootstrap,
        "leave_one_day_out": influence,
        "comparison_policy": (
            "Only cycles where both candidate and exact control have usable ATR and complete context are paired. "
            "Price deltas use only pairs where both triggered. Trigger availability uses Kaplan-Meier estimates over "
            "the same paired cycle set at the longest horizon supported for both settings (15, 5, then 1 minute)."
        ),
    }


def _base_instability_reasons(
    summary: CandidateSummary,
    execution_model: dict[str, Any],
) -> list[str]:
    if summary.control_candidate:
        return ["This row is the evaluation control, not a changed candidate."]
    evidence = summary.paired_evidence
    reasons: list[str] = []
    if int(execution_model.get("conflicting_cycle_contexts_excluded") or 0):
        reasons.append(
            "The empirical execution model excluded conflicting fill or quote context for one or more cycles."
        )
    if evidence.get("candidate_duplicate_cycle_keys_excluded"):
        reasons.append(
            "Duplicate candidate observations were found for one or more cycles; those cycles were excluded."
        )
    if evidence.get("control_duplicate_cycle_keys_excluded"):
        reasons.append(
            "Duplicate control observations were found for one or more cycles; those cycles were excluded."
        )
    if evidence.get("context_mismatch_cycle_keys_excluded"):
        reasons.append(
            "Candidate/control rows disagreed on immutable cycle context; those cycles were excluded."
        )
    paired = int(evidence.get("paired_cycles") or 0)
    both_triggered = int(evidence.get("both_triggered_cycles") or 0)
    days = int(evidence.get("paired_trading_days") or 0)
    if paired < _MIN_PAIRED_WINDOWS:
        reasons.append(
            f"Only {paired} same-cycle candidate/control pairs; at least {_MIN_PAIRED_WINDOWS} are required."
        )
    if both_triggered < _MIN_BOTH_TRIGGERED:
        reasons.append(
            f"Only {both_triggered} paired cycles triggered for both settings; at least {_MIN_BOTH_TRIGGERED} are required."
        )
    if days < _MIN_TRADING_DAYS:
        reasons.append(
            f"Only {days} independent trading days contribute paired price deltas; at least {_MIN_TRADING_DAYS} are required."
        )
    paired_sample_value = evidence.get("minimum_execution_model_samples")
    if paired_sample_value is None:
        # Compatibility fallback for older serialized evidence that predates
        # per-observation model-sample counts. A present value of zero is real
        # evidence that the paired estimate used no empirical residual and must
        # not be replaced by a larger aggregate sample count.
        model_samples = max(
            int(execution_model.get("touch_residual_samples") or 0),
            int(execution_model.get("trigger_to_fill_samples") or 0),
        )
    else:
        model_samples = int(paired_sample_value or 0)
    if model_samples < _MIN_EXECUTION_SAMPLES:
        reasons.append(
            f"Only {model_samples} empirical execution samples; at least {_MIN_EXECUTION_SAMPLES} are required."
        )
    bootstrap = dict(evidence.get("bootstrap") or {})
    ci80_low = _finite(bootstrap.get("ci80_low_bps"))
    probability = _finite(bootstrap.get("probability_positive_pct"))
    if ci80_low is None or ci80_low <= 0:
        reasons.append("The trading-day bootstrap 80% interval does not stay above zero.")
    if probability is None or probability < _MIN_BOOTSTRAP_PROBABILITY_PCT:
        reasons.append(
            "The trading-day bootstrap probability of a positive paired delta is below "
            f"{_MIN_BOOTSTRAP_PROBABILITY_PCT:.0f}%."
        )
    influence = dict(evidence.get("leave_one_day_out") or {})
    loo_min = _finite(influence.get("min_delta_bps"))
    if loo_min is None or loo_min <= 0:
        reasons.append("At least one leave-one-day-out estimate is zero, negative, or unavailable.")
    if int(influence.get("sign_reversals") or 0):
        reasons.append("Removing one trading day reverses the sign of the paired result.")
    shared_horizon = _finite(evidence.get("selected_km_horizon_seconds"))
    if shared_horizon is None or shared_horizon < _MIN_SHARED_KM_HORIZON_SECONDS:
        reasons.append(
            "Candidate and control do not share censoring-aware follow-up through at least five minutes."
        )
    probability_delta = _finite(
        evidence.get("selected_km_trigger_probability_delta_pct")
    )
    if (
        probability_delta is not None
        and probability_delta < -_MAX_TRIGGER_PROBABILITY_DROP_PCT
    ):
        reasons.append(
            "The longest adequately supported censoring-aware trigger probability is more than "
            f"{_MAX_TRIGGER_PROBABILITY_DROP_PCT:.0f} percentage points below the control."
        )
    delay_delta = _finite(evidence.get("median_absolute_delay_delta_seconds"))
    if both_triggered >= _MIN_BOTH_TRIGGERED and delay_delta is None:
        reasons.append(
            "Comparable trigger-timing evidence is unavailable for the paired cycles."
        )
    elif (
        delay_delta is not None
        and delay_delta > _MAX_MEDIAN_ABSOLUTE_DELAY_INCREASE_SECONDS
    ):
        reasons.append(
            "Median absolute trigger timing error increases by more than "
            f"{_MAX_MEDIAN_ABSOLUTE_DELAY_INCREASE_SECONDS / 60.0:.0f} minutes versus the control."
        )
    mae_delta = _finite(evidence.get("median_mae_delta_bps"))
    if both_triggered >= _MIN_BOTH_TRIGGERED and mae_delta is None:
        reasons.append(
            "Comparable post-trigger adverse-excursion evidence is unavailable for the paired cycles."
        )
    elif mae_delta is not None and mae_delta > _MAX_MEDIAN_MAE_INCREASE_BPS:
        reasons.append(
            f"Median adverse excursion increases by more than {_MAX_MEDIAN_MAE_INCREASE_BPS:.0f} bps."
        )
    median_delta = _finite(evidence.get("median_execution_adjusted_delta_bps"))
    if median_delta is None or median_delta <= 0:
        reasons.append("The paired median execution-adjusted price delta is not positive.")
    return reasons


def enrich_paired_evidence(
    observations: list[ReplayObservation],
    summaries: list[CandidateSummary],
    execution_models: dict[str, Any],
) -> None:
    """Attach same-cycle evidence to every candidate summary in place."""

    rows_by_key: dict[tuple[str, str], list[ReplayObservation]] = defaultdict(list)
    control_keys_by_leg: dict[str, set[str]] = defaultdict(set)
    for row in observations:
        rows_by_key[(row.leg, row.candidate_key)].append(row)
        if row.control_candidate:
            control_keys_by_leg[row.leg].add(row.candidate_key)

    for summary in summaries:
        control_keys = sorted(control_keys_by_leg.get(summary.leg, set()))
        control_key = control_keys[0] if len(control_keys) == 1 else ""
        candidate_rows = rows_by_key.get((summary.leg, summary.candidate_key), [])
        control_rows = rows_by_key.get((summary.leg, control_key), [])
        summary.control_candidate = summary.candidate_key == control_key and bool(control_key)
        model = dict(execution_models.get(summary.leg) or {})
        summary.execution_model = model
        ticker = ""
        if candidate_rows:
            ticker = candidate_rows[0].ticker
        elif control_rows:
            ticker = control_rows[0].ticker
        summary.paired_evidence = _paired_evidence(
            candidate_rows,
            control_rows,
            seed=(
                f"paired-v1.3|{ticker}|{summary.leg}|"
                f"{summary.candidate_key}|{control_key}"
            ),
        )
        summary.instability_reasons = _base_instability_reasons(summary, model)
        if len(control_keys) > 1:
            summary.instability_reasons.append(
                "More than one evaluation-control candidate was marked for this replay leg."
            )
        elif not control_key:
            summary.instability_reasons.append(
                "No unambiguous evaluation-control candidate was available for this replay leg."
            )
        summary.evidence_stable = False


def _summary_delta(summary: CandidateSummary) -> float | None:
    return _finite(
        summary.paired_evidence.get("median_execution_adjusted_delta_bps")
    )


def _summary_delta_or(summary: CandidateSummary, fallback: float) -> float:
    """Return a finite paired delta without treating a legitimate zero as missing."""

    value = _summary_delta(summary)
    return value if value is not None else fallback


def _adjacent(
    left: CandidateSummary,
    right: CandidateSummary,
    *,
    multipliers: list[float],
    profits: list[float],
) -> bool:
    if left.leg == "buy":
        return abs(multipliers.index(left.multiplier) - multipliers.index(right.multiplier)) == 1
    left_profit = float(left.minimum_profit_multiplier or 0.0)
    right_profit = float(right.minimum_profit_multiplier or 0.0)
    trail_distance = abs(
        multipliers.index(left.multiplier) - multipliers.index(right.multiplier)
    )
    profit_distance = abs(profits.index(left_profit) - profits.index(right_profit))
    return (trail_distance == 1 and profit_distance == 0) or (
        trail_distance == 0 and profit_distance == 1
    )


def _region_center(component: list[CandidateSummary]) -> CandidateSummary:
    trail_min = min(row.multiplier for row in component)
    trail_max = max(row.multiplier for row in component)
    trail_mid = (trail_min + trail_max) / 2.0
    profits = [
        float(row.minimum_profit_multiplier)
        for row in component
        if row.minimum_profit_multiplier is not None
    ]
    profit_mid = (min(profits) + max(profits)) / 2.0 if profits else 0.0
    trail_scale = max(1e-12, trail_max - trail_min)
    profit_scale = max(1e-12, (max(profits) - min(profits)) if profits else 0.0)

    def distance(row: CandidateSummary) -> tuple[float, float, str]:
        trail_distance = abs(row.multiplier - trail_mid) / trail_scale
        profit_distance = 0.0
        if profits and row.minimum_profit_multiplier is not None:
            profit_distance = (
                abs(float(row.minimum_profit_multiplier) - profit_mid)
                / profit_scale
            )
        delta = _summary_delta(row)
        return (
            trail_distance + profit_distance,
            -(delta if delta is not None else -math.inf),
            row.candidate_key,
        )

    return sorted(component, key=distance)[0]


def _connected_components(
    rows: list[CandidateSummary],
    *,
    multipliers: list[float],
    profits: list[float],
) -> list[list[CandidateSummary]]:
    """Return deterministic grid-connected components for ``rows``."""

    remaining = {row.candidate_key: row for row in rows}
    components: list[list[CandidateSummary]] = []
    while remaining:
        start_key = min(remaining)
        pending: deque[CandidateSummary] = deque([remaining.pop(start_key)])
        component: list[CandidateSummary] = []
        while pending:
            current = pending.popleft()
            component.append(current)
            adjacent_keys = sorted(
                key
                for key, other in remaining.items()
                if _adjacent(
                    current,
                    other,
                    multipliers=multipliers,
                    profits=profits,
                )
            )
            for key in adjacent_keys:
                pending.append(remaining.pop(key))
        components.append(sorted(component, key=lambda item: item.candidate_key))
    return components


def detect_stable_regions(summaries: list[CandidateSummary]) -> None:
    """Find robust adjacent plateaus and finalize candidate stability.

    The highest isolated grid point is deliberately not allowed to suppress a
    slightly lower but genuinely adjacent plateau.  For each local peak, the
    function builds the connected near-best component around that peak, then
    chooses the supported component with the strongest worst-point evidence.
    Only the deterministic center of that preferred component is eligible for
    the single primary setting suggestion.
    """

    groups: dict[tuple[str, int, int], list[CandidateSummary]] = defaultdict(list)
    for summary in summaries:
        groups[(summary.leg, summary.period, summary.bar_seconds)].append(summary)

    for (leg, period, bar_seconds), rows in sorted(groups.items()):
        eligible = [
            row
            for row in rows
            if not row.instability_reasons
            and (delta := _summary_delta(row)) is not None
            and delta > 0.0
            and int(row.paired_evidence.get("paired_cycles") or 0)
            >= _MIN_PAIRED_WINDOWS
            and int(row.paired_evidence.get("both_triggered_cycles") or 0)
            >= _MIN_BOTH_TRIGGERED
        ]
        if not eligible:
            continue

        multipliers = sorted({row.multiplier for row in rows})
        profits = sorted(
            {float(row.minimum_profit_multiplier or 0.0) for row in rows}
        )
        def neighbors(row: CandidateSummary) -> list[CandidateSummary]:
            return sorted(
                (
                    other
                    for other in eligible
                    if other.candidate_key != row.candidate_key
                    and _adjacent(
                        row,
                        other,
                        multipliers=multipliers,
                        profits=profits,
                    )
                ),
                key=lambda item: item.candidate_key,
            )

        local_peaks = [
            row
            for row in eligible
            if not any(
                _summary_delta_or(other, -math.inf)
                > _summary_delta_or(row, -math.inf) + 1e-12
                for other in neighbors(row)
            )
        ]
        region_sets: dict[tuple[str, ...], dict[str, Any]] = {}
        for peak in sorted(local_peaks, key=lambda item: item.candidate_key):
            peak_delta = _summary_delta(peak)
            if peak_delta is None:
                continue
            tolerance = max(2.0, abs(peak_delta) * 0.20)
            permitted = {
                row.candidate_key: row
                for row in eligible
                if _summary_delta_or(row, -math.inf) >= peak_delta - tolerance
            }
            if peak.candidate_key not in permitted:
                continue
            component: list[CandidateSummary] = []
            pending: deque[CandidateSummary] = deque([peak])
            visited: set[str] = set()
            while pending:
                current = pending.popleft()
                if current.candidate_key in visited:
                    continue
                visited.add(current.candidate_key)
                component.append(current)
                for other in sorted(
                    permitted.values(), key=lambda item: item.candidate_key
                ):
                    if (
                        other.candidate_key not in visited
                        and _adjacent(
                            current,
                            other,
                            multipliers=multipliers,
                            profits=profits,
                        )
                    ):
                        pending.append(other)
            keys = tuple(sorted(row.candidate_key for row in component))
            deltas = [
                delta
                for row in component
                if (delta := _summary_delta(row)) is not None
            ]
            if not keys or not deltas:
                continue
            region_sets[keys] = {
                "rows": sorted(component, key=lambda item: item.candidate_key),
                "peak_candidate_key": peak.candidate_key,
                "near_best_tolerance_bps": tolerance,
                "minimum_paired_delta_bps": min(deltas),
                "median_paired_delta_bps": median(deltas),
                "best_paired_delta_bps": max(deltas),
            }

        supported_regions = [
            region
            for region in region_sets.values()
            if len(region["rows"]) >= 2
        ]
        preferred_region: dict[str, Any] | None = None
        if supported_regions:
            preferred_region = sorted(
                supported_regions,
                key=lambda region: (
                    -float(region["minimum_paired_delta_bps"]),
                    -float(region["median_paired_delta_bps"]),
                    -float(region["best_paired_delta_bps"]),
                    -len(region["rows"]),
                    tuple(row.candidate_key for row in region["rows"]),
                ),
            )[0]

        assigned: set[str] = set()
        ordered_regions: list[dict[str, Any]] = []
        if preferred_region is not None:
            ordered_regions.append(preferred_region)
        ordered_regions.extend(
            region
            for region in sorted(
                region_sets.values(),
                key=lambda item: tuple(row.candidate_key for row in item["rows"]),
            )
            if region is not preferred_region
        )
        region_number = 0
        for region_data in ordered_regions:
            remaining_component = [
                row
                for row in region_data["rows"]
                if row.candidate_key not in assigned
            ]
            if not remaining_component:
                continue
            for component in _connected_components(
                remaining_component,
                multipliers=multipliers,
                profits=profits,
            ):
                region_number += 1
                assigned.update(row.candidate_key for row in component)
                preferred = region_data is preferred_region
                center = _region_center(component)
                trail_values = [row.multiplier for row in component]
                profit_values = [
                    float(row.minimum_profit_multiplier)
                    for row in component
                    if row.minimum_profit_multiplier is not None
                ]
                component_deltas = [
                    delta
                    for row in component
                    if (delta := _summary_delta(row)) is not None
                ]
                peak = sorted(
                    component,
                    key=lambda row: (
                        -_summary_delta_or(row, -math.inf),
                        row.candidate_key,
                    ),
                )[0]
                peak_delta = _summary_delta_or(peak, 0.0)
                region = {
                    "region_id": f"{leg}-p{period}-b{bar_seconds}-R{region_number}",
                    "size": len(component),
                    "preferred": preferred,
                    "supported": len(component) >= 2 and preferred,
                    "center_candidate_key": center.candidate_key,
                    "peak_candidate_key": peak.candidate_key,
                    "trail_multiplier_min": min(trail_values),
                    "trail_multiplier_max": max(trail_values),
                    "minimum_profit_multiplier_min": (
                        min(profit_values) if profit_values else None
                    ),
                    "minimum_profit_multiplier_max": (
                        max(profit_values) if profit_values else None
                    ),
                    "minimum_paired_delta_bps": min(component_deltas),
                    "median_paired_delta_bps": median(component_deltas),
                    "best_paired_delta_bps": max(component_deltas),
                    "near_best_tolerance_bps": max(
                        2.0,
                        abs(peak_delta) * 0.20,
                    ),
                    "definition": (
                        "Connected adjacent grid points within the larger of 2 bps "
                        "or 20% of a local peak, where every point independently "
                        "passes the paired, censoring, bootstrap, influence, and "
                        "execution-evidence gates. The preferred region maximizes "
                        "the worst paired delta before median and best-point ties."
                    ),
                }
                for row in component:
                    row.stable_region = {
                        **region,
                        "is_center": row.candidate_key == center.candidate_key,
                    }

        # Eligible rows that were too far below every local near-best plateau
        # remain explicit one-point regions rather than disappearing from the
        # report. They cannot support a changed recommendation.
        for row in sorted(eligible, key=lambda item: item.candidate_key):
            if row.candidate_key in assigned:
                continue
            region_number += 1
            delta = _summary_delta(row)
            row.stable_region = {
                "region_id": f"{leg}-p{period}-b{bar_seconds}-R{region_number}",
                "size": 1,
                "preferred": False,
                "supported": False,
                "center_candidate_key": row.candidate_key,
                "peak_candidate_key": row.candidate_key,
                "trail_multiplier_min": row.multiplier,
                "trail_multiplier_max": row.multiplier,
                "minimum_profit_multiplier_min": row.minimum_profit_multiplier,
                "minimum_profit_multiplier_max": row.minimum_profit_multiplier,
                "minimum_paired_delta_bps": delta,
                "median_paired_delta_bps": delta,
                "best_paired_delta_bps": delta,
                "near_best_tolerance_bps": (
                    max(2.0, abs(delta) * 0.20) if delta is not None else None
                ),
                "definition": "One eligible grid point without adjacent robust support.",
                "is_center": True,
            }

    for summary in summaries:
        if summary.control_candidate:
            summary.evidence_stable = False
            continue
        reasons = list(summary.instability_reasons)
        region = summary.stable_region
        if int(region.get("size") or 0) < 2:
            reasons.append(
                "The candidate is not supported by a multi-point stable parameter region."
            )
        if region and not bool(region.get("preferred")):
            reasons.append(
                "The candidate belongs to a secondary or unsupported region, not the preferred robust region."
            )
        if region and not bool(region.get("is_center")):
            reasons.append(
                "The candidate is not the center of its supported stable region."
            )
        summary.instability_reasons = sorted(set(reasons))
        summary.evidence_stable = not summary.instability_reasons


def evidence_contract() -> dict[str, Any]:
    """Return public thresholds used by reports and regression tests."""

    return {
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "bootstrap_replicates": _BOOTSTRAP_REPLICATES,
        "minimum_paired_windows": _MIN_PAIRED_WINDOWS,
        "minimum_both_triggered": _MIN_BOTH_TRIGGERED,
        "minimum_trading_days": _MIN_TRADING_DAYS,
        "minimum_execution_samples": _MIN_EXECUTION_SAMPLES,
        "minimum_bootstrap_probability_positive_pct": _MIN_BOOTSTRAP_PROBABILITY_PCT,
        "maximum_trigger_probability_drop_pct": _MAX_TRIGGER_PROBABILITY_DROP_PCT,
        "maximum_median_mae_increase_bps": _MAX_MEDIAN_MAE_INCREASE_BPS,
        "maximum_median_absolute_delay_increase_seconds": _MAX_MEDIAN_ABSOLUTE_DELAY_INCREASE_SECONDS,
        "minimum_shared_km_horizon_seconds": _MIN_SHARED_KM_HORIZON_SECONDS,
        "maximum_execution_quote_age_seconds": _MAX_REFERENCE_QUOTE_AGE_SECONDS,
    }


def apply_execution_model(
    observations: list[ReplayObservation],
) -> dict[str, Any]:
    """Public v1.3 name for deterministic empirical execution adjustment."""

    return apply_execution_adjustments(observations)


def enrich_candidate_evidence(
    observations: list[ReplayObservation],
    summaries: list[CandidateSummary],
    execution_models: dict[str, Any],
) -> dict[str, Any]:
    """Attach paired evidence and return the report-facing methodology contract."""

    enrich_paired_evidence(observations, summaries, execution_models)
    detect_stable_regions(summaries)
    stable_candidates = sorted(
        summary.candidate_key
        for summary in summaries
        if summary.evidence_stable
    )
    right_censored = sum(
        1 for row in observations if row.right_censored
    )
    triggered = sum(1 for row in observations if row.triggered)
    unavailable = sum(1 for row in observations if row.outcome == "unavailable")
    return {
        "methodology": "paired-censoring-execution-robustness-v1.3",
        "contract": evidence_contract(),
        "observation_counts": {
            "total": len(observations),
            "triggered": triggered,
            "right_censored": right_censored,
            "unavailable": unavailable,
        },
        "stable_candidate_keys": stable_candidates,
        "stable_candidate_count": len(stable_candidates),
        "paired_comparison": (
            "Every candidate is compared only with the exact evaluation control on the intersection of identical "
            "cycle IDs. Trigger-price deltas use pairs where both settings triggered; censoring-aware trigger "
            "probabilities use the complete paired intersection."
        ),
        "right_censoring": (
            "A valid non-trigger at the end of a saved capture is right-censored: the outcome after capture end is "
            "unknown and is not charged as a confirmed miss. Kaplan-Meier estimates retain its observed exposure."
        ),
        "execution_adjustment": (
            "Triggered counterfactual prices are converted to conservative estimated fills using saved bid/ask "
            "quotes and ticker/leg empirical adverse slippage. A cycle is excluded from its own execution model "
            "whenever another sample exists. Conflicting immutable fill/quote context for one cycle is excluded "
            "fail-closed and blocks a changed primary suggestion."
        ),
        "uncertainty": (
            "Price-delta uncertainty is resampled by UTC trading day with a deterministic 2,000-replicate clustered "
            "bootstrap. Leave-one-day-out estimates expose concentration in a single day."
        ),
        "stable_region": (
            "A changed primary setting must be the center of the preferred connected group of adjacent grid points "
            "whose paired result remains near the best result. Isolated peaks are rejected."
        ),
    }
