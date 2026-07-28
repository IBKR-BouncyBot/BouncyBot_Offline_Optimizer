"""Deterministic reports for the standalone Market Replay optimization workflow."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .atomic_publish import atomic_publish_directory
from .market_replay_models import MarketReplayAnalysisResult
from .version import APP_NAME, APP_VERSION

_CSS = """
:root { color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; }
body { margin: 0; background: #f5f7fa; color: #16202a; line-height: 1.5; }
main { max-width: 1320px; margin: 0 auto; padding: 28px; }
h1, h2, h3 { color: #123f67; }
section { background: white; border: 1px solid #d8e0e8; border-radius: 8px; padding: 20px; margin: 16px 0; }
.notice { border-left: 5px solid #2c6fb7; background: #eef6ff; }
.warning { border-left: 5px solid #b76a00; background: #fff5e5; }
.good { border-left: 5px solid #247044; background: #eff9f2; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
.card { border: 1px solid #dbe3ea; border-radius: 6px; padding: 12px; background: #fbfcfd; }
.metric { font-size: 1.35rem; font-weight: 650; }
.muted { color: #596775; }
.scroll { overflow: auto; max-height: 720px; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
th, td { border: 1px solid #d9e0e6; padding: 7px 9px; text-align: left; vertical-align: top; }
th { position: sticky; top: 0; background: #eaf1f7; z-index: 1; }
code, pre, .formula { font-family: Consolas, Menlo, monospace; }
.formula { white-space: pre-wrap; background: #f5f7f9; border: 1px solid #d9e0e6; padding: 10px; }
a { color: #005ea8; }
"""

_FILE_NAMES = (
    "index.html",
    "market_replay_analysis.json",
    "input_recordings.csv",
    "excluded_sessions.csv",
    "session_quality.csv",
    "atr_window_search.csv",
    "candidate_results.csv",
    "robustness_evidence.csv",
    "recommendation_leave_one_day_out.csv",
    "recommended_atr_settings.csv",
    "recommended_session_results.csv",
    "recommended_simulated_trades.csv",
    "control_session_results.csv",
    "execution_calibration.csv",
    "continuity_evidence.csv",
    "continuity_block_evidence.csv",
    "score_policy_evidence.csv",
    "moving_block_evidence.csv",
    "selection_bootstrap_evidence.csv",
    "walk_forward_evidence.csv",
    "pareto_frontier.csv",
    "search_boundary_evidence.csv",
    "assumption_stress_evidence.csv",
    "recommendation_quality_gates.csv",
    "data_quality_issues.csv",
    "README_REPORT.txt",
    "analysis_manifest.json",
    "SHA256SUMS.txt",
)

_CLAMP_COMPONENTS = (
    "initial_drop",
    "buy_rebound",
    "minimum_profit",
    "sell_trail",
)


def _flatten_clamp_rates(value: Any) -> dict[str, Any]:
    """Return stable scalar columns for per-component clamp evidence."""

    rates = getattr(value, "clamp_component_rates_pct", {}) or {}
    flattened: dict[str, Any] = {}
    for component in _CLAMP_COMPONENTS:
        component_rates = rates.get(component, {})
        for state in ("min", "max", "raw"):
            flattened[f"{component}_{state}_clamp_rate_pct"] = component_rates.get(
                state,
                0.0,
            )
        flattened[f"{component}_zero_mode_rate_pct"] = component_rates.get(
            "zero",
            0.0,
        )
    return flattened


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    raise TypeError(f"Unsupported report value type: {type(value).__name__}")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(
            _json_safe(value),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _csv_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(
            _json_safe(value),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    """Write rows with ``fields`` leading, plus every key seen in any row.

    Several evidence tables legitimately mix row schemas: combine-time session
    exclusions carry fewer keys than quality-gate exclusions, and gate-skipped
    robustness rows carry fewer keys than evaluated ones.  Before version
    1.9.3 the header came from the first row alone, silently dropping the
    extra columns for the whole file.  The union below preserves the caller's
    column order, appends additional keys in first-seen row order, and leaves
    homogeneous files byte-identical.
    """

    ordered = list(dict.fromkeys(str(field) for field in fields))
    seen = set(ordered)
    for row in rows:
        for key in row:
            name = str(key)
            if name not in seen:
                seen.add(name)
                ordered.append(name)
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=ordered, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in ordered})


def _escape(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if not math.isfinite(value):
            return "—"
        return html.escape(f"{value:.4f}".rstrip("0").rstrip("."))
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(
            _json_safe(value),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
    return html.escape(str(value))


def _table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], *, empty: str) -> str:
    if not rows:
        return f"<p class='muted'>{_escape(empty)}</p>"
    header = "".join(f"<th>{_escape(label)}</th>" for _, label in columns)
    body: list[str] = []
    for row in rows:
        cells = "".join(f"<td>{_escape(row.get(key))}</td>" for key, _ in columns)
        body.append(f"<tr>{cells}</tr>")
    return (
        "<div class='scroll'><table><thead><tr>"
        + header
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )


def _profile_row(result: MarketReplayAnalysisResult) -> dict[str, Any]:
    profile = result.recommendation.profile
    return {
        "symbol": result.recording.symbol,
        "profile_key": profile.key(),
        "atr_period": profile.period,
        "atr_bar_seconds": profile.bar_seconds,
        "atr_initial_drop_multiplier": profile.initial_drop_multiplier,
        "atr_buy_rebound_multiplier": profile.buy_rebound_multiplier,
        "atr_minimum_profit_multiplier": profile.minimum_profit_multiplier,
        "atr_sell_trail_multiplier": profile.sell_trail_multiplier,
        "atr_min_pct": profile.min_atr_pct,
        "atr_max_pct": profile.max_atr_pct,
        "screening_score": result.recommendation.score,
        "evidence_stable": result.recommendation.evidence_stable,
        "stable_region_id": result.recommendation.stable_region_id,
        "stable_region_size": result.recommendation.stable_region_size,
        "robustness_evaluated": result.recommendation.robustness_evaluated,
        "robustness_passed": result.recommendation.robustness_passed,
        "control_score_delta": result.recommendation.control_score_delta,
        "paired_trading_days": result.recommendation.paired_trading_days,
        "bootstrap_unit_type": result.recommendation.bootstrap_unit_type,
        "bootstrap_independent_units": (
            result.recommendation.bootstrap_independent_units
        ),
        "bootstrap_ci80_low": result.recommendation.bootstrap_ci80_low,
        "bootstrap_ci80_high": result.recommendation.bootstrap_ci80_high,
        "bootstrap_probability_positive_pct": (
            result.recommendation.bootstrap_probability_positive_pct
        ),
        "leave_one_day_out_min_delta": (
            result.recommendation.leave_one_day_out_min_delta
        ),
        "leave_one_day_out_sign_reversals": (
            result.recommendation.leave_one_day_out_sign_reversals
        ),
        "leave_one_day_out_exact_profile_selection_pct": (
            result.recommendation.leave_one_day_out_exact_profile_selection_pct
        ),
        "leave_one_day_out_same_window_selection_pct": (
            result.recommendation.leave_one_day_out_same_window_selection_pct
        ),
        "leave_one_day_out_selection_mode": (
            result.recommendation.leave_one_day_out_selection_mode
        ),
        "paired_median_return_delta_bps": (
            result.recommendation.paired_median_return_delta_bps
        ),
        "paired_positive_day_pct": result.recommendation.paired_positive_day_pct,
        "control_trade_day_retention_pct": (
            result.recommendation.control_trade_day_retention_pct
        ),
        "maximum_drawdown_delta_bps": (
            result.recommendation.maximum_drawdown_delta_bps
        ),
        "worst_return_delta_bps": result.recommendation.worst_return_delta_bps,
        "atr_phase_cases": result.recommendation.atr_phase_cases,
        "atr_phase_min_score_delta": (
            result.recommendation.atr_phase_min_score_delta
        ),
        "atr_phase_adverse_score_delta": (
            result.recommendation.atr_phase_adverse_score_delta
        ),
        "moving_block_ci80_low": result.recommendation.moving_block_ci80_low,
        "moving_block_ci80_high": result.recommendation.moving_block_ci80_high,
        "moving_block_probability_positive_pct": (
            result.recommendation.moving_block_probability_positive_pct
        ),
        "selection_bootstrap_oob_evaluations": (
            result.recommendation.selection_bootstrap_oob_evaluations
        ),
        "selection_bootstrap_probability_positive_pct": (
            result.recommendation.selection_bootstrap_probability_positive_pct
        ),
        "selection_bootstrap_median_oob_delta": (
            result.recommendation.selection_bootstrap_median_oob_delta
        ),
        "walk_forward_folds": result.recommendation.walk_forward_folds,
        "walk_forward_probability_positive_pct": (
            result.recommendation.walk_forward_probability_positive_pct
        ),
        "walk_forward_median_delta": result.recommendation.walk_forward_median_delta,
        "walk_forward_worst_delta": result.recommendation.walk_forward_worst_delta,
        "walk_forward_same_window_pct": (
            result.recommendation.walk_forward_same_window_pct
        ),
        "score_policy_all_positive": (
            result.recommendation.score_policy_all_positive
        ),
        "pareto_frontier": result.recommendation.pareto_frontier,
        "boundary_resolved": result.recommendation.boundary_resolved,
        "assumption_stress_all_positive": (
            result.recommendation.assumption_stress_all_positive
        ),
        "unmarked_open_position_sessions": (
            result.recommendation.unmarked_open_position_sessions
        ),
        "unmarked_open_position_rate_pct": (
            result.recommendation.unmarked_open_position_rate_pct
        ),
        "exploratory_only": result.exploratory_only,
        "average_completed_trades_per_session": (
            result.recommendation.average_completed_trades_per_session
        ),
        "turnover_penalty_points": result.recommendation.turnover_penalty_points,
        "total_execution_cost_bps": result.recommendation.total_execution_cost_bps,
        "touch_liquidity_coverage_pct": (
            result.recommendation.touch_liquidity_coverage_pct
        ),
        "minimum_clamp_rate_pct": result.recommendation.clamp_min_rate_pct,
        "maximum_clamp_rate_pct": result.recommendation.clamp_max_rate_pct,
        **_flatten_clamp_rates(result.recommendation),
        "primary_eligible_sessions": result.recommendation.primary_eligible_sessions,
        "excluded_quality_sessions": result.recommendation.excluded_quality_sessions,
        "selection_reason": result.recommendation_reason,
        "instability_reasons": " | ".join(result.recommendation.instability_reasons),
    }


def _candidate_rows(result: MarketReplayAnalysisResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rank, candidate in enumerate(result.candidates, start=1):
        row = candidate.to_dict()
        profile = row.pop("profile")
        row = {
            "rank": rank,
            "profile_key": candidate.profile.key(),
            "recommended": candidate.profile.key() == result.recommendation.profile.key(),
            **profile,
            **row,
            **_flatten_clamp_rates(candidate),
        }
        row["instability_reasons"] = " | ".join(candidate.instability_reasons)
        rows.append(row)
    return rows


def _session_rows(items: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        row = asdict(item)
        row.update(_flatten_clamp_rates(item))
        row["issues"] = " | ".join(row.get("issues") or [])
        rows.append(row)
    return rows


def _trade_rows(result: MarketReplayAnalysisResult) -> list[dict[str, Any]]:
    return [asdict(item) for item in result.recommended_trades]


def _balanced_score_formula(result: MarketReplayAnalysisResult) -> str:
    """Render the active balanced score from the analysis contract."""

    policies = result.search_contract.get("score_policies")
    rows = policies if isinstance(policies, list) else []
    balanced = next(
        (
            row
            for row in rows
            if isinstance(row, dict) and row.get("key") == "balanced"
        ),
        None,
    )
    if not isinstance(balanced, dict):
        return "Balanced score policy metadata unavailable."
    return (
        f"score = {balanced['median_return_weight']:.2f} × median conservative session return\n"
        f"      + {balanced['mean_return_weight']:.2f} × mean conservative session return\n"
        f"      + {balanced['worst_return_weight']:.2f} × worst conservative session return\n"
        f"      − {balanced['drawdown_weight']:.2f} × maximum drawdown\n"
        f"      − {balanced['open_position_penalty']:.2f} × open-position session fraction\n"
        f"      − {balanced['right_censored_penalty']:.2f} × right-censored session fraction\n"
        "      − configured turnover penalty per completed trade"
    )


def _cards(result: MarketReplayAnalysisResult) -> str:
    rec = result.recording
    recommendation = result.recommendation
    calibration = result.execution_calibration
    calibration_status = (
        "Applied"
        if calibration.get("applied")
        else "Available; fallback retained"
        if calibration.get("enabled")
        else "Disabled"
    )
    values = [
        ("Ticker", rec.symbol),
        ("Input recordings", rec.input_recording_count),
        ("IBREC formats", f"{rec.format_label} · {rec.container_format}"),
        ("Raw / retained rows", f"{rec.raw_row_count:,} / {rec.retained_row_count:,}"),
        ("RTH sessions", len(rec.periods)),
        ("Primary-eligible sessions", recommendation.primary_eligible_sessions),
        ("Exploratory only", "Yes" if result.exploratory_only else "No"),
        ("Candidates", f"{len(result.candidates):,}"),
        ("Recommended score", f"{recommendation.score:.2f}"),
        ("Stable evidence", "Yes" if recommendation.evidence_stable else "No"),
        ("Synthetic source", "Yes" if rec.is_synthetic else "No"),
        ("Completed simulated trades", recommendation.completed_trades),
        ("Average trades / session", f"{recommendation.average_completed_trades_per_session:.2f}"),
        ("Modeled execution costs", f"{recommendation.total_execution_cost_bps:.2f} bps"),
        ("Execution calibration", calibration_status),
        (
            "Effective cost reserve / side",
            f"{float(calibration.get('effective_execution_cost_bps_per_side') or 0.0):.4f} bps",
        ),
        (
            "Effective trade notional",
            f"{float(calibration.get('effective_trade_notional') or 0.0):,.2f}",
        ),
        (
            "Overnight carry",
            "Enabled"
            if result.search_contract.get("continuous_overnight_replay")
            else "Disabled",
        ),
        (
            "Continuity chains",
            len(
                {
                    int(row.get("continuity_chain_id") or 0)
                    for row in result.continuity_evidence
                    if int(row.get("continuity_chain_id") or 0) > 0
                }
            ),
        ),
        (
            "Touch-size sufficiency",
            (
                f"{recommendation.touch_liquidity_coverage_pct:.1f}%"
                if recommendation.touch_liquidity_coverage_pct is not None
                else "Unavailable"
            ),
        ),
        ("Minimum-clamp decisions", f"{recommendation.clamp_min_rate_pct:.1f}%"),
        ("Maximum-clamp decisions", f"{recommendation.clamp_max_rate_pct:.1f}%"),
        ("Right-censored sessions", recommendation.right_censored_sessions),
        (
            "Unmarked open positions",
            recommendation.unmarked_open_position_sessions,
        ),
        ("Excluded dates", len(rec.excluded_sessions)),
        ("Paired robustness days", recommendation.paired_trading_days),
        ("Bootstrap unit", recommendation.bootstrap_unit_type),
        ("Independent bootstrap units", recommendation.bootstrap_independent_units),
        (
            "Bootstrap positive",
            (
                f"{recommendation.bootstrap_probability_positive_pct:.1f}%"
                if recommendation.bootstrap_probability_positive_pct is not None
                else "Not available"
            ),
        ),
        (
            "LOO window advanced",
            (
                f"{recommendation.leave_one_day_out_same_window_selection_pct:.1f}%"
                if recommendation.leave_one_day_out_same_window_selection_pct
                is not None
                else "Not available"
            ),
        ),
    ]
    return "<div class='grid'>" + "".join(
        f"<div class='card'><div class='muted'>{_escape(label)}</div>"
        f"<div class='metric'>{_escape(value)}</div></div>"
        for label, value in values
    ) + "</div>"


def _html(result: MarketReplayAnalysisResult) -> str:
    rec = result.recording
    profile = _profile_row(result)
    profile_table = _table(
        [profile],
        [
            ("atr_period", "ATR period"),
            ("atr_bar_seconds", "Bar seconds"),
            ("atr_initial_drop_multiplier", "Initial drop × ATR"),
            ("atr_buy_rebound_multiplier", "BUY rebound × ATR"),
            ("atr_minimum_profit_multiplier", "Minimum profit × ATR"),
            ("atr_sell_trail_multiplier", "SELL trail × ATR"),
            ("atr_min_pct", "Minimum clamp %"),
            ("atr_max_pct", "Maximum clamp %"),
            ("screening_score", "Screening score"),
            ("evidence_stable", "Evidence stable"),
            ("stable_region_id", "Stable region"),
            ("stable_region_size", "Region size"),
            ("robustness_passed", "Robustness passed"),
            ("control_score_delta", "Score delta vs control"),
            ("paired_trading_days", "Paired trading days"),
            ("bootstrap_unit_type", "Bootstrap unit"),
            ("bootstrap_independent_units", "Independent units"),
            ("bootstrap_ci80_low", "Bootstrap 80% low"),
            ("bootstrap_ci80_high", "Bootstrap 80% high"),
            ("bootstrap_probability_positive_pct", "Bootstrap positive %"),
            ("leave_one_day_out_min_delta", "Worst leave-one-day-out delta"),
            (
                "leave_one_day_out_same_window_selection_pct",
                "LOO window advanced %",
            ),
            (
                "leave_one_day_out_exact_profile_selection_pct",
                "LOO exact profile %",
            ),
            ("leave_one_day_out_selection_mode", "LOO selection mode"),
            ("average_completed_trades_per_session", "Average trades / session"),
            ("turnover_penalty_points", "Turnover penalty points"),
            ("total_execution_cost_bps", "Modeled execution cost bps"),
            ("touch_liquidity_coverage_pct", "Touch-size sufficiency %"),
            ("minimum_clamp_rate_pct", "Minimum-clamp decisions %"),
            ("maximum_clamp_rate_pct", "Maximum-clamp decisions %"),
            ("initial_drop_min_clamp_rate_pct", "Initial-drop min-clamp %"),
            ("buy_rebound_min_clamp_rate_pct", "BUY-rebound min-clamp %"),
            ("minimum_profit_min_clamp_rate_pct", "Minimum-profit min-clamp %"),
            ("sell_trail_min_clamp_rate_pct", "SELL-trail min-clamp %"),
            ("exploratory_only", "Exploratory only"),
        ],
        empty="No recommendation was generated.",
    )
    sessions = _session_rows(result.recommended_sessions)
    session_table = _table(
        sessions,
        [
            ("session_date", "Session"),
            ("continuity_chain_id", "Continuity chain"),
            ("continuity_broken_before", "Chain break before"),
            ("continuity_break_reason", "Chain-break reason"),
            ("carried_position_in", "Position carried in"),
            ("carried_position_out", "Position carried out"),
            ("carried_sell_trail_in", "SELL trail carried in"),
            ("carried_sell_trail_out", "SELL trail carried out"),
            ("terminal_open_position", "Terminal open position"),
            ("ticks", "Ticks"),
            ("trades", "Trades"),
            ("completed_trades", "Completed"),
            ("open_position", "Open at end"),
            ("open_entry_setup", "Open BUY setup"),
            ("right_censored", "Right-censored"),
            ("primary_eligible", "Primary eligible"),
            ("coverage_pct", "RTH coverage %"),
            ("maximum_event_gap_seconds", "Max event gap s"),
            ("connectivity_event_count", "Connectivity events"),
            ("last_event_minute_coverage_pct", "Last-event minute coverage %"),
            ("last_event_gap_p95_seconds", "Last-event gap p95 s"),
            ("unmarked_open_position", "Unmarked open position"),
            ("conservative_return_bps", "Conservative return bps"),
            ("marked_return_bps", "Marked return bps"),
            ("session_start_equity", "Start equity"),
            ("session_end_equity", "End equity"),
            ("overnight_gap_return_bps", "Overnight gap return bps"),
            ("max_drawdown_bps", "Max drawdown bps"),
            ("session_max_drawdown_bps", "Session drawdown bps"),
            ("chain_max_drawdown_bps", "Continuity-chain drawdown bps"),
            ("total_execution_cost_bps", "Execution cost bps"),
            ("touch_liquidity_coverage_pct", "Touch-size sufficiency %"),
            ("clamp_min_rate_pct", "Minimum-clamp %"),
            ("clamp_max_rate_pct", "Maximum-clamp %"),
            ("initial_drop_min_clamp_rate_pct", "Drop min-clamp %"),
            ("buy_rebound_min_clamp_rate_pct", "BUY min-clamp %"),
            ("minimum_profit_min_clamp_rate_pct", "Profit min-clamp %"),
            ("sell_trail_min_clamp_rate_pct", "SELL min-clamp %"),
            ("issues", "Issues"),
        ],
        empty="No analyzable sessions were available.",
    )
    candidates = _candidate_rows(result)[:250]
    candidate_table = _table(
        candidates,
        [
            ("rank", "Rank"),
            ("recommended", "Recommended"),
            ("period", "Period"),
            ("bar_seconds", "Bar seconds"),
            ("initial_drop_multiplier", "Drop ×"),
            ("buy_rebound_multiplier", "BUY ×"),
            ("minimum_profit_multiplier", "Profit ×"),
            ("sell_trail_multiplier", "SELL ×"),
            ("min_atr_pct", "Minimum clamp %"),
            ("score", "Score"),
            ("completed_trades", "Completed trades"),
            ("median_return_bps", "Median return bps"),
            ("worst_return_bps", "Worst return bps"),
            ("maximum_drawdown_bps", "Max drawdown bps"),
            ("average_completed_trades_per_session", "Average trades / session"),
            ("turnover_penalty_points", "Turnover penalty"),
            ("total_execution_cost_bps", "Execution cost bps"),
            ("touch_liquidity_coverage_pct", "Touch-size sufficiency %"),
            ("clamp_min_rate_pct", "Minimum-clamp %"),
            ("clamp_max_rate_pct", "Maximum-clamp %"),
            ("initial_drop_min_clamp_rate_pct", "Drop min-clamp %"),
            ("buy_rebound_min_clamp_rate_pct", "BUY min-clamp %"),
            ("minimum_profit_min_clamp_rate_pct", "Profit min-clamp %"),
            ("sell_trail_min_clamp_rate_pct", "SELL min-clamp %"),
            ("open_position_rate_pct", "Open %"),
            ("right_censored_rate_pct", "Right-censored %"),
            ("unmarked_open_position_rate_pct", "Unmarked open %"),
            ("no_trade_rate_pct", "No-trade %"),
            ("stable_region_id", "Stable region"),
            ("robustness_evaluated", "Robustness checked"),
            ("robustness_passed", "Robustness passed"),
            ("control_score_delta", "Delta vs control"),
            ("bootstrap_ci80_low", "Bootstrap 80% low"),
            ("bootstrap_probability_positive_pct", "Bootstrap positive %"),
            ("leave_one_day_out_min_delta", "Worst LOO delta"),
            ("paired_median_return_delta_bps", "Median day return Δ bps"),
            ("paired_positive_day_pct", "Better days %"),
            ("control_trade_day_retention_pct", "Trade-day retention %"),
            ("maximum_drawdown_delta_bps", "Max DD Δ bps"),
            ("worst_return_delta_bps", "Worst return Δ bps"),
            ("atr_phase_min_score_delta", "Worst global phase Δ"),
            ("atr_phase_adverse_score_delta", "Adverse session-phase Δ"),
            (
                "leave_one_day_out_same_window_selection_pct",
                "LOO window advanced %",
            ),
        ],
        empty="No candidate rows were generated.",
    )
    window_table = _table(
        result.window_search,
        [
            ("stage", "Stage"),
            ("rank", "Rank"),
            ("period", "ATR period"),
            ("bar_seconds", "Bar seconds"),
            ("selected_for_next_stage", "Advanced"),
            ("score", "Representative-profile score"),
            ("completed_trades", "Completed trades"),
            ("right_censored_rate_pct", "Right-censored %"),
            ("selection_basis", "Selection basis"),
        ],
        empty="No ATR-window search evidence was generated.",
    )
    robustness_table = _table(
        result.robustness_evidence,
        [
            ("candidate_profile_key", "Candidate"),
            ("stable_region_id", "Stable region"),
            ("paired_trading_days", "Paired days"),
            ("bootstrap_unit_type", "Bootstrap unit"),
            ("bootstrap_independent_units", "Independent units"),
            ("observed_score_delta", "Observed delta"),
            ("bootstrap_ci80_low", "Bootstrap 80% low"),
            ("bootstrap_ci80_high", "Bootstrap 80% high"),
            ("bootstrap_probability_positive_pct", "Bootstrap positive %"),
            ("leave_one_day_out_min_delta", "Worst LOO delta"),
            ("paired_median_return_delta_bps", "Median day return Δ bps"),
            ("paired_positive_day_pct", "Better days %"),
            ("control_trade_day_retention_pct", "Trade-day retention %"),
            ("maximum_drawdown_delta_bps", "Max DD Δ bps"),
            ("worst_return_delta_bps", "Worst return Δ bps"),
            ("atr_phase_min_score_delta", "Worst global phase Δ"),
            ("atr_phase_adverse_score_delta", "Adverse session-phase Δ"),
            ("leave_one_day_out_sign_reversals", "LOO sign reversals"),
            (
                "same_atr_window_selection_pct",
                "LOO window advanced %",
            ),
            ("exact_profile_selection_pct", "LOO exact profile %"),
            ("leave_one_day_out_selection_mode", "LOO selection mode"),
            ("eligible_for_changed_recommendation", "Eligible change"),
            ("failure_reasons", "Failure reasons"),
        ],
        empty="No changed stable-region center was available for robustness evaluation.",
    )
    leave_one_out_table = _table(
        result.recommendation_leave_one_day_out,
        [
            ("omitted_trading_day", "Omitted trading day"),
            ("remaining_trading_days", "Remaining days"),
            ("score_delta", "Candidate-control score delta"),
            ("selected_stage1_bar_seconds", "Selected stage-1 bars"),
            ("selected_windows", "Selected ATR windows"),
            ("selected_profile_key", "Selected profile"),
            ("selected_score_delta_vs_control", "Selected delta vs control"),
            ("stage3_search_scope", "Stage-3 rerank scope"),
        ],
        empty=(
            "No changed profile was selected, or fewer than three paired trading days were available; "
            "there is no recommendation-specific leave-one-day-out table."
        ),
    )
    calibration_table = _table(
        [result.execution_calibration],
        [
            ("enabled", "Enabled"),
            ("applied", "Applied"),
            ("ticker", "Ticker"),
            ("con_id", "conId"),
            ("currency", "Currency"),
            ("matched_cycles", "Matched cycles"),
            ("legacy_identity_cycles", "Legacy identity cycles"),
            ("execution_rows_considered", "Execution rows considered"),
            ("execution_rows_usable", "Usable execution rows"),
            ("duplicate_execution_rows", "Duplicate rows rejected"),
            ("buy_order_samples", "BUY order samples"),
            ("sell_order_samples", "SELL order samples"),
            ("buy_quote_matched_orders", "BUY quote-matched orders"),
            ("sell_quote_matched_orders", "SELL quote-matched orders"),
            ("buy_total_adverse_cost_bps_p75", "BUY total cost p75 bps"),
            ("sell_total_adverse_cost_bps_p75", "SELL total cost p75 bps"),
            ("median_buy_notional", "Median BUY-cycle notional"),
            (
                "configured_execution_cost_bps_per_side",
                "Configured cost / side bps",
            ),
            (
                "effective_execution_cost_bps_per_side",
                "Effective cost / side bps",
            ),
            ("configured_trade_notional", "Configured notional"),
            ("effective_trade_notional", "Effective notional"),
            ("minimum_samples", "Minimum samples"),
            ("maximum_quote_age_seconds", "Maximum quote age seconds"),
            ("warnings", "Warnings"),
        ],
        empty="Execution calibration was not requested.",
    )
    continuity_table = _table(
        result.continuity_evidence,
        [
            ("session_date", "Session"),
            ("period_id", "Period"),
            ("continuity_chain_id", "Chain"),
            ("continuity_broken_before", "Break before"),
            ("continuity_break_reason", "Break reason"),
            ("carried_position_in", "Position in"),
            ("carried_position_out", "Position out"),
            ("carried_sell_trail_in", "SELL trail in"),
            ("carried_sell_trail_out", "SELL trail out"),
            ("terminal_open_position", "Terminal open"),
            ("session_start_equity", "Start equity"),
            ("session_end_equity", "End equity"),
            ("overnight_gap_return_bps", "Overnight gap bps"),
        ],
        empty="No continuity evidence was generated.",
    )
    gate_table = _table(
        result.recommendation_gates,
        [
            ("gate_label", "Authorization gate"),
            ("required", "Required"),
            ("passed", "Passed"),
            ("detail", "Evidence / reason"),
            ("profile_key", "Profile"),
        ],
        empty="No changed stable-region center reached recommendation-gate evaluation.",
    )
    policy_table = _table(
        result.score_policy_evidence,
        [
            ("policy_label", "Score policy"),
            ("candidate_score", "Candidate score"),
            ("control_score", "Control score"),
            ("score_delta", "Delta"),
            ("passed", "Passed"),
            ("profile_key", "Profile"),
        ],
        empty="No score-policy stability evidence was generated.",
    )
    advanced_table = _table(
        result.robustness_evidence,
        [
            ("candidate_profile_key", "Candidate"),
            ("moving_block", "Moving-block evidence"),
            ("walk_forward", "Walk-forward evidence"),
            ("selection_bootstrap", "Selection-aware bootstrap"),
            ("boundary", "Boundary evidence"),
            ("pareto", "Pareto evidence"),
            ("assumption_stress", "Assumption stress"),
            ("continuity_blocks", "Economic continuity blocks"),
        ],
        empty="No advanced validation evidence was generated.",
    )
    issue_items = "".join(f"<li>{_escape(issue)}</li>" for issue in result.global_issues)
    input_recording_table = _table(
        list(result.recording.input_components),
        [
            ("recording_index", "Recording"),
            ("role", "Component"),
            ("format_version", "Format"),
            ("container_format", "Container"),
            ("size", "Bytes"),
            ("data_start_utc", "Data start"),
            ("data_end_utc", "Data end"),
            ("sha256", "SHA-256"),
        ],
        empty="No input-recording inventory was generated.",
    )
    excluded_session_table = _table(
        list(result.recording.excluded_sessions),
        [
            ("session_date", "Trading date"),
            ("period_count", "RTH periods"),
            ("recording_hashes", "Recording hashes"),
            ("reason", "Exclusion reason"),
        ],
        empty="No trading dates were excluded.",
    )
    session_quality_table = _table(
        list(result.session_quality),
        [
            ("session_date", "Trading date"),
            ("period_id", "Period"),
            ("manifest_status", "Manifest status"),
            ("source_finalized", "Completion proved"),
            ("coverage_pct", "RTH coverage %"),
            ("start_lag_seconds", "Start lag s"),
            ("end_lead_seconds", "End lead s"),
            ("selected_feed", "Selected feed"),
            ("maximum_event_gap_seconds", "Max event gap s"),
            ("connectivity_event_count", "Connectivity losses"),
            ("last_event_count", "Genuine Last events"),
            ("last_event_minute_coverage_pct", "Last minute coverage %"),
            ("last_event_gap_p95_seconds", "Last gap p95 s"),
            ("primary_eligible", "Primary eligible"),
            ("exclusion_reasons", "Exclusion reasons"),
        ],
        empty="No session-quality evidence was generated.",
    )
    instability = "".join(
        f"<li>{_escape(issue)}</li>" for issue in result.recommendation.instability_reasons
    )
    evidence_class = "good" if result.recommendation.evidence_stable else "warning"
    score_formula = _balanced_score_formula(result)
    input_links = " ".join(
        f"<a href='{_escape(name)}'>{_escape(label)}</a>"
        for name, label in [
            ("market_replay_analysis.json", "Full JSON"),
            ("input_recordings.csv", "Input recordings"),
            ("excluded_sessions.csv", "Excluded sessions"),
            ("session_quality.csv", "Session quality"),
            ("atr_window_search.csv", "ATR-window search"),
            ("candidate_results.csv", "All candidates"),
            ("robustness_evidence.csv", "Robustness evidence"),
            ("recommendation_leave_one_day_out.csv", "Leave-one-day-out"),
            ("recommended_atr_settings.csv", "Recommended settings"),
            ("recommended_session_results.csv", "Session results"),
            ("recommended_simulated_trades.csv", "Simulated trades"),
            ("execution_calibration.csv", "Execution calibration"),
            ("continuity_evidence.csv", "Overnight continuity"),
            ("continuity_block_evidence.csv", "Economic continuity blocks"),
            ("score_policy_evidence.csv", "Score-policy evidence"),
            ("moving_block_evidence.csv", "Moving-block bootstrap"),
            ("selection_bootstrap_evidence.csv", "Selection-aware bootstrap"),
            ("walk_forward_evidence.csv", "Walk-forward validation"),
            ("pareto_frontier.csv", "Pareto frontier"),
            ("search_boundary_evidence.csv", "Search-boundary evidence"),
            ("assumption_stress_evidence.csv", "Assumption stress"),
            ("recommendation_quality_gates.csv", "Recommendation gates"),
            ("SHA256SUMS.txt", "Checksums"),
        ]
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(rec.symbol)} Market Replay ATR report</title><style>{_CSS}</style></head>
<body><main>
<h1>{_escape(rec.symbol)} Market Replay ATR report</h1>
<p class="muted">Generated by {_escape(APP_NAME)} {_escape(APP_VERSION)} from {_escape(rec.input_recording_count)} immutable Market Replay Lab recording(s). Analysis ID: <code>{_escape(result.analysis_id)}</code>.</p>
<section>{_cards(result)}<p>{input_links}</p></section>
<section class="{evidence_class}"><h2>One complete ATR profile to evaluate</h2>
{profile_table}
<p><strong>Why this profile:</strong> {_escape(result.recommendation_reason)}</p>
<ul>{instability}</ul>
<p>A changed profile is shown only when every required authorization gate passes: connected stable-region support, paired candidate/control evidence, whole-day and moving-block bootstrap, exact leave-one-day-out reselection, ATR phase stress, multiple score policies, Pareto non-domination, resolved search boundaries, assumption stress, economic continuity-block evidence, selection-aware out-of-bag bootstrap, and chronological walk-forward validation. When evidence is insufficient or unstable, the unchanged control is displayed <strong>for reference</strong>. An unchanged result means that no data-supported ATR change was found; it does not prove the control optimal. This is not a mathematical optimum, a forecast, or a live-trading instruction. Evaluate it in forward paper trading.</p></section>
<section><h2>Optional calibration from actual BouncyBot executions</h2>
<p>When a stopped BouncyBot data folder is selected, the optimizer acquires the normal bot lock, creates a private read-only SQLite snapshot, and uses only executions belonging to the verified ticker/contract. Actual BUY-cycle notionals can replace the configured notional when the minimum sample threshold is met. Actual commissions and adverse slippage relative to the latest same-side quote at or before each fill are aggregated by broker order; the 75th-percentile supported side cost can raise—but never lower—the configured execution-cost reserve. The source database is never modified.</p>
<p>This calibration improves the replay's notional, liquidity, commission, and slippage assumptions. It does not import historical BouncyBot settings into the Market Replay search, does not use future quotes, and does not imply that a hypothetical order would receive an actual historical fill.</p>
{calibration_table}</section>
<section><h2>Recording coverage and integrity</h2>
<p>The importer accepted {_escape(rec.input_recording_count)} Market Replay recording(s) with format version(s) {_escape(rec.format_label)}. Version 2 is the legacy ZIP format; version 3 is the SQLite format with per-record integrity hashes, chain hashes, committed checkpoints, and explicit RTH-period metadata. Every input was copied to a private temporary location, verified independently, analyzed from the copy, and rechecked for source mutation.</p>
<p>Recordings are combined only when symbol, positive conId, currency, security type, exchange time zone, and minimum tick agree. Exchange-routing metadata may differ and is reported. A trading date represented by overlapping files or more than one RTH fragment is excluded in full; fragments are never spliced because continuous ATR, anchor, order, and position state cannot be proved.</p>
<p><strong>Combined input identity:</strong> <code>{_escape(rec.sha256)}</code> · <strong>conId:</strong> {_escape(rec.con_id)} · <strong>data:</strong> {_escape(rec.data_start_utc)} through {_escape(rec.data_end_utc)} · <strong>raw rows:</strong> {_escape(rec.raw_row_count)} · <strong>retained strategy rows:</strong> {_escape(rec.retained_row_count)}</p>
<h3>Input recordings</h3>{input_recording_table}<h3>Session-quality evidence</h3>{session_quality_table}<h3>Excluded trading dates</h3>{excluded_session_table}
<p>Only primary-eligible sessions can authorize a changed recommendation. A session must cover the RTH boundaries within the configured tolerance, have explicit normal-close evidence, use live data, contain no recorded connectivity-loss event, avoid excessive retained-event gaps, and provide sufficient genuine Last-event density. A container-level <code>complete</code> flag does not convert a manually stopped, sample, or interrupted RTH period into a complete market path. When no session passes these gates, the candidate search is exploratory and the unchanged control is displayed for reference.</p>
<p>All raw rows are validated before optimization. The in-memory strategy stream may then discard only redundant size-, volume-, or high/low-only updates that cannot change the selected strategy price, a Last-trigger event, stop normalization, modeled fill, feed selection, or UTC-second state. Every Last event, full snapshot, price/feed change, first/final row, and at least one usable state per UTC second is retained. The raw and retained counts are reported separately so this reduction is auditable.</p>
<p>Format-3 periods marked <code>active</code> after an interrupted recorder are valid recovery evidence. When such a period has no committed observed end, the optimizer uses the last committed tick receipt time as a conservative observed end and keeps that session right-censored. Recorder wall-clock reversals are reported; monotonic <code>elapsed_ns</code> and sequence remain authoritative for event order, while affected evidence is marked unstable.</p>
<ul>{issue_items}</ul></section>
<section><h2>How market prices are reconstructed</h2>
<p>Each event stores the latest Level 1 state plus <code>changed_fields</code>. The selected application-price approximation follows <code>ib_async.Ticker.marketPrice</code> semantics as closely as the stored fields allow: Last when it lies inside a valid spread, otherwise bid/ask midpoint, then mark, Last, and close. A lone bid or ask is not used as a strategy price. The real bot receives the derived <code>marketPrice</code> directly, so this remains a documented approximation.</p>
<p>Positive native trailing orders are evaluated only on rows that explicitly report a new Last update. A quote-only row carrying a cached Last cannot trigger the simulated trail. A row with blank <code>changed_fields</code> is treated as a complete snapshot, matching the Market Replay format contract.</p>
<p>Live rows are preferred within a session. Delayed rows are used only when the session contains no live rows. Frozen and delayed-frozen rows are retained for audit but excluded from the strategy simulation. Synthetic-source, delayed-only, mixed live/delayed, frozen-interruption, or receipt-clock-reversal evidence forces <code>evidence_stable=false</code>, even when the numerical candidate search otherwise finds a stable parameter region.</p>
<p>Crossed two-sided quotes are preserved as data-quality evidence but neither side is used as an executable touch or initial-stop reference. A market BUY requires a valid recorded ask and a market SELL requires a valid recorded bid. Last, mark, close, the opposite quote, or a caller fallback cannot prove execution.</p></section>
<section><h2>ATR reconstruction</h2>
<p>Prices are grouped into OHLC bars on recorder <code>elapsed_ns</code>, the monotonic time source corresponding to BouncyBot's callback clock. The primary search uses the stored canonical phase. Because the absolute phase of BouncyBot's process clock is not recorded, every changed recommendation is also stress-tested over deterministic five-second candidate/control phase offsets and an independently adverse per-session phase aggregation. For each bar after the first:</p>
<div class="formula">TR = max(high − low, |high − previous close|, |low − previous close|)\nATR = simple mean of the latest N true ranges\nATR% = ATR / latest close × 100\nEffective strategy % = round(clamp(ATR% × multiplier, minimum clamp, maximum clamp), 2)</div>
<p>The calculation requires N+1 bars and includes the current partial bar after warm-up, matching the current BouncyBot simple-ATR calculation. At each event the bar history is limited to the same recent horizon used by BouncyBot: max((period + 4) × bar duration, 300 seconds). A long data gap therefore requires fresh warm-up. A zero BUY or SELL trail multiplier remains immediate market mode and is not raised to the positive minimum clamp.</p></section>
<section><h2>Full-cycle counterfactual replay</h2>
<p>Every fixed profile is replayed through the selected RTH periods in chronological order: ATR warm-up, moving anchor, initial drop, BUY rebound trail, modeled BUY, minimum-profit activation, SELL trail, modeled SELL, and possible subsequent cycles. New entries start five minutes after the scheduled open and remain allowed through exactly fifteen minutes before the scheduled close. An unfilled BUY trail is cancelled five minutes before close.</p>
<p>When overnight replay is enabled, an open long position and any already-submitted native SELL trail or triggered pending SELL are carried into the next provably consecutive primary-eligible weekday recording. The BUY basis, locked SELL-trail percentage, running SELL high, cumulative capital, and active trade identity are preserved. A held position without an active SELL order re-warms the new session's ATR before deriving a new minimum-profit/SELL-trail submission. Continuity fails closed across a missing weekday or holiday ambiguity, a quality-gate failure, overlap, or a disabled setting. BUY entry setups are not carried because the standardized strategy cancels an unfilled BUY trail before close.</p>
{continuity_table}
<p>At native order submission, the initial BUY stop is normalized from the highest available selected/ask/Last/mark reference and rounded upward to the contract minimum tick. The SELL stop uses the lowest selected/bid/Last/mark reference, rounds downward, and must still protect the minimum-profit floor.</p>
<p>Modeled BUY fills require a valid ask and use the worse of the trigger/reference and ask; modeled SELL fills require a valid bid and use the worse of the trigger/reference and bid. When a Last event triggers a native trail but the same-side quote is absent, the resulting market order remains pending until a later event supplies that touch. A configurable fixed execution-cost reserve is charged on each BUY and SELL side. The report also compares the assumed whole-share quantity, calculated from the configured notional in the instrument currency, with the recorded top-of-book size. Missing or invalid size is counted as insufficient evidence rather than omitted from the denominator. This is a recommendation-quality gate only: depth beyond the touch, queue position, partial fills, market impact, hidden liquidity, routing, and actual broker commissions are not reconstructed. The one manifest minimum-tick value is used for rounding; a historical tiered market-rule ladder is not available in the recording. A position or still-open entry opportunity at the observed recording end is right-censored. An unfilled BUY trail is instead treated as cancelled when the recording proves that the configured five-minute-before-close cancellation boundary was reached, even when no event occurred at that exact instant. For an open position, a profitable unrealized mark cannot improve the score, while an unrealized loss remains a penalty. A changed recommendation is rejected when any paired candidate/control session is right-censored, including an open long position without a valid bid-side end mark.</p></section>
<section><h2>Three-stage two-dimensional ATR-window search</h2>
<p>Stage 1 fixes ATR period at 14 and compares 15-, 30-, 60-, and 120-second bars using the same small representative multiplier mini-grid for every duration. The strongest representative result for each duration determines advancement, and the two strongest durations advance. Stage 2 uses the same mini-grid while comparing ATR periods 5, 7, 10, 14, 21, and 28 inside the advancing bar durations. The unchanged 14×60 control is always retained, and at most three period/bar windows advance. Stage 3 runs the full entry/exit multiplier grid and a bounded set of minimum ATR clamps inside those narrowed windows, then refines the twelve strongest multiplier profiles in deterministic 0.25 steps.</p>
<p>This separation makes bar duration and period individually observable during the first two stages instead of changing both dimensions together at four arbitrary points.</p>
<p>The hierarchy is intentionally bounded. The representative mini-grid reduces—but does not eliminate—interaction bias between ATR window and strategy multipliers. The recommendation is therefore the strongest supported profile inside this staged contract, not the optimum of every possible period, bar, clamp, and multiplier cross-product.</p>
{window_table}</section>
<section><h2>Candidate search and ranking</h2>
<p>Every stage-3 candidate is evaluated against every analyzable session; no-trade and open-position sessions stay in the denominator. A no-trade day is not directly penalized because that can reward unnecessary turnover. Instead, a changed profile must retain at least 80% of the control's trading-day participation, while modeled execution costs and an explicit turnover penalty apply to completed trades.</p>
<div class="formula">{_escape(score_formula)}</div>
<p>Returns include the effective per-side execution-cost reserve. Without calibration this is the configured conservative reserve. With supported SQLite calibration it is the higher of the configured reserve and the supported ticker-specific execution evidence. The explicit turnover term reduces the incentive to select rapid zero-trail profiles merely because fixed costs were omitted. The optimizer does not automatically choose an isolated numerical maximum. Near-best adjacent multiplier profiles are grouped only within the same ATR window and clamp values, and the deterministic center of a supported region is preferred. Evidence is marked unstable when the recording set has fewer than five primary-eligible RTH trading days, fewer than three completed simulated trades, trades in fewer than three sessions, more than 20% terminal right-censored session outcomes, inadequate touch-size evidence, excessive minimum- or maximum-clamp saturation in a changed component, or no adjacent near-best region.</p>
{candidate_table}<p class="muted">The HTML shows the first 250 ranked candidates; the CSV contains all candidates.</p></section>
<section><h2>Robust selection and validation</h2>
<p>Candidate and unchanged control are paired on identical RTH session identities. The ordinary trading-day bootstrap resamples whole trading days, or complete overnight-continuity blocks when a position or active SELL order links dates. The moving-block bootstrap additionally resamples adjacent economic units to test sensitivity to short-lived market regimes. Exact leave-one-day-out analysis removes the raw date first, rebuilds continuity and ATR, and reruns all three search stages plus local refinement.</p>
<p>Selection-aware bootstrap reruns the complete selector inside deterministic bootstrap training bags and evaluates the selected profile on out-of-bag units. Chronological walk-forward uses expanding training windows and evaluates frozen profiles on later unseen blocks; validation drawdown is rebased at the fold boundary so training-period peaks and losses cannot contaminate unseen scoring. These advanced gates require at least twenty independent primary-quality sessions. Ticks are never resampled independently.</p>
{robustness_table}
<h3>Selected recommendation: leave-one-day-out details</h3>{leave_one_out_table}
<h3>Recommendation authorization gates</h3>{gate_table}
<h3>Score-policy stability</h3>{policy_table}
<h3>Advanced evidence summary</h3>{advanced_table}</section>
<section><h2>Recommended-profile session results</h2>{session_table}</section>
<section class="warning"><h2>Interpretation limits</h2>
<p>This workflow analyzes the selected recording set under standardized entry rules. Optional SQLite calibration uses actual fills only to derive conservative notional and execution-cost assumptions; it does not import BouncyBot's historical strategy state, protective exits, account constraints, or user start/stop times. Overnight state is carried only through provably continuous primary-eligible recordings and deliberately breaks at ambiguous gaps. Top-of-book size remains a fail-closed evidence gate rather than a full depth or partial-fill model. The standardized replay therefore answers: “Which bounded-grid profile behaved best under this documented simulator and evidence set?” It does not establish the best future settings for the ticker.</p>
<p>A single week remains a small in-sample research set even when it contains millions of events. Bootstrap and leave-one-day-out tests are rejection checks, not independent out-of-sample proof. Consecutive sessions linked by an overnight position count as fewer independent bootstrap units than their raw day count. Prefer many unbiased full-session recordings from different volatility regimes and several independent position-continuity chains, then freeze and validate the one suggested profile on later unseen recordings and forward paper trading before considering any production change.</p></section>
</main></body></html>"""


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_hashes(root: Path, *, exclude: set[str] | None = None) -> dict[str, str]:
    ignored = exclude or set()
    return {
        path.relative_to(root).as_posix(): _hash(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).as_posix() not in ignored
    }


def _same_tree(left: Path, right: Path) -> bool:
    return _tree_hashes(left) == _tree_hashes(right)


def write_market_replay_report(result: MarketReplayAnalysisResult) -> MarketReplayAnalysisResult:
    """Atomically publish a deterministic, content-addressed Market Replay report."""

    destination = result.output_dir
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{destination.name}-", dir=destination.parent) as temporary:
        stage = Path(temporary) / destination.name
        stage.mkdir()
        # Keep report JSON independent of the private staging-directory name.
        result.files_written = [Path(name) for name in _FILE_NAMES]

        candidate_rows = _candidate_rows(result)
        recommended_sessions = _session_rows(result.recommended_sessions)
        control_sessions = _session_rows(result.control_sessions)
        trade_rows = _trade_rows(result)
        profile_rows = [_profile_row(result)]
        issue_rows = [{"issue": issue} for issue in result.global_issues]
        input_rows = list(result.recording.input_components)
        excluded_rows = list(result.recording.excluded_sessions)
        quality_rows = list(result.session_quality)
        window_rows = list(result.window_search)
        robustness_rows = list(result.robustness_evidence)
        leave_one_out_rows = list(result.recommendation_leave_one_day_out)
        calibration_rows = [dict(result.execution_calibration)]
        continuity_rows = list(result.continuity_evidence)
        continuity_block_rows = list(result.continuity_block_evidence)
        score_policy_rows = list(result.score_policy_evidence)
        moving_block_rows = list(result.moving_block_evidence)
        selection_bootstrap_rows = list(result.selection_bootstrap_evidence)
        walk_forward_rows = list(result.walk_forward_evidence)
        pareto_rows = list(result.pareto_evidence)
        boundary_rows = list(result.boundary_evidence)
        stress_rows = list(result.assumption_stress_evidence)
        gate_rows = list(result.recommendation_gates)

        (stage / "index.html").write_text(_html(result), encoding="utf-8")
        _write_json(stage / "market_replay_analysis.json", result.to_jsonable())
        _write_csv(
            stage / "input_recordings.csv",
            input_rows,
            list(input_rows[0])
            if input_rows
            else [
                "recording_index",
                "role",
                "name",
                "format_version",
                "container_format",
                "size",
                "sha256",
            ],
        )
        _write_csv(
            stage / "excluded_sessions.csv",
            excluded_rows,
            list(excluded_rows[0])
            if excluded_rows
            else [
                "session_date",
                "period_count",
                "recording_hashes",
                "reason",
            ],
        )
        _write_csv(
            stage / "session_quality.csv",
            quality_rows,
            list(quality_rows[0])
            if quality_rows
            else [
                "session_date",
                "period_id",
                "manifest_status",
                "source_finalized",
                "coverage_pct",
                "start_lag_seconds",
                "end_lead_seconds",
                "maximum_event_gap_seconds",
                "connectivity_event_count",
                "last_event_count",
                "last_event_minute_coverage_pct",
                "last_event_gap_p95_seconds",
                "primary_eligible",
                "exclusion_reasons",
            ],
        )
        _write_csv(
            stage / "atr_window_search.csv",
            window_rows,
            list(window_rows[0])
            if window_rows
            else ["stage", "rank", "period", "bar_seconds", "selected_for_next_stage"],
        )
        _write_csv(
            stage / "candidate_results.csv",
            candidate_rows,
            list(candidate_rows[0]) if candidate_rows else ["rank", "profile_key"],
        )
        _write_csv(
            stage / "robustness_evidence.csv",
            robustness_rows,
            list(robustness_rows[0])
            if robustness_rows
            else [
                "candidate_profile_key",
                "control_profile_key",
                "paired_trading_days",
                "observed_score_delta",
                "passed",
                "failure_reasons",
            ],
        )
        _write_csv(
            stage / "recommendation_leave_one_day_out.csv",
            leave_one_out_rows,
            list(leave_one_out_rows[0])
            if leave_one_out_rows
            else [
                "candidate_profile_key",
                "control_profile_key",
                "omitted_trading_day",
                "remaining_trading_days",
                "score_delta",
            ],
        )
        _write_csv(stage / "recommended_atr_settings.csv", profile_rows, list(profile_rows[0]))
        session_fields = list(recommended_sessions[0]) if recommended_sessions else [
            "session_date",
            "period_id",
            "ticks",
            "trades",
            "completed_trades",
            "open_position",
            "open_entry_setup",
            "right_censored",
            "unmarked_open_position",
            "conservative_return_bps",
            "issues",
        ]
        _write_csv(stage / "recommended_session_results.csv", recommended_sessions, session_fields)
        trade_fields = list(trade_rows[0]) if trade_rows else [
            "session_date",
            "cycle_number",
            "buy_time_utc",
            "buy_price",
            "sell_time_utc",
            "sell_price",
            "return_bps",
            "open_at_end",
        ]
        _write_csv(stage / "recommended_simulated_trades.csv", trade_rows, trade_fields)
        _write_csv(stage / "control_session_results.csv", control_sessions, session_fields)
        _write_csv(
            stage / "execution_calibration.csv",
            calibration_rows,
            list(calibration_rows[0])
            if calibration_rows
            else [
                "enabled",
                "applied",
                "ticker",
                "con_id",
                "currency",
                "matched_cycles",
                "execution_rows_usable",
                "effective_execution_cost_bps_per_side",
                "effective_trade_notional",
                "warnings",
            ],
        )
        _write_csv(
            stage / "continuity_evidence.csv",
            continuity_rows,
            list(continuity_rows[0])
            if continuity_rows
            else [
                "session_date",
                "period_id",
                "continuity_chain_id",
                "continuity_broken_before",
                "continuity_break_reason",
                "carried_position_in",
                "carried_position_out",
                "carried_sell_trail_in",
                "carried_sell_trail_out",
                "terminal_open_position",
                "session_start_equity",
                "session_end_equity",
                "overnight_gap_return_bps",
            ],
        )
        for file_name, rows, fallback_fields in (
            (
                "continuity_block_evidence.csv",
                continuity_block_rows,
                ["profile_key", "evaluated", "passed", "paired_blocks", "failure_reasons"],
            ),
            (
                "score_policy_evidence.csv",
                score_policy_rows,
                ["profile_key", "policy_key", "candidate_score", "control_score", "score_delta", "passed"],
            ),
            (
                "moving_block_evidence.csv",
                moving_block_rows,
                ["profile_key", "evaluated", "passed", "independent_units", "block_length", "ci80_low", "ci80_high"],
            ),
            (
                "selection_bootstrap_evidence.csv",
                selection_bootstrap_rows,
                ["profile_key", "evaluated", "passed", "oob_evaluations", "probability_positive_pct", "median_oob_delta"],
            ),
            (
                "walk_forward_evidence.csv",
                walk_forward_rows,
                ["profile_key", "evaluated", "passed", "folds", "fixed_median_delta", "fixed_worst_delta", "same_atr_window_pct"],
            ),
            (
                "pareto_frontier.csv",
                pareto_rows,
                ["profile_key", "on_frontier", "dominated_by", "passed"],
            ),
            (
                "search_boundary_evidence.csv",
                boundary_rows,
                ["profile_key", "attempted", "resolved", "unresolved_dimensions", "near_best_threshold", "probes"],
            ),
            (
                "assumption_stress_evidence.csv",
                stress_rows,
                ["profile_key", "evaluated", "passed", "rows", "failure_reasons"],
            ),
            (
                "recommendation_quality_gates.csv",
                gate_rows,
                ["profile_key", "gate_key", "gate_label", "required", "passed", "detail"],
            ),
        ):
            _write_csv(
                stage / file_name,
                rows,
                list(rows[0]) if rows else fallback_fields,
            )
        _write_csv(stage / "data_quality_issues.csv", issue_rows, ["issue"])

        readme = f"""{APP_NAME} {APP_VERSION} — Market Replay report

Analysis ID: {result.analysis_id}
Input recordings: {result.recording.input_recording_count}
Recording formats: {result.recording.format_label} ({result.recording.container_format})
Ticker: {result.recording.symbol}

Open index.html in a browser. The report contains exactly one complete ATR profile to evaluate in forward paper trading. ATR period and bar duration are narrowed with a representative multiplier mini-grid before the full multiplier and minimum-clamp search. A changed profile is published only when all recommendation authorization gates pass, including primary source quality, paired candidate/control evidence, whole-day and moving-block bootstrap, exact leave-one-day-out reselection, ATR phase, score-policy stability, Pareto non-domination, resolved search boundaries, assumption stress, economic continuity blocks, selection-aware out-of-bag bootstrap, and chronological walk-forward validation. Otherwise the unchanged control is shown for reference because no data-supported change was found. The output is not a mathematical or future-market optimum.

By default, continuous overnight replay carries open long positions and already-active native SELL trails across provably consecutive complete primary-eligible RTH recordings. Continuity fails closed at missing weekdays/holiday ambiguity, overlap, or quality failures. Optional BouncyBot SQLite calibration is read-only and can increase the conservative execution-cost reserve and replace the assumed notional when sufficient matching execution evidence exists. It does not import historical strategy state or guarantee hypothetical fills. Top-of-book size remains a fail-closed evidence gate rather than a depth or partial-fill model, and the manifest minimum tick is used because a historical market-rule ladder is not stored.
"""
        (stage / "README_REPORT.txt").write_text(readme, encoding="utf-8")
        manifest = {
            "application": APP_NAME,
            "version": APP_VERSION,
            "analysis_id": result.analysis_id,
            "run_id": result.run_id,
            "generated_at_utc": result.generated_at_utc,
            "recording_file_name": result.recording.logical_file_name,
            "recording_sha256": result.recording.sha256,
            "recording_format_version": result.recording.format_version,
            "recording_format_versions": list(result.recording.format_versions),
            "recording_format_label": result.recording.format_label,
            "recording_container_format": result.recording.container_format,
            "input_recording_count": result.recording.input_recording_count,
            "input_components": result.recording.input_components,
            "excluded_sessions": result.recording.excluded_sessions,
            "session_quality": result.session_quality,
            "execution_calibration": result.execution_calibration,
            "continuity_evidence": result.continuity_evidence,
            "continuity_block_evidence": result.continuity_block_evidence,
            "score_policy_evidence": result.score_policy_evidence,
            "moving_block_evidence": result.moving_block_evidence,
            "selection_bootstrap_evidence": result.selection_bootstrap_evidence,
            "walk_forward_evidence": result.walk_forward_evidence,
            "pareto_evidence": result.pareto_evidence,
            "boundary_evidence": result.boundary_evidence,
            "assumption_stress_evidence": result.assumption_stress_evidence,
            "recommendation_gates": result.recommendation_gates,
            "exploratory_only": result.exploratory_only,
            "ticker": result.recording.symbol,
            "search_contract": result.search_contract,
            "report_files": list(_FILE_NAMES),
        }
        _write_json(stage / "analysis_manifest.json", manifest)
        hashes = _tree_hashes(stage, exclude={"SHA256SUMS.txt"})
        (stage / "SHA256SUMS.txt").write_text(
            "".join(f"{digest}  {name}\n" for name, digest in sorted(hashes.items())),
            encoding="ascii",
        )

        if destination.exists():
            if not destination.is_dir() or not _same_tree(stage, destination):
                raise FileExistsError(
                    f"A different report already exists at the content-addressed destination: {destination}"
                )
        else:
            atomic_publish_directory(stage, destination)

    result.files_written = [destination / name for name in _FILE_NAMES]
    return result
