"""Quality metrics and fail-closed eligibility rules for Market Replay sessions."""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any

from .market_replay_models import (
    IbrecPeriod,
    IbrecRecording,
    IbrecTick,
    MarketReplayConfig,
)
from .utils import finite_float, finite_int, percentile, truthy


@dataclass(slots=True, frozen=True)
class MarketReplaySessionQuality:
    """Deterministic quality evidence for one recorded RTH period."""

    session_date: str
    period_id: int
    source_recording_sha256: str
    manifest_status: str
    source_finalized: bool
    coverage_pct: float
    start_lag_seconds: float
    end_lead_seconds: float
    maximum_event_gap_seconds: float
    connectivity_event_count: int
    last_event_count: int
    last_event_minute_coverage_pct: float
    last_event_gap_p95_seconds: float | None
    selected_feed: str
    mixed_feed: bool
    frozen_feed: bool
    primary_eligible: bool
    exclusion_reasons: tuple[str, ...]
    source_recording_sha256s: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _quantile(values: list[float], probability: float) -> float | None:
    """Delegate to the one shared interpolated-percentile implementation."""

    return percentile(values, probability)


def _period_source_hashes(period: IbrecPeriod) -> tuple[str, ...]:
    """Return every content fingerprint that contributed to one period.

    A merged period stitched from several same-date fragments carries one
    fingerprint per contributing recording; single-source periods keep exactly
    one.  Legacy constructors that only set the joined display string fall
    back to that string so equality matching keeps working.
    """

    if period.source_recording_sha256s:
        return period.source_recording_sha256s
    if period.source_recording_sha256 and "+" not in period.source_recording_sha256:
        return (period.source_recording_sha256,)
    return tuple(
        part
        for part in period.source_recording_sha256.split("+")
        if part
    )


def _manifest_status(recording: IbrecRecording, period: IbrecPeriod) -> str:
    source_hashes = _period_source_hashes(period)
    if source_hashes:
        statuses: list[str] = []
        for source_hash in source_hashes:
            for component in recording.input_components:
                if str(component.get("role") or "") != "recording":
                    continue
                if (
                    str(component.get("recording_content_sha256") or "")
                    == source_hash
                ):
                    statuses.append(
                        str(component.get("manifest_status") or "").strip().lower()
                    )
                    break
        if statuses and len(statuses) == len(source_hashes):
            unique = sorted(set(statuses))
            # A merged period spanning sources with different finalization
            # states must never inherit the most permissive one.
            return unique[0] if len(unique) == 1 else "mixed"
    return str(recording.manifest.get("status") or "").strip().lower()


def _source_format_version(recording: IbrecRecording, period: IbrecPeriod) -> int:
    """Return the format version of the recording that owns ``period``.

    A combined input set uses ``format_version == 0`` when it contains both
    format-v2 and format-v3 recordings.  Each imported component retains its
    original version, so quality decisions must resolve the owning component
    instead of treating the combined container as one format.
    """

    source_hashes = _period_source_hashes(period)
    if source_hashes:
        versions: set[int] = set()
        resolved_sources = 0
        for source_hash in source_hashes:
            for component in recording.input_components:
                if str(component.get("role") or "") != "recording":
                    continue
                if (
                    str(component.get("recording_content_sha256") or "")
                    != source_hash
                ):
                    continue
                version = finite_int(component.get("format_version"))
                if version in {2, 3}:
                    versions.add(version)
                    resolved_sources += 1
                break
        if resolved_sources == len(source_hashes) and len(versions) == 1:
            return next(iter(versions))
        # Missing or mixed-format component provenance must fail closed.  It is
        # not sufficient to resolve only one member of a stitched period.
        return 0
    return recording.format_version if recording.format_version in {2, 3} else 0


def _source_finalized(
    recording: IbrecRecording,
    period: IbrecPeriod,
    *,
    end_lead_seconds: float,
    tolerance_seconds: float,
) -> bool:
    status = _manifest_status(recording, period)
    if end_lead_seconds > tolerance_seconds:
        # A container-level ``complete`` flag means the recorder finalized the
        # file.  It does not prove that an individual RTH period reached its
        # scheduled close; manually stopped and synthetic sample recordings can
        # be complete containers with hours of market data missing.
        return False

    source_version = _source_format_version(recording, period)
    period_status = period.status.strip().lower()
    close_reason = period.close_reason.strip().lower()
    if source_version == 2:
        return (
            status == "complete"
            and close_reason == "legacy_v2_manifest_schedule"
        )
    if source_version != 3:
        # A mixed combined recording has ``format_version == 0``.  If its
        # owning component cannot be resolved, treating the period as v3 would
        # allow malformed provenance to authorize a changed recommendation.
        return False

    # For format 3, only the recorder's explicit normal liquid-hours close is
    # authoritative. A finalized file can still contain a manually stopped,
    # sample, or interrupted period; container finalization must never convert
    # that incomplete market path into a complete strategy outcome.
    return period_status == "closed" and close_reason == "contract_liquid_hours"


def _feed_evidence(ticks: list[IbrecTick]) -> tuple[str, bool, bool]:
    has_live = any(tick.market_data_type == 1 for tick in ticks)
    has_delayed = any(tick.market_data_type == 3 for tick in ticks)
    has_frozen = any(tick.market_data_type in {2, 4} for tick in ticks)
    selected = "live" if has_live else ("delayed" if has_delayed else "none")
    return selected, has_live and has_delayed, has_frozen


def _all_period_ticks(
    recording: IbrecRecording,
    period: IbrecPeriod,
) -> list[IbrecTick]:
    """Return all retained feed classes for one period.

    Strategy replay deliberately prefers live rows. Quality assessment must not
    use that filtered stream, because doing so would hide delayed/frozen feed
    transitions that should prevent a changed recommendation.
    """

    if recording.format_version == 3 or period.source_recording_sha256:
        return [
            tick
            for tick in recording.ticks
            if tick.rth_period_id == period.period_id
        ]
    return [
        tick
        for tick in recording.ticks
        if period.open_timestamp <= tick.timestamp <= period.close_timestamp
    ]


def _event_gaps(ticks: list[IbrecTick]) -> list[float]:
    ordered = sorted(ticks, key=lambda item: (item.elapsed_ns, item.sequence))
    return [
        max(0.0, (right.elapsed_ns - left.elapsed_ns) / 1_000_000_000.0)
        for left, right in zip(ordered, ordered[1:])
    ]


def _last_event_metrics(
    ticks: list[IbrecTick],
    period: IbrecPeriod,
) -> tuple[int, float, float | None]:
    last_events = [
        tick
        for tick in sorted(ticks, key=lambda item: (item.elapsed_ns, item.sequence))
        if tick.has_last_event()
        and tick.last is not None
        and math.isfinite(tick.last)
        and tick.last > 0
    ]
    gaps = [
        max(0.0, (right.elapsed_ns - left.elapsed_ns) / 1_000_000_000.0)
        for left, right in zip(last_events, last_events[1:])
    ]
    duration = max(0.0, period.close_timestamp - period.open_timestamp)
    expected_minutes = max(1, int(math.ceil(duration / 60.0)))
    minute_bins = {
        int(max(0.0, min(duration - 1e-9, tick.timestamp - period.open_timestamp)) // 60.0)
        for tick in last_events
        if period.open_timestamp <= tick.timestamp < period.close_timestamp
    }
    coverage = min(100.0, len(minute_bins) / expected_minutes * 100.0)
    return len(last_events), coverage, _quantile(gaps, 0.95)


def _connectivity_events(
    recording: IbrecRecording,
    period: IbrecPeriod,
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for event in recording.quality_events:
        if not truthy(event.get("disconnect")):
            continue
        source_hash = str(event.get("source_recording_sha256") or "")
        period_hashes = _period_source_hashes(period)
        if period_hashes and source_hash and source_hash not in period_hashes:
            continue
        timestamp = finite_float(event.get("timestamp"))
        if timestamp is None:
            continue
        if period.open_timestamp <= timestamp <= period.close_timestamp:
            values.append(event)
    return values


def assess_market_replay_session(
    recording: IbrecRecording,
    period: IbrecPeriod,
    ticks: list[IbrecTick],
    config: MarketReplayConfig,
) -> MarketReplaySessionQuality:
    """Assess whether one RTH period can authorize an ATR setting change."""

    normalized = config.normalized()
    duration = max(0.001, period.close_timestamp - period.open_timestamp)
    start_lag = max(0.0, period.observed_start_timestamp - period.open_timestamp)
    raw_end_lead = max(0.0, period.close_timestamp - period.observed_end_timestamp)
    finalized = _source_finalized(
        recording,
        period,
        end_lead_seconds=raw_end_lead,
        tolerance_seconds=normalized.session_boundary_tolerance_seconds,
    )
    # A verified normal close proves that the recorder remained responsible for
    # the period through the scheduled close even when no final quote/trade
    # callback occurred.  The absence of callbacks is then market evidence, not
    # a missing tail.  Interrupted or unfinalized periods retain the last tick.
    end_lead = 0.0 if finalized else raw_end_lead
    observed_start = max(period.open_timestamp, period.observed_start_timestamp)
    observed_end = (
        period.close_timestamp
        if finalized
        else min(period.close_timestamp, period.observed_end_timestamp)
    )
    observed_duration = max(0.0, observed_end - observed_start)
    coverage_pct = min(100.0, observed_duration / duration * 100.0)
    gaps = _event_gaps(ticks)
    max_gap = max(gaps, default=0.0)
    disconnects = _connectivity_events(recording, period)
    last_count, last_coverage, last_p95 = _last_event_metrics(ticks, period)
    selected_feed, mixed_feed, frozen_feed = _feed_evidence(
        _all_period_ticks(recording, period)
    )

    reasons: list[str] = []
    if start_lag > normalized.session_boundary_tolerance_seconds:
        reasons.append(
            "The recording starts after the permitted RTH-open tolerance, so the earlier anchor, ATR, order, and position state are unknown."
        )
    if end_lead > normalized.session_boundary_tolerance_seconds:
        reasons.append(
            "The recording ends before the permitted RTH-close tolerance, so the final order and position outcomes are unknown."
        )
    if not finalized:
        reasons.append(
            "The RTH period is not proven complete by a finalized recording or a closed contract-liquid-hours period with a verified checkpoint."
        )
    if selected_feed != "live":
        reasons.append("The session does not contain a live market-data feed.")
    if mixed_feed:
        reasons.append("The session mixes live and delayed market-data feeds.")
    if frozen_feed:
        reasons.append("The session contains frozen or delayed-frozen market-data evidence.")
    if disconnects:
        reasons.append(
            f"The session contains {len(disconnects):,} recorded connectivity-loss event(s)."
        )
    if max_gap > normalized.max_market_event_gap_seconds:
        reasons.append(
            "The retained market-event stream contains a gap of "
            f"{max_gap:.1f} seconds, above the configured "
            f"{normalized.max_market_event_gap_seconds:.1f}-second limit."
        )
    if last_coverage < normalized.min_last_event_minute_coverage_pct:
        reasons.append(
            "Genuine Last events cover only "
            f"{last_coverage:.1f}% of scheduled RTH minutes, below the configured "
            f"{normalized.min_last_event_minute_coverage_pct:.1f}% requirement."
        )
    if last_p95 is None or last_p95 > normalized.max_last_event_gap_p95_seconds:
        rendered = "unavailable" if last_p95 is None else f"{last_p95:.1f} seconds"
        reasons.append(
            "The 95th-percentile genuine-Last gap is "
            f"{rendered}, above the configured "
            f"{normalized.max_last_event_gap_p95_seconds:.1f}-second limit."
        )

    return MarketReplaySessionQuality(
        session_date=period.session_date,
        period_id=period.period_id,
        source_recording_sha256=period.source_recording_sha256,
        manifest_status=_manifest_status(recording, period),
        source_finalized=finalized,
        coverage_pct=coverage_pct,
        start_lag_seconds=start_lag,
        end_lead_seconds=end_lead,
        maximum_event_gap_seconds=max_gap,
        connectivity_event_count=len(disconnects),
        last_event_count=last_count,
        last_event_minute_coverage_pct=last_coverage,
        last_event_gap_p95_seconds=last_p95,
        selected_feed=selected_feed,
        mixed_feed=mixed_feed,
        frozen_feed=frozen_feed,
        primary_eligible=not reasons,
        exclusion_reasons=tuple(reasons),
        source_recording_sha256s=_period_source_hashes(period),
    )


def summarize_last_event_gap(values: list[float]) -> float | None:
    """Public deterministic helper used by report and regression tests."""

    return float(statistics.median(values)) if values else None
