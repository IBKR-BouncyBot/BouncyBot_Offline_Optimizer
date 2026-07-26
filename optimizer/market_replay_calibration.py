"""Optional execution-cost calibration from a stopped BouncyBot SQLite database.

The calibration is deliberately conservative and read-only.  Actual BouncyBot
executions provide order notionals and commissions; when their timestamps overlap
the selected Market Replay recordings, the executions are also matched to the
latest same-side quote at or before the fill.  The replay then uses the higher of
the configured reserve and the supported 75th-percentile observed adverse cost.
"""

from __future__ import annotations

import bisect
import hashlib
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .database import DatabaseDataset, SnapshotDatabase, safe_float, safe_int
from .market_replay_models import IbrecRecording, MarketReplayConfig
from .safety import (
    BotFolderLease,
    SourceSafetyError,
    readonly_database_snapshot,
    source_paths,
    source_state,
    validate_database_source,
)
from .utils import percentile, timestamp_seconds


@dataclass(slots=True, frozen=True)
class ExecutionCalibration:
    """Deterministic ticker-specific assumptions derived from actual executions."""

    enabled: bool
    applied: bool = False
    source_fingerprint: str = ""
    source_components: tuple[dict[str, Any], ...] = ()
    source_database_sha256: str = ""
    ticker: str = ""
    con_id: int = 0
    currency: str = ""
    identity_selection_mode: str = ""
    matched_cycles: int = 0
    legacy_identity_cycles: int = 0
    execution_rows_considered: int = 0
    execution_rows_usable: int = 0
    duplicate_execution_rows: int = 0
    commission_currency_mismatch_rows: int = 0
    commission_currency_assumed_rows: int = 0
    commission_unavailable_rows: int = 0
    cycle_commission_groups_applied: int = 0
    buy_execution_rows: int = 0
    sell_execution_rows: int = 0
    buy_order_samples: int = 0
    sell_order_samples: int = 0
    buy_quote_matched_orders: int = 0
    sell_quote_matched_orders: int = 0
    buy_total_cost_samples: int = 0
    sell_total_cost_samples: int = 0
    trade_notional_samples: int = 0
    buy_commission_bps_p75: float | None = None
    sell_commission_bps_p75: float | None = None
    buy_commission_bps_p50: float | None = None
    sell_commission_bps_p50: float | None = None
    buy_commission_bps_p90: float | None = None
    sell_commission_bps_p90: float | None = None
    buy_adverse_slippage_bps_p75: float | None = None
    sell_adverse_slippage_bps_p75: float | None = None
    buy_adverse_slippage_bps_p50: float | None = None
    sell_adverse_slippage_bps_p50: float | None = None
    buy_adverse_slippage_bps_p90: float | None = None
    sell_adverse_slippage_bps_p90: float | None = None
    buy_total_adverse_cost_bps_p75: float | None = None
    sell_total_adverse_cost_bps_p75: float | None = None
    buy_total_adverse_cost_bps_p50: float | None = None
    sell_total_adverse_cost_bps_p50: float | None = None
    buy_total_adverse_cost_bps_p90: float | None = None
    sell_total_adverse_cost_bps_p90: float | None = None
    median_buy_notional: float | None = None
    configured_execution_cost_bps_per_side: float = 0.0
    effective_execution_cost_bps_per_side: float = 0.0
    effective_buy_execution_cost_bps_per_side: float = 0.0
    effective_sell_execution_cost_bps_per_side: float = 0.0
    configured_trade_notional: float = 0.0
    effective_trade_notional: float = 0.0
    maximum_quote_age_seconds: float = 0.0
    minimum_samples: int = 0
    used_execution_cost_calibration: bool = False
    used_trade_notional_calibration: bool = False
    date_specific_execution_costs: tuple[dict[str, Any], ...] = ()
    date_specific_trade_notionals: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _OrderEvidence:
    """Execution rows aggregated to one broker order where identity is available."""

    cycle_id: str
    side: str
    identity: str
    execution_date: str = ""
    quantity: float = 0.0
    notional: float = 0.0
    commission: float = 0.0
    commission_rows: int = 0
    quote_matched_notional: float = 0.0
    quote_weighted_slippage: float = 0.0
    row_count: int = 0

    @property
    def commission_bps(self) -> float | None:
        if self.notional <= 0 or self.commission_rows <= 0:
            return None
        value = max(0.0, self.commission) / self.notional * 10_000.0
        return value if math.isfinite(value) and 0 <= value < 10_000 else None

    @property
    def adverse_slippage_bps(self) -> float | None:
        if self.quote_matched_notional <= 0:
            return None
        value = self.quote_weighted_slippage / self.quote_matched_notional
        return value if math.isfinite(value) and 0 <= value < 10_000 else None

    @property
    def total_adverse_cost_bps(self) -> float | None:
        slippage = self.adverse_slippage_bps
        commission = self.commission_bps
        if slippage is None or commission is None:
            return None
        return slippage + commission


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_components(
    state: tuple[tuple[str, int, int, str] | None, ...],
) -> tuple[dict[str, Any], ...]:
    values = [
        {"name": name, "size": size, "sha256": digest}
        for item in state
        if item is not None
        for name, size, _modified_ns, digest in (item,)
    ]
    return tuple(sorted(values, key=lambda item: str(item["name"])))


def _source_fingerprint(components: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for component in components:
        digest.update(str(component.get("name") or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(int(component.get("size") or 0)).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(component.get("sha256") or "").encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _execution_side(row: dict[str, Any]) -> str | None:
    value = str(row.get("side") or row.get("action") or "").strip().upper()
    if value in {"BOT", "BUY", "B"}:
        return "BUY"
    if value in {"SLD", "SELL", "S", "PROTECTIVE_SELL"}:
        return "SELL"
    return None


def _positive(value: Any) -> float | None:
    number = safe_float(value)
    return number if number is not None and number > 0 else None


def _recording_currency(recording: IbrecRecording) -> str:
    return str(recording.contract.get("currency") or "").strip().upper()


def _matching_cycle_ids(
    dataset: DatabaseDataset,
    recording: IbrecRecording,
) -> tuple[set[str], int, str]:
    """Select exact contract cycles before any legacy ticker-only fallback.

    A database can contain old cycles from before ``con_id`` and ``currency``
    were persisted as well as newer cycles with a complete contract identity.
    Mixing both groups would let an unrelated same-symbol listing influence the
    calibration even when exact evidence exists.  Exact positive conId matches
    therefore win; legacy ticker/currency-compatible rows are used only when no
    exact cycle is available.
    """

    ticker = recording.symbol.strip().upper()
    currency = _recording_currency(recording)
    exact_ids: set[str] = set()
    legacy_ids: set[str] = set()
    for cycle in dataset.cycles:
        if str(cycle.get("ticker") or "").strip().upper() != ticker:
            continue
        cycle_con_id = safe_int(cycle.get("con_id")) or 0
        cycle_currency = str(cycle.get("currency") or "").strip().upper()
        if recording.con_id > 0 and cycle_con_id > 0 and cycle_con_id != recording.con_id:
            continue
        if currency and cycle_currency and cycle_currency != currency:
            continue
        cycle_id = str(cycle.get("id") or "").strip()
        if not cycle_id:
            continue
        if recording.con_id > 0 and cycle_con_id == recording.con_id:
            exact_ids.add(cycle_id)
        else:
            legacy_ids.add(cycle_id)
    if exact_ids:
        return exact_ids, 0, "exact_con_id"
    return legacy_ids, len(legacy_ids), "legacy_ticker_fallback"


def _execution_dedupe_key(row: dict[str, Any], cycle_id: str, side: str) -> tuple[Any, ...]:
    execution_id = str(row.get("execution_id") or row.get("exec_id") or "").strip()
    if execution_id:
        return ("execution", execution_id)
    return (
        "row",
        cycle_id,
        side,
        str(row.get("order_ref") or ""),
        safe_int(row.get("perm_id")) or 0,
        safe_int(row.get("order_id")) or 0,
        str(row.get("executed_at") or row.get("time") or ""),
        safe_float(row.get("shares") if row.get("shares") is not None else row.get("quantity")),
        safe_float(row.get("price") if row.get("price") is not None else row.get("avg_price")),
        safe_float(row.get("commission")),
    )


def _order_identity(row: dict[str, Any], cycle_id: str, side: str) -> str:
    order_ref = str(row.get("order_ref") or "").strip()
    if order_ref:
        return f"ref:{order_ref}"
    perm_id = safe_int(row.get("perm_id")) or 0
    if perm_id > 0:
        return f"perm:{perm_id}"
    order_id = safe_int(row.get("order_id")) or 0
    if order_id > 0:
        return f"order:{order_id}"
    execution_id = str(row.get("execution_id") or row.get("exec_id") or "").strip()
    if execution_id:
        return f"execution:{execution_id}"
    return f"fallback:{cycle_id}:{side}:{row.get('executed_at') or row.get('time')!s}"


def _quote_timelines(
    recording: IbrecRecording,
) -> tuple[list[float], list[tuple[float, float]], list[float], list[tuple[float, float]]]:
    """Return same-side quote updates suitable for no-future execution matching."""

    bid_times: list[float] = []
    bids: list[tuple[float, float]] = []
    ask_times: list[float] = []
    asks: list[tuple[float, float]] = []
    for tick in sorted(recording.ticks, key=lambda item: (item.timestamp, item.sequence)):
        bid = tick.valid_bid()
        ask = tick.valid_ask()
        # A quote callback refreshes its age even when the numerical price is
        # unchanged.  Preserve every explicit side update rather than only price
        # changes; otherwise a valid same-price quote can be treated as stale.
        if (tick.full_snapshot or "bid" in tick.changed_fields) and bid is not None:
            bid_times.append(tick.timestamp)
            bids.append((tick.timestamp, bid))
        if (tick.full_snapshot or "ask" in tick.changed_fields) and ask is not None:
            ask_times.append(tick.timestamp)
            asks.append((tick.timestamp, ask))
    return bid_times, bids, ask_times, asks


def _latest_quote(
    timestamp: float,
    times: list[float],
    values: list[tuple[float, float]],
    *,
    max_age: float,
) -> float | None:
    index = bisect.bisect_right(times, timestamp) - 1
    if index < 0:
        return None
    quote_time, value = values[index]
    age = timestamp - quote_time
    if age < -1e-9 or age > max_age:
        return None
    return value


def _derive_calibration(
    dataset: DatabaseDataset,
    recording: IbrecRecording,
    config: MarketReplayConfig,
    *,
    components: tuple[dict[str, Any], ...],
    database_sha256: str,
) -> ExecutionCalibration:
    normalized = config.normalized()
    cycle_ids, legacy_identity_cycles, identity_selection_mode = _matching_cycle_ids(
        dataset,
        recording,
    )
    cycles_by_id = {
        str(cycle.get("id") or "").strip(): cycle
        for cycle in dataset.cycles
        if str(cycle.get("id") or "").strip() in cycle_ids
    }
    rows: list[dict[str, Any]] = []
    for cycle_id in sorted(cycle_ids):
        rows.extend(dataset.executions_by_cycle.get(cycle_id, ()))
    rows.sort(
        key=lambda row: (
            timestamp_seconds(row.get("executed_at") or row.get("time")) or math.inf,
            str(row.get("order_ref") or ""),
            str(row.get("execution_id") or row.get("exec_id") or row.get("id") or ""),
        )
    )

    bid_times, bids, ask_times, asks = _quote_timelines(recording)
    groups: dict[tuple[str, str, str], _OrderEvidence] = {}
    seen_rows: set[tuple[Any, ...]] = set()
    buy_notional_by_cycle: dict[str, float] = {}
    usable = 0
    buy_rows = 0
    sell_rows = 0
    duplicates = 0
    commission_currency_mismatches = 0
    commission_currency_assumed = 0
    commission_unavailable = 0
    recording_currency = _recording_currency(recording)

    for row in rows:
        cycle_id = str(row.get("cycle_id") or "").strip()
        side = _execution_side(row)
        quantity = _positive(row.get("shares") if row.get("shares") is not None else row.get("quantity"))
        price = _positive(row.get("price") if row.get("price") is not None else row.get("avg_price"))
        executed_at = timestamp_seconds(row.get("executed_at") or row.get("time"))
        if side is None or quantity is None or price is None or executed_at is None:
            continue
        dedupe_key = _execution_dedupe_key(row, cycle_id, side)
        if dedupe_key in seen_rows:
            duplicates += 1
            continue
        seen_rows.add(dedupe_key)
        usable += 1
        buy_rows += int(side == "BUY")
        sell_rows += int(side == "SELL")
        notional = quantity * price
        if side == "BUY" and cycle_id:
            buy_notional_by_cycle[cycle_id] = buy_notional_by_cycle.get(cycle_id, 0.0) + notional

        identity = _order_identity(row, cycle_id, side)
        execution_date = datetime.fromtimestamp(
            executed_at,
            tz=timezone.utc,
        ).date().isoformat()
        group = groups.setdefault(
            (cycle_id, side, identity),
            _OrderEvidence(
                cycle_id=cycle_id,
                side=side,
                identity=identity,
                execution_date=execution_date,
            ),
        )
        if group.execution_date != execution_date:
            # A broker order should not span UTC trading dates in this evidence
            # model.  Blank the date rather than misclassifying it as prior data
            # during cross-fitting.
            group.execution_date = ""
        group.quantity += quantity
        group.notional += notional
        group.row_count += 1
        commission = safe_float(row.get("commission"))
        commission_currency = str(row.get("currency") or "").strip().upper()
        if commission is None or not math.isfinite(commission) or commission <= 0:
            commission_unavailable += 1
        elif (
            recording_currency
            and commission_currency
            and commission_currency != recording_currency
        ):
            # No FX series is available in this workflow.  Combining a EUR
            # commission with a USD trade notional, for example, would produce
            # a numerically plausible but dimensionally invalid bps estimate.
            commission_currency_mismatches += 1
        else:
            if recording_currency and not commission_currency:
                commission_currency_assumed += 1
            group.commission += commission
            group.commission_rows += 1

        if side == "BUY":
            touch = _latest_quote(
                executed_at,
                ask_times,
                asks,
                max_age=normalized.calibration_max_quote_age_seconds,
            )
            slippage_bps = max(0.0, price / touch - 1.0) * 10_000.0 if touch else None
        else:
            touch = _latest_quote(
                executed_at,
                bid_times,
                bids,
                max_age=normalized.calibration_max_quote_age_seconds,
            )
            slippage_bps = max(0.0, touch / price - 1.0) * 10_000.0 if touch else None
        if slippage_bps is not None and math.isfinite(slippage_bps):
            group.quote_matched_notional += notional
            group.quote_weighted_slippage += notional * slippage_bps

    order_groups = sorted(groups.values(), key=lambda item: (item.side, item.cycle_id, item.identity))
    groups_by_cycle_side: dict[tuple[str, str], list[_OrderEvidence]] = {}
    for group in order_groups:
        groups_by_cycle_side.setdefault((group.cycle_id, group.side), []).append(group)

    # BouncyBot execution rows can retain the schema-default zero when the
    # aggregate commission becomes available only on the completed cycle row.
    # Prefer that authoritative side-level total when it is positive, and
    # allocate it across the usable broker-order groups by executed notional.
    # A protective SELL may be mirrored into the normal SELL history fields, so
    # use the greater side total rather than summing both columns.
    cycle_commission_groups_applied = 0
    for (cycle_id, side), side_groups in sorted(groups_by_cycle_side.items()):
        cycle = cycles_by_id.get(cycle_id)
        if cycle is None:
            continue
        if side == "BUY":
            aggregate_commission = max(
                0.0,
                safe_float(cycle.get("buy_commission")) or 0.0,
            )
        else:
            aggregate_commission = max(
                0.0,
                safe_float(cycle.get("sell_commission")) or 0.0,
                safe_float(cycle.get("protective_sell_commission")) or 0.0,
            )
        total_notional = sum(group.notional for group in side_groups)
        if aggregate_commission <= 0 or total_notional <= 0:
            continue
        for group in side_groups:
            group.commission = aggregate_commission * group.notional / total_notional
            group.commission_rows = max(1, group.commission_rows)
            cycle_commission_groups_applied += 1
    buy_groups = [group for group in order_groups if group.side == "BUY"]
    sell_groups = [group for group in order_groups if group.side == "SELL"]

    def evidence(groups_for_side: list[_OrderEvidence]) -> tuple[list[float], list[float], list[float]]:
        commission_values = [
            value
            for group in groups_for_side
            if (value := group.commission_bps) is not None
        ]
        slippage_values = [
            value
            for group in groups_for_side
            if (value := group.adverse_slippage_bps) is not None
        ]
        total_values = [
            value
            for group in groups_for_side
            if (value := group.total_adverse_cost_bps) is not None
        ]
        return commission_values, slippage_values, total_values

    buy_commission, buy_slippage, buy_total = evidence(buy_groups)
    sell_commission, sell_slippage, sell_total = evidence(sell_groups)

    def percentiles(values: Iterable[float]) -> tuple[float | None, float | None, float | None]:
        materialized = list(values)
        return (
            percentile(materialized, 0.50),
            percentile(materialized, 0.75),
            percentile(materialized, 0.90),
        )

    buy_commission_p50, buy_commission_p75, buy_commission_p90 = percentiles(
        buy_commission
    )
    sell_commission_p50, sell_commission_p75, sell_commission_p90 = percentiles(
        sell_commission
    )
    buy_slippage_p50, buy_slippage_p75, buy_slippage_p90 = percentiles(
        buy_slippage
    )
    sell_slippage_p50, sell_slippage_p75, sell_slippage_p90 = percentiles(
        sell_slippage
    )
    buy_total_p50, buy_total_p75, buy_total_p90 = percentiles(buy_total)
    sell_total_p50, sell_total_p75, sell_total_p90 = percentiles(sell_total)
    median_notional = percentile(buy_notional_by_cycle.values(), 0.50)

    warnings: list[str] = []
    configured_cost = normalized.execution_cost_bps_per_side

    def supported_cost(
        side: str,
        groups_for_side: list[_OrderEvidence],
        total_values: list[float],
        total_p75: float | None,
        slippage_values: list[float],
        slippage_p75: float | None,
        commission_values: list[float],
        commission_p75: float | None,
    ) -> tuple[float, list[tuple[str, float]]]:
        values_by_date: list[tuple[str, float]] = []
        for group in groups_for_side:
            value = group.total_adverse_cost_bps
            if value is None:
                value = group.adverse_slippage_bps
            if value is None:
                value = group.commission_bps
            if value is not None and group.execution_date:
                values_by_date.append((group.execution_date, value))

        result = configured_cost
        if not normalized.calibration_use_execution_cost:
            return result, values_by_date
        if len(total_values) >= normalized.calibration_min_samples and total_p75 is not None:
            result = max(result, total_p75)
        elif len(slippage_values) >= normalized.calibration_min_samples and slippage_p75 is not None:
            result = max(result, slippage_p75)
            warnings.append(
                f"{side} calibration had enough quote-matched slippage samples but too few compatible positive commission samples; that side uses slippage evidence only."
            )
        elif len(commission_values) >= normalized.calibration_min_samples and commission_p75 is not None:
            result = max(result, commission_p75)
            warnings.append(
                f"{side} calibration had enough commission samples but too few quote-matched orders; that side uses commission evidence only."
            )
        else:
            warnings.append(
                f"Too few usable {side} execution-cost samples were available; the configured reserve remains the floor."
            )
        return result, values_by_date

    effective_buy_cost, buy_cost_by_date = supported_cost(
        "BUY",
        buy_groups,
        buy_total,
        buy_total_p75,
        buy_slippage,
        buy_slippage_p75,
        buy_commission,
        buy_commission_p75,
    )
    effective_sell_cost, sell_cost_by_date = supported_cost(
        "SELL",
        sell_groups,
        sell_total,
        sell_total_p75,
        sell_slippage,
        sell_slippage_p75,
        sell_commission,
        sell_commission_p75,
    )
    effective_cost = max(effective_buy_cost, effective_sell_cost)

    effective_notional = normalized.assumed_trade_notional
    used_notional = False
    if normalized.calibration_use_trade_notional:
        if len(buy_notional_by_cycle) >= normalized.calibration_min_samples and median_notional:
            effective_notional = median_notional
            used_notional = True
        else:
            warnings.append(
                "Too few completed BUY-cycle notionals were available; the configured assumed notional was retained."
            )

    def canonical_date(value: str) -> str:
        text = str(value or "").strip()
        if len(text) == 8 and text.isdigit():
            return f"{text[:4]}-{text[4:6]}-{text[6:]}"
        try:
            return datetime.fromisoformat(text).date().isoformat()
        except ValueError:
            return text

    replay_dates = sorted(
        {
            canonical_date(period.session_date)
            for period in recording.periods
            if canonical_date(period.session_date)
        }
    )

    def cross_fitted_value(
        samples: list[tuple[str, float]],
        replay_date: str,
        default: float,
        probability: float,
    ) -> tuple[float, str, int]:
        normalized_samples = [
            (canonical_date(sample_date), value)
            for sample_date, value in samples
            if canonical_date(sample_date) and math.isfinite(value) and value >= 0
        ]
        prior = [value for sample_date, value in normalized_samples if sample_date < replay_date]
        if len(prior) >= normalized.calibration_min_samples:
            estimate = percentile(prior, probability)
            return max(default, estimate or default), "prior_only", len(prior)
        leave_date_out = [
            value
            for sample_date, value in normalized_samples
            if sample_date != replay_date
        ]
        if leave_date_out:
            estimate = percentile(leave_date_out, probability)
            weight = min(
                1.0,
                len(leave_date_out) / normalized.calibration_min_samples,
            )
            shrunk = default + weight * ((estimate or default) - default)
            return max(default, shrunk), "leave_date_out_shrunk", len(leave_date_out)
        return default, "configured_default", 0

    buy_notional_samples: list[tuple[str, float]] = []
    for group in buy_groups:
        if group.execution_date and group.cycle_id in buy_notional_by_cycle:
            buy_notional_samples.append(
                (group.execution_date, buy_notional_by_cycle[group.cycle_id])
            )
    buy_notional_samples = sorted(set(buy_notional_samples))

    date_specific_costs: list[dict[str, Any]] = []
    date_specific_notionals: list[dict[str, Any]] = []
    for replay_date in replay_dates:
        buy_value, buy_mode, buy_count = cross_fitted_value(
            buy_cost_by_date,
            replay_date,
            configured_cost,
            0.75,
        )
        sell_value, sell_mode, sell_count = cross_fitted_value(
            sell_cost_by_date,
            replay_date,
            configured_cost,
            0.75,
        )
        date_specific_costs.append(
            {
                "session_date": replay_date,
                "buy_cost_bps": round(buy_value, 6),
                "sell_cost_bps": round(sell_value, 6),
                "buy_mode": buy_mode,
                "sell_mode": sell_mode,
                "buy_samples": buy_count,
                "sell_samples": sell_count,
            }
        )
        notional_value, notional_mode, notional_count = cross_fitted_value(
            buy_notional_samples,
            replay_date,
            normalized.assumed_trade_notional,
            0.50,
        )
        date_specific_notionals.append(
            {
                "session_date": replay_date,
                "trade_notional": round(notional_value, 2),
                "mode": notional_mode,
                "samples": notional_count,
            }
        )
    if not rows:
        warnings.append(
            "The selected BouncyBot database contained no execution rows for the verified Market Replay instrument."
        )
    elif usable == 0:
        warnings.append(
            "Execution rows existed for the instrument, but none had a valid side, quantity, price, and timestamp."
        )
    if legacy_identity_cycles:
        warnings.append(
            f"{legacy_identity_cycles:,} matched cycle(s) lacked a complete conId/currency identity and were accepted by the available ticker identity."
        )
    if commission_currency_mismatches:
        warnings.append(
            f"{commission_currency_mismatches:,} execution row(s) had a commission currency different from the recording currency; those commissions were excluded because no FX conversion series is available."
        )
    if commission_currency_assumed:
        warnings.append(
            f"{commission_currency_assumed:,} positive commission row(s) did not identify a currency; their commission was conservatively assumed to use the recording currency."
        )
    if commission_unavailable:
        warnings.append(
            f"{commission_unavailable:,} execution row(s) had no positive usable row-level commission. A compatible positive completed-cycle total was substituted where available; otherwise those rows contributed no commission-cost evidence."
        )
    if cycle_commission_groups_applied:
        warnings.append(
            f"Positive completed-cycle commission totals supplied commission evidence for {cycle_commission_groups_applied:,} broker-order group(s) whose row-level values were missing or less authoritative."
        )

    used_cost = (
        effective_buy_cost > normalized.execution_cost_bps_per_side + 1e-12
        or effective_sell_cost > normalized.execution_cost_bps_per_side + 1e-12
        or any(
            row["buy_cost_bps"] > normalized.execution_cost_bps_per_side + 1e-12
            or row["sell_cost_bps"] > normalized.execution_cost_bps_per_side + 1e-12
            for row in date_specific_costs
        )
    )
    return ExecutionCalibration(
        enabled=True,
        applied=used_cost or used_notional,
        source_fingerprint=_source_fingerprint(components),
        source_components=components,
        source_database_sha256=database_sha256,
        ticker=recording.symbol,
        con_id=recording.con_id,
        currency=_recording_currency(recording),
        identity_selection_mode=identity_selection_mode,
        matched_cycles=len(cycle_ids),
        legacy_identity_cycles=legacy_identity_cycles,
        execution_rows_considered=len(rows),
        execution_rows_usable=usable,
        duplicate_execution_rows=duplicates,
        commission_currency_mismatch_rows=commission_currency_mismatches,
        commission_currency_assumed_rows=commission_currency_assumed,
        commission_unavailable_rows=commission_unavailable,
        cycle_commission_groups_applied=cycle_commission_groups_applied,
        buy_execution_rows=buy_rows,
        sell_execution_rows=sell_rows,
        buy_order_samples=len(buy_groups),
        sell_order_samples=len(sell_groups),
        buy_quote_matched_orders=len(buy_slippage),
        sell_quote_matched_orders=len(sell_slippage),
        buy_total_cost_samples=len(buy_total),
        sell_total_cost_samples=len(sell_total),
        trade_notional_samples=len(buy_notional_by_cycle),
        buy_commission_bps_p50=buy_commission_p50,
        buy_commission_bps_p75=buy_commission_p75,
        buy_commission_bps_p90=buy_commission_p90,
        sell_commission_bps_p50=sell_commission_p50,
        sell_commission_bps_p75=sell_commission_p75,
        sell_commission_bps_p90=sell_commission_p90,
        buy_adverse_slippage_bps_p50=buy_slippage_p50,
        buy_adverse_slippage_bps_p75=buy_slippage_p75,
        buy_adverse_slippage_bps_p90=buy_slippage_p90,
        sell_adverse_slippage_bps_p50=sell_slippage_p50,
        sell_adverse_slippage_bps_p75=sell_slippage_p75,
        sell_adverse_slippage_bps_p90=sell_slippage_p90,
        buy_total_adverse_cost_bps_p50=buy_total_p50,
        buy_total_adverse_cost_bps_p75=buy_total_p75,
        buy_total_adverse_cost_bps_p90=buy_total_p90,
        sell_total_adverse_cost_bps_p50=sell_total_p50,
        sell_total_adverse_cost_bps_p75=sell_total_p75,
        sell_total_adverse_cost_bps_p90=sell_total_p90,
        median_buy_notional=median_notional,
        configured_execution_cost_bps_per_side=normalized.execution_cost_bps_per_side,
        effective_execution_cost_bps_per_side=round(effective_cost, 6),
        effective_buy_execution_cost_bps_per_side=round(effective_buy_cost, 6),
        effective_sell_execution_cost_bps_per_side=round(effective_sell_cost, 6),
        configured_trade_notional=normalized.assumed_trade_notional,
        effective_trade_notional=round(effective_notional, 2),
        maximum_quote_age_seconds=normalized.calibration_max_quote_age_seconds,
        minimum_samples=normalized.calibration_min_samples,
        used_execution_cost_calibration=used_cost,
        used_trade_notional_calibration=used_notional,
        date_specific_execution_costs=tuple(date_specific_costs),
        date_specific_trade_notionals=tuple(date_specific_notionals),
        warnings=tuple(sorted(set(warnings))),
    )


def load_execution_calibration(
    recording: IbrecRecording,
    config: MarketReplayConfig,
) -> ExecutionCalibration:
    """Snapshot a stopped BouncyBot database and derive ticker-specific evidence."""

    normalized = config.normalized()
    if normalized.calibration_source_dir is None:
        return ExecutionCalibration(
            enabled=False,
            ticker=recording.symbol,
            con_id=recording.con_id,
            currency=_recording_currency(recording),
            configured_execution_cost_bps_per_side=normalized.execution_cost_bps_per_side,
            effective_execution_cost_bps_per_side=normalized.execution_cost_bps_per_side,
            effective_buy_execution_cost_bps_per_side=normalized.execution_cost_bps_per_side,
            effective_sell_execution_cost_bps_per_side=normalized.execution_cost_bps_per_side,
            configured_trade_notional=normalized.assumed_trade_notional,
            effective_trade_notional=normalized.assumed_trade_notional,
            maximum_quote_age_seconds=normalized.calibration_max_quote_age_seconds,
            minimum_samples=normalized.calibration_min_samples,
        )

    paths = source_paths(normalized.calibration_source_dir)
    # Calibration consumes only bot_state.sqlite. Keep the full database,
    # containment, and live-bot lock checks without treating an absent
    # debug_captures folder as a defect.
    validate_database_source(paths)
    with BotFolderLease(paths.bot_lock):
        before = source_state(paths.database)
        components = _source_components(before)
        database_digest = _sha256(paths.database)
        with readonly_database_snapshot(paths.database) as snapshot:
            dataset = SnapshotDatabase(snapshot).load()
        after = source_state(paths.database)
        if before != after:
            raise SourceSafetyError(
                "The calibration SQLite source changed while the read-only snapshot was being created."
            )
    return _derive_calibration(
        dataset,
        recording,
        normalized,
        components=components,
        database_sha256=database_digest,
    )
