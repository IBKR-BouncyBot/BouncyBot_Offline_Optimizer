"""Data models for standalone Market Replay recording optimization."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .utils import canonical_session_date, finite_float, finite_int


def _positive_finite(value: float | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _strict_positive_int(value: Any, *, name: str, minimum: int) -> int:
    number = finite_int(value)
    if number is None or number < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum:,}.")
    return number


def _override_session_date(value: Any, *, error: str) -> str:
    """Return one validated canonical date for date-specific assumptions."""

    if not isinstance(value, str):
        raise ValueError(error)
    canonical = canonical_session_date(value.strip())
    try:
        valid = datetime.fromisoformat(canonical).date().isoformat() == canonical
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(error)
    return canonical


def _strict_bool(value: Any, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean.")
    return value


@dataclass(slots=True)
class MarketReplayConfig:
    """Configuration for one independent Market Replay analysis.

    ``recording_path`` remains the first field for backwards compatibility
    with the v1.4/v1.5 public API.  It accepts either one path or an immutable
    tuple of paths.  The normalized object always stores an immutable tuple so
    downstream code cannot accidentally mutate the input set while an analysis
    is running.
    """

    recording_path: Path | tuple[Path, ...]
    output_root: Path
    max_rows: int = 2_000_000
    max_input_bytes: int = 4 * 1024 * 1024 * 1024
    max_zip_uncompressed_bytes: int = 8 * 1024 * 1024 * 1024
    max_recordings: int = 64
    min_atr_pct: float = 0.10
    max_atr_pct: float = 20.00
    assumed_trade_notional: float = 10_000.0
    execution_cost_bps_per_side: float = 1.0
    buy_execution_cost_bps_per_side: float | None = None
    sell_execution_cost_bps_per_side: float | None = None
    execution_cost_overrides: tuple[tuple[str, float, float], ...] = ()
    trade_notional_overrides: tuple[tuple[str, float], ...] = ()
    turnover_penalty_bps_per_completed_trade: float = 0.25
    execution_quote_max_age_seconds: float | None = None
    entry_open_delay_seconds: int = 5 * 60
    entry_cutoff_seconds: int = 15 * 60
    buy_trail_cancel_seconds: int = 5 * 60
    session_boundary_tolerance_seconds: float = 120.0
    max_market_event_gap_seconds: float = 60.0
    min_last_event_minute_coverage_pct: float = 80.0
    max_last_event_gap_p95_seconds: float = 60.0
    min_touch_liquidity_coverage_pct: float = 80.0
    continuous_overnight_replay: bool = True
    calibration_source_dir: Path | None = None
    calibration_max_quote_age_seconds: float = 5.0
    calibration_min_samples: int = 5
    calibration_use_execution_cost: bool = True
    calibration_use_trade_notional: bool = True

    @property
    def recording_paths(self) -> tuple[Path, ...]:
        value = self.recording_path
        if isinstance(value, Path):
            return (value,)
        return tuple(Path(item) for item in value)

    @property
    def single_recording_path(self) -> Path:
        paths = self.recording_paths
        if len(paths) != 1:
            raise ValueError("This operation requires exactly one .ibrec recording.")
        return paths[0]

    def normalized(self) -> "MarketReplayConfig":
        # Preserve each final path component so the importer can reject a
        # symlink instead of silently resolving it to a different file.
        paths = tuple(
            sorted(
                (Path(item).expanduser().absolute() for item in self.recording_paths),
                key=lambda item: str(item).casefold(),
            )
        )
        max_recordings = _strict_positive_int(
            self.max_recordings,
            name="max_recordings",
            minimum=1,
        )
        if not paths:
            raise ValueError("Select at least one Market Replay .ibrec recording.")
        if len(paths) > max_recordings:
            raise ValueError(
                f"At most {max_recordings:,} Market Replay recordings can be analyzed together."
            )
        if len(set(paths)) != len(paths):
            raise ValueError("The same Market Replay path was selected more than once.")
        output = Path(self.output_root).expanduser().resolve()
        for path in paths:
            if path.suffix.lower() != ".ibrec":
                raise ValueError("Every Market Replay input must use the .ibrec extension.")
            if output == path or path in output.parents:
                raise ValueError(
                    "Market Replay output cannot replace or be nested under an input file."
                )
        max_rows = _strict_positive_int(self.max_rows, name="max_rows", minimum=100)
        max_input_bytes = _strict_positive_int(
            self.max_input_bytes,
            name="max_input_bytes",
            minimum=1_000_000,
        )
        max_zip_uncompressed_bytes = _strict_positive_int(
            self.max_zip_uncompressed_bytes,
            name="max_zip_uncompressed_bytes",
            minimum=1_000_000,
        )

        def finite_nonnegative(value: Any, *, name: str) -> float:
            number = finite_float(value)
            if number is None or number < 0:
                raise ValueError(f"{name} must be a finite non-negative number.")
            return number

        minimum = finite_nonnegative(self.min_atr_pct, name="min_atr_pct")
        maximum = finite_nonnegative(self.max_atr_pct, name="max_atr_pct")
        # BouncyBot itself applies an absolute 0.01% lower bound and a 99.99%
        # upper bound before rounding strategy percentages to two decimals.
        # Accepting values outside that actionable range would let the
        # optimizer recommend a profile the live application cannot reproduce.
        if minimum < 0.01:
            raise ValueError("min_atr_pct must be at least 0.01%.")
        if maximum <= minimum or maximum > 99.99:
            raise ValueError(
                "max_atr_pct must be at most 99.99% and above min_atr_pct."
            )

        def finite_percentage(value: Any, *, name: str) -> float:
            number = finite_nonnegative(value, name=name)
            if number > 100:
                raise ValueError(f"{name} must be between 0 and 100.")
            return number

        # Bounds are enforced on the exact rounded values that ``normalized``
        # stores.  A raw value can satisfy a bound and still round across it
        # (0.004 rounds to a zero notional; 9,999.9999996 rounds to 10,000.0
        # bps), which previously produced a normalized configuration that
        # failed its own re-normalization.
        assumed_notional = round(
            finite_nonnegative(
                self.assumed_trade_notional,
                name="assumed_trade_notional",
            ),
            2,
        )
        if assumed_notional < 0.01:
            raise ValueError(
                "assumed_trade_notional must be at least 0.01 after rounding to cents."
            )
        execution_cost = round(
            finite_nonnegative(
                self.execution_cost_bps_per_side,
                name="execution_cost_bps_per_side",
            ),
            4,
        )
        if execution_cost >= 10_000:
            raise ValueError(
                "execution_cost_bps_per_side must be below 10,000 bps so modeled execution prices remain positive."
            )
        buy_execution_cost = (
            execution_cost
            if self.buy_execution_cost_bps_per_side is None
            else round(
                finite_nonnegative(
                    self.buy_execution_cost_bps_per_side,
                    name="buy_execution_cost_bps_per_side",
                ),
                6,
            )
        )
        sell_execution_cost = (
            execution_cost
            if self.sell_execution_cost_bps_per_side is None
            else round(
                finite_nonnegative(
                    self.sell_execution_cost_bps_per_side,
                    name="sell_execution_cost_bps_per_side",
                ),
                6,
            )
        )
        if buy_execution_cost >= 10_000 or sell_execution_cost >= 10_000:
            raise ValueError(
                "BUY and SELL execution-cost reserves must remain below 10,000 bps."
            )

        cost_overrides: list[tuple[str, float, float]] = []
        seen_cost_dates: set[str] = set()
        for raw_date, raw_buy, raw_sell in self.execution_cost_overrides:
            error = (
                "execution_cost_overrides require unique non-empty session dates "
                "in a valid format."
            )
            date_key = str(raw_date).strip()
            # Uniqueness is enforced on the canonical ISO form because the
            # replay lookup canonicalizes keys: ``20260721`` and
            # ``2026-07-21`` must be rejected as duplicates, not allowed to
            # silently overwrite one another.
            canonical_key = _override_session_date(raw_date, error=error)
            if canonical_key in seen_cost_dates:
                raise ValueError(error)
            buy_value = finite_nonnegative(
                raw_buy,
                name=f"execution_cost_overrides[{date_key}].buy",
            )
            sell_value = finite_nonnegative(
                raw_sell,
                name=f"execution_cost_overrides[{date_key}].sell",
            )
            rounded_buy = round(buy_value, 6)
            rounded_sell = round(sell_value, 6)
            if rounded_buy >= 10_000 or rounded_sell >= 10_000:
                raise ValueError("Date-specific execution reserves must be below 10,000 bps.")
            seen_cost_dates.add(canonical_key)
            # Store the canonical key as well as comparing with it. Equivalent
            # spellings must normalize to one identical configuration so report
            # identity and bytes do not depend on whether a caller supplied
            # YYYYMMDD or ISO YYYY-MM-DD.
            cost_overrides.append((canonical_key, rounded_buy, rounded_sell))

        notional_overrides: list[tuple[str, float]] = []
        seen_notional_dates: set[str] = set()
        for raw_date, raw_value in self.trade_notional_overrides:
            error = (
                "trade_notional_overrides require unique dates in a valid format "
                "and notionals of at least 0.01."
            )
            date_key = str(raw_date).strip()
            canonical_key = _override_session_date(raw_date, error=error)
            value = round(
                finite_nonnegative(
                    raw_value,
                    name=f"trade_notional_overrides[{date_key}]",
                ),
                2,
            )
            if canonical_key in seen_notional_dates or value < 0.01:
                raise ValueError(error)
            seen_notional_dates.add(canonical_key)
            notional_overrides.append((canonical_key, value))
        turnover_penalty = finite_nonnegative(
            self.turnover_penalty_bps_per_completed_trade,
            name="turnover_penalty_bps_per_completed_trade",
        )
        execution_quote_age = (
            None
            if self.execution_quote_max_age_seconds is None
            else finite_nonnegative(
                self.execution_quote_max_age_seconds,
                name="execution_quote_max_age_seconds",
            )
        )
        if execution_quote_age is not None and execution_quote_age <= 0:
            raise ValueError(
                "execution_quote_max_age_seconds must be positive when configured."
            )
        entry_open_delay = _strict_positive_int(
            self.entry_open_delay_seconds,
            name="entry_open_delay_seconds",
            minimum=1,
        )
        entry_cutoff = _strict_positive_int(
            self.entry_cutoff_seconds,
            name="entry_cutoff_seconds",
            minimum=1,
        )
        buy_cancel = _strict_positive_int(
            self.buy_trail_cancel_seconds,
            name="buy_trail_cancel_seconds",
            minimum=1,
        )
        boundary_tolerance = finite_nonnegative(
            self.session_boundary_tolerance_seconds,
            name="session_boundary_tolerance_seconds",
        )
        max_event_gap = finite_nonnegative(
            self.max_market_event_gap_seconds,
            name="max_market_event_gap_seconds",
        )
        if max_event_gap <= 0:
            raise ValueError("max_market_event_gap_seconds must be greater than zero.")
        last_minute_coverage = finite_percentage(
            self.min_last_event_minute_coverage_pct,
            name="min_last_event_minute_coverage_pct",
        )
        last_gap_p95 = finite_nonnegative(
            self.max_last_event_gap_p95_seconds,
            name="max_last_event_gap_p95_seconds",
        )
        if last_gap_p95 <= 0:
            raise ValueError("max_last_event_gap_p95_seconds must be greater than zero.")
        touch_coverage = finite_percentage(
            self.min_touch_liquidity_coverage_pct,
            name="min_touch_liquidity_coverage_pct",
        )
        quote_age = finite_nonnegative(
            self.calibration_max_quote_age_seconds,
            name="calibration_max_quote_age_seconds",
        )
        if quote_age <= 0:
            raise ValueError(
                "calibration_max_quote_age_seconds must be greater than zero."
            )
        calibration_min_samples = _strict_positive_int(
            self.calibration_min_samples,
            name="calibration_min_samples",
            minimum=1,
        )
        calibration_source = (
            Path(self.calibration_source_dir).expanduser().resolve()
            if self.calibration_source_dir is not None
            else None
        )
        if calibration_source is not None:
            if not calibration_source.exists() or not calibration_source.is_dir():
                raise ValueError(
                    "calibration_source_dir must be an existing BouncyBot data directory."
                )
            if output == calibration_source or calibration_source in output.parents:
                raise ValueError(
                    "Market Replay output cannot replace or be nested under the calibration source directory."
                )
        return MarketReplayConfig(
            recording_path=paths,
            output_root=output,
            max_rows=max_rows,
            max_input_bytes=max_input_bytes,
            max_zip_uncompressed_bytes=max_zip_uncompressed_bytes,
            max_recordings=max_recordings,
            min_atr_pct=round(minimum, 4),
            max_atr_pct=round(maximum, 4),
            assumed_trade_notional=round(assumed_notional, 2),
            execution_cost_bps_per_side=round(execution_cost, 4),
            buy_execution_cost_bps_per_side=round(buy_execution_cost, 6),
            sell_execution_cost_bps_per_side=round(sell_execution_cost, 6),
            execution_cost_overrides=tuple(sorted(cost_overrides)),
            trade_notional_overrides=tuple(sorted(notional_overrides)),
            turnover_penalty_bps_per_completed_trade=round(turnover_penalty, 4),
            execution_quote_max_age_seconds=(
                round(execution_quote_age, 3)
                if execution_quote_age is not None
                else None
            ),
            entry_open_delay_seconds=entry_open_delay,
            entry_cutoff_seconds=entry_cutoff,
            buy_trail_cancel_seconds=buy_cancel,
            session_boundary_tolerance_seconds=round(boundary_tolerance, 3),
            max_market_event_gap_seconds=round(max_event_gap, 3),
            min_last_event_minute_coverage_pct=round(last_minute_coverage, 3),
            max_last_event_gap_p95_seconds=round(last_gap_p95, 3),
            min_touch_liquidity_coverage_pct=round(touch_coverage, 3),
            continuous_overnight_replay=_strict_bool(
                self.continuous_overnight_replay,
                name="continuous_overnight_replay",
            ),
            calibration_source_dir=calibration_source,
            calibration_max_quote_age_seconds=round(quote_age, 3),
            calibration_min_samples=calibration_min_samples,
            calibration_use_execution_cost=_strict_bool(
                self.calibration_use_execution_cost,
                name="calibration_use_execution_cost",
            ),
            calibration_use_trade_notional=_strict_bool(
                self.calibration_use_trade_notional,
                name="calibration_use_trade_notional",
            ),
        )


@dataclass(slots=True, frozen=True)
class IbrecTick:
    sequence: int
    captured_at_utc: str
    timestamp: float
    elapsed_ns: int
    symbol: str
    con_id: int
    source_time_utc: str
    bid: float | None
    bid_size: float | None
    ask: float | None
    ask_size: float | None
    last: float | None
    last_size: float | None
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    mark_price: float | None
    market_data_type: int
    changed_fields: tuple[str, ...]
    full_snapshot: bool
    rth_period_id: int | None = None

    def selected_price(self) -> float | None:
        """Approximate ``ib_async.Ticker.marketPrice`` from stored Level 1 state.

        BouncyBot prefers TWS/``ib_async`` ``marketPrice`` before the explicit
        midpoint.  ``Ticker.marketPrice`` uses Last when it is inside a valid
        spread and otherwise falls back to the midpoint.  A lone bid or ask is
        not a strategy price.  Mark, Last, and Close are deterministic fallbacks
        when a complete quote is unavailable.
        """

        bid = self.valid_bid()
        ask = self.valid_ask()
        last = _positive_finite(self.last)
        if bid is not None and ask is not None:
            if last is not None and bid <= last <= ask:
                return last
            return (bid + ask) / 2.0
        for value in (self.mark_price, self.last, self.close):
            if value is not None and value > 0 and math.isfinite(value):
                return value
        return None

    def has_last_event(self) -> bool:
        """Return whether this row can represent a new Last-trigger event."""

        return self.full_snapshot or "last" in self.changed_fields

    def valid_bid(self) -> float | None:
        """Return a usable bid, excluding a crossed two-sided quote."""

        if self.bid is None or not math.isfinite(self.bid) or self.bid <= 0:
            return None
        if (
            self.ask is not None
            and math.isfinite(self.ask)
            and self.ask > 0
            and self.ask < self.bid
        ):
            return None
        return self.bid

    def valid_ask(self) -> float | None:
        """Return a usable ask, excluding a crossed two-sided quote."""

        if self.ask is None or not math.isfinite(self.ask) or self.ask <= 0:
            return None
        if (
            self.bid is not None
            and math.isfinite(self.bid)
            and self.bid > 0
            and self.ask < self.bid
        ):
            return None
        return self.ask

    def executable_price(self, side: str, fallback: float | None = None) -> float | None:
        """Return the recorded executable touch for a modeled market order.

        ``fallback`` is retained in the signature for compatibility with older
        callers but is deliberately ignored.  Last, mark, close, and the
        opposite quote cannot prove that a market order was executable.
        """

        _ = fallback
        return self.valid_ask() if side.upper() == "BUY" else self.valid_bid()

    def long_mark_price(self) -> float | None:
        """Return a conservative mark for an existing long position."""

        return self.valid_bid()


@dataclass(slots=True, frozen=True)
class IbrecPeriod:
    period_id: int
    session_date: str
    schedule_open_utc: str
    schedule_close_utc: str
    open_timestamp: float
    close_timestamp: float
    observed_start_utc: str
    observed_end_utc: str
    observed_start_timestamp: float
    observed_end_timestamp: float
    status: str
    close_reason: str
    tick_count: int
    source_recording_sha256: str = ""
    primary_eligible: bool = True
    source_finalized: bool = True
    coverage_pct: float = 100.0
    start_lag_seconds: float = 0.0
    end_lead_seconds: float = 0.0
    maximum_event_gap_seconds: float = 0.0
    connectivity_event_count: int = 0
    last_event_count: int = 0
    last_event_minute_coverage_pct: float = 100.0
    last_event_gap_p95_seconds: float | None = None
    quality_exclusion_reasons: tuple[str, ...] = ()


@dataclass(slots=True)
class IbrecRecording:
    path: Path
    sha256: str
    size_bytes: int
    input_components: list[dict[str, Any]]
    container_format: str
    format_version: int
    manifest: dict[str, Any]
    contract: dict[str, Any]
    ticks: list[IbrecTick]
    periods: list[IbrecPeriod]
    issues: list[str]
    feed_counts: dict[str, int]
    raw_row_count: int
    retained_row_count: int
    data_start_utc: str
    data_end_utc: str
    excluded_sessions: list[dict[str, Any]] = field(default_factory=list)
    quality_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def symbol(self) -> str:
        return str(self.contract.get("symbol") or "UNKNOWN").upper().strip() or "UNKNOWN"

    @property
    def con_id(self) -> int:
        value = finite_int(
            self.contract.get("con_id", self.contract.get("conId", 0))
        )
        return max(0, value or 0)

    @property
    def min_tick(self) -> float:
        value = finite_float(
            self.contract.get("min_tick", self.contract.get("minTick"))
        )
        return value if value is not None and value > 0 else 0.01

    @property
    def logical_file_name(self) -> str:
        """Return a path-independent label for deterministic reports."""

        if self.input_components:
            name = str(self.input_components[0].get("name") or "").strip()
            if name:
                return name
        return "recording.ibrec"

    @property
    def input_recording_count(self) -> int:
        return sum(
            1
            for component in self.input_components
            if str(component.get("role") or "") == "recording"
        ) or 1

    @property
    def format_versions(self) -> tuple[int, ...]:
        values: set[int] = set()
        for component in self.input_components:
            version = finite_int(component.get("format_version"))
            if version in {2, 3}:
                values.add(version)
        if not values and self.format_version in {2, 3}:
            values.add(self.format_version)
        return tuple(sorted(values))

    @property
    def format_label(self) -> str:
        versions = self.format_versions
        if versions:
            return "/".join(f"v{value}" for value in versions)
        return "unknown"

    @property
    def is_synthetic(self) -> bool:
        """Return whether manifest provenance explicitly identifies synthetic data."""

        source = self.manifest.get("source")
        source_values = source.values() if isinstance(source, dict) else ()
        values = [self.manifest.get("notes"), *source_values]
        return any("synthetic" in str(value or "").lower() for value in values)


@dataclass(slots=True, frozen=True)
class AtrProfile:
    period: int
    bar_seconds: int
    initial_drop_multiplier: float
    buy_rebound_multiplier: float
    minimum_profit_multiplier: float
    sell_trail_multiplier: float
    min_atr_pct: float = 0.10
    max_atr_pct: float = 20.00

    def key(self) -> str:
        return (
            f"p{self.period}-b{self.bar_seconds}-d{self.initial_drop_multiplier:.2f}"
            f"-buy{self.buy_rebound_multiplier:.2f}-profit{self.minimum_profit_multiplier:.2f}"
            f"-sell{self.sell_trail_multiplier:.2f}-min{self.min_atr_pct:.2f}"
            f"-max{self.max_atr_pct:.2f}"
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MarketReplayTrade:
    session_date: str
    cycle_number: int
    buy_time_utc: str
    buy_price: float
    sell_time_utc: str = ""
    sell_price: float | None = None
    return_bps: float | None = None
    buy_trigger_pct: float | None = None
    minimum_profit_pct: float | None = None
    sell_trigger_pct: float | None = None
    open_at_end: bool = False
    assumed_quantity: int = 0
    buy_touch_size: float | None = None
    sell_touch_size: float | None = None
    buy_touch_sufficient: bool | None = None
    sell_touch_sufficient: bool | None = None
    gross_return_bps: float | None = None
    net_return_bps: float | None = None
    sell_session_date: str = ""
    overnight_sessions_held: int = 0


@dataclass(slots=True)
class MarketReplaySessionResult:
    session_date: str
    period_id: int
    scheduled_open_utc: str
    scheduled_close_utc: str
    observed_start_utc: str
    observed_end_utc: str
    ticks: int
    trades: int
    completed_trades: int
    no_trade: bool
    open_position: bool
    realized_return_bps: float
    marked_return_bps: float
    conservative_return_bps: float
    max_drawdown_bps: float
    session_max_drawdown_bps: float = 0.0
    chain_max_drawdown_bps: float = 0.0
    first_entry_utc: str = ""
    last_exit_utc: str = ""
    open_entry_setup: bool = False
    right_censored: bool = False
    unmarked_open_position: bool = False
    primary_eligible: bool = True
    source_finalized: bool = True
    coverage_pct: float = 0.0
    start_lag_seconds: float = 0.0
    end_lead_seconds: float = 0.0
    maximum_event_gap_seconds: float = 0.0
    connectivity_event_count: int = 0
    last_event_count: int = 0
    last_event_minute_coverage_pct: float = 0.0
    last_event_gap_p95_seconds: float | None = None
    touch_liquidity_checks: int = 0
    touch_liquidity_sufficient_checks: int = 0
    touch_liquidity_coverage_pct: float | None = None
    assumed_trade_notional: float = 0.0
    execution_cost_bps_per_side: float = 0.0
    buy_execution_cost_bps_per_side: float = 0.0
    sell_execution_cost_bps_per_side: float = 0.0
    total_execution_cost_bps: float = 0.0
    clamp_min_count: int = 0
    clamp_max_count: int = 0
    clamp_raw_count: int = 0
    clamp_zero_count: int = 0
    clamp_total_count: int = 0
    clamp_min_rate_pct: float = 0.0
    clamp_max_rate_pct: float = 0.0
    clamp_component_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    clamp_component_rates_pct: dict[str, dict[str, float]] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    continuity_chain_id: int = 0
    continuity_broken_before: bool = False
    continuity_break_reason: str = ""
    carried_position_in: bool = False
    carried_position_out: bool = False
    carried_sell_trail_in: bool = False
    carried_sell_trail_out: bool = False
    terminal_open_position: bool = False
    session_start_equity: float = 1.0
    session_end_equity: float = 1.0
    cumulative_end_equity: float = 1.0
    overnight_gap_return_bps: float | None = None


@dataclass(slots=True)
class MarketReplayCandidateSummary:
    profile: AtrProfile
    score: float
    sessions: int
    completed_sessions: int
    sessions_with_trades: int
    completed_trades: int
    open_position_sessions: int
    no_trade_sessions: int
    median_return_bps: float
    mean_return_bps: float
    worst_return_bps: float
    maximum_drawdown_bps: float
    open_position_rate_pct: float
    no_trade_rate_pct: float
    right_censored_sessions: int = 0
    right_censored_rate_pct: float = 0.0
    unmarked_open_position_sessions: int = 0
    unmarked_open_position_rate_pct: float = 0.0
    stable_region_id: str = ""
    stable_region_size: int = 0
    stable_region_center: bool = False
    near_best: bool = False
    evidence_stable: bool = False
    instability_reasons: list[str] = field(default_factory=list)
    robustness_evaluated: bool = False
    robustness_passed: bool = False
    control_score_delta: float | None = None
    paired_trading_days: int = 0
    bootstrap_unit_type: str = "trading_day"
    bootstrap_independent_units: int = 0
    bootstrap_replicates: int = 0
    bootstrap_ci80_low: float | None = None
    bootstrap_ci80_high: float | None = None
    bootstrap_ci95_low: float | None = None
    bootstrap_ci95_high: float | None = None
    bootstrap_probability_positive_pct: float | None = None
    leave_one_day_out_estimates: int = 0
    leave_one_day_out_min_delta: float | None = None
    leave_one_day_out_median_delta: float | None = None
    leave_one_day_out_max_delta: float | None = None
    leave_one_day_out_positive_pct: float | None = None
    leave_one_day_out_sign_reversals: int = 0
    leave_one_day_out_most_influential_day: str = ""
    leave_one_day_out_largest_change: float | None = None
    leave_one_day_out_selection_runs: int = 0
    leave_one_day_out_exact_profile_selections: int = 0
    leave_one_day_out_exact_profile_selection_pct: float | None = None
    leave_one_day_out_same_window_selections: int = 0
    leave_one_day_out_same_window_selection_pct: float | None = None
    leave_one_day_out_selection_mode: str = ""
    paired_median_return_delta_bps: float | None = None
    paired_positive_day_pct: float | None = None
    control_trade_day_retention_pct: float | None = None
    maximum_drawdown_delta_bps: float | None = None
    worst_return_delta_bps: float | None = None
    atr_phase_cases: int = 0
    atr_phase_min_score_delta: float | None = None
    atr_phase_adverse_score_delta: float | None = None
    average_completed_trades_per_session: float = 0.0
    turnover_penalty_points: float = 0.0
    total_execution_cost_bps: float = 0.0
    touch_liquidity_checks: int = 0
    touch_liquidity_sufficient_checks: int = 0
    touch_liquidity_coverage_pct: float | None = None
    clamp_min_count: int = 0
    clamp_max_count: int = 0
    clamp_raw_count: int = 0
    clamp_zero_count: int = 0
    clamp_total_count: int = 0
    clamp_min_rate_pct: float = 0.0
    clamp_max_rate_pct: float = 0.0
    clamp_component_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    clamp_component_rates_pct: dict[str, dict[str, float]] = field(default_factory=dict)
    primary_eligible_sessions: int = 0
    excluded_quality_sessions: int = 0
    moving_block_replicates: int = 0
    moving_block_length: int = 0
    moving_block_ci80_low: float | None = None
    moving_block_ci80_high: float | None = None
    moving_block_probability_positive_pct: float | None = None
    selection_bootstrap_replicates: int = 0
    selection_bootstrap_oob_evaluations: int = 0
    selection_bootstrap_probability_positive_pct: float | None = None
    selection_bootstrap_median_oob_delta: float | None = None
    selection_bootstrap_control_selection_pct: float | None = None
    walk_forward_folds: int = 0
    walk_forward_positive_folds: int = 0
    walk_forward_probability_positive_pct: float | None = None
    walk_forward_median_delta: float | None = None
    walk_forward_worst_delta: float | None = None
    walk_forward_same_window_pct: float | None = None
    score_policy_results: list[dict[str, Any]] = field(default_factory=list)
    score_policy_all_positive: bool = False
    pareto_frontier: bool = False
    pareto_dominated_by: list[str] = field(default_factory=list)
    boundary_dimensions: list[str] = field(default_factory=list)
    boundary_extension_attempted: bool = False
    boundary_resolved: bool = False
    assumption_stress_results: list[dict[str, Any]] = field(default_factory=list)
    assumption_stress_all_positive: bool = False
    recommendation_gates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["profile_key"] = self.profile.key()
        return value


@dataclass(slots=True)
class MarketReplayAnalysisResult:
    run_id: str
    analysis_id: str
    output_dir: Path
    generated_at_utc: str
    recording: IbrecRecording
    recommendation: MarketReplayCandidateSummary
    recommendation_reason: str
    candidates: list[MarketReplayCandidateSummary]
    recommended_sessions: list[MarketReplaySessionResult]
    recommended_trades: list[MarketReplayTrade]
    control: MarketReplayCandidateSummary
    control_sessions: list[MarketReplaySessionResult]
    global_issues: list[str]
    search_contract: dict[str, Any]
    window_search: list[dict[str, Any]] = field(default_factory=list)
    robustness_evidence: list[dict[str, Any]] = field(default_factory=list)
    recommendation_leave_one_day_out: list[dict[str, Any]] = field(default_factory=list)
    session_quality: list[dict[str, Any]] = field(default_factory=list)
    execution_calibration: dict[str, Any] = field(default_factory=dict)
    continuity_evidence: list[dict[str, Any]] = field(default_factory=list)
    continuity_block_evidence: list[dict[str, Any]] = field(default_factory=list)
    score_policy_evidence: list[dict[str, Any]] = field(default_factory=list)
    moving_block_evidence: list[dict[str, Any]] = field(default_factory=list)
    selection_bootstrap_evidence: list[dict[str, Any]] = field(default_factory=list)
    walk_forward_evidence: list[dict[str, Any]] = field(default_factory=list)
    pareto_evidence: list[dict[str, Any]] = field(default_factory=list)
    boundary_evidence: list[dict[str, Any]] = field(default_factory=list)
    assumption_stress_evidence: list[dict[str, Any]] = field(default_factory=list)
    recommendation_gates: list[dict[str, Any]] = field(default_factory=list)
    exploratory_only: bool = False
    files_written: list[Path] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "analysis_id": self.analysis_id,
            "output_directory_name": self.output_dir.name,
            "generated_at_utc": self.generated_at_utc,
            "recording": {
                "file_name": self.recording.logical_file_name,
                "sha256": self.recording.sha256,
                "size_bytes": self.recording.size_bytes,
                "input_components": self.recording.input_components,
                "container_format": self.recording.container_format,
                "format_version": self.recording.format_version,
                "format_versions": list(self.recording.format_versions),
                "format_label": self.recording.format_label,
                "input_recording_count": self.recording.input_recording_count,
                "symbol": self.recording.symbol,
                "con_id": self.recording.con_id,
                "manifest": self.recording.manifest,
                "contract": self.recording.contract,
                "periods": [asdict(period) for period in self.recording.periods],
                "feed_counts": self.recording.feed_counts,
                "raw_row_count": self.recording.raw_row_count,
                "retained_row_count": self.recording.retained_row_count,
                "data_start_utc": self.recording.data_start_utc,
                "data_end_utc": self.recording.data_end_utc,
                "issues": self.recording.issues,
                "excluded_sessions": self.recording.excluded_sessions,
            },
            "recommendation": self.recommendation.to_dict(),
            "recommendation_reason": self.recommendation_reason,
            "control": self.control.to_dict(),
            "candidate_results": [candidate.to_dict() for candidate in self.candidates],
            "recommended_session_results": [asdict(item) for item in self.recommended_sessions],
            "recommended_simulated_trades": [asdict(item) for item in self.recommended_trades],
            "control_session_results": [asdict(item) for item in self.control_sessions],
            "global_issues": self.global_issues,
            "search_contract": self.search_contract,
            "window_search": self.window_search,
            "robustness_evidence": self.robustness_evidence,
            "recommendation_leave_one_day_out": self.recommendation_leave_one_day_out,
            "session_quality": self.session_quality,
            "execution_calibration": self.execution_calibration,
            "continuity_evidence": self.continuity_evidence,
            "continuity_block_evidence": self.continuity_block_evidence,
            "score_policy_evidence": self.score_policy_evidence,
            "moving_block_evidence": self.moving_block_evidence,
            "selection_bootstrap_evidence": self.selection_bootstrap_evidence,
            "walk_forward_evidence": self.walk_forward_evidence,
            "pareto_evidence": self.pareto_evidence,
            "boundary_evidence": self.boundary_evidence,
            "assumption_stress_evidence": self.assumption_stress_evidence,
            "recommendation_gates": self.recommendation_gates,
            "exploratory_only": self.exploratory_only,
            "files_written": [
                path.relative_to(self.output_dir).as_posix()
                if path.is_absolute() and self.output_dir in path.parents
                else path.as_posix()
                for path in self.files_written
            ],
        }
