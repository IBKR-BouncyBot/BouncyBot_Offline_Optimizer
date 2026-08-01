"""Independent full-recording ATR search for Market Replay Lab data."""

from __future__ import annotations

import hashlib
import itertools
import math
import statistics
import struct
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from .determinism import canonical_json_bytes
from .ibrec import load_ibrec_set
from .market_replay_calibration import load_execution_calibration
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
from .market_replay_quality import assess_market_replay_session
from .market_replay_validation import (
    BALANCED_SCORE_POLICY,
    continuity_block_metrics,
    deterministic_moving_block_indices,
    expanding_walk_forward_folds,
    moving_block_length,
    pareto_dominates,
    pareto_frontier,
    recommendation_gate_rows,
    score_policy_comparison,
    score_policy_contract,
    score_sessions,
)
from .utils import canonical_session_date, percentile, timestamp_seconds
from .version import APP_VERSION


class MarketReplayAnalysisError(RuntimeError):
    """Raised when a recording cannot support a safe deterministic analysis."""


ProgressCallback = Callable[[str, int, int], None]
AtrCacheKey = tuple[str, str, int, int, int]
MARKET_REPLAY_ANALYSIS_CONTRACT_VERSION = 15
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
_COARSE_MIN_ATR_PCT = (0.01, 0.05, 0.10, 0.20)
_PROTECTIVE_MANUAL_TRAILS = (1.0, 2.0, 3.0, 4.0, 5.0)
_PROTECTIVE_ATR_MULTIPLIERS = (1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5)
_PROTECTIVE_MANUAL_STEP = 1.0
_PROTECTIVE_ATR_STEP = 0.5
_PROTECTIVE_MIN_REGION_SIZE = 3
_WINDOW_SCREEN_MULTIPLIERS = (
    (1.50, 0.75, 1.00, 1.00),
    (1.00, 0.50, 0.75, 0.75),
    (2.00, 1.00, 1.25, 1.25),
    (2.00, 1.00, 1.00, 1.00),
    (1.50, 0.75, 0.75, 0.50),
)
_REFINEMENT_STEP = 0.25
_REFINEMENT_SEEDS = 12
_BOOTSTRAP_REPLICATES = 2_000
_MOVING_BLOCK_REPLICATES = 2_000
_SELECTION_BOOTSTRAP_REPLICATES = 32
_MIN_ROBUST_TRADING_DAYS = 5
_MIN_ADVANCED_VALIDATION_DAYS = 20
_WALK_FORWARD_MIN_TRAINING_DAYS = 15
_WALK_FORWARD_VALIDATION_DAYS = 5
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
_MAX_CHANGED_PROFILE_CLAMP_RATE_PCT = 90.0
_CLAMP_COMPONENTS = (
    "initial_drop",
    "buy_rebound",
    "minimum_profit",
    "sell_trail",
    "protective_sell",
)
_CLAMP_STATES = ("min", "max", "raw", "zero")


@dataclass(slots=True)
class _ReplayCarryState:
    """Strategy and equity state that may survive an RTH boundary."""

    stage: str = "WAIT_READY"
    anchor: float | None = None
    buy_stop: float | None = None
    buy_running_low: float | None = None
    buy_trail_pct: float | None = None
    buy_placed_sequence: int = 0
    buy_fill_reference: float | None = None
    buy_price: float | None = None
    buy_cost_basis: float | None = None
    assumed_quantity: int = 0
    buy_time: str = ""
    minimum_profit_pct: float | None = None
    sell_trail_pct: float | None = None
    sell_stop: float | None = None
    sell_running_high: float | None = None
    sell_placed_sequence: int = 0
    sell_fill_reference: float | None = None
    protective_sell_pct: float | None = None
    protective_stop: float | None = None
    protective_initial_stop: float | None = None
    protective_running_high: float | None = None
    protective_placed_sequence: int = 0
    protective_fill_reference: float | None = None
    protective_normal_activation_price: float | None = None
    capital: float = 1.0
    cycle_number: int = 1
    last_long_mark: float | None = None
    last_session_end_equity: float = 1.0
    equity_peak: float = 1.0
    maximum_drawdown_bps: float = 0.0
    active_trade: MarketReplayTrade | None = None

    @property
    def has_open_position(self) -> bool:
        return self.buy_price is not None and self.stage in {
            "HOLD",
            "SELL_TRAIL",
            "SELL_FILL_PENDING",
            "PROTECTIVE_FILL_PENDING",
        }


@dataclass(slots=True)
class _BoundedSelectionRun:
    """Complete three-stage selection result for one chronological subset."""

    selected: MarketReplayCandidateSummary
    reason: str
    control: MarketReplayCandidateSummary
    selected_windows: list[tuple[int, int]]
    candidates: list[MarketReplayCandidateSummary]
    sessions_by_profile: dict[str, list[MarketReplaySessionResult]]
    atr_cache: dict[AtrCacheKey, list[float | None]]
    window_search: list[dict[str, Any]]
    protective_policy_evidence: list[dict[str, Any]]
    protective_policy_reason: str


def _expected_next_weekday(value: date) -> date:
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _continuity_between(
    current: IbrecPeriod,
    following: IbrecPeriod,
) -> tuple[bool, str]:
    """Return whether state can be carried without inventing a missing RTH day."""

    if not current.primary_eligible or not following.primary_eligible:
        return False, "One or both adjacent sessions failed the primary data-quality gate."
    try:
        current_date = date.fromisoformat(current.session_date)
        following_date = date.fromisoformat(following.session_date)
    except ValueError:
        return False, "A session date could not be parsed for continuity analysis."
    expected = _expected_next_weekday(current_date)
    if following_date != expected:
        return False, (
            f"The next recording is {following.session_date}, but the conservative weekday sequence expected "
            f"{expected.isoformat()}; a holiday or missing trading day cannot be distinguished from absent data."
        )
    if following.open_timestamp <= current.close_timestamp:
        return False, "Adjacent session schedules overlap or are out of chronological order."
    return True, ""


def _normalized_replay_config(
    config: MarketReplayConfig | None,
) -> MarketReplayConfig:
    """Return production defaults for internal/test callers that omit config."""

    if config is None:
        config = MarketReplayConfig(
            Path.cwd() / "recording.ibrec",
            Path.cwd() / "optimizer_reports",
            execution_cost_bps_per_side=0.0,
            turnover_penalty_bps_per_completed_trade=0.0,
            min_touch_liquidity_coverage_pct=0.0,
        )
    return config.normalized()


def market_replay_search_contract(config: MarketReplayConfig) -> dict[str, Any]:
    normalized = config.normalized()
    return {
        "contract_version": MARKET_REPLAY_ANALYSIS_CONTRACT_VERSION,
        "optimizer_version": APP_VERSION,
        "supported_ibrec_versions": [2, 3],
        "protective_sell_policy_search": {
            "enabled": normalized.protective_policy_search_enabled,
            "purpose": (
                "compare no protective order with bounded manual and ATR-adaptive native trailing SELL policies "
                "at the unchanged ATR control before conditional ATR optimization"
            ),
            "disabled_control": True,
            "manual_trailing_percentages": list(_PROTECTIVE_MANUAL_TRAILS),
            "atr_adaptive_multipliers": list(_PROTECTIVE_ATR_MULTIPLIERS),
            "minimum_adjacent_region_size": _PROTECTIVE_MIN_REGION_SIZE,
            "maximum_policies_advanced": 2,
            "advanced_policies": (
                "the disabled control and at most one independently supported protective-policy region center"
            ),
            "normal_sell_replacement": (
                "a working protective trail is cancelled before the normal minimum-profit SELL is submitted; "
                "the Market Replay path models this cancel/replace atomically because .ibrec contains market data, not broker acknowledgements"
            ),
        },
        "atr_window_search": {
            "stage_1": {
                "purpose": "compare bar duration with a small representative multiplier mini-grid while holding ATR period fixed",
                "fixed_period": _STAGE1_FIXED_PERIOD,
                "bar_seconds": list(_STAGE1_BAR_SECONDS),
                "keep_best_bar_durations": _STAGE1_KEEP_BAR_DURATIONS,
                "control_bar_seconds_always_retained": _CONTROL_PROFILE.bar_seconds,
                "screening_multiplier_profiles": [list(item) for item in _WINDOW_SCREEN_MULTIPLIERS],
            },
            "stage_2": {
                "purpose": "compare ATR period within the strongest stage-1 bar durations using the same representative multiplier mini-grid",
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
        "coarse_min_atr_percentage_candidates": list(_COARSE_MIN_ATR_PCT),
        "refinement_step": _REFINEMENT_STEP,
        "refinement_seed_count": _REFINEMENT_SEEDS,
        "bootstrap_replicates": _BOOTSTRAP_REPLICATES,
        "moving_block_bootstrap_replicates": _MOVING_BLOCK_REPLICATES,
        "selection_aware_bootstrap_replicates": _SELECTION_BOOTSTRAP_REPLICATES,
        "minimum_advanced_validation_days": _MIN_ADVANCED_VALIDATION_DAYS,
        "walk_forward": {
            "minimum_training_days": _WALK_FORWARD_MIN_TRAINING_DAYS,
            "validation_block_days": _WALK_FORWARD_VALIDATION_DAYS,
            "method": (
                "expanding chronological training windows; profile selection uses training data only, "
                "then the frozen selected profile is evaluated on the following unseen block"
            ),
        },
        "trading_day_bootstrap_replicates": _BOOTSTRAP_REPLICATES,
        "bootstrap_schedule": (
            "all stable-region centers use the same deterministic resample schedule; ordinary flat sessions "
            "are sampled by trading day, while days linked by an overnight position or active SELL order "
            "are sampled as one complete continuity block"
        ),
        "trading_day_bootstrap_schedule": (
            "flat sessions use the deterministic whole-day schedule; overnight-linked sessions use complete continuity blocks"
        ),
        "minimum_robust_trading_days": _MIN_ROBUST_TRADING_DAYS,
        "minimum_bootstrap_probability_positive_pct": _MIN_BOOTSTRAP_POSITIVE_PCT,
        "minimum_leave_one_day_out_window_selection_pct": (
            _MIN_LOO_WINDOW_SELECTION_PCT
        ),
        "changed_recommendation_requires": [
            "protective SELL policy independently passes the bounded stop-policy screen, unless disabled",
            "connected near-best multiplier region",
            "positive score delta versus the unchanged control on identical RTH sessions",
            f"paired score improvement of at least {_MIN_PRACTICAL_SCORE_DELTA:.1f} point",
            "80% day-or-continuity-block bootstrap interval entirely above zero",
            f"at least {_MIN_BOOTSTRAP_POSITIVE_PCT:.0f}% positive bootstrap replicates",
            "positive leave-one-day-out result for every removable trading day",
            "no leave-one-day-out sign reversal",
            "same ATR period/bar window advances through stage 2 in at least 60% of leave-one-day-out reruns",
            "no paired candidate/control session is right-censored",
            "no paired candidate/control session has an unmarked open long position",
            "positive comparison under every predefined score policy",
            "non-dominated membership on the searched Pareto frontier",
            "all active search boundaries resolved by outward probes",
            "positive economic continuity-block comparison",
            "positive moving-block bootstrap evidence",
            "positive results under every configured execution, quote-age, notional, and timing stress",
            "positive chronological walk-forward results on unseen validation blocks",
            "positive selection-aware out-of-bag bootstrap evidence",
        ],
        "min_atr_pct": normalized.min_atr_pct,
        "max_atr_pct": normalized.max_atr_pct,
        "entry_open_delay_seconds": normalized.entry_open_delay_seconds,
        "entry_cutoff_seconds": normalized.entry_cutoff_seconds,
        "buy_trail_cancel_seconds": normalized.buy_trail_cancel_seconds,
        "execution_quote_max_age_seconds": normalized.execution_quote_max_age_seconds,
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
            "same_day_overlap_policy": (
                "stitch the deterministic coverage-first maximal non-overlapping "
                "fragment set when RTH schedules agree; preserve outages for quality "
                "gates; never interleave overlapping streams; exclude schedule conflicts"
            ),
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
        "cost_and_liquidity_model": {
            "assumed_trade_notional": normalized.assumed_trade_notional,
            "execution_cost_bps_per_side": normalized.execution_cost_bps_per_side,
            "buy_execution_cost_bps_per_side": (
                normalized.buy_execution_cost_bps_per_side
            ),
            "sell_execution_cost_bps_per_side": (
                normalized.sell_execution_cost_bps_per_side
            ),
            "date_specific_execution_cost_overrides": len(
                normalized.execution_cost_overrides
            ),
            "date_specific_trade_notional_overrides": len(
                normalized.trade_notional_overrides
            ),
            "turnover_penalty_bps_per_completed_trade": normalized.turnover_penalty_bps_per_completed_trade,
            "minimum_touch_liquidity_coverage_pct": normalized.min_touch_liquidity_coverage_pct,
            "touch_liquidity_interpretation": (
                "recorded top-of-book size is compared with floor(assumed notional / modeled BUY price); "
                "it is a recommendation-quality gate, not a depth or partial-fill simulation"
            ),
        },
        "continuous_overnight_replay": normalized.continuous_overnight_replay,
        "execution_calibration": {
            "enabled": normalized.calibration_source_dir is not None,
            "maximum_quote_age_seconds": normalized.calibration_max_quote_age_seconds,
            "minimum_samples": normalized.calibration_min_samples,
            "use_execution_cost": normalized.calibration_use_execution_cost,
            "use_trade_notional": normalized.calibration_use_trade_notional,
            "method": (
                "Actual execution rows are matched to the latest recorded same-side quote at or before the execution. "
                "BUY and SELL costs are estimated separately. Primary assumptions use cross-fitted 75th-percentile adverse quote residual plus non-negative commission bps; p90 evidence is retained for stress testing."
            ),
        },
        "score_policies": score_policy_contract(),
        "primary_session_quality": {
            "session_boundary_tolerance_seconds": normalized.session_boundary_tolerance_seconds,
            "maximum_market_event_gap_seconds": normalized.max_market_event_gap_seconds,
            "minimum_last_event_minute_coverage_pct": normalized.min_last_event_minute_coverage_pct,
            "maximum_last_event_gap_p95_seconds": normalized.max_last_event_gap_p95_seconds,
            "requirements": [
                "full RTH coverage within the configured boundary tolerance",
                "closed/finalized source evidence",
                "live feed only",
                "no recorded connectivity-loss event during RTH",
                "no excessive retained-event gap",
                "sufficient genuine Last-event density",
            ],
        },
        "atr_clock": (
            "OHLC buckets use recorder elapsed_ns (monotonic time). The primary run uses the canonical stored phase; "
            "changed recommendations are stress-tested over 5-second phase offsets because BouncyBot's process-clock phase is not recorded."
        ),
        "scoring": (
            "0.50*median_conservative_return_bps + 0.30*mean_conservative_return_bps "
            "+ 0.20*worst_conservative_return_bps - 0.35*maximum_drawdown_bps "
            "- 25*open_position_session_fraction - 15*right_censored_session_fraction "
            "- configured turnover penalty per completed trade; modeled session returns "
            "already include the configured per-side execution-cost reserve"
        ),
        "additional_changed_recommendation_gates": [
            f"at least {_MIN_PAIRED_POSITIVE_DAY_PCT:.0f}% of paired trading days have a better conservative return",
            f"candidate trades on at least {_MIN_CONTROL_TRADE_DAY_RETENTION_PCT:.0f}% of control trading days",
            f"maximum drawdown cannot deteriorate by more than {_MAX_DRAWDOWN_DETERIORATION_BPS:.0f} bps",
            f"worst-session return cannot deteriorate by more than {_MAX_WORST_RETURN_DETERIORATION_BPS:.0f} bps",
            "candidate remains above control under every tested ATR bar phase and an adverse per-session phase aggregation",
            f"minimum- and maximum-clamp saturation remain below {_MAX_CHANGED_PROFILE_CLAMP_RATE_PCT:.0f}% for every changed strategy component",
            "recorded touch-size sufficiency meets the configured liquidity-coverage threshold",
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
    value, _ = _effective_percentage_state(
        atr_pct,
        multiplier,
        profile,
        allow_zero=allow_zero,
    )
    return value


def _effective_percentage_state(
    atr_pct: float | None,
    multiplier: float,
    profile: AtrProfile,
    *,
    allow_zero: bool,
) -> tuple[float | None, str]:
    """Return the effective percentage and its clamp classification."""

    if allow_zero and multiplier <= 0:
        return 0.0, "zero"
    if atr_pct is None or not math.isfinite(atr_pct) or atr_pct <= 0:
        return None, "unavailable"
    raw = atr_pct * multiplier
    # Equality is classified as clamp-bound as well. At the exact boundary the
    # effective percentage cannot demonstrate that the multiplier, rather than
    # the clamp, determined the submitted setting.
    if raw <= profile.min_atr_pct:
        return round(profile.min_atr_pct, 2), "min"
    if raw >= profile.max_atr_pct:
        return round(profile.max_atr_pct, 2), "max"
    return round(raw, 2), "raw"


def _profile_distance(left: AtrProfile, right: AtrProfile = _CONTROL_PROFILE) -> float:
    protective_mode_penalty = 0.0
    if left.protective_sell_mode != right.protective_sell_mode:
        protective_mode_penalty = 2.0
    protective_value_penalty = (
        abs(left.protective_sell_value - right.protective_sell_value)
        if left.protective_sell_mode == right.protective_sell_mode
        else abs(left.protective_sell_value) / 2.0
    )
    return (
        abs(left.period - right.period) / 10.0
        + abs(left.bar_seconds - right.bar_seconds) / 60.0
        + abs(left.initial_drop_multiplier - right.initial_drop_multiplier)
        + abs(left.buy_rebound_multiplier - right.buy_rebound_multiplier)
        + abs(left.minimum_profit_multiplier - right.minimum_profit_multiplier)
        + abs(left.sell_trail_multiplier - right.sell_trail_multiplier)
        + abs(left.min_atr_pct - right.min_atr_pct) / 0.10
        + abs(left.max_atr_pct - right.max_atr_pct) / 5.0
        + protective_mode_penalty
        + protective_value_penalty
    )


def _candidate_sort_key(candidate: MarketReplayCandidateSummary) -> tuple[Any, ...]:
    return (-candidate.score, _profile_distance(candidate.profile), candidate.profile.key())


def _changed_clamp_components(
    candidate: AtrProfile,
    control: AtrProfile,
) -> tuple[str, ...]:
    """Return strategy components whose effective percentages can change."""

    if (
        candidate.period != control.period
        or candidate.bar_seconds != control.bar_seconds
        or not math.isclose(candidate.min_atr_pct, control.min_atr_pct)
        or not math.isclose(candidate.max_atr_pct, control.max_atr_pct)
    ):
        changed = list(_CLAMP_COMPONENTS[:-1])
        if candidate.protective_sell_mode == "atr":
            changed.append("protective_sell")
        return tuple(changed)
    changed: list[str] = []
    for component, candidate_value, control_value in (
        (
            "initial_drop",
            candidate.initial_drop_multiplier,
            control.initial_drop_multiplier,
        ),
        (
            "buy_rebound",
            candidate.buy_rebound_multiplier,
            control.buy_rebound_multiplier,
        ),
        (
            "minimum_profit",
            candidate.minimum_profit_multiplier,
            control.minimum_profit_multiplier,
        ),
        (
            "sell_trail",
            candidate.sell_trail_multiplier,
            control.sell_trail_multiplier,
        ),
    ):
        if not math.isclose(candidate_value, control_value):
            changed.append(component)
    if (
        candidate.protective_sell_mode != control.protective_sell_mode
        or not math.isclose(
            candidate.protective_sell_value,
            control.protective_sell_value,
        )
    ) and candidate.protective_sell_mode == "atr":
        changed.append("protective_sell")
    return tuple(changed)


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


def _simulate_session_stateful(
    ticks: list[IbrecTick],
    period: IbrecPeriod,
    profile: AtrProfile,
    atr_values: list[float | None],
    min_tick: float,
    config: MarketReplayConfig | None = None,
    *,
    keep_trades: bool,
    state: _ReplayCarryState | None = None,
    carry_to_next: bool = False,
    continuity_chain_id: int = 1,
    continuity_broken_before: bool = False,
    continuity_break_reason: str = "",
) -> tuple[
    MarketReplaySessionResult,
    list[MarketReplayTrade],
    _ReplayCarryState,
]:
    """Replay one RTH period while optionally preserving an overnight long."""

    if len(ticks) != len(atr_values):
        raise MarketReplayAnalysisError("Internal ATR/tick alignment failure.")
    normalized = _normalized_replay_config(config)
    default_buy_cost = normalized.buy_execution_cost_bps_per_side
    default_sell_cost = normalized.sell_execution_cost_bps_per_side
    if default_buy_cost is None or default_sell_cost is None:
        raise MarketReplayAnalysisError(
            "Normalized replay configuration did not provide side-specific execution reserves."
        )
    # Recording periods store ``YYYYMMDD`` session dates while calibration
    # emits ISO ``YYYY-MM-DD`` override keys.  Both sides of the lookup must
    # normalize through the one shared helper; matching on the raw strings
    # silently ignored every date-specific override before version 1.9.3.
    session_date_key = canonical_session_date(period.session_date)
    cost_overrides = {
        canonical_session_date(date_key): (buy_cost, sell_cost)
        for date_key, buy_cost, sell_cost in normalized.execution_cost_overrides
    }
    buy_cost_bps, sell_cost_bps = cost_overrides.get(
        session_date_key,
        (default_buy_cost, default_sell_cost),
    )
    buy_execution_cost_rate = buy_cost_bps / 10_000.0
    sell_execution_cost_rate = sell_cost_bps / 10_000.0
    notional_overrides = {
        canonical_session_date(date_key): value
        for date_key, value in normalized.trade_notional_overrides
    }
    assumed_trade_notional = notional_overrides.get(
        session_date_key,
        normalized.assumed_trade_notional,
    )
    entry_start = period.open_timestamp + normalized.entry_open_delay_seconds
    entry_cutoff = period.close_timestamp - normalized.entry_cutoff_seconds
    buy_cancel = period.close_timestamp - normalized.buy_trail_cancel_seconds
    carry = state or _ReplayCarryState()
    stage = carry.stage
    anchor = carry.anchor
    buy_stop = carry.buy_stop
    buy_running_low = carry.buy_running_low
    buy_trail_pct = carry.buy_trail_pct
    buy_placed_sequence = carry.buy_placed_sequence
    buy_fill_reference = carry.buy_fill_reference
    buy_price = carry.buy_price
    buy_cost_basis = carry.buy_cost_basis
    assumed_quantity = carry.assumed_quantity
    buy_time = carry.buy_time
    minimum_profit_pct = carry.minimum_profit_pct
    sell_trail_pct = carry.sell_trail_pct
    sell_stop = carry.sell_stop
    sell_running_high = carry.sell_running_high
    sell_placed_sequence = carry.sell_placed_sequence
    sell_fill_reference = carry.sell_fill_reference
    protective_sell_pct = carry.protective_sell_pct
    protective_stop = carry.protective_stop
    protective_initial_stop = carry.protective_initial_stop
    protective_running_high = carry.protective_running_high
    protective_placed_sequence = carry.protective_placed_sequence
    protective_fill_reference = carry.protective_fill_reference
    protective_normal_activation_price = carry.protective_normal_activation_price
    capital = carry.capital
    cycle_number = carry.cycle_number
    carried_position_in = carry.has_open_position
    carried_sell_trail_in = stage in {"SELL_TRAIL", "SELL_FILL_PENDING"}
    carried_protective_trail_in = stage in {
        "HOLD",
        "PROTECTIVE_FILL_PENDING",
    } and protective_stop is not None
    if carried_position_in and stage == "HOLD":
        # The normal SELL order has not yet been submitted, so its ATR-derived
        # percentages are not locked.  Re-warm the new day's ATR and derive
        # fresh values exactly as BouncyBot would after an overnight hold.
        minimum_profit_pct = None
        sell_trail_pct = None
        protective_normal_activation_price = None
    # Each .ibrec file can restart its local sequence counter.  A native SELL
    # trail that was placed in an earlier recording must therefore be eligible
    # on the first genuine Last event of the next session; comparing the new
    # sequence with the prior file's sequence could otherwise block it for the
    # complete day.
    if carried_sell_trail_in:
        sell_placed_sequence = 0
    if carried_protective_trail_in:
        protective_placed_sequence = 0
    active_trade = carry.active_trade
    if carried_position_in and active_trade is not None:
        active_trade.overnight_sessions_held += 1
    session_start_equity = (
        carry.last_session_end_equity
        if math.isfinite(carry.last_session_end_equity)
        and carry.last_session_end_equity > 0
        else capital
    )
    chain_equity_peak = (
        carry.equity_peak
        if math.isfinite(carry.equity_peak) and carry.equity_peak > 0
        else session_start_equity
    )
    chain_max_drawdown = (
        carry.maximum_drawdown_bps
        if math.isfinite(carry.maximum_drawdown_bps)
        and carry.maximum_drawdown_bps >= 0
        else 0.0
    )
    first_mark_equity: float | None = None
    completed_trade_count = 0
    buys_in_session = 0
    protective_exits_in_session = 0
    protective_cancellations_in_session = 0
    trades: list[MarketReplayTrade] = []
    equity_values: list[float] = [session_start_equity]
    first_entry = ""
    last_exit = ""
    # A mark from the preceding close is valid for establishing the next
    # session's starting equity, but it must never be reused as though it were a
    # current-session quote.  Require a fresh bid in every carried session before
    # the long can be marked again or carried onward.
    last_long_mark = None if carried_position_in else carry.last_long_mark
    touch_liquidity_checks = 0
    touch_liquidity_sufficient_checks = 0
    total_execution_cost_bps = 0.0
    clamp_counts = {state: 0 for state in _CLAMP_STATES}
    clamp_component_counts = {
        component: {state: 0 for state in _CLAMP_STATES}
        for component in _CLAMP_COMPONENTS
    }
    clamp_component_seen_seconds: set[tuple[str, int]] = set()
    issues: list[str] = []
    last_bid_update_ns: int | None = None
    last_ask_update_ns: int | None = None
    last_valid_atr_pct: float | None = None

    def refresh_quote_times(tick: IbrecTick) -> None:
        nonlocal last_bid_update_ns, last_ask_update_ns
        if tick.full_snapshot or "bid" in tick.changed_fields:
            last_bid_update_ns = tick.elapsed_ns if tick.valid_bid() is not None else None
        if tick.full_snapshot or "ask" in tick.changed_fields:
            last_ask_update_ns = tick.elapsed_ns if tick.valid_ask() is not None else None

    def quote_is_fresh(tick: IbrecTick, updated_ns: int | None) -> bool:
        maximum_age = normalized.execution_quote_max_age_seconds
        if maximum_age is None:
            return True
        if updated_ns is None or tick.elapsed_ns < updated_ns:
            return False
        return (tick.elapsed_ns - updated_ns) / 1_000_000_000.0 <= maximum_age + 1e-9

    def fresh_bid(tick: IbrecTick) -> float | None:
        return tick.valid_bid() if quote_is_fresh(tick, last_bid_update_ns) else None

    def fresh_ask(tick: IbrecTick) -> float | None:
        return tick.valid_ask() if quote_is_fresh(tick, last_ask_update_ns) else None

    def record_clamp(component: str, status: str, tick: IbrecTick) -> None:
        # A recording can contain many callbacks in one second. Counting every
        # callback would make saturation depend on callback density rather than
        # elapsed market time. Sample each strategy component at most once per
        # monotonic recorder second.
        decision_second = max(0, int(tick.elapsed_ns // 1_000_000_000))
        sample_key = (component, decision_second)
        if sample_key in clamp_component_seen_seconds:
            return
        clamp_component_seen_seconds.add(sample_key)
        if status in clamp_counts:
            clamp_counts[status] += 1
        if component in clamp_component_counts and status in _CLAMP_STATES:
            clamp_component_counts[component][status] += 1

    def record_touch(
        size: float | None,
        quantity: int,
    ) -> tuple[float | None, bool | None]:
        nonlocal touch_liquidity_checks, touch_liquidity_sufficient_checks
        if quantity <= 0:
            return None, None
        touch_liquidity_checks += 1
        if size is None or not math.isfinite(size) or size <= 0:
            # A modeled fill without a usable same-side size is still a
            # liquidity observation. Treating it as absent from the denominator
            # would let a candidate obtain 100% coverage from one known quote
            # while every other modeled fill has unknown top-of-book depth.
            return None, False
        sufficient = size + 1e-9 >= quantity
        if sufficient:
            touch_liquidity_sufficient_checks += 1
        return float(size), sufficient

    def append_long_mark(tick: IbrecTick) -> None:
        nonlocal last_long_mark, first_mark_equity
        if buy_price is None or buy_cost_basis is None:
            return
        bid = fresh_bid(tick)
        if bid is not None:
            last_long_mark = bid
            net_liquidation = bid * (1.0 - sell_execution_cost_rate)
            equity = capital * (net_liquidation / buy_cost_basis)
            if first_mark_equity is None:
                first_mark_equity = equity
            equity_values.append(equity)

    def protective_percentage(
        atr_pct: float | None,
        tick: IbrecTick,
    ) -> float | None:
        if not profile.protective_sell_enabled:
            return None
        if profile.protective_sell_mode == "manual":
            return round(profile.protective_sell_value, 2)
        value, state_name = _effective_percentage_state(
            atr_pct,
            profile.protective_sell_value,
            profile,
            allow_zero=False,
        )
        if value is not None:
            record_clamp("protective_sell", state_name, tick)
        return value

    def place_protective_sell(
        tick: IbrecTick,
        atr_pct: float | None,
    ) -> bool:
        nonlocal protective_sell_pct, protective_stop
        nonlocal protective_initial_stop, protective_running_high
        nonlocal protective_placed_sequence, protective_fill_reference
        nonlocal protective_normal_activation_price
        if not profile.protective_sell_enabled:
            return False
        if buy_price is None:
            raise MarketReplayAnalysisError(
                "Internal protective SELL state is missing the BUY price."
            )
        percentage = protective_percentage(atr_pct, tick)
        if percentage is None or percentage <= 0:
            issues.append(
                "The protective SELL policy was enabled, but its ATR-derived trail could not be calculated at the BUY fill; no protective order was modeled for this position."
            )
            return False
        selected = tick.selected_price()
        pure_stop = buy_price * (1.0 - percentage / 100.0)
        reference_values = [
            value
            for value in (
                selected,
                fresh_bid(tick),
                _valid(tick.last),
                _valid(tick.mark_price),
            )
            if value is not None
        ]
        reference = min(reference_values) if reference_values else buy_price
        normalized_stop = _round_increment(
            min(pure_stop, reference * (1.0 - percentage / 100.0)),
            min_tick,
            "down",
        )
        if normalized_stop <= 0:
            issues.append(
                "The protective SELL order was not modeled because its normalized initial stop was non-positive."
            )
            return False
        protective_sell_pct = percentage
        protective_stop = normalized_stop
        protective_initial_stop = normalized_stop
        protective_running_high = _valid(tick.last) or selected or buy_price
        protective_placed_sequence = tick.sequence
        protective_fill_reference = None
        protective_normal_activation_price = None
        if active_trade is not None:
            active_trade.protective_sell_mode = profile.protective_sell_mode
            active_trade.protective_sell_value = profile.protective_sell_value
            active_trade.protective_sell_pct = percentage
            active_trade.protective_initial_stop_price = normalized_stop
        return True

    def cancel_protective_sell() -> bool:
        nonlocal protective_sell_pct, protective_stop
        nonlocal protective_initial_stop, protective_running_high
        nonlocal protective_placed_sequence, protective_fill_reference
        nonlocal protective_normal_activation_price
        nonlocal protective_cancellations_in_session
        if protective_stop is None and protective_fill_reference is None:
            return False
        protective_cancellations_in_session += 1
        protective_sell_pct = None
        protective_stop = None
        protective_initial_stop = None
        protective_running_high = None
        protective_placed_sequence = 0
        protective_fill_reference = None
        protective_normal_activation_price = None
        return True

    def normal_activation_price_for_atr(
        atr_pct: float | None,
    ) -> float | None:
        """Return the normal SELL activation price at one decision event.

        The value is descriptive evidence for a protective exit. It mirrors
        the same minimum-profit and SELL-trail calculation used by the HOLD
        state, but it does not mutate strategy state or clamp counters.
        """

        if buy_price is None or atr_pct is None:
            return None
        profit_pct, _profit_state = _effective_percentage_state(
            atr_pct,
            profile.minimum_profit_multiplier,
            profile,
            allow_zero=False,
        )
        trail_pct, _sell_state = _effective_percentage_state(
            atr_pct,
            profile.sell_trail_multiplier,
            profile,
            allow_zero=True,
        )
        if profit_pct is None or trail_pct is None:
            return None
        minimum_stop = buy_price * (1.0 + profit_pct / 100.0)
        if trail_pct <= 0:
            return minimum_stop
        denominator = 1.0 - trail_pct / 100.0
        return minimum_stop / denominator if denominator > 0 else None

    def complete_buy(tick: IbrecTick, atr_pct: float | None) -> bool:
        nonlocal anchor, buy_fill_reference, buy_price, buy_cost_basis
        nonlocal buy_time, first_entry, stage
        nonlocal last_long_mark, assumed_quantity, total_execution_cost_bps
        nonlocal buy_stop, buy_running_low, buy_trail_pct, buy_placed_sequence
        nonlocal active_trade, buys_in_session
        ask = fresh_ask(tick)
        if ask is None or buy_fill_reference is None:
            return False
        buy_price = max(buy_fill_reference, ask)
        assumed_quantity = int(
            math.floor(assumed_trade_notional / buy_price)
        )
        if assumed_quantity <= 0:
            stage = "WAIT_DROP"
            anchor = None
            buy_price = None
            buy_cost_basis = None
            buy_fill_reference = None
            buy_stop = None
            buy_running_low = None
            buy_trail_pct = None
            buy_placed_sequence = 0
            buy_time = ""
            last_long_mark = None
            issues.append(
                "The assumed trade notional could not buy one whole share at the modeled ask; the setup was cancelled without a fill."
            )
            return False
        buy_cost_basis = buy_price * (1.0 + buy_execution_cost_rate)
        ask_size, ask_sufficient = record_touch(tick.ask_size, assumed_quantity)
        total_execution_cost_bps += buy_cost_bps
        last_long_mark = None
        buy_time = tick.captured_at_utc
        first_entry = first_entry or buy_time
        stage = "HOLD"
        buys_in_session += 1
        if keep_trades:
            active_trade = MarketReplayTrade(
                session_date=period.session_date,
                cycle_number=cycle_number,
                buy_time_utc=buy_time,
                buy_price=buy_price,
                buy_trigger_pct=buy_trail_pct,
                assumed_quantity=assumed_quantity,
                buy_touch_size=ask_size,
                buy_touch_sufficient=ask_sufficient,
                continuity_chain_id=continuity_chain_id,
            )
            trades.append(active_trade)
        place_protective_sell(tick, atr_pct)
        # Record the immediately executable liquidation value.  This captures
        # the bid/ask spread as drawdown on the fill event itself.
        append_long_mark(tick)
        return True

    def complete_sell(tick: IbrecTick, *, exit_type: str = "normal") -> bool:
        nonlocal capital, last_exit, stage, anchor, buy_price, buy_cost_basis
        nonlocal minimum_profit_pct, sell_trail_pct, sell_stop
        nonlocal sell_running_high, sell_placed_sequence, sell_fill_reference
        nonlocal cycle_number, completed_trade_count, last_long_mark
        nonlocal assumed_quantity, total_execution_cost_bps
        nonlocal active_trade, protective_exits_in_session
        nonlocal protective_sell_pct, protective_stop
        nonlocal protective_initial_stop, protective_running_high
        nonlocal protective_placed_sequence, protective_fill_reference
        nonlocal protective_normal_activation_price
        bid = fresh_bid(tick)
        reference = (
            protective_fill_reference
            if exit_type == "protective"
            else sell_fill_reference
        )
        if (
            bid is None
            or buy_price is None
            or buy_cost_basis is None
            or reference is None
        ):
            return False
        sell_price = min(reference, bid)
        net_sell_price = sell_price * (1.0 - sell_execution_cost_rate)
        capital *= net_sell_price / buy_cost_basis
        gross_return_bps = (sell_price / buy_price - 1.0) * 10_000.0
        net_return_bps = (net_sell_price / buy_cost_basis - 1.0) * 10_000.0
        bid_size, bid_sufficient = record_touch(tick.bid_size, assumed_quantity)
        total_execution_cost_bps += sell_cost_bps
        last_exit = tick.captured_at_utc
        if keep_trades:
            if active_trade is None:
                raise MarketReplayAnalysisError(
                    "Internal detailed replay state is missing the active trade."
                )
            trade = active_trade
            trade.sell_time_utc = last_exit
            trade.sell_session_date = period.session_date
            trade.sell_price = sell_price
            trade.return_bps = net_return_bps
            trade.gross_return_bps = gross_return_bps
            trade.net_return_bps = net_return_bps
            trade.minimum_profit_pct = minimum_profit_pct
            trade.sell_trigger_pct = sell_trail_pct
            trade.sell_touch_size = bid_size
            trade.sell_touch_sufficient = bid_sufficient
            trade.exit_type = exit_type
            if exit_type == "protective":
                trade.protective_trigger_price = reference
                trade.normal_activation_price_at_protective_exit = (
                    protective_normal_activation_price
                )
        if exit_type == "protective":
            protective_exits_in_session += 1
        completed_trade_count += 1
        stage = "WAIT_READY"
        anchor = None
        buy_price = None
        buy_cost_basis = None
        minimum_profit_pct = None
        sell_trail_pct = None
        sell_stop = None
        sell_running_high = None
        sell_placed_sequence = 0
        sell_fill_reference = None
        protective_sell_pct = None
        protective_stop = None
        protective_initial_stop = None
        protective_running_high = None
        protective_placed_sequence = 0
        protective_fill_reference = None
        protective_normal_activation_price = None
        last_long_mark = None
        assumed_quantity = 0
        active_trade = None
        cycle_number += 1
        equity_values.append(capital)
        return True

    for index, tick in enumerate(ticks):
        refresh_quote_times(tick)
        selected = tick.selected_price()
        atr_pct = atr_values[index]
        if atr_pct is not None:
            last_valid_atr_pct = atr_pct

        # A native stop becomes a market order when triggered.  The historical
        # Last event proves the trigger, but a fill is not modeled until the
        # recording supplies the executable same-side quote.  Quote-only rows
        # can therefore complete a previously triggered order.
        if stage == "BUY_FILL_PENDING":
            buy_atr_pct = (
                atr_pct if atr_pct is not None else last_valid_atr_pct
            )
            complete_buy(tick, buy_atr_pct)
            if stage == "BUY_FILL_PENDING":
                equity_values.append(capital)
            continue
        if stage == "SELL_FILL_PENDING":
            append_long_mark(tick)
            complete_sell(tick)
            continue
        if stage == "PROTECTIVE_FILL_PENDING":
            append_long_mark(tick)
            complete_sell(tick, exit_type="protective")
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
            drop_pct, drop_state = _effective_percentage_state(
                atr_pct,
                profile.initial_drop_multiplier,
                profile,
                allow_zero=False,
            )
            buy_pct, buy_state = _effective_percentage_state(
                atr_pct,
                profile.buy_rebound_multiplier,
                profile,
                allow_zero=True,
            )
            record_clamp("initial_drop", drop_state, tick)
            record_clamp("buy_rebound", buy_state, tick)
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
                buy_atr_pct = (
                    atr_pct if atr_pct is not None else last_valid_atr_pct
                )
                if not complete_buy(tick, buy_atr_pct):
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
            buy_atr_pct = (
                atr_pct if atr_pct is not None else last_valid_atr_pct
            )
            if not complete_buy(tick, buy_atr_pct):
                equity_values.append(capital)
            continue

        if buy_price is None:
            raise MarketReplayAnalysisError("Internal position state is missing a BUY price.")
        append_long_mark(tick)

        # The protective SELL is a broker-native trailing order.  It follows
        # favorable Last-price movement from the BUY fill onward and can fire
        # before the application becomes eligible to replace it with the
        # normal minimum-profit SELL.  Quote-only callbacks carrying a cached
        # Last cannot move or trigger the order.
        if stage == "HOLD" and protective_stop is not None:
            if tick.sequence > protective_placed_sequence and tick.has_last_event():
                last = _valid(tick.last)
                if (
                    last is not None
                    and protective_sell_pct is not None
                    and protective_sell_pct > 0
                ):
                    protective_running_high = (
                        last
                        if protective_running_high is None
                        else max(protective_running_high, last)
                    )
                    calculated = protective_running_high * (
                        1.0 - protective_sell_pct / 100.0
                    )
                    protective_stop = max(protective_stop, calculated)
                    if last <= protective_stop:
                        protective_normal_activation_price = (
                            normal_activation_price_for_atr(atr_pct)
                        )
                        protective_fill_reference = last
                        stage = "PROTECTIVE_FILL_PENDING"
                        complete_sell(tick, exit_type="protective")
                        continue

        if stage == "HOLD":
            if atr_pct is None:
                continue
            profit_pct, profit_state = _effective_percentage_state(
                atr_pct,
                profile.minimum_profit_multiplier,
                profile,
                allow_zero=False,
            )
            trail_pct, sell_state = _effective_percentage_state(
                atr_pct,
                profile.sell_trail_multiplier,
                profile,
                allow_zero=True,
            )
            record_clamp("minimum_profit", profit_state, tick)
            record_clamp("sell_trail", sell_state, tick)
            if profit_pct is None or trail_pct is None:
                continue
            minimum_stop = buy_price * (1.0 + profit_pct / 100.0)
            minimum_profit_pct = profit_pct
            sell_trail_pct = trail_pct
            if trail_pct <= 0:
                protective_normal_activation_price = minimum_stop
                if selected < minimum_stop:
                    continue
                cancel_protective_sell()
                sell_fill_reference = selected
                stage = "SELL_FILL_PENDING"
                complete_sell(tick)
                continue
            required_price = minimum_stop / (1.0 - trail_pct / 100.0)
            protective_normal_activation_price = required_price
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
            cancel_protective_sell()
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
    open_entry_price = (
        buy_price
        if stage
        in {
            "HOLD",
            "SELL_TRAIL",
            "SELL_FILL_PENDING",
            "PROTECTIVE_FILL_PENDING",
        }
        else None
    )
    open_cost_basis = buy_cost_basis if open_entry_price is not None else None
    open_position = open_entry_price is not None
    open_entry_setup = stage in {"BUY_TRAIL", "BUY_FILL_PENDING"}
    future_entry_possible = (
        stage in {"WAIT_READY", "WAIT_DROP"} and observation_end < entry_cutoff
    )
    can_carry_open_position = bool(
        carry_to_next and open_position and last_long_mark is not None
    )
    if carry_to_next and open_position and last_long_mark is None:
        issues.append(
            "The long position could not be carried into the next recording because no valid closing bid was available to establish a session boundary mark."
        )
    terminal_open_position = open_position and not can_carry_open_position
    right_censored = terminal_open_position or open_entry_setup or future_entry_possible
    cumulative_realized_equity = capital
    cumulative_marked_equity = cumulative_realized_equity
    unmarked_open_position = False
    if open_entry_price is not None:
        if last_long_mark is not None and open_cost_basis is not None:
            net_liquidation = last_long_mark * (1.0 - sell_execution_cost_rate)
            cumulative_marked_equity = capital * (
                net_liquidation / open_cost_basis
            )
        else:
            unmarked_open_position = True
            issues.append(
                "No valid bid was recorded after the open BUY, so the unresolved long position could not be marked conservatively."
            )
        if keep_trades and active_trade is not None and terminal_open_position:
            active_trade.open_at_end = True
        if can_carry_open_position:
            issues.append(
                "The open long position was carried into the next consecutive primary-eligible RTH recording."
            )
        else:
            issues.append(
                "A position remained open when the available continuous recording chain ended; its outcome is right-censored."
            )
    elif open_entry_setup:
        issues.append(
            "A BUY trailing or triggered market setup remained unresolved when the recording ended; its entry outcome is right-censored."
        )
    elif future_entry_possible:
        issues.append(
            "The recording ended before the standardized entry cutoff; a later setup or additional cycle remains unknown."
        )
    realized_return = (
        cumulative_realized_equity / session_start_equity - 1.0
    ) * 10_000.0
    marked_return = (
        cumulative_marked_equity / session_start_equity - 1.0
    ) * 10_000.0
    conservative = (
        marked_return
        if can_carry_open_position
        else min(realized_return, marked_return)
        if terminal_open_position
        else realized_return
    )
    had_exposure = carried_position_in or buys_in_session > 0 or completed_trade_count > 0
    trades_count = completed_trade_count + (1 if open_position else 0)
    session_end_equity = (
        cumulative_marked_equity if open_position else cumulative_realized_equity
    )
    overnight_gap_return = (
        (first_mark_equity / session_start_equity - 1.0) * 10_000.0
        if carried_position_in
        and first_mark_equity is not None
        and session_start_equity > 0
        else None
    )
    clamp_total = clamp_counts["min"] + clamp_counts["max"] + clamp_counts["raw"]
    touch_coverage = (
        touch_liquidity_sufficient_checks / touch_liquidity_checks * 100.0
        if touch_liquidity_checks
        else None
    )
    component_rates: dict[str, dict[str, float]] = {}
    for component in _CLAMP_COMPONENTS:
        values = clamp_component_counts[component]
        component_total = sum(values[state] for state in _CLAMP_STATES)
        component_rates[component] = {
            state: values[state] / component_total * 100.0
            if component_total
            else 0.0
            for state in _CLAMP_STATES
        }
    session_max_drawdown = _max_drawdown(equity_values)
    for equity in equity_values:
        if not math.isfinite(equity) or equity <= 0:
            continue
        chain_equity_peak = max(chain_equity_peak, equity)
        if chain_equity_peak > 0:
            chain_max_drawdown = max(
                chain_max_drawdown,
                (chain_equity_peak - equity) / chain_equity_peak * 10_000.0,
            )
    result = MarketReplaySessionResult(
        session_date=period.session_date,
        period_id=period.period_id,
        scheduled_open_utc=period.schedule_open_utc,
        scheduled_close_utc=period.schedule_close_utc,
        observed_start_utc=period.observed_start_utc,
        observed_end_utc=period.observed_end_utc,
        ticks=len(ticks),
        trades=trades_count,
        completed_trades=completed_trade_count,
        no_trade=not had_exposure,
        open_position=open_position,
        realized_return_bps=realized_return,
        marked_return_bps=marked_return,
        conservative_return_bps=conservative,
        # Preserve the historical field as the conservative continuity-chain
        # drawdown used by candidate scoring.  The new explicit local/chain
        # fields make the report semantics auditable.
        max_drawdown_bps=chain_max_drawdown,
        session_max_drawdown_bps=session_max_drawdown,
        chain_max_drawdown_bps=chain_max_drawdown,
        first_entry_utc=first_entry,
        last_exit_utc=last_exit,
        open_entry_setup=open_entry_setup,
        right_censored=right_censored,
        unmarked_open_position=unmarked_open_position,
        primary_eligible=period.primary_eligible,
        source_finalized=period.source_finalized,
        coverage_pct=period.coverage_pct,
        start_lag_seconds=period.start_lag_seconds,
        end_lead_seconds=period.end_lead_seconds,
        maximum_event_gap_seconds=period.maximum_event_gap_seconds,
        connectivity_event_count=period.connectivity_event_count,
        last_event_count=period.last_event_count,
        last_event_minute_coverage_pct=period.last_event_minute_coverage_pct,
        last_event_gap_p95_seconds=period.last_event_gap_p95_seconds,
        touch_liquidity_checks=touch_liquidity_checks,
        touch_liquidity_sufficient_checks=touch_liquidity_sufficient_checks,
        touch_liquidity_coverage_pct=touch_coverage,
        assumed_trade_notional=assumed_trade_notional,
        execution_cost_bps_per_side=max(buy_cost_bps, sell_cost_bps),
        buy_execution_cost_bps_per_side=buy_cost_bps,
        sell_execution_cost_bps_per_side=sell_cost_bps,
        total_execution_cost_bps=total_execution_cost_bps,
        clamp_min_count=clamp_counts["min"],
        clamp_max_count=clamp_counts["max"],
        clamp_raw_count=clamp_counts["raw"],
        clamp_zero_count=clamp_counts["zero"],
        clamp_total_count=clamp_total,
        clamp_min_rate_pct=(
            clamp_counts["min"] / clamp_total * 100.0 if clamp_total else 0.0
        ),
        clamp_max_rate_pct=(
            clamp_counts["max"] / clamp_total * 100.0 if clamp_total else 0.0
        ),
        clamp_component_counts={
            component: dict(values)
            for component, values in clamp_component_counts.items()
        },
        clamp_component_rates_pct=component_rates,
        issues=issues,
        continuity_chain_id=continuity_chain_id,
        continuity_broken_before=continuity_broken_before,
        continuity_break_reason=continuity_break_reason,
        carried_position_in=carried_position_in,
        carried_position_out=can_carry_open_position,
        carried_sell_trail_in=carried_sell_trail_in,
        carried_sell_trail_out=bool(
            can_carry_open_position and stage in {"SELL_TRAIL", "SELL_FILL_PENDING"}
        ),
        terminal_open_position=terminal_open_position,
        session_start_equity=session_start_equity,
        session_end_equity=session_end_equity,
        cumulative_end_equity=session_end_equity,
        overnight_gap_return_bps=overnight_gap_return,
        protective_exits=protective_exits_in_session,
        protective_cancellations=protective_cancellations_in_session,
        protective_trigger_pending_at_end=bool(
            open_position and stage == "PROTECTIVE_FILL_PENDING"
        ),
        carried_protective_trail_in=carried_protective_trail_in,
        carried_protective_trail_out=bool(
            can_carry_open_position
            and (
                protective_stop is not None
                or stage == "PROTECTIVE_FILL_PENDING"
            )
        ),
    )

    next_state = _ReplayCarryState(
        stage=stage,
        anchor=anchor,
        buy_stop=buy_stop,
        buy_running_low=buy_running_low,
        buy_trail_pct=buy_trail_pct,
        buy_placed_sequence=buy_placed_sequence,
        buy_fill_reference=buy_fill_reference,
        buy_price=buy_price,
        buy_cost_basis=buy_cost_basis,
        assumed_quantity=assumed_quantity,
        buy_time=buy_time,
        minimum_profit_pct=minimum_profit_pct,
        sell_trail_pct=sell_trail_pct,
        sell_stop=sell_stop,
        sell_running_high=sell_running_high,
        sell_placed_sequence=sell_placed_sequence,
        sell_fill_reference=sell_fill_reference,
        protective_sell_pct=protective_sell_pct,
        protective_stop=protective_stop,
        protective_initial_stop=protective_initial_stop,
        protective_running_high=protective_running_high,
        protective_placed_sequence=protective_placed_sequence,
        protective_fill_reference=protective_fill_reference,
        protective_normal_activation_price=protective_normal_activation_price,
        capital=capital,
        cycle_number=cycle_number,
        last_long_mark=last_long_mark,
        last_session_end_equity=session_end_equity,
        equity_peak=chain_equity_peak,
        maximum_drawdown_bps=chain_max_drawdown,
        active_trade=active_trade,
    )
    if not can_carry_open_position:
        if not terminal_open_position:
            next_state.stage = "WAIT_READY"
            next_state.anchor = None
            next_state.buy_stop = None
            next_state.buy_running_low = None
            next_state.buy_trail_pct = None
            next_state.buy_placed_sequence = 0
            next_state.buy_fill_reference = None
            next_state.buy_price = None
            next_state.buy_cost_basis = None
            next_state.assumed_quantity = 0
            next_state.buy_time = ""
            next_state.minimum_profit_pct = None
            next_state.sell_trail_pct = None
            next_state.sell_stop = None
            next_state.sell_running_high = None
            next_state.sell_placed_sequence = 0
            next_state.sell_fill_reference = None
            next_state.protective_sell_pct = None
            next_state.protective_stop = None
            next_state.protective_initial_stop = None
            next_state.protective_running_high = None
            next_state.protective_placed_sequence = 0
            next_state.protective_fill_reference = None
            next_state.protective_normal_activation_price = None
            next_state.last_long_mark = None
            next_state.active_trade = None
        next_state.last_session_end_equity = session_end_equity
    return result, trades, next_state


def _simulate_session(
    ticks: list[IbrecTick],
    period: IbrecPeriod,
    profile: AtrProfile,
    atr_values: list[float | None],
    min_tick: float,
    config: MarketReplayConfig | None = None,
    *,
    keep_trades: bool,
) -> tuple[MarketReplaySessionResult, list[MarketReplayTrade]]:
    """Replay one isolated RTH session for compatibility and focused tests."""

    result, trades, _ = _simulate_session_stateful(
        ticks,
        period,
        profile,
        atr_values,
        min_tick,
        config,
        keep_trades=keep_trades,
    )
    return result, trades


def _period_sort_key(item: tuple[IbrecPeriod, list[IbrecTick]]) -> tuple[Any, ...]:
    period, _ = item
    return (
        period.open_timestamp,
        period.session_date,
        period.source_recording_sha256,
        period.period_id,
    )


def _atr_cache_key(
    period: IbrecPeriod,
    atr_period: int,
    bar_seconds: int,
) -> AtrCacheKey:
    return (
        period.source_recording_sha256,
        period.session_date,
        period.period_id,
        atr_period,
        bar_seconds,
    )


def _evaluate_period_sequence(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    profile: AtrProfile,
    config: MarketReplayConfig,
    atr_provider: Callable[[IbrecPeriod, list[IbrecTick]], list[float | None]],
    *,
    keep_details: bool,
) -> tuple[list[MarketReplaySessionResult], list[MarketReplayTrade]]:
    """Replay periods chronologically, carrying only provably continuous longs."""

    normalized = config.normalized()
    ordered = sorted(period_ticks, key=_period_sort_key)
    sessions: list[MarketReplaySessionResult] = []
    trades: list[MarketReplayTrade] = []
    state: _ReplayCarryState | None = None
    chain_id = 1
    previous: IbrecPeriod | None = None

    for index, (period, ticks) in enumerate(ordered):
        broken_before = False
        break_reason = ""
        if previous is not None:
            if normalized.continuous_overnight_replay:
                continuous, break_reason = _continuity_between(previous, period)
            else:
                continuous = False
                break_reason = "Continuous overnight replay was disabled by configuration."
            if not continuous:
                broken_before = True
                chain_id += 1
                state = None

        following = ordered[index + 1][0] if index + 1 < len(ordered) else None
        carry_to_next = False
        if normalized.continuous_overnight_replay and following is not None:
            carry_to_next, _ = _continuity_between(period, following)

        result, session_trades, next_state = _simulate_session_stateful(
            ticks,
            period,
            profile,
            atr_provider(period, ticks),
            recording.min_tick,
            normalized,
            keep_trades=keep_details,
            state=state,
            carry_to_next=carry_to_next,
            continuity_chain_id=chain_id,
            continuity_broken_before=broken_before,
            continuity_break_reason=break_reason,
        )
        sessions.append(result)
        if keep_details:
            trades.extend(session_trades)

        # A terminal open position is deliberately not injected into a later
        # chain.  Flat state may continue across consecutive sessions so the
        # cumulative equity fields remain auditable, while the per-session
        # return calculation still starts from the preceding closing equity.
        # ``carry_to_next`` expresses schedule continuity, but an unresolved
        # position is carryable only when the session produced a valid closing
        # bid and explicitly marked it as carried.  Do not leak a terminal,
        # unmarked position into the next recording merely because the dates are
        # consecutive.
        if result.terminal_open_position:
            state = None
        else:
            state = next_state if carry_to_next or not next_state.has_open_position else None
        if not normalized.continuous_overnight_replay:
            state = None
        previous = period
    return sessions, trades


def _enrich_protective_trade_diagnostics(
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    sessions: list[MarketReplaySessionResult],
    trades: list[MarketReplayTrade],
) -> None:
    """Add descriptive post-stop recovery and avoided-loss evidence.

    These diagnostics never affect candidate ranking.  They answer the
    practical question that follows a protective exit: did the executable bid
    continue lower, or did it recover to the original BUY/normal activation
    level before the next modeled entry?  The observation is bounded by the
    same verified continuity chain and by the next BUY, so it never borrows a
    later independent trade or an unrecorded overnight path.
    """

    chain_by_period = {
        (session.session_date, session.period_id): session.continuity_chain_id
        for session in sessions
    }
    ticks_by_chain: dict[int, list[IbrecTick]] = defaultdict(list)
    for period, ticks in period_ticks:
        chain_id = chain_by_period.get((period.session_date, period.period_id))
        if chain_id is not None:
            ticks_by_chain[chain_id].extend(ticks)
    for values in ticks_by_chain.values():
        values.sort(key=lambda item: (item.timestamp, item.sequence))

    buys_by_chain: dict[int, list[float]] = defaultdict(list)
    for trade in trades:
        buy_timestamp = timestamp_seconds(trade.buy_time_utc)
        if buy_timestamp is not None:
            buys_by_chain[trade.continuity_chain_id].append(buy_timestamp)
    for values in buys_by_chain.values():
        values.sort()

    for trade in trades:
        if trade.exit_type != "protective" or trade.sell_price is None:
            continue
        sell_timestamp = timestamp_seconds(trade.sell_time_utc)
        if sell_timestamp is None:
            continue
        following_buys = [
            value
            for value in buys_by_chain.get(trade.continuity_chain_id, [])
            if value > sell_timestamp + 1e-9
        ]
        observation_end = min(following_buys) if following_buys else math.inf
        observed_ticks = ticks_by_chain.get(trade.continuity_chain_id, [])
        if following_buys:
            trade.protective_observation_end_utc = next(
                (
                    tick.captured_at_utc
                    for tick in observed_ticks
                    if tick.timestamp >= observation_end
                ),
                "",
            )
        elif observed_ticks:
            trade.protective_observation_end_utc = observed_ticks[-1].captured_at_utc
        future_bids = [
            bid
            for tick in observed_ticks
            if sell_timestamp < tick.timestamp < observation_end
            and (tick.full_snapshot or "bid" in tick.changed_fields)
            and (bid := tick.valid_bid()) is not None
        ]
        if not future_bids:
            continue
        future_min = min(future_bids)
        future_max = max(future_bids)
        trade.protective_recovered_to_buy = future_max + 1e-9 >= trade.buy_price
        threshold = trade.normal_activation_price_at_protective_exit
        trade.protective_reached_normal_activation = (
            future_max + 1e-9 >= threshold if threshold is not None else None
        )
        trade.protective_loss_avoided_bps = max(
            0.0,
            (trade.sell_price - future_min) / trade.buy_price * 10_000.0,
        )
        trade.protective_regret_bps = max(
            0.0,
            (future_max - trade.sell_price) / trade.buy_price * 10_000.0,
        )

def _summary(
    profile: AtrProfile,
    sessions: list[MarketReplaySessionResult],
    config: MarketReplayConfig | None = None,
) -> MarketReplayCandidateSummary:
    normalized = _normalized_replay_config(config)
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
            primary_eligible_sessions=0,
            excluded_quality_sessions=0,
            instability_reasons=["No analyzable RTH session was available."],
        )
    score_components = score_sessions(
        sessions,
        turnover_penalty_bps_per_completed_trade=(
            normalized.turnover_penalty_bps_per_completed_trade
        ),
        policy=BALANCED_SCORE_POLICY,
    )
    median_return = score_components["median_return_bps"]
    mean_return = score_components["mean_return_bps"]
    worst_return = score_components["worst_return_bps"]
    max_drawdown = score_components["maximum_drawdown_bps"]
    open_count = sum(1 for item in sessions if item.terminal_open_position)
    no_trade_count = sum(1 for item in sessions if item.no_trade)
    right_censored_count = sum(1 for item in sessions if item.right_censored)
    unmarked_open_count = sum(
        1 for item in sessions if item.unmarked_open_position
    )
    open_fraction = open_count / session_count
    no_trade_fraction = no_trade_count / session_count
    right_censored_fraction = right_censored_count / session_count
    completed_trades = sum(item.completed_trades for item in sessions)
    protective_exits = sum(item.protective_exits for item in sessions)
    protective_cancellations = sum(
        item.protective_cancellations for item in sessions
    )
    average_completed_trades = score_components["average_completed_trades"]
    turnover_penalty = score_components["turnover_penalty_points"]
    touch_checks = sum(item.touch_liquidity_checks for item in sessions)
    touch_sufficient = sum(
        item.touch_liquidity_sufficient_checks for item in sessions
    )
    touch_coverage = (
        touch_sufficient / touch_checks * 100.0 if touch_checks else None
    )
    clamp_min_count = sum(item.clamp_min_count for item in sessions)
    clamp_max_count = sum(item.clamp_max_count for item in sessions)
    clamp_raw_count = sum(item.clamp_raw_count for item in sessions)
    clamp_zero_count = sum(item.clamp_zero_count for item in sessions)
    clamp_total_count = sum(item.clamp_total_count for item in sessions)
    component_counts = {
        component: {state: 0 for state in _CLAMP_STATES}
        for component in _CLAMP_COMPONENTS
    }
    for session in sessions:
        for component in _CLAMP_COMPONENTS:
            values = session.clamp_component_counts.get(component, {})
            for state in _CLAMP_STATES:
                component_counts[component][state] += int(values.get(state, 0) or 0)
    component_rates: dict[str, dict[str, float]] = {}
    for component, values in component_counts.items():
        component_total = sum(values[state] for state in _CLAMP_STATES)
        component_rates[component] = {
            state: values[state] / component_total * 100.0
            if component_total
            else 0.0
            for state in _CLAMP_STATES
        }
    score = score_components["score"]
    return MarketReplayCandidateSummary(
        profile=profile,
        score=score,
        sessions=session_count,
        completed_sessions=sum(1 for item in sessions if not item.right_censored),
        sessions_with_trades=sum(1 for item in sessions if not item.no_trade),
        completed_trades=completed_trades,
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
        average_completed_trades_per_session=average_completed_trades,
        turnover_penalty_points=turnover_penalty,
        total_execution_cost_bps=sum(
            item.total_execution_cost_bps for item in sessions
        ),
        touch_liquidity_checks=touch_checks,
        touch_liquidity_sufficient_checks=touch_sufficient,
        touch_liquidity_coverage_pct=touch_coverage,
        clamp_min_count=clamp_min_count,
        clamp_max_count=clamp_max_count,
        clamp_raw_count=clamp_raw_count,
        clamp_zero_count=clamp_zero_count,
        clamp_total_count=clamp_total_count,
        clamp_min_rate_pct=(
            clamp_min_count / clamp_total_count * 100.0
            if clamp_total_count
            else 0.0
        ),
        clamp_max_rate_pct=(
            clamp_max_count / clamp_total_count * 100.0
            if clamp_total_count
            else 0.0
        ),
        clamp_component_counts=component_counts,
        clamp_component_rates_pct=component_rates,
        primary_eligible_sessions=sum(
            1 for item in sessions if item.primary_eligible
        ),
        excluded_quality_sessions=sum(
            1 for item in sessions if not item.primary_eligible
        ),
        protective_exits=protective_exits,
        protective_cancellations=protective_cancellations,
        protective_exit_rate_pct=(
            protective_exits / completed_trades * 100.0
            if completed_trades
            else 0.0
        ),
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
        protective_sell_mode="disabled",
        protective_sell_value=0.0,
    )


def _profile_with_protective_policy(
    profile: AtrProfile,
    policy: AtrProfile,
) -> AtrProfile:
    """Return ``profile`` with the protective policy from ``policy``.

    The policy layer is intentionally orthogonal to the normal ATR entry and
    profit-exit parameters.  Keeping the copy operation in one helper prevents
    stage 1/2 window probes or stage 3 refinement from silently falling back to
    the disabled default when they are evaluated under a protective policy.
    """

    return replace(
        profile,
        protective_sell_mode=policy.protective_sell_mode,
        protective_sell_value=policy.protective_sell_value,
    )


def _window_profile(
    config: MarketReplayConfig,
    period: int,
    bar_seconds: int,
    *,
    base_profile: AtrProfile | None = None,
) -> AtrProfile:
    control = base_profile or _control_profile(config)
    return AtrProfile(
        period=period,
        bar_seconds=bar_seconds,
        initial_drop_multiplier=control.initial_drop_multiplier,
        buy_rebound_multiplier=control.buy_rebound_multiplier,
        minimum_profit_multiplier=control.minimum_profit_multiplier,
        sell_trail_multiplier=control.sell_trail_multiplier,
        min_atr_pct=control.min_atr_pct,
        max_atr_pct=control.max_atr_pct,
        protective_sell_mode=control.protective_sell_mode,
        protective_sell_value=control.protective_sell_value,
    )


def _window_screen_profiles(
    config: MarketReplayConfig,
    period: int,
    bar_seconds: int,
    *,
    base_profile: AtrProfile | None = None,
) -> list[AtrProfile]:
    """Return the small representative multiplier set used in stages 1 and 2."""

    control = base_profile or _control_profile(config)
    profiles = {
        AtrProfile(
            period=period,
            bar_seconds=bar_seconds,
            initial_drop_multiplier=initial,
            buy_rebound_multiplier=buy,
            minimum_profit_multiplier=profit,
            sell_trail_multiplier=sell,
            min_atr_pct=control.min_atr_pct,
            max_atr_pct=control.max_atr_pct,
            protective_sell_mode=control.protective_sell_mode,
            protective_sell_value=control.protective_sell_value,
        )
        for initial, buy, profit, sell in _WINDOW_SCREEN_MULTIPLIERS
    }
    profiles.add(
        _window_profile(
            config,
            period,
            bar_seconds,
            base_profile=control,
        )
    )
    return sorted(profiles, key=lambda profile: profile.key())


def _best_window_candidates(
    profiles: Iterable[AtrProfile],
    summaries: dict[str, MarketReplayCandidateSummary],
) -> list[MarketReplayCandidateSummary]:
    """Choose the strongest representative profile for each ATR window."""

    return _best_candidates_by_window(summaries[profile.key()] for profile in profiles)


def _best_candidates_by_window(
    candidates: Iterable[MarketReplayCandidateSummary],
) -> list[MarketReplayCandidateSummary]:
    """Choose one deterministic strongest candidate per period/bar window."""

    by_window: dict[tuple[int, int], MarketReplayCandidateSummary] = {}
    for candidate in candidates:
        profile = candidate.profile
        window = (profile.period, profile.bar_seconds)
        previous = by_window.get(window)
        # Stages 1 and 2 are narrowing stages. A zero-trade profile can have a
        # deceptively attractive score of zero when every active profile lost
        # money, but it provides no entry/exit evidence for the later ATR
        # search. Prefer a representative that generated at least one trade;
        # fall back to the ordinary score ordering only when neither (or both)
        # profiles traded.
        candidate_key = (
            candidate.sessions_with_trades == 0,
            _candidate_sort_key(candidate),
        )
        previous_key = (
            previous.sessions_with_trades == 0,
            _candidate_sort_key(previous),
        ) if previous is not None else None
        if previous is None or previous_key is None or candidate_key < previous_key:
            by_window[window] = candidate
    return [by_window[window] for window in sorted(by_window)]


def _coarse_profiles(
    config: MarketReplayConfig,
    windows: Iterable[tuple[int, int]] | None = None,
    *,
    search_clamps: bool = True,
    policy_profiles: Iterable[AtrProfile] | None = None,
) -> list[AtrProfile]:
    normalized = config.normalized()
    selected_windows = tuple(sorted(set(windows or ((_CONTROL_PROFILE.period, _CONTROL_PROFILE.bar_seconds),))))
    minimum_candidates = (
        tuple(
            sorted(
                {
                    normalized.min_atr_pct,
                    *(
                        value
                        for value in _COARSE_MIN_ATR_PCT
                        if 0 < value < normalized.max_atr_pct
                    ),
                }
            )
        )
        if search_clamps
        else (normalized.min_atr_pct,)
    )
    policies = tuple(policy_profiles or (_control_profile(config),))
    profiles = {
        AtrProfile(
            period=period,
            bar_seconds=bar_seconds,
            initial_drop_multiplier=initial,
            buy_rebound_multiplier=buy,
            minimum_profit_multiplier=profit,
            sell_trail_multiplier=sell,
            min_atr_pct=min_atr_pct,
            max_atr_pct=normalized.max_atr_pct,
            protective_sell_mode=policy.protective_sell_mode,
            protective_sell_value=policy.protective_sell_value,
        )
        for (
            policy,
            (period, bar_seconds),
            initial,
            buy,
            profit,
            sell,
            min_atr_pct,
        ) in itertools.product(
            policies,
            selected_windows,
            _COARSE_INITIAL,
            _COARSE_BUY,
            _COARSE_PROFIT,
            _COARSE_SELL,
            minimum_candidates,
        )
    }
    profiles.add(_control_profile(config))
    return sorted(profiles, key=lambda profile: profile.key())


def _coarse_profiles_for_policy_windows(
    config: MarketReplayConfig,
    policy_windows: Iterable[tuple[AtrProfile, Iterable[tuple[int, int]]]],
    *,
    search_clamps: bool,
) -> list[AtrProfile]:
    """Build Stage 3 profiles only inside each policy's selected ATR windows.

    The disabled and enabled protective policies are narrowed independently in
    Stages 1 and 2. Taking a global union and crossing it with every policy
    would reintroduce period/bar combinations that did not advance under that
    policy, weakening the staged-search contract.
    """

    profiles = {
        profile
        for policy, windows in policy_windows
        for profile in _coarse_profiles(
            config,
            windows,
            search_clamps=search_clamps,
            policy_profiles=(policy,),
        )
    }
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
    # Validation only: ``normalized()`` raises on an invalid configuration.
    # Its return value is deliberately unused because refinement neighbours
    # are derived from the seed profiles, not from the configured defaults.
    config.normalized()
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
                    min_atr_pct=profile.min_atr_pct,
                    max_atr_pct=profile.max_atr_pct,
                    protective_sell_mode=profile.protective_sell_mode,
                    protective_sell_value=profile.protective_sell_value,
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

    groups: dict[
        tuple[int, int, str, float],
        list[MarketReplayCandidateSummary],
    ] = defaultdict(list)
    for candidate in sorted(candidates, key=_candidate_sort_key):
        profile = candidate.profile
        groups[
            (
                profile.period,
                profile.bar_seconds,
                profile.protective_sell_mode,
                profile.protective_sell_value,
            )
        ].append(candidate)
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
    """Delegate to the one shared interpolated-percentile implementation.

    Keeping a module-local name preserves existing call sites while ensuring
    every quantile in the application is computed by exactly one function
    (:func:`optimizer.utils.percentile`), so the copies can never drift.
    """

    return percentile(values, probability)


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


def _paired_session_list(
    candidate_sessions: Iterable[MarketReplaySessionResult],
    control_sessions: Iterable[MarketReplaySessionResult],
) -> list[tuple[MarketReplaySessionResult, MarketReplaySessionResult]]:
    """Return candidate/control pairs in deterministic session-identity order."""

    pairs_by_day = _paired_sessions_by_day(candidate_sessions, control_sessions)
    return [pair for day in sorted(pairs_by_day) for pair in pairs_by_day[day]]


def _paired_bootstrap_units(
    pairs_by_day: dict[
        str,
        list[tuple[MarketReplaySessionResult, MarketReplaySessionResult]],
    ],
) -> tuple[
    str,
    list[tuple[str, ...]],
]:
    """Return independent resampling units without breaking overnight exposure.

    Ordinary profiles that finish every day flat are resampled one trading day
    at a time.  If either candidate or control carries a position or active SELL
    order across an RTH boundary, the linked days are one continuity block.  A
    day bootstrap that split those observations would combine a position entry
    from one sample with an exit from another and would therefore understate the
    serial dependence introduced by continuous replay.
    """

    days = sorted(pairs_by_day)
    if not days:
        return "trading_day", []

    def carries_out(day: str) -> bool:
        return any(
            left.carried_position_out
            or left.carried_sell_trail_out
            or right.carried_position_out
            or right.carried_sell_trail_out
            for left, right in pairs_by_day[day]
        )

    def carries_in(day: str) -> bool:
        return any(
            left.carried_position_in
            or left.carried_sell_trail_in
            or right.carried_position_in
            or right.carried_sell_trail_in
            for left, right in pairs_by_day[day]
        )

    units: list[list[str]] = [[days[0]]]
    overnight_dependency = False
    for previous_day, current_day in zip(days, days[1:]):
        linked = carries_out(previous_day) and carries_in(current_day)
        if linked:
            units[-1].append(current_day)
            overnight_dependency = True
        else:
            units.append([current_day])
    unit_type = "overnight_continuity_block" if overnight_dependency else "trading_day"
    return unit_type, [tuple(unit) for unit in units]


def _bootstrap_units_across_profiles(
    session_groups: Iterable[Iterable[MarketReplaySessionResult]],
) -> tuple[str, list[tuple[str, ...]]]:
    """Build conservative resampling units from overnight links in any profile."""

    indexes = [_session_index(group) for group in session_groups]
    indexes = [index for index in indexes if index]
    if not indexes:
        return "trading_day", []
    identity_sets = [set(index) for index in indexes]
    if any(identity != identity_sets[0] for identity in identity_sets[1:]):
        raise MarketReplayAnalysisError(
            "Profiles used for bootstrap dependency analysis do not share identical RTH sessions."
        )
    days = sorted({day for day, _ in identity_sets[0]})
    by_group_day: list[dict[str, list[MarketReplaySessionResult]]] = []
    for index in indexes:
        grouped: dict[str, list[MarketReplaySessionResult]] = defaultdict(list)
        for (day, _period_id), session in sorted(index.items()):
            grouped[day].append(session)
        by_group_day.append(grouped)

    def linked(previous_day: str, current_day: str) -> bool:
        for grouped in by_group_day:
            previous = grouped.get(previous_day, [])
            current = grouped.get(current_day, [])
            carries_out = any(
                item.carried_position_out or item.carried_sell_trail_out
                for item in previous
            )
            carries_in = any(
                item.carried_position_in or item.carried_sell_trail_in
                for item in current
            )
            if carries_out and carries_in:
                return True
        return False

    units: list[list[str]] = [[days[0]]]
    dependency = False
    for previous_day, current_day in zip(days, days[1:]):
        if linked(previous_day, current_day):
            units[-1].append(current_day)
            dependency = True
        else:
            units.append([current_day])
    return (
        "overnight_continuity_block" if dependency else "trading_day",
        [tuple(unit) for unit in units],
    )


def _score_delta_for_pairs(
    candidate_profile: AtrProfile,
    control_profile: AtrProfile,
    pairs: Iterable[tuple[MarketReplaySessionResult, MarketReplaySessionResult]],
    config: MarketReplayConfig | None = None,
) -> float | None:
    materialized = list(pairs)
    if not materialized:
        return None
    candidate = _summary(
        candidate_profile,
        [left for left, _ in materialized],
        config,
    )
    control = _summary(
        control_profile,
        [right for _, right in materialized],
        config,
    )
    delta = candidate.score - control.score
    return delta if math.isfinite(delta) else None


def _candidate_robustness(
    candidate: MarketReplayCandidateSummary,
    candidate_sessions: list[MarketReplaySessionResult],
    control: MarketReplayCandidateSummary,
    control_sessions: list[MarketReplaySessionResult],
    config: MarketReplayConfig | None = None,
    *,
    seed: str,
    selection_rows: list[dict[str, Any]] | None = None,
    exact_leave_one_out: Callable[
        [str],
        tuple[list[MarketReplaySessionResult], list[MarketReplaySessionResult]],
    ]
    | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized = _normalized_replay_config(config)
    pairs_by_day = _paired_sessions_by_day(candidate_sessions, control_sessions)
    days = sorted(pairs_by_day)
    bootstrap_unit_type, bootstrap_units = _paired_bootstrap_units(pairs_by_day)
    all_pairs = [pair for day in days for pair in pairs_by_day[day]]
    observed_delta = _score_delta_for_pairs(
        candidate.profile,
        control.profile,
        all_pairs,
        config,
    )
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
        config,
    ) if all_pairs else None
    control_paired_summary = _summary(
        control.profile,
        [right for _, right in all_pairs],
        config,
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
    if len(bootstrap_units) >= 3:
        for sampled_indices in _deterministic_bootstrap_indices(
            seed,
            replicates=_BOOTSTRAP_REPLICATES,
            draws=len(bootstrap_units),
            size=len(bootstrap_units),
        ):
            sample: list[tuple[MarketReplaySessionResult, MarketReplaySessionResult]] = []
            for sampled_index in sampled_indices:
                for selected_day in bootstrap_units[sampled_index]:
                    sample.extend(pairs_by_day[selected_day])
            delta = _score_delta_for_pairs(
                candidate.profile,
                control.profile,
                sample,
                config,
            )
            if delta is not None:
                bootstrap_values.append(delta)

    leave_one_out_rows: list[dict[str, Any]] = []
    if len(days) >= 3:
        for omitted_day in days:
            if exact_leave_one_out is not None:
                exact_candidate, exact_control = exact_leave_one_out(omitted_day)
                exact_pairs_by_day = _paired_sessions_by_day(
                    exact_candidate,
                    exact_control,
                )
                remaining = [
                    pair
                    for day in sorted(exact_pairs_by_day)
                    for pair in exact_pairs_by_day[day]
                ]
            else:
                remaining = [
                    pair
                    for day in days
                    if day != omitted_day
                    for pair in pairs_by_day[day]
                ]
            delta = _score_delta_for_pairs(
                candidate.profile,
                control.profile,
                remaining,
                config,
            )
            leave_one_out_rows.append(
                {
                    "candidate_profile_key": candidate.profile.key(),
                    "control_profile_key": control.profile.key(),
                    "omitted_trading_day": omitted_day,
                    "remaining_trading_days": len(days) - 1,
                    "score_delta": delta,
                    "continuous_replay_recomputed": exact_leave_one_out is not None,
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
    overnight_dependency = bootstrap_unit_type == "overnight_continuity_block"
    selection_modes = {
        str(row.get("selection_mode") or "")
        for row in selection_rows or []
        if str(row.get("selection_mode") or "")
    }
    selection_mode = (
        sorted(selection_modes)[0]
        if len(selection_modes) == 1
        else "mixed_or_unavailable"
    )
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
    if len(bootstrap_units) < _MIN_ROBUST_TRADING_DAYS:
        noun = (
            "independent overnight-continuity blocks"
            if overnight_dependency
            else "independent trading-day units"
        )
        failure_reasons.append(
            f"Fewer than {_MIN_ROBUST_TRADING_DAYS} {noun} were available for bootstrap authorization."
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
            "The day-or-continuity-block bootstrap 80% interval does not remain entirely above zero."
        )
    if probability_positive is None or probability_positive < _MIN_BOOTSTRAP_POSITIVE_PCT:
        failure_reasons.append(
            "Fewer than "
            f"{_MIN_BOOTSTRAP_POSITIVE_PCT:.0f}% of day-or-continuity-block bootstrap replicates favor the candidate."
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
    if normalized.min_touch_liquidity_coverage_pct > 0 and (
        candidate_paired_summary is None
        or candidate_paired_summary.touch_liquidity_coverage_pct is None
        or candidate_paired_summary.touch_liquidity_coverage_pct
        < normalized.min_touch_liquidity_coverage_pct
    ):
        failure_reasons.append(
            "Recorded top-of-book size is unavailable or sufficient for fewer than "
            f"{normalized.min_touch_liquidity_coverage_pct:.0f}% of modeled BUY/SELL fills."
        )
    changed_components = _changed_clamp_components(candidate.profile, control.profile)
    saturated_components: list[str] = []
    if candidate_paired_summary is None:
        saturated_components.append("unavailable")
    elif sum(
        int(state_count)
        for component_counts in candidate_paired_summary.clamp_component_counts.values()
        for state_count in component_counts.values()
    ) > 0:
        for component in changed_components:
            rates = candidate_paired_summary.clamp_component_rates_pct.get(
                component,
                {},
            )
            if (
                float(rates.get("min", 0.0) or 0.0)
                >= _MAX_CHANGED_PROFILE_CLAMP_RATE_PCT
                or float(rates.get("max", 0.0) or 0.0)
                >= _MAX_CHANGED_PROFILE_CLAMP_RATE_PCT
            ):
                saturated_components.append(component)
    elif (
        candidate_paired_summary.clamp_min_rate_pct
        >= _MAX_CHANGED_PROFILE_CLAMP_RATE_PCT
        or candidate_paired_summary.clamp_max_rate_pct
        >= _MAX_CHANGED_PROFILE_CLAMP_RATE_PCT
    ):
        # Backwards-compatible fail-closed fallback for imported/test evidence
        # that predates per-component clamp accounting.
        saturated_components.append("aggregate")
    if saturated_components:
        failure_reasons.append(
            "The candidate reaches an ATR clamp for at least "
            f"{_MAX_CHANGED_PROFILE_CLAMP_RATE_PCT:.0f}% of observations in one or more changed components "
            f"({', '.join(saturated_components)}), so those settings are not identifiable."
        )

    evidence = {
        "candidate_profile_key": candidate.profile.key(),
        "control_profile_key": control.profile.key(),
        "paired_sessions": len(all_pairs),
        "paired_trading_days": len(days),
        "bootstrap_unit_type": bootstrap_unit_type,
        "bootstrap_independent_units": len(bootstrap_units),
        "bootstrap_unit_members": [list(unit) for unit in bootstrap_units],
        "overnight_dependency_detected": overnight_dependency,
        "observed_score_delta": observed_delta,
        "paired_median_return_delta_bps": paired_median_return_delta,
        "paired_positive_day_pct": paired_positive_day_pct,
        "candidate_trade_days": candidate_trade_days,
        "control_trade_days": control_trade_days,
        "control_trade_day_retention_pct": control_trade_day_retention_pct,
        "maximum_drawdown_delta_bps": maximum_drawdown_delta,
        "worst_return_delta_bps": worst_return_delta,
        "candidate_touch_liquidity_coverage_pct": (
            candidate_paired_summary.touch_liquidity_coverage_pct
            if candidate_paired_summary is not None
            else None
        ),
        "candidate_clamp_min_rate_pct": (
            candidate_paired_summary.clamp_min_rate_pct
            if candidate_paired_summary is not None
            else None
        ),
        "candidate_clamp_max_rate_pct": (
            candidate_paired_summary.clamp_max_rate_pct
            if candidate_paired_summary is not None
            else None
        ),
        "candidate_clamp_component_rates_pct": (
            candidate_paired_summary.clamp_component_rates_pct
            if candidate_paired_summary is not None
            else {}
        ),
        "candidate_clamp_saturated_components": saturated_components,
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
        "leave_one_day_out_selection_mode": selection_mode,
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
        f"{recording_sha256}|{control_profile.key()}|shared-day-or-continuity-block-bootstrap"
    )


def _apply_robustness(
    candidate: MarketReplayCandidateSummary,
    evidence: dict[str, Any],
) -> None:
    candidate.robustness_evaluated = True
    candidate.robustness_passed = bool(evidence.get("passed"))
    candidate.control_score_delta = evidence.get("observed_score_delta")
    candidate.paired_trading_days = int(evidence.get("paired_trading_days") or 0)
    candidate.bootstrap_unit_type = str(
        evidence.get("bootstrap_unit_type") or "trading_day"
    )
    candidate.bootstrap_independent_units = int(
        evidence.get("bootstrap_independent_units") or 0
    )
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
    candidate.leave_one_day_out_selection_mode = str(
        evidence.get("leave_one_day_out_selection_mode") or ""
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
    if (
        left.period,
        left.bar_seconds,
        left.min_atr_pct,
        left.max_atr_pct,
        left.protective_sell_mode,
        left.protective_sell_value,
    ) != (
        right.period,
        right.bar_seconds,
        right.min_atr_pct,
        right.max_atr_pct,
        right.protective_sell_mode,
        right.protective_sell_value,
    ):
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
                round(profile.min_atr_pct, 9),
                round(profile.max_atr_pct, 9),
                profile.protective_sell_mode,
                round(profile.protective_sell_value, 9),
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



def _boundary_extension_profiles(
    center: MarketReplayCandidateSummary,
    candidates: list[MarketReplayCandidateSummary],
) -> tuple[list[AtrProfile], list[dict[str, Any]]]:
    """Return deterministic outward probes when a region touches a search edge."""

    profile = center.profile
    all_profiles = [candidate.profile for candidate in candidates]
    same_window = [
        item
        for item in all_profiles
        if item.period == profile.period
        and item.bar_seconds == profile.bar_seconds
        and item.protective_sell_mode == profile.protective_sell_mode
        and abs(item.protective_sell_value - profile.protective_sell_value) <= 1e-9
    ]
    probes: list[AtrProfile] = []
    rows: list[dict[str, Any]] = []

    def add_probe(dimension: str, direction: str, value: float | int) -> None:
        replacement = {dimension: value}
        probe = replace(profile, **replacement)
        if probe.key() == profile.key() or probe.key() in {item.key() for item in probes}:
            return
        probes.append(probe)
        rows.append(
            {
                "profile_key": profile.key(),
                "dimension": dimension,
                "direction": direction,
                "boundary_value": getattr(profile, dimension),
                "probe_value": value,
                "probe_profile_key": probe.key(),
            }
        )

    periods = sorted({item.period for item in all_profiles})
    if periods and profile.period == periods[0]:
        gap = periods[1] - periods[0] if len(periods) > 1 else max(1, profile.period // 2)
        add_probe("period", "lower", max(2, profile.period - gap))
    if periods and profile.period == periods[-1]:
        gap = periods[-1] - periods[-2] if len(periods) > 1 else max(1, profile.period // 2)
        add_probe("period", "upper", min(100, profile.period + gap))

    bars = sorted({item.bar_seconds for item in all_profiles})
    if bars and profile.bar_seconds == bars[0]:
        add_probe("bar_seconds", "lower", max(5, profile.bar_seconds // 2))
    if bars and profile.bar_seconds == bars[-1]:
        add_probe("bar_seconds", "upper", min(900, profile.bar_seconds * 2))

    for dimension in (
        "initial_drop_multiplier",
        "buy_rebound_multiplier",
        "minimum_profit_multiplier",
        "sell_trail_multiplier",
    ):
        values = sorted({float(getattr(item, dimension)) for item in same_window})
        current = float(getattr(profile, dimension))
        if values and abs(current - values[0]) <= 1e-9 and current > 0.0:
            add_probe(dimension, "lower", max(0.0, current - _REFINEMENT_STEP))
        if values and abs(current - values[-1]) <= 1e-9:
            add_probe(dimension, "upper", min(10.0, current + _REFINEMENT_STEP))

    if center.clamp_min_rate_pct > 0.0:
        minimum_values = sorted({item.min_atr_pct for item in same_window})
        if minimum_values and abs(profile.min_atr_pct - minimum_values[0]) <= 1e-9:
            add_probe("min_atr_pct", "lower", max(0.01, profile.min_atr_pct / 2.0))
        if minimum_values and abs(profile.min_atr_pct - minimum_values[-1]) <= 1e-9:
            add_probe(
                "min_atr_pct",
                "upper",
                min(profile.max_atr_pct - 0.01, profile.min_atr_pct + 0.10),
            )
    if center.clamp_max_rate_pct > 0.0 and profile.max_atr_pct < 99.99:
        add_probe("max_atr_pct", "upper", min(99.99, profile.max_atr_pct * 1.5))
    return probes, rows


def _resolve_boundary_evidence(
    center: MarketReplayCandidateSummary,
    rows: list[dict[str, Any]],
    summaries: dict[str, MarketReplayCandidateSummary],
) -> dict[str, Any]:
    """Determine whether all outward probes fall outside the near-best plateau."""

    tolerance = max(2.0, abs(center.score) * 0.10)
    threshold = center.score - tolerance
    materialized: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for row in rows:
        summary = summaries.get(str(row["probe_profile_key"]))
        score = summary.score if summary is not None else None
        near_or_better = score is not None and score >= threshold
        materialized.append(
            {
                **row,
                "probe_score": score,
                "near_best_or_better": near_or_better,
            }
        )
        if summary is None or near_or_better:
            unresolved.append(str(row["dimension"]))
    return {
        "profile_key": center.profile.key(),
        "attempted": bool(rows),
        "resolved": not unresolved,
        "unresolved_dimensions": sorted(set(unresolved)),
        "near_best_threshold": threshold,
        "probes": materialized,
    }

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
    atr_cache: dict[AtrCacheKey, list[float | None]],
    profile: AtrProfile,
    config: MarketReplayConfig,
    *,
    keep_details: bool,
    summary_day_weights: dict[str, int] | None = None,
) -> tuple[MarketReplayCandidateSummary, list[MarketReplaySessionResult], list[MarketReplayTrade]]:
    def atr_provider(
        period: IbrecPeriod,
        _ticks: list[IbrecTick],
    ) -> list[float | None]:
        return atr_cache[_atr_cache_key(period, profile.period, profile.bar_seconds)]

    sessions, trades = _evaluate_period_sequence(
        recording,
        period_ticks,
        profile,
        config,
        atr_provider,
        keep_details=keep_details,
    )
    if keep_details and trades:
        _enrich_protective_trade_diagnostics(period_ticks, sessions, trades)
    summary_sessions = sessions
    if summary_day_weights is not None:
        summary_sessions = [
            session
            for session in sessions
            for _ in range(max(0, int(summary_day_weights.get(session.session_date, 0))))
        ]
    return _summary(profile, summary_sessions, config), sessions, trades


def _evaluate_profile_phase(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    profile: AtrProfile,
    phase_seconds: int,
    config: MarketReplayConfig,
) -> list[MarketReplaySessionResult]:
    def atr_provider(
        _period: IbrecPeriod,
        ticks: list[IbrecTick],
    ) -> list[float | None]:
        return _precompute_atr(
            ticks,
            profile.period,
            profile.bar_seconds,
            phase_seconds=phase_seconds,
        )

    sessions, _ = _evaluate_period_sequence(
        recording,
        period_ticks,
        profile,
        config,
        atr_provider,
        keep_details=False,
    )
    return sessions


def _phase_values(bar_seconds: int) -> tuple[int, ...]:
    return tuple(range(0, max(_ATR_PHASE_STEP_SECONDS, bar_seconds), _ATR_PHASE_STEP_SECONDS))


def _atr_phase_robustness(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    candidate: AtrProfile,
    control: AtrProfile,
    cache: dict[tuple[str, int], list[MarketReplaySessionResult]],
    config: MarketReplayConfig,
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
                config,
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
            delta = _score_delta_for_pairs(candidate, control, pairs, config)
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
        config,
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
    atr_cache: dict[AtrCacheKey, list[float | None]],
    windows: Iterable[tuple[int, int]],
    *,
    progress: ProgressCallback | None,
    message: str,
) -> None:
    missing = [
        (ibrec_period, atr_period, bar_seconds, ticks)
        for ibrec_period, ticks in period_ticks
        for atr_period, bar_seconds in sorted(set(windows))
        if _atr_cache_key(ibrec_period, atr_period, bar_seconds) not in atr_cache
    ]
    total = len(missing)
    for index, (ibrec_period, atr_period, bar_seconds, ticks) in enumerate(
        missing,
        start=1,
    ):
        atr_cache[_atr_cache_key(ibrec_period, atr_period, bar_seconds)] = _precompute_atr(
            ticks,
            atr_period,
            bar_seconds,
        )
        _emit(progress, message, index, total)


def _evaluate_profiles(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    atr_cache: dict[AtrCacheKey, list[float | None]],
    profiles: Iterable[AtrProfile],
    summaries: dict[str, MarketReplayCandidateSummary],
    sessions_by_profile: dict[str, list[MarketReplaySessionResult]],
    config: MarketReplayConfig,
    *,
    progress: ProgressCallback | None,
    message: str,
    summary_day_weights: dict[str, int] | None = None,
) -> None:
    pending = [profile for profile in profiles if profile.key() not in summaries]
    total = len(pending)
    for index, profile in enumerate(pending, start=1):
        summary, sessions, _ = _evaluate_profile(
            recording,
            period_ticks,
            atr_cache,
            profile,
            config,
            keep_details=False,
            summary_day_weights=summary_day_weights,
        )
        summaries[profile.key()] = summary
        sessions_by_profile[profile.key()] = sessions
        _emit(progress, message, index, total)


def _window_search_rows(
    stage: str,
    candidates: list[MarketReplayCandidateSummary],
    selected_windows: set[tuple[int, int]],
    explanation: str,
    *,
    policy: AtrProfile | None = None,
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
                "protective_sell_mode": candidate.profile.protective_sell_mode,
                "protective_sell_value": candidate.profile.protective_sell_value,
                "protective_policy_label": candidate.profile.protective_policy_label,
                "window_search_policy": (
                    policy.protective_policy_label if policy is not None else "Disabled"
                ),
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
    atr_cache: dict[AtrCacheKey, list[float | None]],
    summaries: dict[str, MarketReplayCandidateSummary],
    sessions_by_profile: dict[str, list[MarketReplaySessionResult]],
    prepare_leave_one_out: bool,
    *,
    progress: ProgressCallback | None,
    summary_day_weights: dict[str, int] | None = None,
    base_profile: AtrProfile | None = None,
) -> tuple[
    list[tuple[int, int]],
    list[dict[str, Any]],
    list[AtrProfile],
    list[AtrProfile],
]:
    policy = base_profile or _control_profile(config)
    control_window = (policy.period, policy.bar_seconds)

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
        profile
        for period, bar_seconds in stage1_windows
        for profile in _window_screen_profiles(
            config,
            period,
            bar_seconds,
            base_profile=policy,
        )
    ]
    _evaluate_profiles(
        recording,
        period_ticks,
        atr_cache,
        stage1_profiles,
        summaries,
        sessions_by_profile,
        config,
        progress=progress,
        message="Stage 1 of 3: comparing ATR bar durations",
        summary_day_weights=summary_day_weights,
    )
    stage1_candidates = _best_window_candidates(stage1_profiles, summaries)
    selected_bars = _select_stage1_bars(stage1_candidates)
    rows = _window_search_rows(
        "1_bar_duration",
        stage1_candidates,
        {(_STAGE1_FIXED_PERIOD, value) for value in selected_bars},
        "Period was fixed at 14. Each bar duration was screened with the same small representative multiplier mini-grid; the strongest result for each duration determined advancement.",
        policy=policy,
    )

    # Precompute every period/bar pair so leave-one-day-out reruns can select a
    # different stage-1 bar without silently removing its stage-2 competitors.
    stage2_bars = (
        _STAGE1_BAR_SECONDS if prepare_leave_one_out else tuple(selected_bars)
    )
    all_stage2_windows = {
        (period, bar_seconds)
        for bar_seconds in stage2_bars
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
        profile
        for period, bar_seconds in sorted(all_stage2_windows)
        for profile in _window_screen_profiles(
            config,
            period,
            bar_seconds,
            base_profile=policy,
        )
    ]
    _evaluate_profiles(
        recording,
        period_ticks,
        atr_cache,
        all_stage2_profiles,
        summaries,
        sessions_by_profile,
        config,
        progress=progress,
        message="Stage 2 of 3: comparing ATR periods",
        summary_day_weights=summary_day_weights,
    )
    stage2_profiles = [
        profile
        for profile in all_stage2_profiles
        if profile.bar_seconds in selected_bars
        or (profile.period, profile.bar_seconds) == control_window
    ]
    stage2_candidates = _best_window_candidates(stage2_profiles, summaries)
    selected_windows = _select_stage2_windows(stage2_candidates)
    selected_window_set = set(selected_windows)
    rows.extend(
        _window_search_rows(
            "2_period",
            stage2_candidates,
            selected_window_set,
            "Periods were compared only inside the strongest stage-1 bar durations. Every period/bar window used the same representative multiplier mini-grid; the unchanged 14×60 control window was always retained.",
            policy=policy,
        )
    )
    rows.extend(
        _window_search_rows(
            "3_multiplier_search_windows",
            [
                next(
                    candidate
                    for candidate in stage2_candidates
                    if (
                        candidate.profile.period,
                        candidate.profile.bar_seconds,
                    )
                    == (period, bar_seconds)
                )
                for period, bar_seconds in selected_windows
            ],
            selected_window_set,
            "Only these narrowed ATR windows entered the full entry/exit multiplier grid and local multiplier refinement.",
            policy=policy,
        )
    )
    return selected_windows, rows, stage1_profiles, all_stage2_profiles


def _protective_policy_profiles(config: MarketReplayConfig) -> list[AtrProfile]:
    """Return the bounded protective-policy screen around the control ATR profile."""

    control = _control_profile(config)
    profiles = {control}
    profiles.update(
        replace(
            control,
            protective_sell_mode="manual",
            protective_sell_value=value,
        )
        for value in _PROTECTIVE_MANUAL_TRAILS
    )
    profiles.update(
        replace(
            control,
            protective_sell_mode="atr",
            protective_sell_value=value,
        )
        for value in _PROTECTIVE_ATR_MULTIPLIERS
    )
    return sorted(profiles, key=lambda item: item.key())


def _protective_policy_components(
    candidates: list[MarketReplayCandidateSummary],
) -> list[list[MarketReplayCandidateSummary]]:
    """Return supported adjacent near-best manual/ATR policy components."""

    components: list[list[MarketReplayCandidateSummary]] = []
    for mode, step in (
        ("manual", _PROTECTIVE_MANUAL_STEP),
        ("atr", _PROTECTIVE_ATR_STEP),
    ):
        mode_candidates = sorted(
            (
                item
                for item in candidates
                if item.profile.protective_sell_mode == mode
            ),
            key=lambda item: (
                item.profile.protective_sell_value,
                item.profile.key(),
            ),
        )
        if not mode_candidates:
            continue
        best_score = max(item.score for item in mode_candidates)
        tolerance = max(2.0, abs(best_score) * 0.10)
        near = [
            item for item in mode_candidates if item.score >= best_score - tolerance
        ]
        current: list[MarketReplayCandidateSummary] = []
        for candidate in near:
            if not current:
                current = [candidate]
                continue
            previous = current[-1]
            difference = (
                candidate.profile.protective_sell_value
                - previous.profile.protective_sell_value
            )
            if 1e-9 < difference <= step + 1e-9:
                current.append(candidate)
            else:
                if len(current) >= _PROTECTIVE_MIN_REGION_SIZE:
                    components.append(current)
                current = [candidate]
        if len(current) >= _PROTECTIVE_MIN_REGION_SIZE:
            components.append(current)
    return components


def _protective_policy_center(
    component: list[MarketReplayCandidateSummary],
) -> MarketReplayCandidateSummary:
    values = [item.profile.protective_sell_value for item in component]
    center_value = float(statistics.median(values))
    return min(
        component,
        key=lambda item: (
            abs(item.profile.protective_sell_value - center_value),
            -item.score,
            item.profile.key(),
        ),
    )


def _select_protective_policy(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    config: MarketReplayConfig,
    atr_cache: dict[AtrCacheKey, list[float | None]],
    summaries: dict[str, MarketReplayCandidateSummary],
    sessions_by_profile: dict[str, list[MarketReplaySessionResult]],
    *,
    progress: ProgressCallback | None,
    summary_day_weights: dict[str, int] | None = None,
    collect_diagnostics: bool = False,
) -> tuple[list[AtrProfile], list[dict[str, Any]], str]:
    """Choose at most one protective policy for conditional ATR optimization.

    This is a bounded risk-policy screen at the unchanged ATR profile.  It is
    deliberately separate from the later ATR search so a large combined grid
    cannot manufacture an apparently precise stop policy from sparse data.
    The final robust authorization still compares the complete selected
    profile against the disabled control.
    """

    control = _control_profile(config)
    if not config.normalized().protective_policy_search_enabled:
        return [control], [
            {
                "mode": "disabled",
                "value": 0.0,
                "label": control.protective_policy_label,
                "profile_key": control.key(),
                "selected_for_atr_search": True,
                "eligible": True,
                "decision": "Protective-policy search was disabled by configuration.",
            }
        ], "Protective-policy search was disabled; only the no-stop control was evaluated."

    profiles = _protective_policy_profiles(config)
    _ensure_atr_windows(
        period_ticks,
        atr_cache,
        [(control.period, control.bar_seconds)],
        progress=progress,
        message="Protective policy: reconstructing control ATR window",
    )
    _evaluate_profiles(
        recording,
        period_ticks,
        atr_cache,
        profiles,
        summaries,
        sessions_by_profile,
        config,
        progress=progress,
        message="Protective policy: comparing disabled, manual, and ATR-adaptive trails",
        summary_day_weights=summary_day_weights,
    )
    candidates = [summaries[profile.key()] for profile in profiles]
    disabled = summaries[control.key()]
    trading_days = len({period.session_date for period, _ in period_ticks})
    component_rows: dict[str, tuple[str, int]] = {}
    centers: list[MarketReplayCandidateSummary] = []
    for component in _protective_policy_components(candidates):
        center = _protective_policy_center(component)
        region_id = "PROTECTIVE-" + hashlib.sha256(
            "|".join(item.profile.key() for item in component).encode("utf-8")
        ).hexdigest()[:10].upper()
        for item in component:
            component_rows[item.profile.key()] = (region_id, len(component))
        centers.append(center)

    eligible: list[MarketReplayCandidateSummary] = []
    decisions: dict[str, list[str]] = defaultdict(list)
    for center in centers:
        sessions = sessions_by_profile.get(center.profile.key(), [])
        protective_days = len(
            {
                item.session_date
                for item in sessions
                if item.protective_exits > 0
            }
        )
        score_delta = center.score - disabled.score
        if trading_days < _MIN_ROBUST_TRADING_DAYS:
            decisions[center.profile.key()].append(
                f"Only {trading_days} trading day(s) were available; at least {_MIN_ROBUST_TRADING_DAYS} are required."
            )
        if center.protective_exits < 3 or protective_days < 3:
            decisions[center.profile.key()].append(
                "Fewer than three protective exits across three trading days were observed."
            )
        if score_delta < _MIN_PRACTICAL_SCORE_DELTA:
            decisions[center.profile.key()].append(
                f"The score improvement ({score_delta:.3f}) was below the {_MIN_PRACTICAL_SCORE_DELTA:.3f}-point practical threshold."
            )
        if (
            center.maximum_drawdown_bps
            > disabled.maximum_drawdown_bps + _MAX_DRAWDOWN_DETERIORATION_BPS
        ):
            decisions[center.profile.key()].append(
                "Maximum drawdown deteriorated materially versus the disabled control."
            )
        if (
            center.worst_return_bps
            < disabled.worst_return_bps - _MAX_WORST_RETURN_DETERIORATION_BPS
        ):
            decisions[center.profile.key()].append(
                "Worst-session return deteriorated materially versus the disabled control."
            )
        if center.profile.protective_sell_mode == "atr":
            rates = center.clamp_component_rates_pct.get("protective_sell", {})
            if max(float(rates.get("min", 0.0)), float(rates.get("max", 0.0))) >= _MAX_CHANGED_PROFILE_CLAMP_RATE_PCT:
                decisions[center.profile.key()].append(
                    "The ATR-adaptive protective trail was clamp-bound at least 90% of the time, so its multiplier was not identifiable."
                )
        if not decisions[center.profile.key()]:
            center.protective_policy_stable = True
            eligible.append(center)

    selected = (
        min(
            eligible,
            key=lambda item: (
                -(item.score - disabled.score),
                item.maximum_drawdown_bps - disabled.maximum_drawdown_bps,
                -item.worst_return_bps,
                _profile_distance(item.profile, control),
                item.profile.key(),
            ),
        )
        if eligible
        else disabled
    )
    diagnostic_rows: dict[str, dict[str, Any]] = {}
    if collect_diagnostics:
        for profile in profiles:
            _detail, _sessions, trades = _evaluate_profile(
                recording,
                period_ticks,
                atr_cache,
                profile,
                config,
                keep_details=True,
                summary_day_weights=summary_day_weights,
            )
            protective_trades = [
                trade for trade in trades if trade.exit_type == "protective"
            ]
            recovery_observed = [
                trade
                for trade in protective_trades
                if trade.protective_recovered_to_buy is not None
            ]
            activation_observed = [
                trade
                for trade in protective_trades
                if trade.protective_reached_normal_activation is not None
            ]
            loss_avoided = [
                float(trade.protective_loss_avoided_bps)
                for trade in protective_trades
                if trade.protective_loss_avoided_bps is not None
                and math.isfinite(trade.protective_loss_avoided_bps)
            ]
            regret = [
                float(trade.protective_regret_bps)
                for trade in protective_trades
                if trade.protective_regret_bps is not None
                and math.isfinite(trade.protective_regret_bps)
            ]
            recovered_count = sum(
                1 for trade in recovery_observed if trade.protective_recovered_to_buy
            )
            activation_count = sum(
                1
                for trade in activation_observed
                if trade.protective_reached_normal_activation
            )
            diagnostic_rows[profile.key()] = {
                "diagnostic_protective_exits": len(protective_trades),
                "recovery_observed_exits": len(recovery_observed),
                "recovered_to_buy_count": recovered_count,
                "recovered_to_buy_pct": (
                    recovered_count / len(recovery_observed) * 100.0
                    if recovery_observed
                    else None
                ),
                "normal_activation_observed_exits": len(activation_observed),
                "later_reached_normal_activation_count": activation_count,
                "later_reached_normal_activation_pct": (
                    activation_count / len(activation_observed) * 100.0
                    if activation_observed
                    else None
                ),
                "median_further_loss_avoided_bps": (
                    float(statistics.median(loss_avoided)) if loss_avoided else None
                ),
                "median_recovery_regret_bps": (
                    float(statistics.median(regret)) if regret else None
                ),
                "overnight_protective_exits": sum(
                    1
                    for trade in protective_trades
                    if trade.overnight_sessions_held > 0
                ),
            }
    evidence: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=_candidate_sort_key):
        region_id, region_size = component_rows.get(candidate.profile.key(), ("", 0))
        reasons = decisions.get(candidate.profile.key(), [])
        evidence.append(
            {
                "mode": candidate.profile.protective_sell_mode,
                "value": candidate.profile.protective_sell_value,
                "label": candidate.profile.protective_policy_label,
                "profile_key": candidate.profile.key(),
                "score": candidate.score,
                "score_delta_vs_disabled": candidate.score - disabled.score,
                "median_return_bps": candidate.median_return_bps,
                "mean_return_bps": candidate.mean_return_bps,
                "worst_return_bps": candidate.worst_return_bps,
                "maximum_drawdown_bps": candidate.maximum_drawdown_bps,
                "completed_trades": candidate.completed_trades,
                "protective_exits": candidate.protective_exits,
                "protective_cancellations": candidate.protective_cancellations,
                "protective_exit_rate_pct": candidate.protective_exit_rate_pct,
                "stable_region_id": region_id,
                "stable_region_size": region_size,
                "stable_region_center": candidate in centers,
                "selected_policy": candidate is selected,
                "eligible": candidate in eligible or candidate.profile.protective_sell_mode == "disabled",
                "selected_for_atr_search": candidate.profile.key() in {
                    disabled.profile.key(),
                    selected.profile.key(),
                },
                "decision": (
                    "Passed the bounded protective-policy screen."
                    if candidate in eligible
                    else "Disabled reference policy."
                    if candidate.profile.protective_sell_mode == "disabled"
                    else "; ".join(reasons)
                    if reasons
                    else "Not the center of a supported adjacent policy region."
                ),
                **diagnostic_rows.get(candidate.profile.key(), {}),
            }
        )
    if selected is disabled:
        reason = (
            "No enabled protective SELL policy passed the bounded stability, sample, score, tail-risk, and clamp-identifiability screen. "
            "The full ATR search therefore retained the disabled control only."
        )
        return [control], evidence, reason
    reason = (
        f"{selected.profile.protective_policy_label} was the center of the strongest supported protective-policy region at the unchanged ATR profile. "
        "The full ATR search evaluated that policy and the disabled control separately."
    )
    return [control, selected.profile], evidence, reason


def _run_bounded_selector(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    config: MarketReplayConfig,
    *,
    progress: ProgressCallback | None = None,
    day_weights: dict[str, int] | None = None,
) -> _BoundedSelectionRun:
    """Rerun all three search stages on one exact chronological subset."""

    if not period_ticks:
        raise MarketReplayAnalysisError(
            "The bounded selector requires at least one Market Replay period."
        )
    atr_cache: dict[AtrCacheKey, list[float | None]] = {}
    summaries: dict[str, MarketReplayCandidateSummary] = {}
    sessions_by_profile: dict[str, list[MarketReplaySessionResult]] = {}
    policy_profiles, protective_evidence, protective_reason = _select_protective_policy(
        recording,
        period_ticks,
        config,
        atr_cache,
        summaries,
        sessions_by_profile,
        progress=progress,
        summary_day_weights=day_weights,
    )
    selected_window_set: set[tuple[int, int]] = set()
    policy_window_sets: list[tuple[AtrProfile, list[tuple[int, int]]]] = []
    window_search: list[dict[str, Any]] = []
    for policy in policy_profiles:
        policy_windows, policy_rows, _, _ = _select_windows(
            recording,
            period_ticks,
            config,
            atr_cache,
            summaries,
            sessions_by_profile,
            False,
            progress=progress,
            summary_day_weights=day_weights,
            base_profile=policy,
        )
        selected_window_set.update(policy_windows)
        policy_window_sets.append((policy, policy_windows))
        window_search.extend(policy_rows)
    selected_windows = sorted(selected_window_set)
    profiles = _coarse_profiles_for_policy_windows(
        config,
        policy_window_sets,
        search_clamps=True,
    )
    _evaluate_profiles(
        recording,
        period_ticks,
        atr_cache,
        profiles,
        summaries,
        sessions_by_profile,
        config,
        progress=progress,
        message="Rerunning stage 3 multiplier search",
        summary_day_weights=day_weights,
    )
    coarse_candidates = [summaries[profile.key()] for profile in profiles]
    refined = [
        profile
        for profile in _refined_profiles(
            _refinement_seeds(coarse_candidates),
            config,
        )
        if profile.key() not in summaries
    ]
    if refined:
        _evaluate_profiles(
            recording,
            period_ticks,
            atr_cache,
            refined,
            summaries,
            sessions_by_profile,
            config,
            progress=progress,
            message="Rerunning local multiplier refinement",
            summary_day_weights=day_weights,
        )
    candidate_keys = {profile.key() for profile in profiles}
    candidate_keys.update(profile.key() for profile in refined)
    control_profile = _control_profile(config)
    if control_profile.key() not in summaries:
        _ensure_atr_windows(
            period_ticks,
            atr_cache,
            [(control_profile.period, control_profile.bar_seconds)],
            progress=progress,
            message="Reconstructing control ATR window",
        )
        _evaluate_profiles(
            recording,
            period_ticks,
            atr_cache,
            [control_profile],
            summaries,
            sessions_by_profile,
            config,
            progress=progress,
            message="Evaluating control profile",
            summary_day_weights=day_weights,
        )
    candidate_keys.add(control_profile.key())
    candidates = sorted(
        (summaries[key] for key in candidate_keys),
        key=_candidate_sort_key,
    )
    trading_days = len({period.session_date for period, _ in period_ticks})
    selected, reason = _stable_recommendation(
        candidates,
        sessions=trading_days,
        control_profile=control_profile,
    )
    return _BoundedSelectionRun(
        selected=selected,
        reason=reason,
        control=summaries[control_profile.key()],
        selected_windows=selected_windows,
        candidates=candidates,
        sessions_by_profile=sessions_by_profile,
        atr_cache=atr_cache,
        window_search=window_search,
        protective_policy_evidence=protective_evidence,
        protective_policy_reason=protective_reason,
    )


def _subset_candidate(
    profile: AtrProfile,
    sessions_by_profile: dict[str, list[MarketReplaySessionResult]],
    omitted_day: str,
    config: MarketReplayConfig,
) -> MarketReplayCandidateSummary | None:
    sessions = [
        session
        for session in sessions_by_profile.get(profile.key(), [])
        if session.session_date != omitted_day
    ]
    return _summary(profile, sessions, config) if sessions else None


def _leave_one_day_out_selection_rows(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    config: MarketReplayConfig,
) -> list[dict[str, Any]]:
    """Rerun the complete selector after removing each trading date.

    Filtering the raw chronological periods before replay is essential when an
    overnight position exists: removing a date can split a continuity chain and
    must reset state at the new boundary.  Reusing full-run session summaries
    would retain state that depends on the omitted market path.
    """

    days = sorted({period.session_date for period, _ in period_ticks})
    if len(days) < 3:
        return []
    rows: list[dict[str, Any]] = []
    for omitted_day in days:
        subset = [
            item for item in period_ticks if item[0].session_date != omitted_day
        ]
        if not subset:
            continue
        run = _run_bounded_selector(recording, subset, config)
        control_score = run.control.score
        selected_score = run.selected.score
        rows.append(
            {
                "omitted_trading_day": omitted_day,
                "omitted_period_ids": sorted(
                    period.period_id
                    for period, _ in period_ticks
                    if period.session_date == omitted_day
                ),
                "remaining_trading_days": len(
                    {period.session_date for period, _ in subset}
                ),
                "selected_windows": [
                    {"period": period, "bar_seconds": bar_seconds}
                    for period, bar_seconds in run.selected_windows
                ],
                "available_stage3_windows": [
                    {"period": period, "bar_seconds": bar_seconds}
                    for period, bar_seconds in sorted(
                        {
                            (
                                candidate.profile.period,
                                candidate.profile.bar_seconds,
                            )
                            for candidate in run.candidates
                        }
                    )
                ],
                "selected_profile_key": run.selected.profile.key(),
                "selected_period": run.selected.profile.period,
                "selected_profile_bar_seconds": run.selected.profile.bar_seconds,
                "selected_score": selected_score,
                "selected_score_delta_vs_control": selected_score - control_score,
                "selection_reason": run.reason,
                "selection_mode": "exact_chronology_rebuilt",
                "stage3_search_scope": (
                    "Removed the date before replay, rebuilt overnight continuity, reran stages 1 and 2, "
                    "rebuilt the complete stage-3 grid, and performed omission-specific local refinement."
                ),
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



def _evaluate_profile_fresh(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    profile: AtrProfile,
    config: MarketReplayConfig,
) -> tuple[MarketReplayCandidateSummary, list[MarketReplaySessionResult]]:
    """Evaluate one profile with ATR and continuity rebuilt for this exact subset."""

    cache: dict[AtrCacheKey, list[float | None]] = {}
    _ensure_atr_windows(
        period_ticks,
        cache,
        [(profile.period, profile.bar_seconds)],
        progress=None,
        message="Reconstructing validation ATR window",
    )
    summary, sessions, _ = _evaluate_profile(
        recording,
        period_ticks,
        cache,
        profile,
        config,
        keep_details=False,
    )
    return summary, sessions


def _continuity_block_comparison(
    candidate_sessions: list[MarketReplaySessionResult],
    control_sessions: list[MarketReplaySessionResult],
) -> dict[str, Any]:
    """Compare candidate/control outcomes on complete economic continuity blocks."""

    candidate_rows = continuity_block_metrics(candidate_sessions)
    control_rows = continuity_block_metrics(control_sessions)

    def index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        indexed: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row["start_session_date"]), str(row["end_session_date"]))
            if key in indexed:
                raise MarketReplayAnalysisError(
                    f"Duplicate economic continuity block evidence for {key[0]} through {key[1]}."
                )
            indexed[key] = row
        return indexed

    candidate_index = index(candidate_rows)
    control_index = index(control_rows)
    common = sorted(set(candidate_index) & set(control_index))
    rows: list[dict[str, Any]] = []
    for key in common:
        candidate = candidate_index[key]
        control = control_index[key]
        rows.append(
            {
                "start_session_date": key[0],
                "end_session_date": key[1],
                "candidate_return_bps": candidate["block_return_bps"],
                "control_return_bps": control["block_return_bps"],
                "return_delta_bps": (
                    float(candidate["block_return_bps"])
                    - float(control["block_return_bps"])
                ),
                "candidate_maximum_drawdown_bps": candidate["maximum_drawdown_bps"],
                "control_maximum_drawdown_bps": control["maximum_drawdown_bps"],
                "drawdown_delta_bps": (
                    float(candidate["maximum_drawdown_bps"])
                    - float(control["maximum_drawdown_bps"])
                ),
                "candidate_right_censored": candidate["right_censored"],
                "control_right_censored": control["right_censored"],
                "candidate_terminal_open_position": candidate["terminal_open_position"],
                "control_terminal_open_position": control["terminal_open_position"],
                "candidate": candidate,
                "control": control,
            }
        )
    return_deltas = [float(row["return_delta_bps"]) for row in rows]
    reasons: list[str] = []
    if set(candidate_index) != set(control_index):
        reasons.append(
            "Candidate and control did not produce identical economic continuity-block boundaries."
        )
    if not rows:
        reasons.append("No comparable economic continuity blocks were available.")
    if return_deltas and float(statistics.median(return_deltas)) <= 0.0:
        reasons.append(
            "Median candidate-minus-control return across economic continuity blocks was not positive."
        )
    if any(
        float(row["drawdown_delta_bps"]) > _MAX_DRAWDOWN_DETERIORATION_BPS
        for row in rows
    ):
        reasons.append(
            "At least one economic continuity block materially worsened maximum drawdown."
        )
    if any(
        bool(row["candidate_right_censored"])
        and not bool(row["control_right_censored"])
        for row in rows
    ):
        reasons.append(
            "The candidate introduced right-censoring in an economic block completed by the control."
        )
    if any(
        bool(row["candidate_terminal_open_position"])
        and not bool(row["control_terminal_open_position"])
        for row in rows
    ):
        reasons.append(
            "The candidate left an economic block open when the control finished flat."
        )
    return {
        "evaluated": bool(rows),
        "passed": bool(rows) and not reasons,
        "candidate_blocks": candidate_rows,
        "control_blocks": control_rows,
        "paired_blocks": len(rows),
        "median_return_delta_bps": (
            float(statistics.median(return_deltas)) if return_deltas else None
        ),
        "worst_return_delta_bps": min(return_deltas) if return_deltas else None,
        "rows": rows,
        "failure_reasons": reasons,
    }


def _rebase_validation_drawdown(
    sessions: Iterable[MarketReplaySessionResult],
) -> list[MarketReplaySessionResult]:
    """Remove training-period drawdown history from validation-only scoring.

    Session returns are already local to each RTH period, but continuous replay
    carries the chain maximum drawdown into later sessions.  Walk-forward
    validation must start its drawdown peak at the validation boundary rather
    than inheriting losses or peaks from the training interval.
    """

    ordered = sorted(sessions, key=lambda item: (item.session_date, item.period_id))
    if not ordered:
        return []
    peak = float(ordered[0].session_start_equity)
    running_drawdown = 0.0
    rebased: list[MarketReplaySessionResult] = []
    for session in ordered:
        start = float(session.session_start_equity)
        end = float(session.session_end_equity)
        if math.isfinite(start) and start > 0.0:
            peak = max(peak, start)
        if math.isfinite(end) and end > 0.0:
            peak = max(peak, end)
            running_drawdown = max(
                running_drawdown,
                (peak - end) / peak * 10_000.0 if peak > 0.0 else 0.0,
            )
        running_drawdown = max(
            running_drawdown,
            float(session.session_max_drawdown_bps),
        )
        rebased.append(
            replace(
                session,
                max_drawdown_bps=running_drawdown,
                chain_max_drawdown_bps=running_drawdown,
            )
        )
    return rebased


def _moving_block_robustness(
    candidate: MarketReplayCandidateSummary,
    candidate_sessions: list[MarketReplaySessionResult],
    control: MarketReplayCandidateSummary,
    control_sessions: list[MarketReplaySessionResult],
    config: MarketReplayConfig,
    *,
    seed: str,
) -> dict[str, Any]:
    """Resample adjacent economic units to test short-lived regime dependence."""

    pairs_by_day = _paired_sessions_by_day(candidate_sessions, control_sessions)
    unit_type, units = _paired_bootstrap_units(pairs_by_day)
    block_length = moving_block_length(len(units))
    if block_length is None:
        return {
            "evaluated": False,
            "passed": False,
            "unit_type": unit_type,
            "independent_units": len(units),
            "block_length": None,
            "replicates": 0,
            "failure_reasons": [
                "At least 15 independent trading-day or overnight-continuity units are required for moving-block bootstrap authorization."
            ],
        }
    values: list[float] = []
    for indices in deterministic_moving_block_indices(
        seed,
        replicates=_MOVING_BLOCK_REPLICATES,
        unit_count=len(units),
        block_length=block_length,
    ):
        pairs: list[tuple[MarketReplaySessionResult, MarketReplaySessionResult]] = []
        for index in indices:
            for selected_day in units[index]:
                pairs.extend(pairs_by_day[selected_day])
        delta = _score_delta_for_pairs(
            candidate.profile,
            control.profile,
            pairs,
            config,
        )
        if delta is not None:
            values.append(delta)
    probability = (
        sum(1 for value in values if value > 0.0) / len(values) * 100.0
        if values
        else None
    )
    ci80_low = _quantile(values, 0.10)
    ci80_high = _quantile(values, 0.90)
    reasons: list[str] = []
    if len(values) != _MOVING_BLOCK_REPLICATES:
        reasons.append("Moving-block bootstrap evidence was incomplete.")
    if ci80_low is None or ci80_low <= 0.0:
        reasons.append("The 80% moving-block interval was not entirely above zero.")
    if probability is None or probability < _MIN_BOOTSTRAP_POSITIVE_PCT:
        reasons.append(
            f"Fewer than {_MIN_BOOTSTRAP_POSITIVE_PCT:.0f}% of moving-block replicates were positive."
        )
    return {
        "evaluated": True,
        "passed": not reasons,
        "unit_type": unit_type,
        "independent_units": len(units),
        "block_length": block_length,
        "replicates": len(values),
        "ci80_low": ci80_low,
        "ci80_high": ci80_high,
        "ci95_low": _quantile(values, 0.025),
        "ci95_high": _quantile(values, 0.975),
        "probability_positive_pct": probability,
        "failure_reasons": reasons,
    }


def _walk_forward_validation(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    candidate_profile: AtrProfile,
    config: MarketReplayConfig,
) -> dict[str, Any]:
    """Select on earlier sessions and evaluate frozen profiles on later sessions."""

    days = sorted({period.session_date for period, _ in period_ticks})
    folds = expanding_walk_forward_folds(
        days,
        minimum_training_days=_WALK_FORWARD_MIN_TRAINING_DAYS,
        validation_days=_WALK_FORWARD_VALIDATION_DAYS,
    )
    rows: list[dict[str, Any]] = []
    control_profile = _control_profile(config)
    for fold_number, (training_days, validation_days) in enumerate(folds, start=1):
        training_set = set(training_days)
        validation_set = set(validation_days)
        training_ticks = [
            item for item in period_ticks if item[0].session_date in training_set
        ]
        prefix_set = training_set | validation_set
        prefix_ticks = [
            item for item in period_ticks if item[0].session_date in prefix_set
        ]
        selector = _run_bounded_selector(recording, training_ticks, config)
        _, selected_sessions = _evaluate_profile_fresh(
            recording,
            prefix_ticks,
            selector.selected.profile,
            config,
        )
        _, control_sessions = _evaluate_profile_fresh(
            recording,
            prefix_ticks,
            control_profile,
            config,
        )
        _, fixed_candidate_sessions = _evaluate_profile_fresh(
            recording,
            prefix_ticks,
            candidate_profile,
            config,
        )
        selected_validation = [
            session
            for session in selected_sessions
            if session.session_date in validation_set
        ]
        control_validation = [
            session
            for session in control_sessions
            if session.session_date in validation_set
        ]
        fixed_validation = [
            session
            for session in fixed_candidate_sessions
            if session.session_date in validation_set
        ]
        selected_validation = _rebase_validation_drawdown(selected_validation)
        control_validation = _rebase_validation_drawdown(control_validation)
        fixed_validation = _rebase_validation_drawdown(fixed_validation)
        selected_delta = _score_delta_for_pairs(
            selector.selected.profile,
            control_profile,
            _paired_session_list(selected_validation, control_validation),
            config,
        )
        fixed_delta = _score_delta_for_pairs(
            candidate_profile,
            control_profile,
            _paired_session_list(fixed_validation, control_validation),
            config,
        )
        rows.append(
            {
                "fold": fold_number,
                "training_start": training_days[0],
                "training_end": training_days[-1],
                "training_days": len(training_days),
                "validation_start": validation_days[0],
                "validation_end": validation_days[-1],
                "validation_days": len(validation_days),
                "selected_profile_key": selector.selected.profile.key(),
                "selected_period": selector.selected.profile.period,
                "selected_bar_seconds": selector.selected.profile.bar_seconds,
                "selected_validation_delta": selected_delta,
                "fixed_candidate_validation_delta": fixed_delta,
                "same_atr_window_as_final_candidate": (
                    selector.selected.profile.period == candidate_profile.period
                    and selector.selected.profile.bar_seconds
                    == candidate_profile.bar_seconds
                ),
            }
        )
    selected_values = [
        float(row["selected_validation_delta"])
        for row in rows
        if row["selected_validation_delta"] is not None
    ]
    fixed_values = [
        float(row["fixed_candidate_validation_delta"])
        for row in rows
        if row["fixed_candidate_validation_delta"] is not None
    ]
    same_window_pct = (
        sum(1 for row in rows if row["same_atr_window_as_final_candidate"])
        / len(rows)
        * 100.0
        if rows
        else None
    )
    reasons: list[str] = []
    if not rows:
        reasons.append(
            "Insufficient sessions for chronological out-of-sample authorization."
        )
    if len(selected_values) != len(rows) or len(fixed_values) != len(rows):
        reasons.append("Walk-forward validation evidence was incomplete.")
    if selected_values and min(selected_values) <= 0.0:
        reasons.append(
            "At least one unseen validation block did not beat the control after training-only selection."
        )
    if fixed_values and min(fixed_values) <= 0.0:
        reasons.append(
            "The final fixed candidate did not beat the control in every unseen validation block."
        )
    if same_window_pct is None or same_window_pct < _MIN_LOO_WINDOW_SELECTION_PCT:
        reasons.append(
            "The final ATR window did not recur in enough training-only walk-forward selections."
        )
    return {
        "evaluated": bool(rows),
        "passed": bool(rows) and not reasons,
        "folds": len(rows),
        "positive_selected_folds": sum(1 for value in selected_values if value > 0.0),
        "positive_fixed_folds": sum(1 for value in fixed_values if value > 0.0),
        "selected_probability_positive_pct": (
            sum(1 for value in selected_values if value > 0.0)
            / len(selected_values)
            * 100.0
            if selected_values
            else None
        ),
        "fixed_probability_positive_pct": (
            sum(1 for value in fixed_values if value > 0.0)
            / len(fixed_values)
            * 100.0
            if fixed_values
            else None
        ),
        "selected_median_delta": (
            float(statistics.median(selected_values)) if selected_values else None
        ),
        "fixed_median_delta": (
            float(statistics.median(fixed_values)) if fixed_values else None
        ),
        "fixed_worst_delta": min(fixed_values) if fixed_values else None,
        "same_atr_window_pct": same_window_pct,
        "failure_reasons": reasons,
        "rows": rows,
    }


def _selection_aware_bootstrap(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    candidate_profile: AtrProfile,
    candidate_sessions: list[MarketReplaySessionResult],
    control_sessions: list[MarketReplaySessionResult],
    config: MarketReplayConfig,
    *,
    seed: str,
    dependency_session_groups: Iterable[
        Iterable[MarketReplaySessionResult]
    ] | None = None,
) -> dict[str, Any]:
    """Rerun selection on bootstrap training bags and score on out-of-bag units."""

    pairs_by_day = _paired_sessions_by_day(candidate_sessions, control_sessions)
    if dependency_session_groups is None:
        unit_type, units = _paired_bootstrap_units(pairs_by_day)
    else:
        unit_type, units = _bootstrap_units_across_profiles(
            dependency_session_groups
        )
    if len(units) < _MIN_ADVANCED_VALIDATION_DAYS:
        return {
            "evaluated": False,
            "passed": False,
            "unit_type": unit_type,
            "independent_units": len(units),
            "replicates": 0,
            "oob_evaluations": 0,
            "failure_reasons": [
                f"At least {_MIN_ADVANCED_VALIDATION_DAYS} independent units are required for selection-aware out-of-bag bootstrap authorization."
            ],
            "rows": [],
        }
    rows: list[dict[str, Any]] = []
    control_profile = _control_profile(config)
    for replicate, indices in enumerate(
        _deterministic_bootstrap_indices(
            seed,
            replicates=_SELECTION_BOOTSTRAP_REPLICATES,
            draws=len(units),
            size=len(units),
        ),
        start=1,
    ):
        counts: dict[int, int] = defaultdict(int)
        for index in indices:
            counts[index] += 1
        oob_indices = [index for index in range(len(units)) if index not in counts]
        if not oob_indices:
            continue
        day_weights: dict[str, int] = defaultdict(int)
        for index, count in counts.items():
            for day in units[index]:
                day_weights[day] += count
        training_days = set(day_weights)
        oob_days = {
            day for index in oob_indices for day in units[index]
        }
        training_ticks = [
            item for item in period_ticks if item[0].session_date in training_days
        ]
        oob_ticks = [
            item for item in period_ticks if item[0].session_date in oob_days
        ]
        if not training_ticks or not oob_ticks:
            continue
        selector = _run_bounded_selector(
            recording,
            training_ticks,
            config,
            day_weights=dict(day_weights),
        )
        _, selected_oob = _evaluate_profile_fresh(
            recording,
            oob_ticks,
            selector.selected.profile,
            config,
        )
        _, control_oob = _evaluate_profile_fresh(
            recording,
            oob_ticks,
            control_profile,
            config,
        )
        delta = _score_delta_for_pairs(
            selector.selected.profile,
            control_profile,
            _paired_session_list(selected_oob, control_oob),
            config,
        )
        rows.append(
            {
                "replicate": replicate,
                "training_units": len(counts),
                "out_of_bag_units": len(oob_indices),
                "selected_profile_key": selector.selected.profile.key(),
                "selected_period": selector.selected.profile.period,
                "selected_bar_seconds": selector.selected.profile.bar_seconds,
                "selected_control": (
                    selector.selected.profile.key() == control_profile.key()
                ),
                "same_atr_window_as_final_candidate": (
                    selector.selected.profile.period == candidate_profile.period
                    and selector.selected.profile.bar_seconds
                    == candidate_profile.bar_seconds
                ),
                "out_of_bag_score_delta": delta,
            }
        )
    values = [
        float(row["out_of_bag_score_delta"])
        for row in rows
        if row["out_of_bag_score_delta"] is not None
    ]
    probability = (
        sum(1 for value in values if value > 0.0) / len(values) * 100.0
        if values
        else None
    )
    same_window_pct = (
        sum(1 for row in rows if row["same_atr_window_as_final_candidate"])
        / len(rows)
        * 100.0
        if rows
        else None
    )
    reasons: list[str] = []
    minimum_evaluations = max(10, _SELECTION_BOOTSTRAP_REPLICATES // 3)
    if len(values) < minimum_evaluations:
        reasons.append("Too few selection-aware bootstrap replicates had out-of-bag evidence.")
    if probability is None or probability < _MIN_BOOTSTRAP_POSITIVE_PCT:
        reasons.append(
            f"Fewer than {_MIN_BOOTSTRAP_POSITIVE_PCT:.0f}% of selection-aware out-of-bag results were positive."
        )
    if same_window_pct is None or same_window_pct < _MIN_LOO_WINDOW_SELECTION_PCT:
        reasons.append(
            "The final ATR window was not selected in enough bootstrap training bags."
        )
    return {
        "evaluated": True,
        "passed": not reasons,
        "unit_type": unit_type,
        "independent_units": len(units),
        "replicates": _SELECTION_BOOTSTRAP_REPLICATES,
        "oob_evaluations": len(values),
        "probability_positive_pct": probability,
        "median_oob_delta": float(statistics.median(values)) if values else None,
        "ci80_low": _quantile(values, 0.10),
        "ci80_high": _quantile(values, 0.90),
        "control_selection_pct": (
            sum(1 for row in rows if row["selected_control"]) / len(rows) * 100.0
            if rows
            else None
        ),
        "same_atr_window_pct": same_window_pct,
        "failure_reasons": reasons,
        "rows": rows,
    }


def _assumption_stress_validation(
    recording: IbrecRecording,
    period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]],
    candidate_profile: AtrProfile,
    control_profile: AtrProfile,
    config: MarketReplayConfig,
    *,
    buy_cost_p90: float | None,
    sell_cost_p90: float | None,
) -> dict[str, Any]:
    """Replay fixed candidate/control profiles under deterministic assumption stress."""

    normalized = config.normalized()
    base_buy = normalized.buy_execution_cost_bps_per_side
    base_sell = normalized.sell_execution_cost_bps_per_side
    if base_buy is None or base_sell is None:
        raise MarketReplayAnalysisError("Side-specific execution reserves were not normalized.")
    variants: list[tuple[str, MarketReplayConfig]] = [
        (
            "execution_cost_150pct",
            replace(
                normalized,
                buy_execution_cost_bps_per_side=base_buy * 1.5,
                sell_execution_cost_bps_per_side=base_sell * 1.5,
                execution_cost_overrides=tuple(
                    (day, buy * 1.5, sell * 1.5)
                    for day, buy, sell in normalized.execution_cost_overrides
                ),
            ),
        ),
        (
            "execution_cost_p90",
            replace(
                normalized,
                buy_execution_cost_bps_per_side=max(base_buy, buy_cost_p90 or base_buy),
                sell_execution_cost_bps_per_side=max(base_sell, sell_cost_p90 or base_sell),
                execution_cost_overrides=(),
            ),
        ),
        (
            "half_notional",
            replace(
                normalized,
                assumed_trade_notional=normalized.assumed_trade_notional * 0.5,
                trade_notional_overrides=tuple(
                    (day, value * 0.5)
                    for day, value in normalized.trade_notional_overrides
                ),
            ),
        ),
        (
            "double_notional",
            replace(
                normalized,
                assumed_trade_notional=normalized.assumed_trade_notional * 2.0,
                trade_notional_overrides=tuple(
                    (day, value * 2.0)
                    for day, value in normalized.trade_notional_overrides
                ),
            ),
        ),
        (
            "quote_age_1s",
            replace(normalized, execution_quote_max_age_seconds=1.0),
        ),
        (
            "quote_age_3s",
            replace(normalized, execution_quote_max_age_seconds=3.0),
        ),
        (
            "quote_age_10s",
            replace(normalized, execution_quote_max_age_seconds=10.0),
        ),
        (
            "entry_delay_15m",
            replace(normalized, entry_open_delay_seconds=15 * 60),
        ),
        (
            "entry_delay_30m",
            replace(normalized, entry_open_delay_seconds=30 * 60),
        ),
        (
            "entry_cutoff_10m",
            replace(normalized, entry_cutoff_seconds=10 * 60),
        ),
        (
            "entry_cutoff_30m",
            replace(normalized, entry_cutoff_seconds=30 * 60),
        ),
    ]
    rows: list[dict[str, Any]] = []
    for key, variant in variants:
        candidate_summary, candidate_sessions = _evaluate_profile_fresh(
            recording,
            period_ticks,
            candidate_profile,
            variant,
        )
        control_summary, control_sessions = _evaluate_profile_fresh(
            recording,
            period_ticks,
            control_profile,
            variant,
        )
        delta = _score_delta_for_pairs(
            candidate_profile,
            control_profile,
            _paired_session_list(candidate_sessions, control_sessions),
            variant,
        )
        passed = (
            delta is not None
            and delta > 0.0
            and candidate_summary.unmarked_open_position_sessions == 0
            and candidate_summary.right_censored_sessions
            <= control_summary.right_censored_sessions
        )
        rows.append(
            {
                "stress_key": key,
                "candidate_score": candidate_summary.score,
                "control_score": control_summary.score,
                "score_delta": delta,
                "candidate_right_censored_sessions": (
                    candidate_summary.right_censored_sessions
                ),
                "control_right_censored_sessions": control_summary.right_censored_sessions,
                "candidate_unmarked_open_positions": (
                    candidate_summary.unmarked_open_position_sessions
                ),
                "passed": passed,
            }
        )
    return {
        "evaluated": True,
        "passed": all(row["passed"] for row in rows),
        "rows": rows,
        "failure_reasons": [
            f"Assumption stress {row['stress_key']} did not remain above the control."
            for row in rows
            if not row["passed"]
        ],
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
    target.bootstrap_unit_type = source.bootstrap_unit_type
    target.bootstrap_independent_units = source.bootstrap_independent_units
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
    target.leave_one_day_out_selection_mode = (
        source.leave_one_day_out_selection_mode
    )
    target.paired_median_return_delta_bps = source.paired_median_return_delta_bps
    target.paired_positive_day_pct = source.paired_positive_day_pct
    target.control_trade_day_retention_pct = source.control_trade_day_retention_pct
    target.maximum_drawdown_delta_bps = source.maximum_drawdown_delta_bps
    target.worst_return_delta_bps = source.worst_return_delta_bps
    target.atr_phase_cases = source.atr_phase_cases
    target.atr_phase_min_score_delta = source.atr_phase_min_score_delta
    target.atr_phase_adverse_score_delta = source.atr_phase_adverse_score_delta
    target.moving_block_replicates = source.moving_block_replicates
    target.moving_block_length = source.moving_block_length
    target.moving_block_ci80_low = source.moving_block_ci80_low
    target.moving_block_ci80_high = source.moving_block_ci80_high
    target.moving_block_probability_positive_pct = (
        source.moving_block_probability_positive_pct
    )
    target.selection_bootstrap_replicates = source.selection_bootstrap_replicates
    target.selection_bootstrap_oob_evaluations = (
        source.selection_bootstrap_oob_evaluations
    )
    target.selection_bootstrap_probability_positive_pct = (
        source.selection_bootstrap_probability_positive_pct
    )
    target.selection_bootstrap_median_oob_delta = (
        source.selection_bootstrap_median_oob_delta
    )
    target.selection_bootstrap_control_selection_pct = (
        source.selection_bootstrap_control_selection_pct
    )
    target.walk_forward_folds = source.walk_forward_folds
    target.walk_forward_positive_folds = source.walk_forward_positive_folds
    target.walk_forward_probability_positive_pct = (
        source.walk_forward_probability_positive_pct
    )
    target.walk_forward_median_delta = source.walk_forward_median_delta
    target.walk_forward_worst_delta = source.walk_forward_worst_delta
    target.walk_forward_same_window_pct = source.walk_forward_same_window_pct
    target.score_policy_results = [dict(row) for row in source.score_policy_results]
    target.score_policy_all_positive = source.score_policy_all_positive
    target.pareto_frontier = source.pareto_frontier
    target.pareto_dominated_by = list(source.pareto_dominated_by)
    target.boundary_dimensions = list(source.boundary_dimensions)
    target.boundary_extension_attempted = source.boundary_extension_attempted
    target.boundary_resolved = source.boundary_resolved
    target.assumption_stress_results = [
        dict(row) for row in source.assumption_stress_results
    ]
    target.assumption_stress_all_positive = source.assumption_stress_all_positive
    target.recommendation_gates = [dict(row) for row in source.recommendation_gates]
    target.protective_policy_stable = source.protective_policy_stable


def run_market_replay_analysis(
    config: MarketReplayConfig,
    *,
    progress: ProgressCallback | None = None,
) -> MarketReplayAnalysisResult:
    """Analyze Market Replay data, optionally calibrated by stopped bot executions."""

    requested_config = config.normalized()
    recording = load_ibrec_set(requested_config, progress=progress)
    _emit(progress, "Calibrating optional BouncyBot execution assumptions", 0, 1)
    calibration = load_execution_calibration(recording, requested_config)
    # All later simulation uses immutable derived values.  Removing the source
    # directory from the effective configuration prevents thousands of profile
    # evaluations from repeatedly touching the user's filesystem and ensures
    # deterministic report content never contains its absolute path.
    normalized = replace(
        requested_config,
        assumed_trade_notional=calibration.effective_trade_notional,
        execution_cost_bps_per_side=(
            calibration.effective_execution_cost_bps_per_side
        ),
        buy_execution_cost_bps_per_side=(
            calibration.effective_buy_execution_cost_bps_per_side
        ),
        sell_execution_cost_bps_per_side=(
            calibration.effective_sell_execution_cost_bps_per_side
        ),
        execution_cost_overrides=tuple(
            (
                str(row["session_date"]),
                float(row["buy_cost_bps"]),
                float(row["sell_cost_bps"]),
            )
            for row in calibration.date_specific_execution_costs
        ),
        trade_notional_overrides=tuple(
            (str(row["session_date"]), float(row["trade_notional"]))
            for row in calibration.date_specific_trade_notionals
        ),
        calibration_source_dir=None,
    ).normalized()
    _emit(progress, "Calibrating optional BouncyBot execution assumptions", 1, 1)
    raw_period_ticks = [
        (period, ticks)
        for period in recording.periods
        if (ticks := _session_ticks(recording, period))
    ]
    if not raw_period_ticks:
        raise MarketReplayAnalysisError(
            "Recording has no live or delayed market-data rows inside an analyzable session."
        )

    session_quality: list[dict[str, Any]] = []
    assessed_period_ticks: list[tuple[IbrecPeriod, list[IbrecTick]]] = []
    for period, ticks in raw_period_ticks:
        quality = assess_market_replay_session(recording, period, ticks, normalized)
        assessed = replace(
            period,
            primary_eligible=quality.primary_eligible,
            source_finalized=quality.source_finalized,
            coverage_pct=quality.coverage_pct,
            start_lag_seconds=quality.start_lag_seconds,
            end_lead_seconds=quality.end_lead_seconds,
            maximum_event_gap_seconds=quality.maximum_event_gap_seconds,
            connectivity_event_count=quality.connectivity_event_count,
            last_event_count=quality.last_event_count,
            last_event_minute_coverage_pct=quality.last_event_minute_coverage_pct,
            last_event_gap_p95_seconds=quality.last_event_gap_p95_seconds,
            quality_exclusion_reasons=quality.exclusion_reasons,
        )
        assessed_period_ticks.append((assessed, ticks))
        session_quality.append(quality.to_dict())
        if not quality.primary_eligible:
            recording.excluded_sessions.append(
                {
                    "reason_type": "quality_gate",
                    "session_date": quality.session_date,
                    "period_id": quality.period_id,
                    "period_count": 1,
                    "source_recording_sha256": quality.source_recording_sha256,
                    "recording_hashes": list(
                        quality.source_recording_sha256s
                        or (quality.source_recording_sha256,)
                    ),
                    "coverage_pct": quality.coverage_pct,
                    "reason": " | ".join(quality.exclusion_reasons),
                    "reasons": list(quality.exclusion_reasons),
                }
            )
    recording.periods = [period for period, _ in assessed_period_ticks]
    eligible_period_ticks = [
        item for item in assessed_period_ticks if item[0].primary_eligible
    ]
    exploratory_only = not eligible_period_ticks
    period_ticks = eligible_period_ticks or assessed_period_ticks
    trading_day_count = len({period.session_date for period, _ in period_ticks})
    robustness_possible = (
        not exploratory_only and trading_day_count >= _MIN_ROBUST_TRADING_DAYS
    )

    control_profile = _control_profile(normalized)
    atr_cache: dict[AtrCacheKey, list[float | None]] = {}
    summaries: dict[str, MarketReplayCandidateSummary] = {}
    sessions_by_profile: dict[str, list[MarketReplaySessionResult]] = {}
    policy_profiles, protective_policy_evidence, protective_policy_reason = _select_protective_policy(
        recording,
        period_ticks,
        normalized,
        atr_cache,
        summaries,
        sessions_by_profile,
        progress=progress,
        collect_diagnostics=True,
    )
    supported_protective_policies = {
        (str(row.get("mode") or ""), float(row.get("value") or 0.0))
        for row in protective_policy_evidence
        if bool(row.get("selected_for_atr_search"))
        and bool(row.get("eligible"))
        and str(row.get("mode") or "") != "disabled"
    }
    policy_window_sets: list[tuple[AtrProfile, list[tuple[int, int]]]] = []
    window_search: list[dict[str, Any]] = []
    stage1_profiles: list[AtrProfile] = []
    stage2_profiles: list[AtrProfile] = []
    for policy in policy_profiles:
        policy_windows, policy_rows, policy_stage1, policy_stage2 = _select_windows(
            recording,
            period_ticks,
            normalized,
            atr_cache,
            summaries,
            sessions_by_profile,
            robustness_possible,
            progress=progress,
            base_profile=policy,
        )
        policy_window_sets.append((policy, policy_windows))
        window_search.extend(policy_rows)
        stage1_profiles.extend(policy_stage1)
        stage2_profiles.extend(policy_stage2)
    coarse_profiles = _coarse_profiles_for_policy_windows(
        normalized,
        policy_window_sets,
        search_clamps=robustness_possible,
    )
    _evaluate_profiles(
        recording,
        period_ticks,
        atr_cache,
        coarse_profiles,
        summaries,
        sessions_by_profile,
        normalized,
        progress=progress,
        message="Stage 3 of 3: evaluating multiplier profiles",
    )
    stage3_keys = {profile.key() for profile in coarse_profiles}
    coarse_candidates = [summaries[key] for key in sorted(stage3_keys)]
    seeds = _refinement_seeds(coarse_candidates) if robustness_possible else []
    refined_profiles = (
        [
            profile
            for profile in _refined_profiles(seeds, normalized)
            if profile.key() not in stage3_keys
        ]
        if robustness_possible
        else []
    )
    if refined_profiles:
        _evaluate_profiles(
            recording,
            period_ticks,
            atr_cache,
            refined_profiles,
            summaries,
            sessions_by_profile,
            normalized,
            progress=progress,
            message="Stage 3 of 3: refining multiplier profiles",
        )
    stage3_keys.update(profile.key() for profile in refined_profiles)
    candidates = sorted(
        (summaries[key] for key in stage3_keys),
        key=_candidate_sort_key,
    )

    # A stable region that touches the tested parameter boundary may simply be
    # truncated by the search grid.  Probe outward twice before treating it as
    # an interior supported plateau.  The probes become normal candidates and
    # therefore participate in Pareto, policy, and stability evidence.
    if robustness_possible:
        for extension_round in range(2):
            for candidate in candidates:
                candidate.stable_region_id = ""
                candidate.stable_region_size = 0
                candidate.stable_region_center = False
                candidate.near_best = False
                candidate.evidence_stable = False
            extension_centers = _stable_region_centers(
                candidates,
                sessions=trading_day_count,
            )
            outward_profiles: dict[str, AtrProfile] = {}
            for center, _ in extension_centers:
                probes, _ = _boundary_extension_profiles(center, candidates)
                for profile in probes:
                    if profile.key() not in summaries:
                        outward_profiles[profile.key()] = profile
            if not outward_profiles:
                break
            _ensure_atr_windows(
                period_ticks,
                atr_cache,
                sorted(
                    {
                        (profile.period, profile.bar_seconds)
                        for profile in outward_profiles.values()
                    }
                ),
                progress=progress,
                message=(
                    f"Extending truncated search boundaries, round {extension_round + 1}"
                ),
            )
            _evaluate_profiles(
                recording,
                period_ticks,
                atr_cache,
                list(outward_profiles.values()),
                summaries,
                sessions_by_profile,
                normalized,
                progress=progress,
                message=(
                    f"Evaluating outward search-boundary probes, round {extension_round + 1}"
                ),
            )
            stage3_keys.update(outward_profiles)
            candidates = sorted(
                (summaries[key] for key in stage3_keys),
                key=_candidate_sort_key,
            )

    control_summary = summaries[control_profile.key()]
    control_sessions = sessions_by_profile[control_profile.key()]
    leave_one_day_out_selection_rows = (
        _leave_one_day_out_selection_rows(
            recording,
            period_ticks,
            normalized,
        )
        if robustness_possible
        and trading_day_count >= _MIN_ROBUST_TRADING_DAYS
        else []
    )

    selected_feed_evidence = [
        _session_feed_evidence(recording, period)
        for period, _ in period_ticks
    ]
    global_instability: list[str] = []
    if exploratory_only:
        global_instability.append(
            "No recorded RTH session passed every primary coverage, feed, connectivity, completeness, and Last-event-density gate; all candidate results are exploratory only."
        )
    elif trading_day_count < _MIN_ROBUST_TRADING_DAYS:
        global_instability.append(
            f"Only {trading_day_count} primary-eligible trading day(s) were available; at least {_MIN_ROBUST_TRADING_DAYS} are required before bootstrap, leave-one-day-out, phase authorization, or a changed recommendation is attempted."
        )
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

    for candidate in candidates:
        candidate.stable_region_id = ""
        candidate.stable_region_size = 0
        candidate.stable_region_center = False
        candidate.near_best = False
        candidate.evidence_stable = False
    region_centers = (
        _stable_region_centers(candidates, sessions=trading_day_count)
        if robustness_possible
        else []
    )
    pareto_keys = pareto_frontier(candidates) if candidates else set()
    robustness_evidence: list[dict[str, Any]] = []
    leave_one_out_by_key: dict[str, list[dict[str, Any]]] = {}
    phase_cache: dict[tuple[str, int], list[MarketReplaySessionResult]] = {}
    continuity_block_evidence: list[dict[str, Any]] = []
    score_policy_evidence: list[dict[str, Any]] = []
    moving_block_evidence: list[dict[str, Any]] = []
    selection_bootstrap_evidence: list[dict[str, Any]] = []
    walk_forward_evidence: list[dict[str, Any]] = []
    pareto_evidence: list[dict[str, Any]] = []
    boundary_evidence: list[dict[str, Any]] = []
    assumption_stress_evidence: list[dict[str, Any]] = []
    recommendation_gate_evidence: list[dict[str, Any]] = []
    eligible_centers: list[
        tuple[MarketReplayCandidateSummary, str, dict[str, Any]]
    ] = []
    for center, region_reason in region_centers:
        center_key = center.profile.key()
        candidate_sessions = sessions_by_profile[center_key]
        exact_loo_cache: dict[
            str,
            tuple[
                list[MarketReplaySessionResult],
                list[MarketReplaySessionResult],
            ],
        ] = {}

        def exact_loo(
            omitted_day: str,
        ) -> tuple[
            list[MarketReplaySessionResult],
            list[MarketReplaySessionResult],
        ]:
            if omitted_day not in exact_loo_cache:
                subset = [
                    item
                    for item in period_ticks
                    if item[0].session_date != omitted_day
                ]
                _, candidate_subset, _ = _evaluate_profile(
                    recording,
                    subset,
                    atr_cache,
                    center.profile,
                    normalized,
                    keep_details=False,
                )
                _, control_subset, _ = _evaluate_profile(
                    recording,
                    subset,
                    atr_cache,
                    control_summary.profile,
                    normalized,
                    keep_details=False,
                )
                exact_loo_cache[omitted_day] = (
                    candidate_subset,
                    control_subset,
                )
            return exact_loo_cache[omitted_day]

        evidence, leave_one_out = _candidate_robustness(
            center,
            candidate_sessions,
            control_summary,
            control_sessions,
            normalized,
            seed=_market_replay_bootstrap_seed(
                recording.sha256,
                control_summary.profile,
            ),
            selection_rows=leave_one_day_out_selection_rows,
            exact_leave_one_out=exact_loo,
        )
        base_passed = bool(evidence.get("passed"))
        if base_passed:
            phase_evidence = _atr_phase_robustness(
                recording,
                period_ticks,
                center.profile,
                control_summary.profile,
                phase_cache,
                normalized,
            )
        else:
            phase_evidence = {
                "atr_phase_cases": 0,
                "atr_phase_finite_cases": 0,
                "atr_phase_min_score_delta": None,
                "atr_phase_median_score_delta": None,
                "atr_phase_max_score_delta": None,
                "atr_phase_adverse_score_delta": None,
                "atr_phase_candidate_offsets": [],
                "atr_phase_control_offsets": [],
                "atr_phase_failure_reasons": [
                    "ATR bar-phase stress was skipped because the candidate had already failed an earlier robustness gate."
                ],
                "atr_phase_passed": False,
            }
        evidence.update(phase_evidence)

        policy_rows = score_policy_comparison(
            candidate_sessions,
            control_sessions,
            turnover_penalty_bps_per_completed_trade=(
                normalized.turnover_penalty_bps_per_completed_trade
            ),
        )
        policy_passed = bool(policy_rows) and all(
            bool(row["passed"]) for row in policy_rows
        )
        center.score_policy_results = [dict(row) for row in policy_rows]
        center.score_policy_all_positive = policy_passed
        score_policy_evidence.extend(
            {"profile_key": center_key, **row} for row in policy_rows
        )

        dominators = sorted(
            candidate.profile.key()
            for candidate in candidates
            if candidate.profile.key() != center_key
            and pareto_dominates(candidate, center)
        )
        center.pareto_frontier = center_key in pareto_keys
        center.pareto_dominated_by = dominators
        pareto_row = {
            "profile_key": center_key,
            "on_frontier": center.pareto_frontier,
            "dominated_by": dominators,
            "passed": center.pareto_frontier,
        }
        pareto_evidence.append(pareto_row)

        boundary_probes, boundary_rows = _boundary_extension_profiles(
            center,
            candidates,
        )
        del boundary_probes
        boundary_result = _resolve_boundary_evidence(
            center,
            boundary_rows,
            summaries,
        )
        center.boundary_dimensions = sorted(
            {str(row["dimension"]) for row in boundary_rows}
        )
        center.boundary_extension_attempted = bool(boundary_rows)
        center.boundary_resolved = bool(boundary_result["resolved"])
        boundary_evidence.append(boundary_result)

        continuity_result = _continuity_block_comparison(
            candidate_sessions,
            control_sessions,
        )
        continuity_block_evidence.append(
            {"profile_key": center_key, **continuity_result}
        )

        protective_policy_key = (
            center.profile.protective_sell_mode,
            center.profile.protective_sell_value,
        )
        protective_policy_passed = (
            center.profile.protective_sell_mode == "disabled"
            or protective_policy_key in supported_protective_policies
        )
        center.protective_policy_stable = protective_policy_passed

        pre_advanced_passed = (
            base_passed
            and bool(phase_evidence.get("atr_phase_passed"))
            and policy_passed
            and center.pareto_frontier
            and center.boundary_resolved
            and bool(continuity_result.get("passed"))
            and protective_policy_passed
        )
        if pre_advanced_passed:
            moving_result = _moving_block_robustness(
                center,
                candidate_sessions,
                control_summary,
                control_sessions,
                normalized,
                seed=(
                    _market_replay_bootstrap_seed(
                        recording.sha256,
                        control_summary.profile,
                    )
                    + "|moving-block"
                ),
            )
        else:
            skipped_reason = (
                "Skipped because an earlier recommendation authorization gate failed."
            )
            moving_result = {
                "evaluated": False,
                "passed": False,
                "replicates": 0,
                "block_length": None,
                "probability_positive_pct": None,
                "failure_reasons": [skipped_reason],
            }

        if pre_advanced_passed and bool(moving_result.get("passed")):
            stress_result = _assumption_stress_validation(
                recording,
                period_ticks,
                center.profile,
                control_summary.profile,
                normalized,
                buy_cost_p90=calibration.buy_total_adverse_cost_bps_p90,
                sell_cost_p90=calibration.sell_total_adverse_cost_bps_p90,
            )
        else:
            skipped_reason = (
                "Skipped because an earlier recommendation authorization gate failed."
            )
            stress_result = {
                "evaluated": False,
                "passed": False,
                "rows": [],
                "failure_reasons": [skipped_reason],
            }
        moving_block_evidence.append({"profile_key": center_key, **moving_result})
        assumption_stress_evidence.append(
            {"profile_key": center_key, **stress_result}
        )

        before_expensive_passed = (
            pre_advanced_passed
            and bool(moving_result.get("passed"))
            and bool(stress_result.get("passed"))
        )
        if before_expensive_passed:
            walk_result = _walk_forward_validation(
                recording,
                period_ticks,
                center.profile,
                normalized,
            )
            selection_result = _selection_aware_bootstrap(
                recording,
                period_ticks,
                center.profile,
                candidate_sessions,
                control_sessions,
                normalized,
                seed=(
                    _market_replay_bootstrap_seed(
                        recording.sha256,
                        control_summary.profile,
                    )
                    + "|selection-aware"
                ),
                dependency_session_groups=sessions_by_profile.values(),
            )
        else:
            skipped_reason = (
                "Skipped because an earlier recommendation authorization gate failed."
            )
            walk_result = {
                "evaluated": False,
                "passed": False,
                "folds": 0,
                "rows": [],
                "failure_reasons": [skipped_reason],
            }
            selection_result = {
                "evaluated": False,
                "passed": False,
                "replicates": 0,
                "oob_evaluations": 0,
                "rows": [],
                "failure_reasons": [skipped_reason],
            }
        walk_forward_evidence.append({"profile_key": center_key, **walk_result})
        selection_bootstrap_evidence.append(
            {"profile_key": center_key, **selection_result}
        )

        center.moving_block_replicates = int(moving_result.get("replicates") or 0)
        center.moving_block_length = int(moving_result.get("block_length") or 0)
        center.moving_block_ci80_low = moving_result.get("ci80_low")
        center.moving_block_ci80_high = moving_result.get("ci80_high")
        center.moving_block_probability_positive_pct = moving_result.get(
            "probability_positive_pct"
        )
        center.selection_bootstrap_replicates = int(
            selection_result.get("replicates") or 0
        )
        center.selection_bootstrap_oob_evaluations = int(
            selection_result.get("oob_evaluations") or 0
        )
        center.selection_bootstrap_probability_positive_pct = selection_result.get(
            "probability_positive_pct"
        )
        center.selection_bootstrap_median_oob_delta = selection_result.get(
            "median_oob_delta"
        )
        center.selection_bootstrap_control_selection_pct = selection_result.get(
            "control_selection_pct"
        )
        center.walk_forward_folds = int(walk_result.get("folds") or 0)
        center.walk_forward_positive_folds = int(
            walk_result.get("positive_fixed_folds") or 0
        )
        center.walk_forward_probability_positive_pct = walk_result.get(
            "fixed_probability_positive_pct"
        )
        center.walk_forward_median_delta = walk_result.get("fixed_median_delta")
        center.walk_forward_worst_delta = walk_result.get("fixed_worst_delta")
        center.walk_forward_same_window_pct = walk_result.get("same_atr_window_pct")
        center.assumption_stress_results = [
            dict(row) for row in stress_result.get("rows") or []
        ]
        center.assumption_stress_all_positive = bool(stress_result.get("passed"))

        raw_region_stable = center.evidence_stable
        changed_profile = center_key != control_summary.profile.key()
        gate_rows = recommendation_gate_rows(
            [
                {
                    "gate_key": "01_source_quality",
                    "gate_label": "Source quality",
                    "passed": not global_instability,
                    "detail": " | ".join(global_instability) or "All source-quality gates passed.",
                },
                {
                    "gate_key": "02_stable_region",
                    "gate_label": "Stable parameter region",
                    "passed": raw_region_stable,
                    "detail": region_reason,
                },
                {
                    "gate_key": "03_protective_policy",
                    "gate_label": "Protective SELL policy stability",
                    "passed": protective_policy_passed,
                    "detail": (
                        "Protective SELL is disabled for this profile."
                        if center.profile.protective_sell_mode == "disabled"
                        else protective_policy_reason
                        if protective_policy_passed
                        else "The profile uses a protective SELL policy that did not pass the independent bounded policy screen."
                    ),
                },
                {
                    "gate_key": "04_paired_bootstrap_and_loo",
                    "gate_label": "Paired bootstrap and exact leave-one-day-out",
                    "passed": base_passed,
                    "detail": " | ".join(evidence.get("failure_reasons") or []) or "Passed.",
                },
                {
                    "gate_key": "05_atr_phase",
                    "gate_label": "ATR bar-phase stress",
                    "passed": bool(phase_evidence.get("atr_phase_passed")),
                    "detail": " | ".join(phase_evidence.get("atr_phase_failure_reasons") or []) or "Passed.",
                },
                {
                    "gate_key": "06_score_policies",
                    "gate_label": "Score-policy stability",
                    "passed": policy_passed,
                    "detail": "All predefined score policies favored the candidate." if policy_passed else "At least one predefined score policy did not favor the candidate.",
                },
                {
                    "gate_key": "07_pareto",
                    "gate_label": "Pareto frontier",
                    "passed": center.pareto_frontier,
                    "detail": "Candidate is non-dominated." if center.pareto_frontier else f"Dominated by {', '.join(dominators)}.",
                },
                {
                    "gate_key": "08_search_boundary",
                    "gate_label": "Search-boundary support",
                    "passed": center.boundary_resolved,
                    "detail": "Search boundary resolved." if center.boundary_resolved else f"Unresolved dimensions: {', '.join(boundary_result['unresolved_dimensions'])}.",
                },
                {
                    "gate_key": "09_economic_continuity",
                    "gate_label": "Economic continuity blocks",
                    "passed": bool(continuity_result.get("passed")),
                    "detail": " | ".join(continuity_result.get("failure_reasons") or []) or "Passed.",
                },
                {
                    "gate_key": "10_moving_block",
                    "gate_label": "Moving-block bootstrap",
                    "passed": bool(moving_result.get("passed")),
                    "detail": " | ".join(moving_result.get("failure_reasons") or []) or "Passed.",
                },
                {
                    "gate_key": "11_assumption_stress",
                    "gate_label": "Assumption stress",
                    "passed": bool(stress_result.get("passed")),
                    "detail": " | ".join(stress_result.get("failure_reasons") or []) or "Passed.",
                },
                {
                    "gate_key": "12_walk_forward",
                    "gate_label": "Chronological walk-forward",
                    "passed": bool(walk_result.get("passed")),
                    "detail": " | ".join(walk_result.get("failure_reasons") or []) or "Passed.",
                },
                {
                    "gate_key": "13_selection_bootstrap",
                    "gate_label": "Selection-aware bootstrap",
                    "passed": bool(selection_result.get("passed")),
                    "detail": " | ".join(selection_result.get("failure_reasons") or []) or "Passed.",
                },
            ]
        )
        center.recommendation_gates = gate_rows
        recommendation_gate_evidence.extend(
            {"profile_key": center_key, **row} for row in gate_rows
        )
        all_required_passed = all(
            not row["required"] or row["passed"] for row in gate_rows
        )
        evidence["failure_reasons"] = sorted(
            set(
                [
                    *list(evidence.get("failure_reasons") or []),
                    *list(phase_evidence.get("atr_phase_failure_reasons") or []),
                    *list(continuity_result.get("failure_reasons") or []),
                    *list(moving_result.get("failure_reasons") or []),
                    *list(stress_result.get("failure_reasons") or []),
                    *list(walk_result.get("failure_reasons") or []),
                    *list(selection_result.get("failure_reasons") or []),
                ]
            )
        )
        evidence["passed"] = all_required_passed
        _apply_robustness(center, evidence)
        eligible = changed_profile and all_required_passed
        center.evidence_stable = eligible
        if global_instability:
            center.instability_reasons.extend(global_instability)
        if not eligible:
            center.instability_reasons.extend(
                row["detail"] for row in gate_rows if row["required"] and not row["passed"]
            )
        center.instability_reasons = sorted(set(center.instability_reasons))
        evidence = {
            **evidence,
            "stable_region_id": center.stable_region_id,
            "stable_region_size": center.stable_region_size,
            "raw_region_stable": raw_region_stable,
            "changed_profile": changed_profile,
            "global_instability_reasons": list(global_instability),
            "eligible_for_changed_recommendation": eligible,
            "region_selection_reason": region_reason,
            "score_policy_results": policy_rows,
            "pareto": pareto_row,
            "boundary": boundary_result,
            "continuity_blocks": continuity_result,
            "moving_block": moving_result,
            "assumption_stress": stress_result,
            "walk_forward": walk_result,
            "selection_bootstrap": selection_result,
            "recommendation_gates": gate_rows,
        }
        robustness_evidence.append(evidence)
        leave_one_out_by_key[center_key] = leave_one_out
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
            -finite_or_negative(
                (evidence.get("walk_forward") or {}).get("fixed_worst_delta")
            ),
            -finite_or_negative(
                (evidence.get("selection_bootstrap") or {}).get("ci80_low")
            ),
            -finite_or_negative(
                (evidence.get("moving_block") or {}).get("ci80_low")
            ),
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
            f"{region_reason} The candidate passed every required source-quality, paired, bootstrap, "
            "leave-one-day-out, ATR-phase, score-policy, Pareto, search-boundary, economic-continuity, "
            "assumption-stress, selection-aware out-of-bag, and chronological walk-forward gate against "
            "the unchanged BouncyBot control."
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
                "No searched profile completed a simulated trade. No data-supported ATR change was found; "
                "the unchanged BouncyBot control is displayed for reference."
            )
            chosen.instability_reasons.append(
                "No candidate produced a simulated trade in the recorded data."
            )
        elif global_instability:
            chosen_reason = (
                "A changed profile cannot be recommended because the recording failed one or more source-quality "
                "stability gates. No data-supported ATR change was found; the unchanged BouncyBot control is "
                "displayed for reference."
            )
        elif region_centers:
            chosen_reason = (
                "No changed stable-region center passed every recommendation authorization gate, including "
                "paired outcomes, day and moving-block bootstrap, exact leave-one-day-out reselection, ATR phase, "
                "score-policy stability, Pareto non-domination, resolved search boundaries, assumption stress, "
                "selection-aware out-of-bag bootstrap, and chronological walk-forward validation. No data-supported "
                "ATR change was found; the unchanged BouncyBot control is displayed for reference."
            )
        else:
            chosen_reason = (
                "No supported adjacent near-best multiplier region was found. No data-supported ATR change was found; "
                "the unchanged BouncyBot control is displayed for reference."
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
            normalized,
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
            normalized,
            keep_details=True,
        )
        _copy_selection_evidence(chosen, recommendation)
        summaries[chosen.profile.key()] = recommendation

        control_detail, detailed_control_sessions, _ = _evaluate_profile(
            recording,
            period_ticks,
            atr_cache,
            control_profile,
            normalized,
            keep_details=True,
        )
        _copy_selection_evidence(control_summary, control_detail)
        summaries[control_profile.key()] = control_detail

    candidates = sorted(
        (summaries[key] for key in stage3_keys),
        key=_candidate_sort_key,
    )
    contract = market_replay_search_contract(requested_config)
    contract["cost_and_liquidity_model"].update(
        {
            "configured_assumed_trade_notional": (
                calibration.configured_trade_notional
            ),
            "effective_assumed_trade_notional": calibration.effective_trade_notional,
            "configured_execution_cost_bps_per_side": (
                calibration.configured_execution_cost_bps_per_side
            ),
            "effective_execution_cost_bps_per_side": (
                calibration.effective_execution_cost_bps_per_side
            ),
        }
    )
    contract["execution_calibration"]["result"] = calibration.to_dict()
    fingerprint_payload = {
        "input_components": recording.input_components,
        "search_contract": contract,
        "execution_calibration": calibration.to_dict(),
    }
    analysis_id = hashlib.sha256(canonical_json_bytes(fingerprint_payload)).hexdigest()
    output_dir = normalized.output_root / f"market_replay_{analysis_id[:16]}"
    issues = list(recording.issues)
    issues.extend(
        f"Execution calibration: {warning}" for warning in calibration.warnings
    )
    issues.append(f"Protective SELL policy screen: {protective_policy_reason}")
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
    continuity_evidence = [
        {
            "session_date": session.session_date,
            "period_id": session.period_id,
            "continuity_chain_id": session.continuity_chain_id,
            "continuity_broken_before": session.continuity_broken_before,
            "continuity_break_reason": session.continuity_break_reason,
            "carried_position_in": session.carried_position_in,
            "carried_position_out": session.carried_position_out,
            "carried_sell_trail_in": session.carried_sell_trail_in,
            "carried_sell_trail_out": session.carried_sell_trail_out,
            "carried_protective_trail_in": session.carried_protective_trail_in,
            "carried_protective_trail_out": session.carried_protective_trail_out,
            "protective_trigger_pending_at_end": (
                session.protective_trigger_pending_at_end
            ),
            "terminal_open_position": session.terminal_open_position,
            "session_start_equity": session.session_start_equity,
            "session_end_equity": session.session_end_equity,
            "cumulative_end_equity": session.cumulative_end_equity,
            "overnight_gap_return_bps": session.overnight_gap_return_bps,
        }
        for session in recommended_sessions
    ]
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
        session_quality=session_quality,
        execution_calibration=calibration.to_dict(),
        continuity_evidence=continuity_evidence,
        continuity_block_evidence=continuity_block_evidence,
        score_policy_evidence=score_policy_evidence,
        moving_block_evidence=moving_block_evidence,
        selection_bootstrap_evidence=selection_bootstrap_evidence,
        walk_forward_evidence=walk_forward_evidence,
        pareto_evidence=pareto_evidence,
        boundary_evidence=boundary_evidence,
        assumption_stress_evidence=assumption_stress_evidence,
        recommendation_gates=recommendation_gate_evidence,
        protective_policy_evidence=protective_policy_evidence,
        exploratory_only=exploratory_only,
    )
