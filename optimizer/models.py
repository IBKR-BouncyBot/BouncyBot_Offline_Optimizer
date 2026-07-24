"""Typed records exchanged by the optimizer's analysis pipeline."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

ProgressCallback = Callable[[str, int, int], None]


def _normalized_limit(value: Any, *, name: str, minimum: int) -> int:
    """Return a finite integer safety limit without bool/fraction truncation."""

    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, not a boolean.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite integer.") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError(f"{name} must be a finite integer.")
    return max(minimum, int(number))


@dataclass(slots=True, frozen=True)
class SourcePaths:
    root: Path
    database: Path
    captures: Path
    bot_lock: Path


@dataclass(slots=True)
class AnalysisConfig:
    source_dir: Path
    output_root: Path
    max_archive_uncompressed_bytes: int = 256 * 1024 * 1024
    max_rows_per_capture: int = 200_000
    hash_capture_files: bool = True

    def normalized(self) -> "AnalysisConfig":
        if self.hash_capture_files is not True:
            raise ValueError(
                "Capture SHA-256 hashing cannot be disabled: deterministic report identity "
                "and source-mutation detection require content hashes."
            )
        return AnalysisConfig(
            source_dir=Path(self.source_dir).expanduser().resolve(),
            output_root=Path(self.output_root).expanduser().resolve(),
            max_archive_uncompressed_bytes=_normalized_limit(
                self.max_archive_uncompressed_bytes,
                name="max_archive_uncompressed_bytes",
                minimum=1_000_000,
            ),
            max_rows_per_capture=_normalized_limit(
                self.max_rows_per_capture,
                name="max_rows_per_capture",
                minimum=100,
            ),
            hash_capture_files=True,
        )


@dataclass(slots=True)
class PricePoint:
    timestamp: float
    captured_at_utc: str
    price: float
    trigger_price: float
    bid: float | None = None
    ask: float | None = None
    atr_pct: float | None = None
    stage: str = ""
    fresh_update: bool = True


@dataclass(slots=True)
class CaptureMeta:
    path: Path
    ticker: str = ""
    cycle_id: str = ""
    cycle_number: int | None = None
    event_type: str = ""
    event_time_utc: str = ""
    order_ref: str = ""
    perm_id: int | None = None
    rows_declared: int | None = None
    first_row_utc: str = ""
    last_row_utc: str = ""
    pre_window_seconds: float | None = None
    post_window_seconds: float | None = None
    sha256: str = ""
    archive_bytes: int = 0
    uncompressed_bytes: int = 0
    issues: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    event: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return not any(issue.startswith("fatal:") for issue in self.issues)


@dataclass(slots=True)
class CaptureData:
    meta: CaptureMeta
    points: list[PricePoint] = field(default_factory=list)
    raw_rows: int = 0
    invalid_rows: int = 0
    duplicate_rows: int = 0


@dataclass(slots=True, frozen=True)
class CaptureStats:
    """Memory-bounded summary retained after a capture has been parsed.

    Full price rows are needed only while replaying the cycle that selected an
    archive.  Keeping every archive's ``PricePoint`` list alive for the whole
    run can consume hundreds of megabytes on a realistic multi-ticker history,
    so the analysis retains this compact summary instead.
    """

    raw_rows: int
    usable_points: int
    invalid_rows: int
    duplicate_rows: int
    atr_points: int


@dataclass(slots=True)
class ReplayObservation:
    cycle_id: str
    cycle_number: int | None
    ticker: str
    leg: str
    candidate_key: str
    multiplier: float
    minimum_profit_multiplier: float | None
    period: int
    bar_seconds: int
    effective_pct: float | None
    triggered: bool
    baseline_window: bool = True
    effective_minimum_profit_pct: float | None = None
    trigger_time_utc: str = ""
    trigger_price: float | None = None
    actual_fill_price: float | None = None
    actual_fill_time_utc: str = ""
    price_improvement_bps: float | None = None
    delay_seconds: float | None = None
    post_trigger_mfe_bps: float | None = None
    post_trigger_mae_bps: float | None = None
    left_censored: bool = False
    control_candidate: bool = False
    outcome: str = "unavailable"
    right_censored: bool = False
    observation_start_utc: str = ""
    observation_end_utc: str = ""
    observed_seconds: float | None = None
    time_to_trigger_seconds: float | None = None
    trading_day_utc: str = ""
    trigger_bid: float | None = None
    trigger_ask: float | None = None
    trigger_spread_bps: float | None = None
    fill_reference_price: float | None = None
    fill_bid: float | None = None
    fill_ask: float | None = None
    fill_spread_bps: float | None = None
    fill_quote_age_seconds: float | None = None
    estimated_fill_price: float | None = None
    execution_adjustment_bps: float | None = None
    adjusted_price_improvement_bps: float | None = None
    execution_model_method: str = ""
    execution_model_samples: int = 0
    execution_model_leave_one_out: bool = False
    atr_source: str = ""
    note: str = ""
    historical_atr_profile_id: str = "UNKNOWN"


@dataclass(slots=True)
class CandidateSummary:
    leg: str
    candidate_key: str
    multiplier: float
    minimum_profit_multiplier: float | None
    period: int
    bar_seconds: int
    baseline_window: bool
    observations: int
    scoreable_observations: int
    triggered: int
    scoreable_triggered: int
    candidate_atr_observations: int
    candidate_atr_coverage_pct: float | None
    trigger_rate_pct: float | None
    median_improvement_bps: float | None
    median_delay_seconds: float | None
    median_absolute_delay_seconds: float | None
    median_mfe_bps: float | None
    median_mae_bps: float | None
    left_censored_observations: int
    left_censored_rate_pct: float | None
    screening_score: float | None
    evidence: str
    priority: str
    rationale: str
    historical_atr_profile_count: int = 0
    historical_atr_profiles: list[str] = field(default_factory=list)
    historical_atr_profile_observations: dict[str, int] = field(default_factory=dict)
    historical_atr_profile_breakdown: list[dict[str, Any]] = field(default_factory=list)
    control_candidate: bool = False
    right_censored_observations: int = 0
    right_censored_rate_pct: float | None = None
    unavailable_observations: int = 0
    km_trigger_probability_1m_pct: float | None = None
    km_trigger_probability_5m_pct: float | None = None
    km_trigger_probability_15m_pct: float | None = None
    median_adjusted_improvement_bps: float | None = None
    paired_evidence: dict[str, Any] = field(default_factory=dict)
    stable_region: dict[str, Any] = field(default_factory=dict)
    execution_model: dict[str, Any] = field(default_factory=dict)
    evidence_stable: bool = False
    instability_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TickerAnalysis:
    ticker: str
    coverage: dict[str, Any]
    execution_model: dict[str, Any]
    evidence_methodology: dict[str, Any]
    cycles: list[dict[str, Any]]
    atr_settings_summary: dict[str, Any]
    atr_settings_profiles: list[dict[str, Any]]
    atr_settings_regimes: list[dict[str, Any]]
    atr_settings_history: list[dict[str, Any]]
    capture_inventory: list[dict[str, Any]]
    replay_observations: list[ReplayObservation]
    candidate_summaries: list[CandidateSummary]
    suggested_settings: list[dict[str, Any]]
    primary_evaluation_setting: dict[str, Any]
    issues: list[str]
    limitations: list[str]

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "coverage": self.coverage,
            "execution_model": self.execution_model,
            "evidence_methodology": self.evidence_methodology,
            "cycles": self.cycles,
            "atr_settings_summary": self.atr_settings_summary,
            "atr_settings_profiles": self.atr_settings_profiles,
            "atr_settings_regimes": self.atr_settings_regimes,
            "atr_settings_history": self.atr_settings_history,
            "capture_inventory": self.capture_inventory,
            "replay_observations": [asdict(item) for item in self.replay_observations],
            "candidate_summaries": [asdict(item) for item in self.candidate_summaries],
            "suggested_settings": self.suggested_settings,
            "primary_evaluation_setting": self.primary_evaluation_setting,
            "issues": self.issues,
            "limitations": self.limitations,
        }


@dataclass(slots=True)
class AnalysisResult:
    run_id: str
    analysis_id: str
    source: SourcePaths
    output_dir: Path
    generated_at_utc: str
    data_through_utc: str
    input_fingerprint: str
    database_sha256: str
    database_snapshot_bytes: int
    schema: dict[str, Any]
    tickers: list[TickerAnalysis]
    global_issues: list[str]
    files_written: list[Path] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "analysis_id": self.analysis_id,
            "source": {
                "database_name": self.source.database.name,
                "captures_name": self.source.captures.name,
                "bot_lock_name": self.source.bot_lock.name,
            },
            "output_directory_name": self.output_dir.name,
            "generated_at_utc": self.generated_at_utc,
            "data_through_utc": self.data_through_utc,
            "input_fingerprint": self.input_fingerprint,
            "database_sha256": self.database_sha256,
            "database_snapshot_bytes": self.database_snapshot_bytes,
            "schema": self.schema,
            "tickers": [ticker.to_jsonable() for ticker in self.tickers],
            "global_issues": self.global_issues,
            "files_written": [
                path.relative_to(self.output_dir).as_posix()
                if path.is_absolute() and self.output_dir in path.parents
                else path.as_posix()
                for path in self.files_written
            ],
        }
