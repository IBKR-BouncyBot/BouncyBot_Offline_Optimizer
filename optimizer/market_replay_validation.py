"""Robust validation helpers for Market Replay ATR recommendations.

This module deliberately contains no file I/O and no optimizer search loop.  It
owns the score policy, Pareto comparison, continuity-block summaries, and
resampling primitives so that calculation, reporting, and tests share one
source of truth.
"""

from __future__ import annotations

import hashlib
import math
import statistics
import struct
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

from .market_replay_models import (
    AtrProfile,
    MarketReplayCandidateSummary,
    MarketReplaySessionResult,
)


@dataclass(slots=True, frozen=True)
class ScorePolicy:
    """Immutable policy for converting session evidence into one screening score."""

    key: str
    label: str
    median_return_weight: float
    mean_return_weight: float
    worst_return_weight: float
    drawdown_weight: float
    open_position_penalty: float
    right_censored_penalty: float
    turnover_multiplier: float = 1.0
    extra_execution_cost_multiplier: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


BALANCED_SCORE_POLICY = ScorePolicy(
    key="balanced",
    label="Balanced",
    median_return_weight=0.50,
    mean_return_weight=0.30,
    worst_return_weight=0.20,
    drawdown_weight=0.35,
    open_position_penalty=25.0,
    right_censored_penalty=15.0,
)

DRAWDOWN_SCORE_POLICY = ScorePolicy(
    key="drawdown_focused",
    label="Drawdown focused",
    median_return_weight=0.35,
    mean_return_weight=0.15,
    worst_return_weight=0.50,
    drawdown_weight=0.70,
    open_position_penalty=35.0,
    right_censored_penalty=25.0,
    turnover_multiplier=1.25,
)

RETURN_SCORE_POLICY = ScorePolicy(
    key="return_focused",
    label="Return focused",
    median_return_weight=0.55,
    mean_return_weight=0.35,
    worst_return_weight=0.10,
    drawdown_weight=0.20,
    open_position_penalty=20.0,
    right_censored_penalty=10.0,
    turnover_multiplier=0.75,
)

COST_STRESSED_SCORE_POLICY = ScorePolicy(
    key="cost_stressed",
    label="Cost stressed",
    median_return_weight=0.50,
    mean_return_weight=0.30,
    worst_return_weight=0.20,
    drawdown_weight=0.35,
    open_position_penalty=25.0,
    right_censored_penalty=15.0,
    turnover_multiplier=1.50,
    # Session returns already contain the configured execution reserve.  This
    # term applies an additional conservative penalty equal to half of the
    # recorded execution-cost total per session.
    extra_execution_cost_multiplier=0.50,
)

SCORE_POLICIES = (
    BALANCED_SCORE_POLICY,
    DRAWDOWN_SCORE_POLICY,
    RETURN_SCORE_POLICY,
    COST_STRESSED_SCORE_POLICY,
)


def score_policy_contract() -> list[dict[str, Any]]:
    """Return the exact policies used by calculation and report generation."""

    return [policy.to_dict() for policy in SCORE_POLICIES]


def score_sessions(
    sessions: Sequence[MarketReplaySessionResult],
    *,
    turnover_penalty_bps_per_completed_trade: float,
    policy: ScorePolicy = BALANCED_SCORE_POLICY,
) -> dict[str, float]:
    """Return deterministic score components for one list of session results.

    No-trade sessions remain in the denominator and contribute a zero return,
    but there is intentionally no direct no-trade penalty.  Costs, turnover,
    unresolved positions, censoring, and drawdown remain explicit.
    """

    if not sessions:
        return {
            "score": -math.inf,
            "median_return_bps": 0.0,
            "mean_return_bps": 0.0,
            "worst_return_bps": 0.0,
            "maximum_drawdown_bps": 0.0,
            "open_position_fraction": 0.0,
            "right_censored_fraction": 0.0,
            "average_completed_trades": 0.0,
            "turnover_penalty_points": 0.0,
            "extra_execution_cost_penalty_points": 0.0,
        }

    returns = [float(item.conservative_return_bps) for item in sessions]
    session_count = len(sessions)
    median_return = float(statistics.median(returns))
    mean_return = float(statistics.fmean(returns))
    worst_return = min(returns)
    maximum_drawdown = max(float(item.chain_max_drawdown_bps) for item in sessions)
    open_fraction = (
        sum(1 for item in sessions if item.terminal_open_position) / session_count
    )
    censored_fraction = (
        sum(1 for item in sessions if item.right_censored) / session_count
    )
    completed_trades = sum(int(item.completed_trades) for item in sessions)
    average_completed_trades = completed_trades / session_count
    turnover_penalty = (
        float(turnover_penalty_bps_per_completed_trade)
        * average_completed_trades
        * policy.turnover_multiplier
    )
    average_execution_cost = (
        sum(max(0.0, float(item.total_execution_cost_bps)) for item in sessions)
        / session_count
    )
    extra_cost_penalty = (
        average_execution_cost * policy.extra_execution_cost_multiplier
    )
    score = (
        policy.median_return_weight * median_return
        + policy.mean_return_weight * mean_return
        + policy.worst_return_weight * worst_return
        - policy.drawdown_weight * maximum_drawdown
        - policy.open_position_penalty * open_fraction
        - policy.right_censored_penalty * censored_fraction
        - turnover_penalty
        - extra_cost_penalty
    )
    return {
        "score": score,
        "median_return_bps": median_return,
        "mean_return_bps": mean_return,
        "worst_return_bps": worst_return,
        "maximum_drawdown_bps": maximum_drawdown,
        "open_position_fraction": open_fraction,
        "right_censored_fraction": censored_fraction,
        "average_completed_trades": average_completed_trades,
        "turnover_penalty_points": turnover_penalty,
        "extra_execution_cost_penalty_points": extra_cost_penalty,
    }


def score_policy_comparison(
    candidate_sessions: Sequence[MarketReplaySessionResult],
    control_sessions: Sequence[MarketReplaySessionResult],
    *,
    turnover_penalty_bps_per_completed_trade: float,
) -> list[dict[str, Any]]:
    """Compare candidate and control under every predefined score policy."""

    rows: list[dict[str, Any]] = []
    for policy in SCORE_POLICIES:
        candidate = score_sessions(
            candidate_sessions,
            turnover_penalty_bps_per_completed_trade=(
                turnover_penalty_bps_per_completed_trade
            ),
            policy=policy,
        )
        control = score_sessions(
            control_sessions,
            turnover_penalty_bps_per_completed_trade=(
                turnover_penalty_bps_per_completed_trade
            ),
            policy=policy,
        )
        candidate_score = float(candidate["score"])
        control_score = float(control["score"])
        rows.append(
            {
                "policy_key": policy.key,
                "policy_label": policy.label,
                "candidate_score": candidate_score,
                "control_score": control_score,
                "score_delta": candidate_score - control_score,
                "passed": candidate_score - control_score > 0.0,
                "policy": policy.to_dict(),
            }
        )
    return rows


def _participation_rate(summary: MarketReplayCandidateSummary) -> float:
    return (
        summary.sessions_with_trades / summary.sessions
        if summary.sessions > 0
        else 0.0
    )


def pareto_dominates(
    left: MarketReplayCandidateSummary,
    right: MarketReplayCandidateSummary,
    *,
    tolerance: float = 1e-9,
) -> bool:
    """Return whether ``left`` is at least as good everywhere and better once."""

    maximizing = (
        (left.median_return_bps, right.median_return_bps),
        (left.mean_return_bps, right.mean_return_bps),
        (left.worst_return_bps, right.worst_return_bps),
        (_participation_rate(left), _participation_rate(right)),
    )
    minimizing = (
        (left.maximum_drawdown_bps, right.maximum_drawdown_bps),
        (left.right_censored_rate_pct, right.right_censored_rate_pct),
        (
            left.average_completed_trades_per_session,
            right.average_completed_trades_per_session,
        ),
        (left.total_execution_cost_bps, right.total_execution_cost_bps),
    )
    no_worse = all(a + tolerance >= b for a, b in maximizing) and all(
        a <= b + tolerance for a, b in minimizing
    )
    strictly_better = any(a > b + tolerance for a, b in maximizing) or any(
        a + tolerance < b for a, b in minimizing
    )
    return no_worse and strictly_better


def pareto_frontier(
    candidates: Sequence[MarketReplayCandidateSummary],
) -> set[str]:
    """Return profile keys that are not dominated by another candidate."""

    frontier: set[str] = set()
    for candidate in candidates:
        if not any(
            other.profile.key() != candidate.profile.key()
            and pareto_dominates(other, candidate)
            for other in candidates
        ):
            frontier.add(candidate.profile.key())
    return frontier


def continuity_block_metrics(
    sessions: Sequence[MarketReplaySessionResult],
) -> list[dict[str, Any]]:
    """Aggregate economic evidence across overnight continuity chains."""

    grouped: dict[int, list[MarketReplaySessionResult]] = {}
    for session in sorted(
        sessions,
        key=lambda item: (item.session_date, item.period_id),
    ):
        grouped.setdefault(int(session.continuity_chain_id), []).append(session)

    rows: list[dict[str, Any]] = []
    for chain_id in sorted(grouped):
        chain = grouped[chain_id]
        start_equity = float(chain[0].session_start_equity)
        end_equity = float(chain[-1].session_end_equity)
        endpoints = [start_equity, *(float(item.session_end_equity) for item in chain)]
        peak = endpoints[0] if endpoints else 1.0
        trough_relative = 0.0
        favorable_relative = 0.0
        maximum_drawdown = 0.0
        underwater_sessions = 0
        for equity in endpoints:
            if not math.isfinite(equity) or equity <= 0:
                continue
            peak = max(peak, equity)
            favorable_relative = max(
                favorable_relative,
                (equity / start_equity - 1.0) * 10_000.0
                if start_equity > 0
                else 0.0,
            )
            trough_relative = min(
                trough_relative,
                (equity / start_equity - 1.0) * 10_000.0
                if start_equity > 0
                else 0.0,
            )
            drawdown = (peak - equity) / peak * 10_000.0 if peak > 0 else 0.0
            maximum_drawdown = max(maximum_drawdown, drawdown)
            if equity + 1e-12 < peak:
                underwater_sessions += 1
        rows.append(
            {
                "continuity_chain_id": chain_id,
                "start_session_date": chain[0].session_date,
                "end_session_date": chain[-1].session_date,
                "session_count": len(chain),
                "overnight_boundaries": sum(
                    1
                    for item in chain[:-1]
                    if item.carried_position_out or item.carried_sell_trail_out
                ),
                "start_equity": start_equity,
                "end_equity": end_equity,
                "block_return_bps": (
                    (end_equity / start_equity - 1.0) * 10_000.0
                    if start_equity > 0
                    else 0.0
                ),
                "maximum_drawdown_bps": max(
                    maximum_drawdown,
                    max(float(item.chain_max_drawdown_bps) for item in chain),
                ),
                "maximum_adverse_excursion_bps": trough_relative,
                "maximum_favorable_excursion_bps": favorable_relative,
                "underwater_session_endpoints": underwater_sessions,
                "overnight_gap_contribution_bps": sum(
                    float(item.overnight_gap_return_bps or 0.0) for item in chain
                ),
                "completed_trades": sum(int(item.completed_trades) for item in chain),
                "right_censored": any(item.right_censored for item in chain),
                "terminal_open_position": bool(chain[-1].terminal_open_position),
            }
        )
    return rows


def moving_block_length(unit_count: int) -> int | None:
    """Return the conservative circular moving-block length for a sample size."""

    if unit_count < 15:
        return None
    if unit_count < 30:
        return 2
    if unit_count < 60:
        return 3
    return min(5, max(3, int(round(unit_count ** (1.0 / 3.0)))))


def deterministic_moving_block_indices(
    seed: str,
    *,
    replicates: int,
    unit_count: int,
    block_length: int,
) -> Iterable[tuple[int, ...]]:
    """Yield deterministic circular moving-block samples of ``unit_count`` units."""

    if replicates <= 0 or unit_count <= 0 or block_length <= 0:
        return
    blocks_per_rep = math.ceil(unit_count / block_length)
    stream = hashlib.shake_256(
        (
            "market-replay-moving-block-v1|"
            f"{seed}|{replicates}|{unit_count}|{block_length}"
        ).encode("utf-8")
    ).digest(replicates * blocks_per_rep * 8)
    values = struct.iter_unpack(">Q", stream)
    for _ in range(replicates):
        sample: list[int] = []
        for _block in range(blocks_per_rep):
            start = next(values)[0] % unit_count
            sample.extend(
                (start + offset) % unit_count for offset in range(block_length)
            )
        yield tuple(sample[:unit_count])


def expanding_walk_forward_folds(
    dates: Sequence[str],
    *,
    minimum_training_days: int = 15,
    validation_days: int = 5,
) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Return deterministic expanding chronological train/validation folds."""

    ordered = tuple(sorted(dict.fromkeys(dates)))
    if minimum_training_days < 1 or validation_days < 1:
        raise ValueError("Walk-forward lengths must be positive integers.")
    folds: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    training_end = minimum_training_days
    while training_end + validation_days <= len(ordered):
        folds.append(
            (
                ordered[:training_end],
                ordered[training_end : training_end + validation_days],
            )
        )
        training_end += validation_days
    return folds


def profile_window(profile: AtrProfile) -> tuple[int, int]:
    return profile.period, profile.bar_seconds


def recommendation_gate_rows(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize recommendation gates into deterministic report order."""

    normalized = []
    for row in rows:
        key = str(row.get("gate_key") or "").strip()
        if not key:
            raise ValueError("Every recommendation gate requires a non-empty gate_key.")
        normalized.append(
            {
                "gate_key": key,
                "gate_label": str(row.get("gate_label") or key),
                "required": bool(row.get("required", True)),
                "passed": bool(row.get("passed", False)),
                "detail": str(row.get("detail") or ""),
            }
        )
    return sorted(normalized, key=lambda item: item["gate_key"])
