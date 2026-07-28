"""End-to-end read-only data coverage and counterfactual replay analysis."""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any

from .atr import (
    normalize_atr_clamps,
    normalize_atr_multiplier,
    normalize_atr_window,
)
from .captures import inventory_captures, load_capture
from .database import DatabaseDataset, SnapshotDatabase, completed_cycle, safe_float, safe_int
from .determinism import (
    analysis_fingerprint,
    data_through_utc,
    database_content_fingerprint,
)
from .evidence import apply_execution_model, enrich_candidate_evidence
from .models import (
    AnalysisConfig,
    AnalysisResult,
    CandidateSummary,
    CaptureData,
    CaptureMeta,
    CaptureStats,
    ProgressCallback,
    TickerAnalysis,
)
from .replay import (
    buy_candidates,
    replay_buy,
    replay_sell,
    sell_candidates,
    summarize_observations,
)
from .safety import (
    BotFolderLease,
    capture_source_state,
    readonly_database_snapshot,
    source_paths,
    source_state,
    validate_source,
)
from .settings_history import (
    ATR_SETTING_DEFAULTS,
    HISTORICAL_MEDIAN_LABEL,
    build_settings_audit,
    cycle_profile_lookup,
)
from .utils import median, parse_datetime, timestamp_seconds, truthy
from .version import APP_VERSION


class AnalysisError(RuntimeError):
    """Raised when a complete, internally consistent analysis cannot be produced."""


_LIMITATIONS = [
    "Replay is restricted to market-data windows saved around actual fills; it is not a full-session backtest.",
    "Initial-drop counterfactuals are not scored because the anchor-to-drop path is often outside a fill-centred capture.",
    "The saved top-of-book/application price stream is not a tick-complete exchange tape or historical order book.",
    "Counterfactual trigger prices are adjusted with saved top-of-book quotes and an empirical adverse-slippage model, but the result still does not reproduce broker minimum-tick normalization, exchange-specific stop-trigger semantics, queue position, partial-fill sequencing, gaps, commissions, or market impact.",
    "BUY and normal-SELL candidates are screened as separate local replay legs; a combined row is not a jointly simulated full-strategy result.",
    "Normal-SELL captures that begin after the BUY fill or candidate activation are left-censored and excluded from ranking rather than treated as complete evidence.",
    "Capture-end non-triggers are right-censored and compared with Kaplan-Meier trigger estimates; this assumes censoring is not systematically related to the unseen future trigger after conditioning on the saved context.",
    "Trading-day bootstrap and leave-one-day-out checks measure sensitivity to observed UTC days but do not remove historical trade-selection bias, regime confounding, or multiple-candidate selection effects.",
    "Stable-region detection reduces isolated-grid-point overfitting but cannot prove that a plateau will persist outside the recorded sample.",
    "Completed-trade data is selection-biased: settings that would create missed or entirely different trades cannot be fully observed.",
    "Protective SELL fills are inventoried for coverage but are not ranked as normal profit-taking SELL exits.",
    "Suggested settings are experiments to evaluate in paper trading, not instructions for live deployment.",
]


def _notify(
    progress: ProgressCallback | None,
    message: str,
    current: int = 0,
    total: int = 0,
) -> None:
    if progress:
        progress(message, current, total)


def _median_setting(cycles: list[dict[str, Any]], name: str, default: float) -> float:
    value = median(safe_float(cycle.get(name)) for cycle in cycles)
    return float(value if value is not None else default)


def _median_int_setting(cycles: list[dict[str, Any]], name: str, default: int) -> int:
    value = median(
        float(item)
        for cycle in cycles
        if (item := safe_int(cycle.get(name))) is not None
    )
    return max(1, int(round(value if value is not None else default)))


def _majority_bool(cycles: list[dict[str, Any]], name: str, default: bool) -> bool:
    values = [truthy(cycle.get(name), default=default) for cycle in cycles]
    if not values:
        return default
    return sum(1 for value in values if value) >= (len(values) / 2.0)


def _capture_leg(meta: CaptureMeta) -> str:
    event = meta.event_type.upper()
    if "PROTECTIVE" in event and "SELL" in event and "FILL" in event:
        return "protective_sell"
    if "BUY" in event and "FILL" in event:
        return "buy"
    if "SELL" in event and "FILL" in event:
        return "sell"
    return "other"


def _cycle_match(meta: CaptureMeta, cycle: dict[str, Any]) -> bool:
    cycle_id = str(cycle.get("id") or "")
    if meta.cycle_id and cycle_id:
        # Explicit IDs are authoritative. A conflicting ID must not be rescued
        # by a coincidentally equal ticker/cycle number from another install.
        return meta.cycle_id == cycle_id
    number = safe_int(cycle.get("cycle_number"))
    ticker = str(cycle.get("ticker") or "").strip().upper()
    return bool(
        meta.cycle_number is not None
        and number == meta.cycle_number
        and meta.ticker == ticker
    )


def _capture_candidates(
    metas: list[CaptureMeta],
    dataset: DatabaseDataset,
    cycle: dict[str, Any],
    leg: str,
    fill_time: str,
) -> list[CaptureMeta]:
    cycle_candidates = [
        meta
        for meta in metas
        if meta.usable and _capture_leg(meta) == leg and _cycle_match(meta, cycle)
    ]
    if not cycle_candidates:
        return []
    expected_refs = _leg_order_refs(dataset, cycle, leg)

    def reference_rank(meta: CaptureMeta) -> int | None:
        captured_ref = str(meta.order_ref or "").strip()
        if not expected_refs:
            return 0
        if captured_ref in expected_refs:
            return 0
        if not captured_ref:
            # Legacy captures did not always persist order_ref. Keep them as a
            # lower-priority cycle/time fallback, but never let them outrank an
            # exact order-reference match.
            return 1
        # A non-empty conflicting reference belongs to another order attempt
        # and must not be rescued by cycle identity or timestamp proximity.
        return None

    ranked = [
        (rank, meta)
        for meta in cycle_candidates
        if (rank := reference_rank(meta)) is not None
    ]
    if not ranked:
        return []
    target = timestamp_seconds(fill_time)

    def sort_key(item: tuple[int, CaptureMeta]) -> tuple[int, float, str]:
        rank, meta = item
        event_time = timestamp_seconds(meta.event_time_utc)
        difference = (
            abs(event_time - target)
            if target is not None and event_time is not None
            else float("inf")
        )
        return rank, difference, str(meta.path)

    return [meta for _, meta in sorted(ranked, key=sort_key)]


def _select_usable_capture(
    *,
    metas: list[CaptureMeta],
    dataset: DatabaseDataset,
    cycle: dict[str, Any],
    leg: str,
    fill_time: str,
    capture_stats: dict[Path, CaptureStats],
    config: AnalysisConfig,
) -> tuple[CaptureMeta | None, CaptureData | None, int]:
    """Return the nearest matching archive that has usable market rows.

    Manifest-valid archives can still contain no usable prices.  Try later
    matching archives instead of allowing the nearest empty archive to hide a
    valid capture or inflate the matched-capture count.
    """
    candidates = _capture_candidates(metas, dataset, cycle, leg, fill_time)
    for meta in candidates:
        data = _load_capture(meta, config)
        capture_stats[meta.path] = _capture_stats(data)
        if meta.usable and data.points:
            return meta, data, len(candidates)
    return None, None, len(candidates)


def _stored_atr_window(
    cycle: dict[str, Any],
    meta: CaptureMeta,
) -> tuple[int, int] | None:
    """Return the exact ATR period/bar saved for one capture context.

    A captured ATR value can be reused only for the ATR window that produced
    it. Prefer the cycle snapshot because it is the durable historical record.
    If an older cycle row lacks both fields, use the strategy snapshot embedded
    in the capture event. Never substitute the ticker-wide median merely because
    metadata is missing; doing so would label an unknown ATR as candidate-
    specific evidence.
    """

    period = safe_int(cycle.get("atr_period"))
    bar_seconds = safe_int(cycle.get("atr_bar_seconds"))
    if period is not None and period > 0 and bar_seconds is not None and bar_seconds > 0:
        return period, bar_seconds

    strategy = meta.event.get("strategy")
    if not isinstance(strategy, dict):
        return None
    period = safe_int(strategy.get("atr_period"))
    bar_seconds = safe_int(strategy.get("atr_bar_seconds"))
    if period is None or period <= 0 or bar_seconds is None or bar_seconds <= 0:
        return None
    return period, bar_seconds


def _order_matches_leg(action: Any, leg: str) -> bool:
    normalized = str(action or "").strip().upper()
    if leg == "buy":
        return normalized == "BUY" or normalized.startswith("BUY_")
    if leg == "protective_sell":
        return "PROTECTIVE" in normalized and "SELL" in normalized
    return "SELL" in normalized and "PROTECTIVE" not in normalized


def _order_time(
    dataset: DatabaseDataset,
    cycle: dict[str, Any],
    leg: str,
    *,
    preferred_order_ref: str = "",
) -> str:
    cycle_id = str(cycle.get("id") or "")
    orders = dataset.orders_by_cycle.get(cycle_id, [])
    ref_field = {
        "buy": "buy_order_ref",
        "sell": "sell_order_ref",
        "protective_sell": "protective_sell_order_ref",
    }[leg]
    expected_ref = str(preferred_order_ref or cycle.get(ref_field) or "").strip()
    matching = [
        row
        for row in orders
        if expected_ref and str(row.get("order_ref") or "") == expected_ref
    ]
    if not matching and not expected_ref:
        matching = [row for row in orders if _order_matches_leg(row.get("action"), leg)]
    times = [
        str(row.get("created_at") or "")
        for row in matching
        if timestamp_seconds(row.get("created_at")) is not None
    ]
    if times:
        return min(
            times,
            key=lambda value: (
                timestamp if (timestamp := timestamp_seconds(value)) is not None else math.inf
            ),
        )
    if expected_ref:
        # A specific order attempt is known, but its submission timestamp is
        # absent. Do not substitute a generic stage decision from another order
        # attempt; the replay will be marked left-censored instead.
        return ""

    decisions = dataset.decisions_by_cycle.get(cycle_id, [])
    if leg == "buy":
        keywords = ("BUY_ORDER", "BUY_TRAIL")
    elif leg == "protective_sell":
        keywords = ("PROTECTIVE_SELL",)
    else:
        keywords = ("SELL_ORDER", "SELL_TRAIL")
    decision_times = [
        str(row.get("created_at") or "")
        for row in decisions
        if any(
            keyword in str(row.get("event_type") or "").upper()
            for keyword in keywords
        )
        and timestamp_seconds(row.get("created_at")) is not None
    ]
    if not decision_times:
        return ""
    return min(
        decision_times,
        key=lambda value: (
            timestamp if (timestamp := timestamp_seconds(value)) is not None else math.inf
        ),
    )


def _leg_order_refs(
    dataset: DatabaseDataset,
    cycle: dict[str, Any],
    leg: str,
) -> set[str]:
    ref_field = {
        "buy": "buy_order_ref",
        "sell": "sell_order_ref",
        "protective_sell": "protective_sell_order_ref",
    }[leg]
    explicit = str(cycle.get(ref_field) or "").strip()
    if explicit:
        return {explicit}
    cycle_id = str(cycle.get("id") or "")
    return {
        str(row.get("order_ref") or "").strip()
        for row in dataset.orders_by_cycle.get(cycle_id, [])
        if _order_matches_leg(row.get("action"), leg)
        and str(row.get("order_ref") or "").strip()
    }


def _has_other_sell_leg(
    dataset: DatabaseDataset,
    cycle: dict[str, Any],
    leg: str,
) -> bool:
    if leg == "sell":
        if str(cycle.get("protective_sell_order_ref") or "").strip():
            return True
        if (safe_float(cycle.get("protective_sell_filled_qty")) or 0.0) > 0:
            return True
        other = "protective_sell"
    else:
        if str(cycle.get("sell_order_ref") or "").strip():
            return True
        if (safe_float(cycle.get("sell_filled_qty")) or 0.0) > 0:
            return True
        other = "sell"
    cycle_id = str(cycle.get("id") or "")
    return any(
        _order_matches_leg(row.get("action"), other)
        for row in dataset.orders_by_cycle.get(cycle_id, [])
    )


def _execution_fill(
    dataset: DatabaseDataset,
    cycle: dict[str, Any],
    leg: str,
) -> tuple[str, float | None]:
    """Recover final-fill time and weighted average from execution rows."""
    cycle_id = str(cycle.get("id") or "")
    rows = dataset.executions_by_cycle.get(cycle_id, [])
    refs = _leg_order_refs(dataset, cycle, leg)
    if refs:
        matching = [
            row
            for row in rows
            if str(row.get("order_ref") or "").strip() in refs
        ]
    else:
        if leg in {"sell", "protective_sell"} and _has_other_sell_leg(
            dataset,
            cycle,
            leg,
        ):
            return "", None
        if leg == "buy":
            accepted_sides = {"BOT", "BUY", "B"}
        else:
            accepted_sides = {"SLD", "SELL", "S", "PROTECTIVE_SELL"}
        matching = [
            row
            for row in rows
            if str(row.get("side") or row.get("action") or "").upper()
            in accepted_sides
        ]

    weighted_total = 0.0
    quantity_total = 0.0
    timestamps: list[str] = []
    for row in matching:
        quantity = safe_float(
            row.get("shares")
            if row.get("shares") is not None
            else row.get("quantity")
        )
        price = safe_float(
            row.get("price")
            if row.get("price") is not None
            else row.get("avg_price")
        )
        if quantity is not None and quantity > 0 and price is not None and price > 0:
            weighted_total += quantity * price
            quantity_total += quantity
        executed_at = str(row.get("executed_at") or row.get("time") or "")
        if timestamp_seconds(executed_at) is not None:
            timestamps.append(executed_at)
    average = weighted_total / quantity_total if quantity_total > 0 else None
    final_time = (
        max(
            timestamps,
            key=lambda value: (
                timestamp if (timestamp := timestamp_seconds(value)) is not None else -math.inf
            ),
        )
        if timestamps
        else ""
    )
    return final_time, average


def _fill_fields(
    dataset: DatabaseDataset,
    cycle: dict[str, Any],
    *,
    leg: str,
    time_field: str,
    price_field: str,
) -> tuple[str, float | None]:
    stored_time = str(cycle.get(time_field) or "")
    stored_price = safe_float(cycle.get(price_field))
    fallback_time, fallback_price = _execution_fill(dataset, cycle, leg)
    selected_time = (
        stored_time
        if timestamp_seconds(stored_time) is not None
        else fallback_time
    )
    selected_price = (
        stored_price
        if stored_price is not None and stored_price > 0
        else fallback_price
    )
    return selected_time, selected_price


def _buy_fields(
    dataset: DatabaseDataset,
    cycle: dict[str, Any],
) -> tuple[str, float | None]:
    return _fill_fields(
        dataset,
        cycle,
        leg="buy",
        time_field="buy_filled_at",
        price_field="avg_buy_price",
    )


def _normal_sell_fields(
    dataset: DatabaseDataset,
    cycle: dict[str, Any],
) -> tuple[str, float | None]:
    protective_quantity = safe_float(cycle.get("protective_sell_filled_qty")) or 0.0
    protective_price = safe_float(cycle.get("protective_avg_sell_price"))
    protective_time = timestamp_seconds(cycle.get("protective_sell_filled_at"))
    has_protective_exit = bool(
        protective_quantity > 0
        or (protective_price is not None and protective_price > 0)
        or protective_time is not None
    )
    if has_protective_exit:
        normal_ref = str(cycle.get("sell_order_ref") or "").strip()
        protective_ref = str(cycle.get("protective_sell_order_ref") or "").strip()
        if not normal_ref or normal_ref == protective_ref:
            # BouncyBot mirrors a protective fill into the normal SELL fields so
            # history/P&L can treat the position as closed. Those mirrored fields
            # must not be ranked as a normal profit-taking exit.
            return "", None
        # A protective fill can be mirrored into the normal SELL fields while a
        # cancelled or superseded normal order keeps a different reference.
        # Therefore distinct references alone do not prove that a separate
        # profit-taking SELL filled.  Require execution evidence tied to the
        # normal reference; otherwise exclude the mirrored fields from normal
        # SELL optimization.
        execution_time, execution_price = _execution_fill(dataset, cycle, "sell")
        if execution_time or execution_price is not None:
            return execution_time, execution_price
        return "", None
    return _fill_fields(
        dataset,
        cycle,
        leg="sell",
        time_field="sell_filled_at",
        price_field="avg_sell_price",
    )


def _protective_sell_fields(
    dataset: DatabaseDataset,
    cycle: dict[str, Any],
) -> tuple[str, float | None]:
    return _fill_fields(
        dataset,
        cycle,
        leg="protective_sell",
        time_field="protective_sell_filled_at",
        price_field="protective_avg_sell_price",
    )


def _sanitize_cycle(cycle: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "cycle_number",
        "ticker",
        "stage",
        "created_at",
        "updated_at",
        "initial_drop_pct",
        "buy_rebound_trail_pct",
        "rise_trigger_pct",
        "sell_trailing_stop_pct",
        "slippage_buffer_enabled",
        "slippage_buffer_pct",
        "atr_adaptive_enabled",
        "atr_adapt_minimum_profit_enabled",
        "atr_adapt_protective_sell_enabled",
        "atr_period",
        "atr_bar_seconds",
        "atr_initial_drop_multiplier",
        "atr_buy_rebound_multiplier",
        "atr_minimum_profit_multiplier",
        "atr_sell_trail_multiplier",
        "atr_protective_sell_multiplier",
        "atr_min_pct",
        "atr_max_pct",
        "protective_sell_enabled",
        "protective_sell_trailing_stop_pct",
        "buy_filled_qty",
        "avg_buy_price",
        "buy_filled_at",
        "sell_filled_qty",
        "avg_sell_price",
        "sell_filled_at",
        "protective_sell_filled_qty",
        "protective_avg_sell_price",
        "protective_sell_filled_at",
        "gross_pnl",
        "net_pnl",
    )
    return {name: cycle.get(name) for name in fields if name in cycle}


def _capture_inventory(
    meta: CaptureMeta,
    stats: CaptureStats | None,
    source_root: Path,
) -> dict[str, Any]:
    try:
        relative = meta.path.resolve().relative_to(source_root.resolve()).as_posix()
    except ValueError:
        relative = meta.path.name
    return {
        "path": relative,
        "ticker": meta.ticker,
        "cycle_id": meta.cycle_id,
        "cycle_number": meta.cycle_number,
        "event_type": meta.event_type,
        "event_time_utc": meta.event_time_utc,
        "order_ref": meta.order_ref,
        "rows_declared": meta.rows_declared,
        "rows_read": stats.raw_rows if stats else None,
        "usable_points": stats.usable_points if stats else None,
        "invalid_rows": stats.invalid_rows if stats else None,
        "duplicate_rows": stats.duplicate_rows if stats else None,
        "rows_with_saved_atr": stats.atr_points if stats else None,
        "first_row_utc": meta.first_row_utc,
        "last_row_utc": meta.last_row_utc,
        "pre_window_seconds": meta.pre_window_seconds,
        "post_window_seconds": meta.post_window_seconds,
        "archive_bytes": meta.archive_bytes,
        "uncompressed_bytes": meta.uncompressed_bytes,
        "sha256": meta.sha256,
        "issues": list(meta.issues),
    }


def _date_span_days(cycles: list[dict[str, Any]]) -> float:
    dates = []
    for cycle in cycles:
        for key in (
            "buy_filled_at",
            "sell_filled_at",
            "protective_sell_filled_at",
            "created_at",
        ):
            parsed = parse_datetime(cycle.get(key))
            if parsed:
                dates.append(parsed)
    if len(dates) < 2:
        return 0.0
    return max(0.0, (max(dates) - min(dates)).total_seconds() / 86_400.0)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _coverage(
    *,
    cycles: list[dict[str, Any]],
    metas: list[CaptureMeta],
    capture_stats: dict[Path, CaptureStats],
    buy_fills: int,
    normal_sell_fills: int,
    protective_sell_fills: int,
    matched_buy: int,
    matched_sell: int,
    matched_protective: int,
    replayable_buy: int,
    replayable_sell: int,
    replay_windows_with_usable_atr: int,
    captured_atr_rows: int,
) -> dict[str, Any]:
    completed = [cycle for cycle in cycles if completed_cycle(cycle)]
    usable = [
        meta
        for meta in metas
        if meta.usable
        and meta.path in capture_stats
        and capture_stats[meta.path].usable_points > 0
    ]
    usable_paths = {meta.path for meta in usable}
    unusable = [meta for meta in metas if meta.path not in usable_paths]
    without_prices = [
        meta
        for meta in metas
        if meta.path in capture_stats
        and capture_stats[meta.path].usable_points == 0
    ]
    total_points = sum(stats.usable_points for stats in capture_stats.values())
    exit_fills = normal_sell_fills + protective_sell_fills
    matched_exits = matched_sell + matched_protective
    buy_rate = _ratio(matched_buy, buy_fills)
    sell_rate = _ratio(matched_sell, normal_sell_fills)
    protective_rate = _ratio(matched_protective, protective_sell_fills)
    exit_rate = _ratio(matched_exits, exit_fills)
    usable_rate = _ratio(len(usable), len(metas))
    replay_buy_rate = _ratio(replayable_buy, buy_fills)
    replay_sell_rate = _ratio(replayable_sell, normal_sell_fills)

    score_components = {
        "cycle_presence": 10.0 if cycles else 0.0,
        "completed_cycle_depth": min(20.0, float(len(completed))),
        "buy_capture_match": 15.0 * buy_rate,
        "exit_capture_match": 15.0 * exit_rate,
        "usable_archive_ratio": 10.0 * usable_rate,
        "buy_replay_ratio": 10.0 * replay_buy_rate,
        "sell_replay_ratio": 10.0 * replay_sell_rate,
        "atr_evidence_present": 5.0 if replay_windows_with_usable_atr > 0 else 0.0,
        "date_span_depth": min(5.0, _date_span_days(cycles) / 6.0),
    }
    score = sum(score_components.values())
    score = round(min(100.0, score), 1)
    if score >= 80 and len(completed) >= 20:
        grade = "A"
        evidence = "strong"
    elif score >= 65 and len(completed) >= 10:
        grade = "B"
        evidence = "moderate"
    elif score >= 45 and len(completed) >= 3:
        grade = "C"
        evidence = "limited"
    else:
        grade = "D"
        evidence = "insufficient"

    adaptive_cycles = sum(
        1
        for cycle in cycles
        if truthy(cycle.get("atr_adaptive_enabled"), default=True)
    )
    adaptive_profit_cycles = sum(
        1
        for cycle in cycles
        if truthy(cycle.get("atr_adapt_minimum_profit_enabled"), default=True)
    )
    return {
        "coverage_score": score,
        "coverage_score_components": {
            key: round(value, 2) for key, value in score_components.items()
        },
        "coverage_grade": grade,
        "evidence_level": evidence,
        "cycle_rows": len(cycles),
        "completed_cycles": len(completed),
        "buy_fills": buy_fills,
        "sell_fills": normal_sell_fills,
        "normal_sell_fills": normal_sell_fills,
        "protective_sell_fills": protective_sell_fills,
        "exit_fills": exit_fills,
        "capture_archives": len(metas),
        "usable_capture_archives": len(usable),
        "corrupt_or_unsupported_archives": len(unusable),
        "archives_without_usable_prices": len(without_prices),
        "matched_buy_captures": matched_buy,
        "matched_sell_captures": matched_sell,
        "matched_protective_sell_captures": matched_protective,
        "replayable_buy_windows": replayable_buy,
        "replayable_sell_windows": replayable_sell,
        "usable_price_points": total_points,
        # Preserve the original key for consumers while making its meaning
        # explicit.  Candidate ATR can be reconstructed from prices even when
        # no capture row carries a saved bot ATR value.
        "replay_rows_with_atr": captured_atr_rows,
        "capture_rows_with_saved_atr": captured_atr_rows,
        "replay_windows_with_usable_atr": replay_windows_with_usable_atr,
        "date_span_days": round(_date_span_days(cycles), 2),
        "atr_adaptive_cycles": adaptive_cycles,
        "manual_percentage_cycles": len(cycles) - adaptive_cycles,
        "atr_adaptive_minimum_profit_cycles": adaptive_profit_cycles,
        "manual_minimum_profit_cycles": len(cycles) - adaptive_profit_cycles,
        "buy_capture_match_pct": round(buy_rate * 100.0, 1)
        if buy_fills
        else None,
        "sell_capture_match_pct": round(sell_rate * 100.0, 1)
        if normal_sell_fills
        else None,
        "protective_sell_capture_match_pct": round(protective_rate * 100.0, 1)
        if protective_sell_fills
        else None,
        "exit_capture_match_pct": round(exit_rate * 100.0, 1)
        if exit_fills
        else None,
    }


def _eligible_summary(row: CandidateSummary) -> bool:
    return (
        row.screening_score is not None
        and math.isfinite(row.screening_score)
        and row.priority
        in {
            "evaluate first",
            "secondary evaluation",
            "low priority",
        }
    )


def _best_candidate(rows: list[CandidateSummary]) -> CandidateSummary | None:
    """Return the strongest row with deterministic, evidence-aware tie breaks."""

    if not rows:
        return None

    def finite_or(value: float | None, fallback: float) -> float:
        if value is None or not math.isfinite(value):
            return fallback
        return value

    return sorted(
        rows,
        key=lambda row: (
            0 if row.evidence_stable else 1,
            0 if row.stable_region.get("is_center") else 1,
            -finite_or(
                safe_float(
                    row.paired_evidence.get(
                        "paired_execution_adjusted_median_bps"
                    )
                ),
                -math.inf,
            ),
            -finite_or(
                safe_float(
                    (row.paired_evidence.get("bootstrap") or {}).get(
                        "probability_positive_pct"
                    )
                ),
                -math.inf,
            ),
            -finite_or(row.screening_score, -math.inf),
            -finite_or(row.trigger_rate_pct, -math.inf),
            -finite_or(row.candidate_atr_coverage_pct, -math.inf),
            finite_or(row.median_absolute_delay_seconds, math.inf),
            row.candidate_key,
        ),
    )[0]


def _profile_common(
    *,
    cycles: list[dict[str, Any]],
    retained_settings: dict[str, Any] | None,
    period: int,
    bar_seconds: int,
    drop: float,
    buy: float,
    profit: float,
    sell: float,
    lower: float,
    upper: float,
) -> dict[str, Any]:
    retained = retained_settings or {}
    normalized_period, normalized_bar_seconds = normalize_atr_window(
        period,
        bar_seconds,
    )

    def retained_bool(name: str, default: bool) -> bool:
        stored = retained.get(name)
        if isinstance(stored, bool):
            return stored
        return _majority_bool(cycles, name, default)

    protective_multiplier = safe_float(retained.get("atr_protective_sell_multiplier"))
    if protective_multiplier is None:
        protective_multiplier = _median_setting(
            cycles,
            "atr_protective_sell_multiplier",
            3.0,
        )
    normalized_lower, normalized_upper = normalize_atr_clamps(lower, upper)
    return {
        "atr_adaptive_enabled": retained_bool(
            "atr_adaptive_enabled",
            True,
        ),
        "atr_adapt_minimum_profit_enabled": retained_bool(
            "atr_adapt_minimum_profit_enabled",
            True,
        ),
        "atr_block_new_buy_until_ready": retained_bool(
            "atr_block_new_buy_until_ready",
            False,
        ),
        "atr_adapt_protective_sell_enabled": retained_bool(
            "atr_adapt_protective_sell_enabled",
            False,
        ),
        "atr_period": normalized_period,
        "atr_bar_seconds": normalized_bar_seconds,
        "atr_initial_drop_multiplier": normalize_atr_multiplier(
            drop,
            allow_zero=False,
        ),
        "atr_buy_rebound_multiplier": normalize_atr_multiplier(
            buy,
            allow_zero=True,
        ),
        # BouncyBot permits zero for the BUY and SELL trailing multipliers
        # because zero selects immediate market-style behavior.  The adaptive
        # minimum-profit multiplier is different: its GUI minimum is 0.01 and
        # zero is not a valid saved setting.
        "atr_minimum_profit_multiplier": normalize_atr_multiplier(
            profit,
            allow_zero=False,
        ),
        "atr_sell_trail_multiplier": normalize_atr_multiplier(
            sell,
            allow_zero=True,
        ),
        "atr_protective_sell_multiplier": normalize_atr_multiplier(
            protective_multiplier,
            allow_zero=False,
        ),
        "atr_min_pct": round(normalized_lower, 2),
        "atr_max_pct": round(normalized_upper, 2),
    }


def _setting_profiles(
    *,
    cycles: list[dict[str, Any]],
    summaries: list[CandidateSummary],
    coverage: dict[str, Any],
    settings_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build controls and bounded paper-evaluation profiles.

    Candidate generation is anchored to one complete evaluation control.  The
    field-wise historical median remains descriptive because it can combine
    values that never ran together.  Baseline-window BUY-only and SELL-only
    experiments are emitted separately so the highlighted recommendation can
    change one independently replayed decision at a time.  Combined and
    alternate-window profiles remain visible, but are not eligible for the one
    highlighted set because they have not been jointly replayed and an ATR
    window change also changes the unobserved initial-drop decision.
    """

    median_settings = dict(settings_summary.get("historical_median_settings") or {})
    source_control_settings = dict(
        settings_summary.get("evaluation_control_settings") or {}
    )
    replay_control_settings = dict(
        settings_summary.get("counterfactual_replay_control_settings")
        or source_control_settings
    )

    def median_value(name: str) -> Any:
        stored = median_settings.get(name)
        return ATR_SETTING_DEFAULTS[name] if stored is None else stored

    def control_value(name: str) -> Any:
        stored = replay_control_settings.get(name)
        return median_value(name) if stored is None else stored

    raw_base_period = settings_summary.get(
        "evaluation_control_raw_atr_period",
        control_value("atr_period"),
    )
    raw_base_bar = settings_summary.get(
        "evaluation_control_raw_atr_bar_seconds",
        control_value("atr_bar_seconds"),
    )
    base_period, base_bar = normalize_atr_window(
        control_value("atr_period"),
        control_value("atr_bar_seconds"),
    )
    raw_period_value = safe_int(raw_base_period)
    raw_bar_value = safe_int(raw_base_bar)
    window_normalized = bool(
        settings_summary.get("counterfactual_replay_atr_window_normalized")
    ) or (base_period, base_bar) != (raw_period_value, raw_bar_value)
    base_drop = normalize_atr_multiplier(
        control_value("atr_initial_drop_multiplier"),
        allow_zero=False,
    )
    base_buy = normalize_atr_multiplier(
        control_value("atr_buy_rebound_multiplier"),
        allow_zero=True,
    )
    base_profit = normalize_atr_multiplier(
        control_value("atr_minimum_profit_multiplier"),
        allow_zero=False,
    )
    base_sell = normalize_atr_multiplier(
        control_value("atr_sell_trail_multiplier"),
        allow_zero=True,
    )
    base_min, base_max = normalize_atr_clamps(
        control_value("atr_min_pct"),
        control_value("atr_max_pct"),
    )
    adaptive_profit = bool(control_value("atr_adapt_minimum_profit_enabled"))
    control_atr_enabled = bool(control_value("atr_adaptive_enabled"))
    settings_varied = bool(settings_summary.get("settings_varied_between_cycles"))
    configured_cycles = int(settings_summary.get("cycles_with_any_stored_atr_settings") or 0)
    fallback_fields = list(
        settings_summary.get("historical_median_default_fallback_fields") or []
    )
    control_fallback_fields = list(
        settings_summary.get("evaluation_control_missing_fields_before_fallback") or []
    )

    baseline = _profile_common(
        cycles=cycles,
        retained_settings=replay_control_settings,
        period=base_period,
        bar_seconds=base_bar,
        drop=base_drop,
        buy=base_buy,
        profit=base_profit,
        sell=base_sell,
        lower=base_min,
        upper=base_max,
    )
    # ``_profile_common`` is the single normalization boundary for values that
    # will be replayed or suggested. Do not overwrite its GUI-enterable values
    # with malformed legacy/source values. The original values and every
    # adjustment are retained separately in ``settings_summary``.
    baseline_label = str(
        settings_summary.get("evaluation_control_label")
        or "Evaluation control: historical median/default summary"
    )
    baseline_source = str(
        settings_summary.get("evaluation_control_source")
        or "historical median/default summary"
    )
    baseline_evidence = (
        "This complete control is the normalized profile used to center every replay candidate. "
        f"Its provenance is {baseline_source}."
    )
    if window_normalized:
        baseline_evidence += (
            f" The saved ATR window {raw_base_period}x{raw_base_bar}s is outside the trading app's "
            "current controls, so replay and suggested profiles use the nearest GUI-enterable "
            f"window {base_period}x{base_bar}s. The original values remain in the settings-history exports."
        )
    normalization_adjustments = dict(
        settings_summary.get("evaluation_control_normalization_adjustments") or {}
    )
    if settings_summary.get("evaluation_control_exact_for_replay"):
        baseline_evidence += " All replay-relevant source fields come from one exact profile saved together."
    else:
        baseline_evidence += (
            " A single exact replay-relevant saved profile was unavailable, so the field-wise "
            "historical median/default summary was used."
        )
    if normalization_adjustments:
        baseline_evidence += (
            " Invalid or out-of-range saved values were normalized to current GUI-enterable values for: "
            + ", ".join(sorted(normalization_adjustments))
            + ". The source values and replacements are exported explicitly."
        )
    if control_fallback_fields:
        baseline_evidence += (
            " Non-replay display fields were completed from the historical median/defaults for: "
            + ", ".join(control_fallback_fields)
            + "."
        )
    baseline.update(
        {
            "profile": baseline_label,
            "evaluation_priority": 1,
            "evaluation_status": "control: candidate-grid evaluation control",
            "settings_source": baseline_source,
            "actual_settings_control": not bool(normalization_adjustments),
            "evaluation_control": True,
            "primary_eligible": False,
            "changed_fields": [],
            "changed_legs": [],
            "historical_cycle_count": configured_cycles,
            "distinct_historical_profiles": settings_summary.get("distinct_atr_profiles"),
            "settings_changed_between_cycles": settings_varied,
            "candidate_atr_coverage_pct": None,
            "evaluation_control_window_normalized_for_replay": window_normalized,
            "evaluation_control_raw_atr_period": raw_base_period,
            "evaluation_control_raw_atr_bar_seconds": raw_base_bar,
            "evaluation_control_normalization_adjustments": normalization_adjustments,
            "evidence": baseline_evidence,
        }
    )
    profiles: list[dict[str, Any]] = [baseline]

    if normalization_adjustments:
        actual_source_profile = {
            name: source_control_settings.get(name)
            for name in ATR_SETTING_DEFAULTS
        }
        actual_source_profile.update(
            {
                "profile": f"{baseline_label} — stored source values",
                "evaluation_priority": len(profiles) + 1,
                "evaluation_status": (
                    "descriptive actual-settings evidence: normalized before replay"
                ),
                "settings_source": baseline_source,
                "actual_settings_control": True,
                "evaluation_control": False,
                "primary_eligible": False,
                "changed_fields": [],
                "changed_legs": [],
                "candidate_atr_coverage_pct": None,
                "normalization_adjustments": normalization_adjustments,
                "evidence": (
                    "These are the source values selected as the evaluation-control provenance. "
                    "They are displayed as historical/current evidence and were not replayed "
                    "unchanged because one or more values are outside BouncyBot's current GUI "
                    "contract. The candidate-grid control above lists the exact normalized values "
                    "used for replay."
                ),
            }
        )
        profiles.append(actual_source_profile)

    # Keep the field-wise median as descriptive evidence when it differs from
    # the exact control. It is never the candidate control merely because it is
    # numerically central.
    median_complete = {name: median_value(name) for name in ATR_SETTING_DEFAULTS}
    median_lower, median_upper = normalize_atr_clamps(
        median_complete["atr_min_pct"],
        median_complete["atr_max_pct"],
    )
    median_complete["atr_min_pct"] = median_lower
    median_complete["atr_max_pct"] = median_upper
    if median_complete != {name: baseline.get(name) for name in ATR_SETTING_DEFAULTS}:
        median_profile = dict(median_complete)
        median_profile.update(
            {
                "profile": HISTORICAL_MEDIAN_LABEL,
                "evaluation_priority": len(profiles) + 1,
                "evaluation_status": "descriptive reference: not used as candidate control",
                "settings_source": "cycles table ATR columns",
                "actual_settings_control": bool(configured_cycles),
                "evaluation_control": False,
                "primary_eligible": False,
                "changed_fields": [],
                "changed_legs": [],
                "historical_cycle_count": configured_cycles,
                "distinct_historical_profiles": settings_summary.get("distinct_atr_profiles"),
                "settings_changed_between_cycles": settings_varied,
                "median_matches_observed_complete_profile": settings_summary.get(
                    "historical_median_matches_an_observed_complete_profile"
                ),
                "candidate_atr_coverage_pct": None,
                "evidence": (
                    "Numeric values are field-wise medians of stored cycle settings and booleans are "
                    "deterministic majorities. This is descriptive actual-settings evidence, not the "
                    "candidate-grid control and not necessarily one profile that ever ran."
                    + (
                        " Defaults were required for: " + ", ".join(fallback_fields) + "."
                        if fallback_fields
                        else ""
                    )
                ),
            }
        )
        profiles.append(median_profile)

    current_settings = dict(settings_summary.get("current_app_settings") or {})
    if (
        settings_summary.get("current_app_settings_complete_for_replay")
        and baseline_source != "app_settings.strategy"
    ):
        current = {
            name: (
                current_settings.get(name)
                if current_settings.get(name) is not None
                else median_complete.get(name)
            )
            for name in ATR_SETTING_DEFAULTS
        }
        current.update(
            {
                "profile": "Current saved app settings (replay-exact; completed non-replay fields where needed)",
                "evaluation_priority": len(profiles) + 1,
                "evaluation_status": "control reference: current saved settings, not assumed historical",
                "settings_source": "app_settings.strategy",
                "actual_settings_control": True,
                "evaluation_control": False,
                "primary_eligible": False,
                "changed_fields": [],
                "changed_legs": [],
                "candidate_atr_coverage_pct": None,
                "evidence": (
                    "Replay-relevant values come from the current app_settings.strategy record. "
                    "They describe what is currently saved, not what every historical cycle used."
                ),
            }
        )
        profiles.append(current)

    retained_unevaluated_fields = (
        "atr_block_new_buy_until_ready",
        "atr_adapt_protective_sell_enabled",
        "atr_protective_sell_multiplier",
    )
    retained_settings = dict(replay_control_settings)
    retained_source = (
        "the evaluation control; these fields are displayed but are not optimized by BUY/normal-SELL replay"
    )

    control_buy_summary = None
    control_sell_summary = None
    if control_atr_enabled:
        control_buy_summary = next(
            (
                row
                for row in summaries
                if row.leg == "buy"
                and row.period == base_period
                and row.bar_seconds == base_bar
                and abs(row.multiplier - base_buy) < 1e-8
            ),
            None,
        )
        control_sell_summary = next(
            (
                row
                for row in summaries
                if row.leg == "sell"
                and row.period == base_period
                and row.bar_seconds == base_bar
                and abs(row.multiplier - base_sell) < 1e-8
                and row.minimum_profit_multiplier is not None
                and (
                    not adaptive_profit
                    or abs(row.minimum_profit_multiplier - base_profit) < 1e-8
                )
            ),
            None,
        )

    def score(row: CandidateSummary | None) -> float | None:
        value = row.screening_score if row is not None else None
        return value if value is not None and math.isfinite(value) else None

    def coverage_for(rows: tuple[CandidateSummary | None, ...]) -> float | None:
        return median(
            row.candidate_atr_coverage_pct
            for row in rows
            if row is not None and row.candidate_atr_coverage_pct is not None
        )

    rank = len(profiles) + 1

    def append_screened_profile(
        *,
        label: str,
        period: int,
        bar_seconds: int,
        buy_row: CandidateSummary | None,
        sell_row: CandidateSummary | None,
        combined: bool,
        alternate_window: bool,
    ) -> None:
        nonlocal rank
        buy_changed = bool(
            buy_row is not None and abs(buy_row.multiplier - base_buy) >= 1e-8
        )
        sell_trail_changed = bool(
            sell_row is not None and abs(sell_row.multiplier - base_sell) >= 1e-8
        )
        sell_profit_changed = bool(
            adaptive_profit
            and sell_row is not None
            and sell_row.minimum_profit_multiplier is not None
            and abs(sell_row.minimum_profit_multiplier - base_profit) >= 1e-8
        )
        changed_fields: list[str] = []
        if not control_atr_enabled:
            changed_fields.append("atr_adaptive_enabled")
        if alternate_window:
            changed_fields.extend(["atr_period", "atr_bar_seconds"])
        if buy_changed:
            changed_fields.append("atr_buy_rebound_multiplier")
        if sell_profit_changed:
            changed_fields.append("atr_minimum_profit_multiplier")
        if sell_trail_changed:
            changed_fields.append("atr_sell_trail_multiplier")
        if not changed_fields:
            return

        changed_legs: list[str] = []
        if buy_row is not None and (buy_changed or alternate_window):
            changed_legs.append("BUY")
        if sell_row is not None and (
            sell_trail_changed or sell_profit_changed or alternate_window
        ):
            changed_legs.append("normal SELL")

        profile = _profile_common(
            cycles=cycles,
            retained_settings=retained_settings,
            period=period,
            bar_seconds=bar_seconds,
            drop=base_drop,
            buy=buy_row.multiplier if buy_row is not None else base_buy,
            profit=(
                sell_row.minimum_profit_multiplier
                if adaptive_profit
                and sell_row is not None
                and sell_row.minimum_profit_multiplier is not None
                else base_profit
            ),
            sell=sell_row.multiplier if sell_row is not None else base_sell,
            lower=base_min,
            upper=base_max,
        )
        # All replay rows in this branch are ATR-derived experiments. Enabling
        # ATR is therefore an explicit changed field when the actual control was
        # manual; it is never hidden as a mere multiplier adjustment.
        profile["atr_adaptive_enabled"] = True
        entry_score = score(buy_row)
        exit_score = score(sell_row)
        entry_control_score = score(control_buy_summary)
        exit_control_score = score(control_sell_summary)
        selected_row = buy_row if changed_legs == ["BUY"] else sell_row
        primary_eligible = bool(
            control_atr_enabled
            and not alternate_window
            and not combined
            and len(changed_legs) == 1
            and selected_row is not None
            and selected_row.evidence_stable
            and selected_row.stable_region.get("is_center") is True
        )
        status = "screened on saved fill windows"
        if not control_atr_enabled:
            status = "locally screened; enabling ATR changes unobserved initial-drop behavior"
        elif alternate_window:
            status = "locally screened; alternate ATR window changes unobserved initial-drop behavior"
        elif combined:
            status = "screened independently on saved fill windows; not jointly simulated"

        evidence = (
            f"{coverage.get('evidence_level', 'unknown')} evidence. "
            "Only complete-context windows with usable candidate ATR contribute to ranking. "
        )
        if combined:
            evidence += (
                "BUY and normal-SELL legs were selected independently; this combined row is a comparison profile, "
                "not a jointly simulated full-cycle P/L result. "
            )
        if alternate_window:
            evidence += (
                "Changing the ATR window also changes initial-drop behavior outside the fill-centred captures, so this "
                "profile is not eligible for the single highlighted setting. "
            )
        if not control_atr_enabled:
            evidence += (
                "The historical manual-percentage control had ATR adaptation disabled. Enabling ATR also changes initial-drop selection, "
                "which the saved windows cannot score; this profile is therefore not highlight-eligible. "
            )
        if not adaptive_profit and sell_row is not None:
            evidence += (
                "Minimum profit stays manual on each source cycle; only the SELL trailing multiplier was screened. "
            )
        if settings_varied:
            evidence += (
                "Results pool multiple historical settings profiles; subgroup tables should be checked for time/regime confounding. "
            )
        evidence += (
            "Initial-drop multiplier and the protective/readiness controls remain unchanged because these captures do not evaluate them."
        )

        profile.update(
            {
                "profile": label,
                "evaluation_priority": rank,
                "evaluation_status": status,
                "settings_source": "fixed counterfactual candidate around the evaluation control",
                "actual_settings_control": False,
                "evaluation_control": False,
                "primary_eligible": primary_eligible,
                "changed_fields": changed_fields,
                "changed_legs": changed_legs,
                "alternate_atr_window": alternate_window,
                "independently_combined_legs": combined,
                "candidate_atr_coverage_pct": coverage_for((buy_row, sell_row)),
                "entry_candidate_key": buy_row.candidate_key if buy_row else "",
                "exit_candidate_key": sell_row.candidate_key if sell_row else "",
                "entry_screening_score": entry_score,
                "exit_screening_score": exit_score,
                "entry_control_screening_score": entry_control_score,
                "exit_control_screening_score": exit_control_score,
                "entry_score_delta_vs_control": (
                    entry_score - entry_control_score
                    if entry_score is not None and entry_control_score is not None
                    else None
                ),
                "exit_score_delta_vs_control": (
                    exit_score - exit_control_score
                    if exit_score is not None and exit_control_score is not None
                    else None
                ),
                "entry_scoreable_windows": buy_row.scoreable_observations if buy_row else 0,
                "exit_scoreable_windows": sell_row.scoreable_observations if sell_row else 0,
                "entry_scoreable_triggers": buy_row.scoreable_triggered if buy_row else 0,
                "exit_scoreable_triggers": sell_row.scoreable_triggered if sell_row else 0,
                "entry_trigger_rate_pct": buy_row.trigger_rate_pct if buy_row else None,
                "exit_trigger_rate_pct": sell_row.trigger_rate_pct if sell_row else None,
                "entry_left_censored_rate_pct": (
                    buy_row.left_censored_rate_pct if buy_row else None
                ),
                "exit_left_censored_rate_pct": (
                    sell_row.left_censored_rate_pct if sell_row else None
                ),
                "entry_right_censored_rate_pct": (
                    buy_row.right_censored_rate_pct if buy_row else None
                ),
                "exit_right_censored_rate_pct": (
                    sell_row.right_censored_rate_pct if sell_row else None
                ),
                "entry_paired_evidence": (
                    dict(buy_row.paired_evidence) if buy_row else {}
                ),
                "exit_paired_evidence": (
                    dict(sell_row.paired_evidence) if sell_row else {}
                ),
                "entry_stable_region": (
                    dict(buy_row.stable_region) if buy_row else {}
                ),
                "exit_stable_region": (
                    dict(sell_row.stable_region) if sell_row else {}
                ),
                "entry_execution_model": (
                    dict(buy_row.execution_model) if buy_row else {}
                ),
                "exit_execution_model": (
                    dict(sell_row.execution_model) if sell_row else {}
                ),
                "entry_evidence_stable": bool(
                    buy_row and buy_row.evidence_stable
                ),
                "exit_evidence_stable": bool(
                    sell_row and sell_row.evidence_stable
                ),
                "entry_instability_reasons": (
                    list(buy_row.instability_reasons) if buy_row else []
                ),
                "exit_instability_reasons": (
                    list(sell_row.instability_reasons) if sell_row else []
                ),
                "replay_evaluated_fields": [
                    field
                    for field in (
                        "atr_period" if alternate_window else "",
                        "atr_bar_seconds" if alternate_window else "",
                        "atr_buy_rebound_multiplier" if buy_row is not None else "",
                        "atr_minimum_profit_multiplier"
                        if adaptive_profit and sell_row is not None
                        else "",
                        "atr_sell_trail_multiplier" if sell_row is not None else "",
                    )
                    if field
                ],
                "retained_unevaluated_fields": list(retained_unevaluated_fields),
                "retained_unevaluated_settings_source": retained_source,
                "evidence": evidence,
            }
        )
        profiles.append(profile)
        rank += 1

    grouped_windows = sorted(
        {(row.period, row.bar_seconds) for row in summaries},
        key=lambda value: (
            value != (base_period, base_bar),
            value[0],
            value[1],
        ),
    )
    for period, bar_seconds in grouped_windows:
        buy_rows = [
            row
            for row in summaries
            if row.leg == "buy"
            and row.period == period
            and row.bar_seconds == bar_seconds
            and _eligible_summary(row)
        ]
        sell_rows = [
            row
            for row in summaries
            if row.leg == "sell"
            and row.period == period
            and row.bar_seconds == bar_seconds
            and _eligible_summary(row)
        ]
        all_window_rows = [
            row
            for row in summaries
            if row.period == period and row.bar_seconds == bar_seconds
        ]
        if not all_window_rows:
            continue
        best_buy = _best_candidate(buy_rows)
        best_sell = _best_candidate(sell_rows)
        alternate = period != base_period or bar_seconds != base_bar

        if not alternate:
            buy_changed = bool(
                best_buy is not None and abs(best_buy.multiplier - base_buy) >= 1e-8
            )
            sell_changed = bool(
                best_sell is not None
                and (
                    abs(best_sell.multiplier - base_sell) >= 1e-8
                    or (
                        adaptive_profit
                        and best_sell.minimum_profit_multiplier is not None
                        and abs(best_sell.minimum_profit_multiplier - base_profit) >= 1e-8
                    )
                )
            )
            if buy_changed:
                append_screened_profile(
                    label=f"Replay-screened BUY-only candidate {period}x{bar_seconds}s",
                    period=period,
                    bar_seconds=bar_seconds,
                    buy_row=best_buy,
                    sell_row=None,
                    combined=False,
                    alternate_window=False,
                )
            if sell_changed:
                append_screened_profile(
                    label=f"Replay-screened SELL-only candidate {period}x{bar_seconds}s",
                    period=period,
                    bar_seconds=bar_seconds,
                    buy_row=None,
                    sell_row=best_sell,
                    combined=False,
                    alternate_window=False,
                )
            if buy_changed and sell_changed:
                append_screened_profile(
                    label=f"Combined independently screened comparison {period}x{bar_seconds}s",
                    period=period,
                    bar_seconds=bar_seconds,
                    buy_row=best_buy,
                    sell_row=best_sell,
                    combined=True,
                    alternate_window=False,
                )
            continue

        if best_buy is not None or best_sell is not None:
            append_screened_profile(
                label=f"Alternate-window local replay experiment {period}x{bar_seconds}s",
                period=period,
                bar_seconds=bar_seconds,
                buy_row=best_buy,
                sell_row=best_sell,
                combined=best_buy is not None and best_sell is not None,
                alternate_window=True,
            )
            continue

        insufficient = all(
            row.priority == "insufficient ATR coverage" for row in all_window_rows
        )
        if insufficient:
            profile = _profile_common(
                cycles=cycles,
                retained_settings=retained_settings,
                period=period,
                bar_seconds=bar_seconds,
                drop=base_drop,
                buy=base_buy,
                profit=base_profit,
                sell=base_sell,
                lower=base_min,
                upper=base_max,
            )
            profile["atr_adaptive_enabled"] = True
            changed_fields = ["atr_period", "atr_bar_seconds"]
            if not control_atr_enabled:
                changed_fields.insert(0, "atr_adaptive_enabled")
            available = [
                row.candidate_atr_coverage_pct
                for row in all_window_rows
                if row.candidate_atr_coverage_pct is not None
            ]
            evidence = (
                "The saved fill windows could not reconstruct this ATR window often enough for ranking. "
                "The window would also change unobserved initial-drop behavior. Evaluate only as a controlled "
                "paper experiment while collecting longer pre-fill history."
            )
            if not control_atr_enabled:
                evidence += (
                    " The historical manual-percentage control had ATR adaptation disabled; this experiment "
                    "explicitly enables ATR and therefore changes entry selection outside the saved windows."
                )
            profile.update(
                {
                    "profile": f"Unscored ATR-window experiment {period}x{bar_seconds}s",
                    "evaluation_priority": rank,
                    "evaluation_status": "unscored: insufficient candidate-specific ATR history",
                    "settings_source": "fixed alternate-window experiment around the evaluation control",
                    "actual_settings_control": False,
                    "evaluation_control": False,
                    "primary_eligible": False,
                    "changed_fields": changed_fields,
                    "changed_legs": ["BUY", "normal SELL"],
                    "alternate_atr_window": True,
                    "candidate_atr_coverage_pct": median(available),
                    "evidence": evidence,
                    "replay_evaluated_fields": ["atr_period", "atr_bar_seconds"],
                    "retained_unevaluated_fields": list(retained_unevaluated_fields),
                    "retained_unevaluated_settings_source": retained_source,
                }
            )
            profiles.append(profile)
            rank += 1

    for label, factor in (("lower", 0.85), ("higher", 1.15)):
        profile = _profile_common(
            cycles=cycles,
            retained_settings=retained_settings,
            period=base_period,
            bar_seconds=base_bar,
            # 0.01 is the trading app's GUI minimum for the initial-drop
            # multiplier (see normalize_atr_multiplier); the sensitivity
            # experiment must not use a stricter private floor.
            drop=max(0.01, min(50.0, round(base_drop * factor, 2))),
            buy=base_buy,
            profit=base_profit,
            sell=base_sell,
            lower=base_min,
            upper=base_max,
        )
        profile["atr_adaptive_enabled"] = True
        changed_fields = ["atr_initial_drop_multiplier"]
        if not control_atr_enabled:
            changed_fields.insert(0, "atr_adaptive_enabled")
        evidence = (
            "The optimizer cannot score initial-drop changes from fill-centred captures. "
            "Use only in paper trading or with future full-cycle data."
        )
        if not control_atr_enabled:
            evidence += (
                " The historical manual-percentage control had ATR adaptation disabled; this experiment "
                "explicitly enables ATR and therefore changes the unobserved initial-drop decision."
            )
        profile.update(
            {
                "profile": f"Initial-drop sensitivity {label}",
                "evaluation_priority": rank,
                "evaluation_status": "unscored: requires full pre-entry path",
                "settings_source": "unscored sensitivity around the evaluation control",
                "actual_settings_control": False,
                "evaluation_control": False,
                "primary_eligible": False,
                "changed_fields": changed_fields,
                "changed_legs": ["initial entry selection"],
                "candidate_atr_coverage_pct": None,
                "evidence": evidence,
                "replay_evaluated_fields": [],
                "unscored_sensitivity_field": "atr_initial_drop_multiplier",
                "retained_unevaluated_fields": list(retained_unevaluated_fields),
                "retained_unevaluated_settings_source": retained_source,
            }
        )
        profiles.append(profile)
        rank += 1
    return profiles


def _primary_evaluation_setting(
    profiles: list[dict[str, Any]],
    coverage: dict[str, Any],
) -> dict[str, Any]:
    """Select one conservative settings set backed by stable paired evidence.

    A changed profile is eligible only when one independently replayed leg uses
    the control ATR window, compares candidate and control on identical cycles,
    survives deterministic trading-day bootstrap and leave-one-day-out checks,
    is not materially worse after right-censoring, and sits at the center of a
    supported adjacent parameter region. Otherwise the unchanged evaluation
    control remains the single paper-test set.
    """

    def finite(value: Any) -> float | None:
        number = safe_float(value)
        return number if number is not None and math.isfinite(number) else None

    def json_safe(value: Any) -> Any:
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, dict):
            return {str(key): json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_safe(item) for item in value]
        return value

    def stable_value(value: Any) -> str:
        safe = json_safe(value)
        if isinstance(safe, list):
            return "[" + ",".join(stable_value(item) for item in safe) + "]"
        if isinstance(safe, dict):
            return "{" + ",".join(
                f"{key}:{stable_value(safe[key])}" for key in sorted(safe)
            ) + "}"
        return repr(safe)

    def profile_key(profile: dict[str, Any]) -> tuple[str, ...]:
        return tuple(
            f"{key}={stable_value(profile.get(key))}" for key in sorted(profile)
        )

    evaluated: list[tuple[dict[str, Any], str, dict[str, Any], dict[str, Any]]] = []
    rejected: list[tuple[str, list[str]]] = []
    for profile in profiles:
        if profile.get("evaluation_status") != "screened on saved fill windows":
            continue
        changed_legs = [str(name) for name in (profile.get("changed_legs") or [])]
        changed_fields = [str(name) for name in (profile.get("changed_fields") or [])]
        if len(changed_legs) != 1 or changed_legs[0] not in {"BUY", "normal SELL"}:
            rejected.append(
                (
                    str(profile.get("profile") or "candidate"),
                    ["the highlighted set requires exactly one independently replayed BUY or normal-SELL leg"],
                )
            )
            continue
        prefix = "entry" if changed_legs[0] == "BUY" else "exit"
        paired = dict(profile.get(f"{prefix}_paired_evidence") or {})
        region = dict(profile.get(f"{prefix}_stable_region") or {})
        reasons = list(profile.get(f"{prefix}_instability_reasons") or [])
        if profile.get("primary_eligible") is not True:
            if not reasons:
                reasons.append(
                    "the candidate did not pass the paired uncertainty and stable-region requirements"
                )
            rejected.append((str(profile.get("profile") or "candidate"), reasons))
            continue
        if profile.get(f"{prefix}_evidence_stable") is not True:
            rejected.append(
                (
                    str(profile.get("profile") or "candidate"),
                    reasons or ["paired evidence was marked unstable"],
                )
            )
            continue
        if region.get("supported") is not True or region.get("is_center") is not True:
            rejected.append(
                (
                    str(profile.get("profile") or "candidate"),
                    ["candidate is not the center of a supported adjacent parameter region"],
                )
            )
            continue
        evaluated.append((profile, prefix, paired, region))

    if evaluated:
        def rank(
            item: tuple[dict[str, Any], str, dict[str, Any], dict[str, Any]],
        ) -> tuple[Any, ...]:
            profile, _, paired, region = item
            improvement = finite(
                paired.get("paired_execution_adjusted_median_bps")
            )
            bootstrap = dict(paired.get("bootstrap") or {})
            ci80_lower = finite(bootstrap.get("ci80_lower_bps"))
            probability = finite(bootstrap.get("probability_positive_pct"))
            lodo = dict(paired.get("leave_one_day_out") or {})
            lodo_minimum = finite(lodo.get("minimum_bps"))
            changed_fields = list(profile.get("changed_fields") or [])
            priority = safe_int(profile.get("evaluation_priority"))
            return (
                len(changed_fields),
                -(ci80_lower if ci80_lower is not None else -math.inf),
                -(improvement if improvement is not None else -math.inf),
                -(probability if probability is not None else -math.inf),
                -(lodo_minimum if lodo_minimum is not None else -math.inf),
                -int(region.get("size") or 0),
                priority if priority is not None else math.inf,
                str(profile.get("profile") or ""),
                profile_key(profile),
            )

        selected_profile, prefix, paired, region = sorted(evaluated, key=rank)[0]
        selected = dict(json_safe(selected_profile))
        changed_legs = list(selected.get("changed_legs") or [])
        changed_fields = sorted(str(name) for name in selected.get("changed_fields") or [])
        bootstrap = dict(paired.get("bootstrap") or {})
        lodo = dict(paired.get("leave_one_day_out") or {})
        leg_screening_score = finite(selected.get(f"{prefix}_screening_score"))
        control_screening_score = finite(
            selected.get(f"{prefix}_control_screening_score")
        )
        screening_delta = (
            leg_screening_score - control_screening_score
            if leg_screening_score is not None
            and control_screening_score is not None
            else None
        )
        selected.update(
            {
                "selection_kind": "single-leg paired-and-robust paper-evaluation change",
                "selection_is_change": True,
                "selection_candidate_count": len(evaluated),
                "selection_rejected_candidate_count": len(rejected),
                "selection_scored_legs": changed_legs[0],
                "selection_changed_fields": changed_fields,
                "selection_paired_windows": paired.get("paired_windows"),
                "selection_paired_both_triggered": paired.get("paired_both_triggered"),
                "selection_paired_execution_adjusted_cycles": paired.get(
                    "paired_execution_adjusted_cycles"
                ),
                "selection_minimum_execution_model_samples": paired.get(
                    "minimum_execution_model_samples"
                ),
                "selection_paired_execution_adjusted_median_bps": paired.get(
                    "paired_execution_adjusted_median_bps"
                ),
                "selection_bootstrap_probability_positive_pct": bootstrap.get(
                    "probability_positive_pct"
                ),
                "selection_bootstrap_ci80_lower_bps": bootstrap.get("ci80_lower_bps"),
                "selection_bootstrap_ci80_upper_bps": bootstrap.get("ci80_upper_bps"),
                "selection_bootstrap_ci95_lower_bps": bootstrap.get("ci95_lower_bps"),
                "selection_bootstrap_ci95_upper_bps": bootstrap.get("ci95_upper_bps"),
                "selection_lodo_positive_pct": lodo.get("positive_pct"),
                "selection_lodo_minimum_bps": lodo.get("minimum_bps"),
                "selection_independent_trading_days": paired.get(
                    "independent_trading_days"
                ),
                "selection_candidate_km_trigger_probability": paired.get(
                    "candidate_km_trigger_probability"
                ),
                "selection_control_km_trigger_probability": paired.get(
                    "control_km_trigger_probability"
                ),
                "selection_km_horizon": paired.get("selected_km_horizon"),
                "selection_km_horizon_seconds": paired.get(
                    "selected_km_horizon_seconds"
                ),
                "selection_candidate_km_trigger_probability_pct": paired.get(
                    "selected_candidate_km_trigger_probability_pct"
                ),
                "selection_control_km_trigger_probability_pct": paired.get(
                    "selected_control_km_trigger_probability_pct"
                ),
                "selection_km_trigger_probability_delta_pct": paired.get(
                    "selected_km_trigger_probability_delta_pct"
                ),
                "selection_median_absolute_delay_delta_seconds": paired.get(
                    "median_absolute_delay_delta_seconds"
                ),
                "selection_median_mae_delta_bps": paired.get(
                    "median_mae_delta_bps"
                ),
                "selection_stable_region": region,
                "selection_execution_model": selected.get(
                    f"{prefix}_execution_model"
                )
                or {},
                "selection_evidence_stable": True,
                "selection_leg_screening_score": leg_screening_score,
                "selection_score_delta_vs_control": screening_delta,
                "selection_combined_screening_score": leg_screening_score,
                "selection_weakest_leg_screening_score": leg_screening_score,
                "selection_mean_score_delta_vs_control": screening_delta,
                "selection_evidence_level": coverage.get(
                    "evidence_level", "unknown"
                ),
                "selection_method": (
                    "Compare candidate and control on identical cycles; treat capture-end non-triggers as right-censored; "
                    "adjust simulated fills with saved spread and empirical adverse slippage; require at least five paired "
                    "windows, three both-triggered pairs, and five UTC trading days; require the deterministic 80% "
                    "trading-day bootstrap interval to stay above zero and every available leave-one-day-out estimate "
                    "to remain positive, "
                    "no material censor-aware trigger-probability deficit, and an independently supported adjacent "
                    "parameter region; then prefer that region's center."
                ),
                "selection_reason": (
                    f"Selected from {len(evaluated)} stable one-leg region-center profile(s). It changes "
                    f"{', '.join(changed_fields)} for {changed_legs[0]}. The paired execution-adjusted median is "
                    f"{_format_selection_number(paired.get('paired_execution_adjusted_median_bps'))} bps across "
                    f"{paired.get('paired_windows', 0)} identical-cycle pairs and "
                    f"{paired.get('independent_trading_days', 0)} UTC trading days."
                ),
                "selection_warning": (
                    "This remains an in-sample local-window paper-trading experiment, not an optimized live configuration. "
                    "The robustness gates reduce but do not eliminate selection bias, missing full-cycle context, or "
                    "differences between simulated and broker execution."
                ),
            }
        )
        return selected

    control_candidates = [
        profile for profile in profiles if profile.get("evaluation_control") is True
    ]
    if not control_candidates:
        control_candidates = [
            profile
            for profile in profiles
            if profile.get("actual_settings_control") is True
        ]
    if not control_candidates:
        control_candidates = list(profiles)
    control = (
        sorted(
            control_candidates,
            key=lambda profile: (
                safe_int(profile.get("evaluation_priority"))
                if safe_int(profile.get("evaluation_priority")) is not None
                else math.inf,
                str(profile.get("profile") or ""),
                profile_key(profile),
            ),
        )[0]
        if control_candidates
        else None
    )
    if control is None:
        return {}
    selected = dict(json_safe(control))
    rejected_text = ""
    if rejected:
        rejected_text = (
            f" {len(rejected)} changed replay-screened profile(s) were reviewed but failed one or more paired, "
            "censoring, bootstrap, influence, execution-model, or stable-region requirements."
        )
    selected.update(
        {
            "selection_kind": "unchanged evaluation-control paper test",
            "selection_is_change": False,
            "selection_candidate_count": 0,
            "selection_rejected_candidate_count": len(rejected),
            "selection_scored_legs": "none",
            "selection_changed_fields": [],
            "selection_evidence_stable": False,
            "selection_paired_windows": None,
            "selection_paired_both_triggered": None,
            "selection_paired_execution_adjusted_cycles": None,
            "selection_minimum_execution_model_samples": None,
            "selection_paired_execution_adjusted_median_bps": None,
            "selection_bootstrap_probability_positive_pct": None,
            "selection_bootstrap_ci80_lower_bps": None,
            "selection_bootstrap_ci80_upper_bps": None,
            "selection_bootstrap_ci95_lower_bps": None,
            "selection_bootstrap_ci95_upper_bps": None,
            "selection_lodo_positive_pct": None,
            "selection_lodo_minimum_bps": None,
            "selection_independent_trading_days": None,
            "selection_candidate_km_trigger_probability": {},
            "selection_control_km_trigger_probability": {},
            "selection_km_horizon": "",
            "selection_km_horizon_seconds": None,
            "selection_candidate_km_trigger_probability_pct": None,
            "selection_control_km_trigger_probability_pct": None,
            "selection_km_trigger_probability_delta_pct": None,
            "selection_median_absolute_delay_delta_seconds": None,
            "selection_median_mae_delta_bps": None,
            "selection_stable_region": {},
            "selection_execution_model": {},
            "selection_leg_screening_score": None,
            "selection_score_delta_vs_control": None,
            "selection_combined_screening_score": None,
            "selection_weakest_leg_screening_score": None,
            "selection_mean_score_delta_vs_control": None,
            "selection_evidence_level": coverage.get("evidence_level", "unknown"),
            "selection_method": (
                "Retain the exact candidate-grid evaluation control unless one baseline-window, one-leg candidate "
                "passes identical-cycle pairing, right-censoring, execution adjustment, trading-day bootstrap, "
                "leave-one-day-out, trigger-probability, and stable-region requirements."
            ),
            "selection_reason": (
                "The saved data does not support one changed ATR profile with sufficiently stable paired evidence. "
                "Continue evaluating the unchanged control while collecting more complete and independent windows."
                + rejected_text
            ),
            "selection_warning": (
                "This unchanged control is not an optimized live configuration and is not evidence that the settings "
                "are optimal. It is the least assumptive next paper-test set given the available fill-centred captures."
            ),
        }
    )
    return selected


def _format_selection_number(value: Any) -> str:
    number = safe_float(value)
    return f"{number:.3f}" if number is not None and math.isfinite(number) else "unavailable"


def _load_capture(meta: CaptureMeta, config: AnalysisConfig) -> CaptureData:
    """Parse one archive without retaining its full price series globally."""

    return load_capture(
        meta,
        max_rows=config.max_rows_per_capture,
        max_archive_uncompressed_bytes=config.max_archive_uncompressed_bytes,
    )


def _capture_stats(data: CaptureData) -> CaptureStats:
    return CaptureStats(
        raw_rows=data.raw_rows,
        usable_points=len(data.points),
        invalid_rows=data.invalid_rows,
        duplicate_rows=data.duplicate_rows,
        atr_points=sum(1 for point in data.points if point.atr_pct is not None),
    )


def _append_note(observation: Any, text: str) -> None:
    observation.note = "; ".join(
        part
        for part in (str(observation.note or "").strip(), text.strip())
        if part
    )


def _analyze_ticker(
    *,
    ticker: str,
    cycles: list[dict[str, Any]],
    metas: list[CaptureMeta],
    dataset: DatabaseDataset,
    config: AnalysisConfig,
    source_root: Path,
    progress: ProgressCallback | None,
    ticker_count: int,
) -> TickerAnalysis:
    settings_summary, settings_profiles, settings_regimes, settings_history = build_settings_audit(
        cycles,
        settings=dataset.settings,
        settings_updated_at=dataset.settings_updated_at,
        ticker=ticker,
        ticker_count=ticker_count,
    )
    historical_profiles_by_cycle = cycle_profile_lookup(settings_history)
    source_control_settings = dict(settings_summary["evaluation_control_settings"])

    # Normalize the complete control once, before candidate generation. Saved
    # databases can predate current GUI validation or be manually edited. Using
    # raw values in the grid while displaying normalized values would make a
    # report impossible to reproduce. Source values remain in the report and
    # every changed field is disclosed below.
    replay_control_settings = _profile_common(
        cycles=cycles,
        retained_settings=source_control_settings,
        period=source_control_settings.get("atr_period", ATR_SETTING_DEFAULTS["atr_period"]),
        bar_seconds=source_control_settings.get(
            "atr_bar_seconds",
            ATR_SETTING_DEFAULTS["atr_bar_seconds"],
        ),
        drop=source_control_settings.get(
            "atr_initial_drop_multiplier",
            ATR_SETTING_DEFAULTS["atr_initial_drop_multiplier"],
        ),
        buy=source_control_settings.get(
            "atr_buy_rebound_multiplier",
            ATR_SETTING_DEFAULTS["atr_buy_rebound_multiplier"],
        ),
        profit=source_control_settings.get(
            "atr_minimum_profit_multiplier",
            ATR_SETTING_DEFAULTS["atr_minimum_profit_multiplier"],
        ),
        sell=source_control_settings.get(
            "atr_sell_trail_multiplier",
            ATR_SETTING_DEFAULTS["atr_sell_trail_multiplier"],
        ),
        lower=source_control_settings.get("atr_min_pct", ATR_SETTING_DEFAULTS["atr_min_pct"]),
        upper=source_control_settings.get("atr_max_pct", ATR_SETTING_DEFAULTS["atr_max_pct"]),
    )

    def setting_equal(name: str, source: Any, used: Any) -> bool:
        if name in {
            "atr_adaptive_enabled",
            "atr_adapt_minimum_profit_enabled",
            "atr_block_new_buy_until_ready",
            "atr_adapt_protective_sell_enabled",
        }:
            return isinstance(source, bool) and source is used
        if name in {"atr_period", "atr_bar_seconds"}:
            return safe_int(source) == used
        source_number = safe_float(source)
        used_number = safe_float(used)
        return (
            source_number is not None
            and used_number is not None
            and math.isclose(source_number, used_number, rel_tol=0.0, abs_tol=1e-12)
        )

    control_adjustments = {
        name: {
            "saved": source_control_settings.get(name),
            "used": replay_control_settings.get(name),
        }
        for name in ATR_SETTING_DEFAULTS
        if not setting_equal(
            name,
            source_control_settings.get(name),
            replay_control_settings.get(name),
        )
    }
    raw_baseline_period = source_control_settings.get("atr_period")
    raw_baseline_bar = source_control_settings.get("atr_bar_seconds")
    baseline_period = int(replay_control_settings["atr_period"])
    baseline_bar = int(replay_control_settings["atr_bar_seconds"])
    baseline_buy_multiplier = float(
        replay_control_settings["atr_buy_rebound_multiplier"]
    )
    baseline_profit_multiplier = float(
        replay_control_settings["atr_minimum_profit_multiplier"]
    )
    baseline_sell_multiplier = float(
        replay_control_settings["atr_sell_trail_multiplier"]
    )
    control_atr_enabled = truthy(
        replay_control_settings.get("atr_adaptive_enabled"),
        default=True,
    )
    control_minimum_profit_adaptive = truthy(
        replay_control_settings.get("atr_adapt_minimum_profit_enabled"),
        default=True,
    )
    baseline_atr_min = float(replay_control_settings["atr_min_pct"])
    baseline_atr_max = float(replay_control_settings["atr_max_pct"])
    raw_atr_min = source_control_settings.get("atr_min_pct")
    raw_atr_max = source_control_settings.get("atr_max_pct")
    settings_summary["evaluation_control_source_values"] = source_control_settings
    # Keep the actual saved/historical control separate from the normalized
    # profile used for counterfactual calculations.  Calling normalized values
    # "actual settings" would erase evidence from legacy or manually edited
    # databases and could make the report claim that a value ran when it did
    # not.  Suggestions use the reproducible replay control; provenance tables
    # continue to show the source values.
    settings_summary["counterfactual_replay_control_settings"] = replay_control_settings
    settings_summary["evaluation_control_normalization_adjustments"] = control_adjustments
    settings_summary["evaluation_control_values_preserved_exactly_for_replay"] = not bool(
        control_adjustments
    )
    settings_summary["evaluation_control_raw_atr_period"] = raw_baseline_period
    settings_summary["evaluation_control_raw_atr_bar_seconds"] = raw_baseline_bar
    settings_summary["counterfactual_replay_atr_period"] = baseline_period
    settings_summary["counterfactual_replay_atr_bar_seconds"] = baseline_bar
    settings_summary["counterfactual_replay_atr_window_normalized"] = any(
        field in control_adjustments for field in ("atr_period", "atr_bar_seconds")
    )
    settings_summary["evaluation_control_raw_atr_min_pct"] = raw_atr_min
    settings_summary["evaluation_control_raw_atr_max_pct"] = raw_atr_max
    settings_summary["counterfactual_replay_clamps_normalized"] = any(
        field in control_adjustments for field in ("atr_min_pct", "atr_max_pct")
    )
    settings_summary["counterfactual_replay_clamp_policy"] = (
        "Every candidate uses the evaluation-control ATR minimum and maximum clamps "
        "for every cycle. Historical cycle profiles are retained as provenance and "
        "reported separately; they are not silently substituted into a candidate."
    )
    settings_summary["counterfactual_replay_atr_min_pct"] = baseline_atr_min
    settings_summary["counterfactual_replay_atr_max_pct"] = baseline_atr_max
    settings_summary["counterfactual_replay_atr_enabled"] = control_atr_enabled
    settings_summary["counterfactual_minimum_profit_adaptive"] = (
        control_minimum_profit_adaptive
    )
    settings_summary["counterfactual_minimum_profit_policy"] = (
        "The evaluation control's ATR minimum-profit toggle is held fixed for every cycle. "
        "When it is off, each cycle's stored manual rise_trigger_pct is retained and only "
        "the SELL trail is varied. Historical cycle toggles never silently redefine one candidate."
    )
    buy_grid = buy_candidates(
        baseline_buy_multiplier,
        baseline_period,
        baseline_bar,
    )
    sell_grid = sell_candidates(
        baseline_profit_multiplier,
        baseline_sell_multiplier,
        baseline_period,
        baseline_bar,
        vary_minimum_profit=control_minimum_profit_adaptive,
    )

    capture_stats: dict[Path, CaptureStats] = {}
    observations = []
    matched_buy = 0
    matched_sell = 0
    matched_protective = 0
    replayable_buy = 0
    replayable_sell = 0
    replay_capture_paths: set[Path] = set()
    buy_fill_count = 0
    normal_sell_fill_count = 0
    protective_sell_fill_count = 0
    issues: list[str] = []

    ticker_metas = [meta for meta in metas if meta.ticker == ticker]
    total = len(cycles)
    for index, cycle in enumerate(cycles, start=1):
        _notify(
            progress,
            f"Replaying {ticker} cycle {cycle.get('cycle_number', index)}",
            index,
            total,
        )
        cycle_id = str(cycle.get("id") or "")
        cycle_number = safe_int(cycle.get("cycle_number"))
        cycle_label = cycle_number if cycle_number is not None else cycle_id or "unknown"
        cycle_atr_enabled = truthy(
            cycle.get("atr_adaptive_enabled"),
            default=True,
        )

        buy_time, buy_price = _buy_fields(dataset, cycle)
        if buy_price is not None and buy_price > 0:
            buy_fill_count += 1
            buy_meta, data, buy_candidate_count = _select_usable_capture(
                metas=ticker_metas,
                dataset=dataset,
                cycle=cycle,
                leg="buy",
                fill_time=buy_time,
                capture_stats=capture_stats,
                config=config,
            )
            if buy_meta is not None and data is not None:
                matched_buy += 1
                replay_capture_paths.add(buy_meta.path)
                replayable_buy += 1
                order_time = _order_time(
                    dataset,
                    cycle,
                    "buy",
                    preferred_order_ref=buy_meta.order_ref,
                )
                stored_atr_window = _stored_atr_window(cycle, buy_meta)
                for candidate in buy_grid:
                    observation = replay_buy(
                        ticker=ticker,
                        cycle_id=cycle_id,
                        cycle_number=cycle_number,
                        points=data.points,
                        candidate=candidate,
                        order_time_utc=order_time,
                        actual_fill_time_utc=buy_time,
                        actual_fill_price=buy_price,
                        atr_min_pct=baseline_atr_min,
                        atr_max_pct=baseline_atr_max,
                        allow_captured_atr_fallback=(
                            stored_atr_window
                            == (candidate.period, candidate.bar_seconds)
                        ),
                    )
                    if not cycle_atr_enabled:
                        _append_note(
                            observation,
                            "Historical cycle used manual percentages; this is a hypothetical ATR-adaptive BUY replay",
                        )
                    observation.historical_atr_profile_id = historical_profiles_by_cycle.get(
                        cycle_id,
                        "UNKNOWN",
                    )
                    observations.append(observation)
            elif buy_candidate_count:
                issues.append(
                    f"Cycle {cycle_label}: {buy_candidate_count} matching BUY capture archive(s) contained no usable market rows"
                )
            else:
                issues.append(
                    f"Cycle {cycle_label}: no matching BUY fill capture"
                )

        sell_time, sell_price = _normal_sell_fields(dataset, cycle)
        if sell_price is not None and sell_price > 0:
            normal_sell_fill_count += 1
            sell_meta, data, sell_candidate_count = _select_usable_capture(
                metas=ticker_metas,
                dataset=dataset,
                cycle=cycle,
                leg="sell",
                fill_time=sell_time,
                capture_stats=capture_stats,
                config=config,
            )
            if sell_meta is not None and data is not None:
                matched_sell += 1
                replay_capture_paths.add(sell_meta.path)
                replayable_sell += 1
                reference_time = _order_time(
                    dataset,
                    cycle,
                    "sell",
                    preferred_order_ref=sell_meta.order_ref,
                ) or sell_time
                historical_minimum_profit_adaptive = truthy(
                    cycle.get("atr_adapt_minimum_profit_enabled"),
                    default=True,
                )
                manual_minimum_profit = safe_float(
                    cycle.get("rise_trigger_pct")
                )
                stored_atr_window = _stored_atr_window(cycle, sell_meta)
                for candidate in sell_grid:
                    observation = replay_sell(
                        ticker=ticker,
                        cycle_id=cycle_id,
                        cycle_number=cycle_number,
                        points=data.points,
                        candidate=candidate,
                        reference_time_utc=reference_time,
                        strategy_start_time_utc=buy_time,
                        actual_fill_time_utc=sell_time,
                        actual_fill_price=sell_price,
                        average_buy_price=buy_price,
                        atr_min_pct=baseline_atr_min,
                        atr_max_pct=baseline_atr_max,
                        minimum_profit_adaptive=control_minimum_profit_adaptive,
                        manual_minimum_profit_pct=manual_minimum_profit,
                        slippage_buffer_enabled=truthy(
                            cycle.get("slippage_buffer_enabled"),
                            default=False,
                        ),
                        slippage_buffer_pct=(
                            safe_float(cycle.get("slippage_buffer_pct")) or 0.0
                        ),
                        allow_captured_atr_fallback=(
                            stored_atr_window
                            == (candidate.period, candidate.bar_seconds)
                        ),
                    )
                    if not cycle_atr_enabled:
                        _append_note(
                            observation,
                            "Historical cycle used manual percentages; this is a hypothetical ATR-adaptive SELL replay",
                        )
                    if (
                        historical_minimum_profit_adaptive
                        != control_minimum_profit_adaptive
                    ):
                        _append_note(
                            observation,
                            "The cycle's historical minimum-profit mode differed from the fixed evaluation-control mode",
                        )
                    observation.historical_atr_profile_id = historical_profiles_by_cycle.get(
                        cycle_id,
                        "UNKNOWN",
                    )
                    observations.append(observation)
            elif sell_candidate_count:
                issues.append(
                    f"Cycle {cycle_label}: {sell_candidate_count} matching normal SELL capture archive(s) contained no usable market rows"
                )
            else:
                issues.append(
                    f"Cycle {cycle_label}: no matching normal SELL fill capture"
                )

        protective_time, protective_price = _protective_sell_fields(dataset, cycle)
        if protective_price is not None and protective_price > 0:
            protective_sell_fill_count += 1
            protective_meta, _, protective_candidate_count = _select_usable_capture(
                metas=ticker_metas,
                dataset=dataset,
                cycle=cycle,
                leg="protective_sell",
                fill_time=protective_time,
                capture_stats=capture_stats,
                config=config,
            )
            if protective_meta is not None:
                matched_protective += 1
            elif protective_candidate_count:
                issues.append(
                    f"Cycle {cycle_label}: {protective_candidate_count} matching protective SELL capture archive(s) contained no usable market rows"
                )
            else:
                issues.append(
                    f"Cycle {cycle_label}: no matching protective SELL fill capture"
                )

    # Parse every usable archive, including unmatched ones, so a ZIP with valid
    # metadata but no usable prices cannot inflate the coverage score.
    for meta in ticker_metas:
        if meta.usable and meta.path not in capture_stats:
            data = _load_capture(meta, config)
            capture_stats[meta.path] = _capture_stats(data)

    captured_atr_rows = sum(
        capture_stats[path].atr_points
        for path in replay_capture_paths
        if path in capture_stats
    )

    if protective_sell_fill_count:
        issues.append(
            "Protective SELL fills are included in coverage and capture inventory but excluded from normal-profit SELL candidate ranking."
        )
    for meta in ticker_metas:
        if not meta.usable:
            issues.append(
                f"Unreadable capture {meta.path.name}: {'; '.join(meta.issues)}"
            )

    execution_model = apply_execution_model(observations)
    summaries = summarize_observations(observations)
    evidence_methodology = enrich_candidate_evidence(
        observations,
        summaries,
        execution_model,
    )
    replay_windows_with_usable_atr = len(
        {
            (row.cycle_id, row.leg)
            for row in observations
            if row.atr_source
            in {"capture_reconstructed", "bot_captured_fallback"}
        }
    )
    coverage = _coverage(
        cycles=cycles,
        metas=ticker_metas,
        capture_stats=capture_stats,
        buy_fills=buy_fill_count,
        normal_sell_fills=normal_sell_fill_count,
        protective_sell_fills=protective_sell_fill_count,
        matched_buy=matched_buy,
        matched_sell=matched_sell,
        matched_protective=matched_protective,
        replayable_buy=replayable_buy,
        replayable_sell=replayable_sell,
        replay_windows_with_usable_atr=replay_windows_with_usable_atr,
        captured_atr_rows=captured_atr_rows,
    )
    suggested = _setting_profiles(
        cycles=cycles,
        summaries=summaries,
        coverage=coverage,
        settings_summary=settings_summary,
    )
    primary_evaluation_setting = _primary_evaluation_setting(suggested, coverage)
    inventory = [
        _capture_inventory(meta, capture_stats.get(meta.path), source_root)
        for meta in sorted(
            ticker_metas,
            key=lambda item: (
                -1 if item.cycle_number is None else item.cycle_number,
                item.event_time_utc,
                str(item.path),
            ),
        )
    ]
    return TickerAnalysis(
        ticker=ticker,
        coverage=coverage,
        cycles=[_sanitize_cycle(cycle) for cycle in cycles],
        atr_settings_summary=settings_summary,
        atr_settings_profiles=settings_profiles,
        atr_settings_regimes=settings_regimes,
        atr_settings_history=settings_history,
        capture_inventory=inventory,
        replay_observations=observations,
        candidate_summaries=summaries,
        execution_model=execution_model,
        evidence_methodology=evidence_methodology,
        suggested_settings=suggested,
        primary_evaluation_setting=primary_evaluation_setting,
        issues=sorted(set(issues)),
        limitations=list(_LIMITATIONS),
    )


def _output_is_unsafe(
    output_root: Path,
    database: Path,
    captures: Path,
    bot_lock: Path,
) -> bool:
    output = output_root.resolve()
    database = database.resolve()
    reserved_files = (
        database,
        Path(f"{database}-wal").resolve(),
        Path(f"{database}-shm").resolve(),
        bot_lock.resolve(),
    )
    if any(output == path or path in output.parents for path in reserved_files):
        return True
    capture_root = captures.resolve()
    return output == capture_root or capture_root in output.parents


def run_analysis(
    config: AnalysisConfig,
    *,
    progress: ProgressCallback | None = None,
) -> AnalysisResult:
    """Analyze a stopped bot folder and return in-memory report data.

    Callers must obtain explicit user confirmation before invoking this function.
    It atomically acquires the bot's normal lock, copies the source SQLite/WAL
    files through ordinary read-only filesystem access, opens only that private
    copy to create a temporary snapshot, reads captures without extraction, and
    releases the lock after all source reads are complete.
    """
    config = config.normalized()
    paths = source_paths(config.source_dir)
    global_issues = validate_source(paths)
    if _output_is_unsafe(
        config.output_root,
        paths.database,
        paths.captures,
        paths.bot_lock,
    ):
        raise AnalysisError(
            "Output path cannot replace or be nested under the database, SQLite sidecars, bot lock, "
            "capture directory, or a directory inside debug_captures."
        )

    _notify(progress, "Acquiring the portable-folder safety lease")
    with BotFolderLease(paths.bot_lock):
        initial_source_state = source_state(paths.database)
        initial_capture_state = capture_source_state(paths.captures)
        _notify(progress, "Creating a temporary read-only SQLite snapshot")
        with readonly_database_snapshot(paths.database) as snapshot:
            snapshot_size = snapshot.stat().st_size
            dataset = SnapshotDatabase(snapshot).load()
            _notify(progress, "Inventorying capture archives")
            metas = inventory_captures(
                paths.captures,
                max_archive_uncompressed_bytes=(
                    config.max_archive_uncompressed_bytes
                ),
                # Initial capture_source_state already hashed every archive for
                # deterministic identity and mutation detection. Avoid reading
                # all ZIP bytes a second time during manifest inventory.
                hash_files=False,
                progress=progress,
            )
            capture_hashes = {row[0]: row[3] for row in initial_capture_state}
            for meta in metas:
                try:
                    relative = meta.path.resolve().relative_to(paths.captures.resolve()).as_posix()
                except ValueError:
                    continue
                meta.sha256 = capture_hashes.get(relative, "")

            cycle_tickers = dataset.tickers
            capture_tickers = sorted({meta.ticker for meta in metas if meta.ticker})
            tickers = sorted(set(cycle_tickers) | set(capture_tickers))
            analyses: list[TickerAnalysis] = []
            for index, ticker in enumerate(tickers, start=1):
                _notify(progress, f"Analyzing {ticker}", index, len(tickers))
                ticker_cycles = [
                    cycle
                    for cycle in dataset.cycles
                    if str(cycle.get("ticker") or "").strip().upper()
                    == ticker
                ]
                analyses.append(
                    _analyze_ticker(
                        ticker=ticker,
                        cycles=ticker_cycles,
                        metas=metas,
                        dataset=dataset,
                        config=config,
                        source_root=paths.root,
                        progress=progress,
                        ticker_count=len(tickers),
                    )
                )

            if source_state(paths.database) != initial_source_state:
                raise AnalysisError(
                    "The source SQLite database or its WAL state changed during analysis; the run was aborted."
                )
            if capture_source_state(paths.captures) != initial_capture_state:
                raise AnalysisError(
                    "The debug_captures archive set changed during analysis; the run was aborted."
                )

    if not tickers:
        global_issues.append(
            "No ticker records were found in cycles or capture manifests."
        )
    corrupt_count = sum(1 for meta in metas if not meta.usable)
    if corrupt_count:
        global_issues.append(
            f"{corrupt_count} capture archive(s) were corrupt, oversized, or unsupported."
        )
    event_types = Counter(meta.event_type or "UNKNOWN" for meta in metas)
    schema = dict(dataset.schema)
    schema["capture_event_types"] = dict(sorted(event_types.items()))
    schema["optimizer_version"] = APP_VERSION
    fingerprint = analysis_fingerprint(
        database_state=initial_source_state,
        capture_state=initial_capture_state,
        config=config,
    )
    analysis_id = fingerprint
    run_id = f"optimizer_{fingerprint[:16]}"
    output_dir = config.output_root / run_id
    data_through = data_through_utc(dataset, metas)
    return AnalysisResult(
        run_id=run_id,
        analysis_id=analysis_id,
        source=paths,
        output_dir=output_dir,
        # Retained as a compatibility alias. It is evidence-derived rather than
        # wall-clock run time so repeated analyses are byte-for-byte repeatable.
        generated_at_utc=data_through,
        data_through_utc=data_through,
        input_fingerprint=fingerprint,
        database_sha256=database_content_fingerprint(initial_source_state),
        database_snapshot_bytes=snapshot_size,
        schema=schema,
        tickers=analyses,
        global_issues=global_issues,
    )
