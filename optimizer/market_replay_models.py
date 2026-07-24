"""Data models for standalone Market Replay recording optimization."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def _positive_finite(value: float | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _strict_positive_int(value: Any, *, name: str, minimum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer of at least {minimum:,}.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer of at least {minimum:,}.") from exc
    if not math.isfinite(number) or not number.is_integer() or number < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum:,}.")
    return int(number)


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
        minimum = float(self.min_atr_pct)
        maximum = float(self.max_atr_pct)
        if not math.isfinite(minimum) or minimum <= 0:
            raise ValueError("min_atr_pct must be finite and positive.")
        if not math.isfinite(maximum) or maximum <= minimum or maximum >= 100:
            raise ValueError("max_atr_pct must be finite, below 100, and above min_atr_pct.")
        return MarketReplayConfig(
            recording_path=paths,
            output_root=output,
            max_rows=max_rows,
            max_input_bytes=max_input_bytes,
            max_zip_uncompressed_bytes=max_zip_uncompressed_bytes,
            max_recordings=max_recordings,
            min_atr_pct=round(minimum, 4),
            max_atr_pct=round(maximum, 4),
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

    @property
    def symbol(self) -> str:
        return str(self.contract.get("symbol") or "UNKNOWN").upper().strip() or "UNKNOWN"

    @property
    def con_id(self) -> int:
        try:
            return max(0, int(self.contract.get("con_id", self.contract.get("conId", 0)) or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    @property
    def min_tick(self) -> float:
        try:
            value = float(self.contract.get("min_tick", self.contract.get("minTick", 0.01)) or 0.01)
        except (TypeError, ValueError, OverflowError):
            value = 0.01
        return value if math.isfinite(value) and value > 0 else 0.01

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
        values = {
            int(component.get("format_version") or 0)
            for component in self.input_components
            if int(component.get("format_version") or 0) in {2, 3}
        }
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
    first_entry_utc: str = ""
    last_exit_utc: str = ""
    open_entry_setup: bool = False
    right_censored: bool = False
    unmarked_open_position: bool = False
    issues: list[str] = field(default_factory=list)


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
    paired_median_return_delta_bps: float | None = None
    paired_positive_day_pct: float | None = None
    control_trade_day_retention_pct: float | None = None
    maximum_drawdown_delta_bps: float | None = None
    worst_return_delta_bps: float | None = None
    atr_phase_cases: int = 0
    atr_phase_min_score_delta: float | None = None
    atr_phase_adverse_score_delta: float | None = None

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
            "files_written": [
                path.relative_to(self.output_dir).as_posix()
                if path.is_absolute() and self.output_dir in path.parents
                else path.as_posix()
                for path in self.files_written
            ],
        }
