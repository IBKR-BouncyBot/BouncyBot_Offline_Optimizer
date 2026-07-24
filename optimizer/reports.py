"""Deterministic HTML, JSON, and CSV report writers."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .atomic_publish import atomic_publish_directory
from .models import AnalysisResult, TickerAnalysis
from .utils import ticker_folder_name
from .version import APP_NAME, APP_VERSION

_CSS = """
:root { color-scheme: light; font-family: Segoe UI, Arial, sans-serif; }
body { margin: 0; background: #f4f6f8; color: #17202a; line-height: 1.45; }
header { padding: 24px 30px; background: #172a3a; color: white; }
main { max-width: 1500px; margin: 0 auto; padding: 24px; }
section { background: white; border: 1px solid #d9e0e6; border-radius: 8px; padding: 18px; margin-bottom: 18px; }
h1, h2, h3 { margin-top: 0; }
h3 { margin-top: 18px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { border: 1px solid #d9e0e6; padding: 7px 8px; text-align: left; vertical-align: top; }
th { background: #edf2f6; position: sticky; top: 0; }
.good { color: #176b36; font-weight: 600; }
.warn { color: #8a5600; font-weight: 600; }
.bad { color: #9f2020; font-weight: 600; }
.muted { color: #5f6b76; }
.code { font-family: Consolas, monospace; overflow-wrap: anywhere; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
.card { border: 1px solid #d9e0e6; border-radius: 6px; padding: 12px; background: #fbfcfd; }
.metric { font-size: 24px; font-weight: 700; }
.notice { border-left: 5px solid #d58a00; padding: 10px 12px; background: #fff7e6; }
.info { border-left: 5px solid #2c6fb7; padding: 10px 12px; background: #eef6ff; }
a { color: #005ea8; }
.bar { height: 12px; background: #e6ebef; border-radius: 6px; overflow: hidden; }
.bar > span { display: block; height: 100%; background: #2c6fb7; }
.scroll { overflow: auto; max-height: 620px; }
.formula { font-family: Consolas, monospace; background: #f5f7f9; border: 1px solid #d9e0e6; padding: 8px; }
.small { font-size: 12px; }
"""


def _escape(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


def _json_safe(value: Any) -> Any:
    """Replace non-JSON numeric values recursively and preserve stable order."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _format(value: Any, decimals: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        if not math.isfinite(value):
            return "—"
        return f"{value:.{decimals}f}"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(
            _json_safe(value),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
    return str(value)


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


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    if fieldnames is None:
        fieldnames = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames or ["message"],
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in writer.fieldnames})


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
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith(("=", "+", "-", "@")):
            return "'" + value
    return value


def _table(
    rows: list[dict[str, Any]],
    columns: list[tuple[str, str]],
    *,
    empty: str = "No rows",
) -> str:
    if not rows:
        return f"<p class='muted'>{_escape(empty)}</p>"
    header = "".join(f"<th>{_escape(label)}</th>" for _, label in columns)
    body = []
    for row in rows:
        cells = "".join(
            f"<td>{_escape(_format(row.get(key)))}</td>" for key, _ in columns
        )
        body.append(f"<tr>{cells}</tr>")
    return (
        "<div class='scroll'><table><thead><tr>"
        + header
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )


def _pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:.1f}%" if math.isfinite(number) else "—"


def _coverage_cards(ticker: TickerAnalysis) -> str:
    coverage = ticker.coverage
    items = [
        ("Coverage", f"{coverage.get('coverage_grade', '—')} / {coverage.get('coverage_score', 0)}"),
        ("Completed cycles", coverage.get("completed_cycles")),
        ("BUY capture match", _pct(coverage.get("buy_capture_match_pct"))),
        ("Normal SELL capture match", _pct(coverage.get("sell_capture_match_pct"))),
        (
            "Protective SELL fills / matched",
            f"{coverage.get('protective_sell_fills', 0)} / {coverage.get('matched_protective_sell_captures', 0)}",
        ),
        ("Replayable BUY windows", coverage.get("replayable_buy_windows")),
        ("Replayable SELL windows", coverage.get("replayable_sell_windows")),
        (
            "Usable archives / no usable prices",
            f"{coverage.get('usable_capture_archives', 0)} / "
            f"{coverage.get('archives_without_usable_prices', 0)}",
        ),
        (
            "ATR-adaptive / manual cycles",
            f"{coverage.get('atr_adaptive_cycles', 0)} / {coverage.get('manual_percentage_cycles', 0)}",
        ),
        ("Usable price points", coverage.get("usable_price_points")),
        ("Date span", f"{_format(coverage.get('date_span_days'))} days"),
    ]
    cards = "".join(
        f"<div class='card'><div class='muted'>{_escape(label)}</div><div class='metric'>{_escape(value)}</div></div>"
        for label, value in items
    )
    try:
        score = float(coverage.get("coverage_score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if not math.isfinite(score):
        score = 0.0
    return (
        f"<div class='grid'>{cards}</div>"
        f"<div class='bar'><span style='width:{min(100.0, max(0.0, score)):.1f}%'></span></div>"
    )


def _settings_explanation(ticker: TickerAnalysis) -> str:
    summary = ticker.atr_settings_summary
    try:
        configured_cycles = int(summary.get("cycles_with_any_stored_atr_settings") or 0)
    except (TypeError, ValueError, OverflowError):
        configured_cycles = 0
    varied = bool(summary.get("settings_varied_between_cycles"))
    median_matches = bool(summary.get("historical_median_matches_an_observed_complete_profile"))
    if configured_cycles == 0:
        variation = (
            "No cycle row contained a usable ATR settings snapshot. The control row therefore contains documented "
            "fallback defaults and must not be interpreted as settings that actually ran for this ticker."
        )
    elif varied:
        variation = (
            f"The cycle rows contain {summary.get('distinct_atr_profiles', 0)} distinct exact ATR profile(s), "
            f"{summary.get('contiguous_setting_regimes', 0)} chronological regime(s), and "
            f"{summary.get('configuration_change_count', 0)} setting transition(s). "
            "For a fair counterfactual comparison, each replay candidate is held constant across all usable cycles: the "
            "candidate period, bar duration, multipliers, and evaluation-control minimum/maximum clamps are applied to "
            "every window. The original settings are not silently substituted back into that candidate. Every replay "
            "observation is instead tagged with the exact historical profile stored on its cycle. The pooled score uses "
            "all tagged windows, while the candidate-by-profile breakdown shows the same candidate separately inside "
            "each settings era. This prevents the report from pretending that one configuration was active throughout "
            "history, but neither the pooled result nor a small subgroup proves that the settings caused the outcome; "
            "settings eras may coincide with different market regimes."
        )
    else:
        variation = (
            "All cycle rows with ATR data resolve to one exact historical profile, so no between-cycle ATR setting change "
            "was detected."
        )
    if configured_cycles == 0:
        median_note = "There is no observed complete historical profile against which the fallback control can be matched."
    elif median_matches:
        median_note = "The historical median baseline exactly matches one observed complete cycle profile."
    else:
        median_note = (
            "The historical median baseline is a field-wise statistical summary and does not match any one complete "
            "observed profile."
        )
    return f"<p>{_escape(variation)}</p><p>{_escape(median_note)}</p>"


def _candidate_evidence_rows(ticker: TickerAnalysis) -> list[dict[str, Any]]:
    """Flatten nested v1.3 evidence for readable HTML and CSV output."""

    rows: list[dict[str, Any]] = []
    for summary in ticker.candidate_summaries:
        row = asdict(summary)
        paired = dict(summary.paired_evidence or {})
        bootstrap = dict(paired.get("bootstrap") or {})
        influence = dict(paired.get("leave_one_day_out") or {})
        region = dict(summary.stable_region or {})
        candidate_km = dict(paired.get("candidate_km_trigger_probability") or {})
        control_km = dict(paired.get("control_km_trigger_probability") or {})
        row.update(
            {
                "paired_windows": paired.get("paired_windows"),
                "paired_both_triggered": paired.get("paired_both_triggered"),
                "paired_execution_adjusted_cycles": paired.get(
                    "paired_execution_adjusted_cycles"
                ),
                "minimum_execution_model_samples": paired.get(
                    "minimum_execution_model_samples"
                ),
                "independent_trading_days": paired.get("independent_trading_days"),
                "paired_execution_adjusted_median_bps": paired.get(
                    "paired_execution_adjusted_median_bps"
                ),
                "paired_median_mae_delta_bps": paired.get("median_mae_delta_bps"),
                "paired_median_absolute_delay_delta_seconds": paired.get(
                    "median_absolute_delay_delta_seconds"
                ),
                "candidate_km_1m_pct": candidate_km.get("1m_pct"),
                "candidate_km_5m_pct": candidate_km.get("5m_pct"),
                "candidate_km_15m_pct": candidate_km.get("15m_pct"),
                "control_km_1m_pct": control_km.get("1m_pct"),
                "control_km_5m_pct": control_km.get("5m_pct"),
                "control_km_15m_pct": control_km.get("15m_pct"),
                "selected_km_horizon": paired.get("selected_km_horizon"),
                "selected_km_horizon_seconds": paired.get(
                    "selected_km_horizon_seconds"
                ),
                "selected_candidate_km_trigger_probability_pct": paired.get(
                    "selected_candidate_km_trigger_probability_pct"
                ),
                "selected_control_km_trigger_probability_pct": paired.get(
                    "selected_control_km_trigger_probability_pct"
                ),
                "selected_km_trigger_probability_delta_pct": paired.get(
                    "selected_km_trigger_probability_delta_pct"
                ),
                "bootstrap_probability_positive_pct": bootstrap.get(
                    "probability_positive_pct"
                ),
                "bootstrap_ci80_lower_bps": bootstrap.get("ci80_lower_bps"),
                "bootstrap_ci80_upper_bps": bootstrap.get("ci80_upper_bps"),
                "bootstrap_ci95_lower_bps": bootstrap.get("ci95_lower_bps"),
                "bootstrap_ci95_upper_bps": bootstrap.get("ci95_upper_bps"),
                "lodo_minimum_bps": influence.get("minimum_bps"),
                "lodo_positive_pct": influence.get("positive_pct"),
                "lodo_sign_reversals": influence.get("sign_reversals"),
                "lodo_most_influential_day": influence.get(
                    "most_influential_day"
                ),
                "candidate_duplicate_cycles_excluded": "; ".join(
                    str(item)
                    for item in paired.get(
                        "candidate_duplicate_cycle_keys_excluded", []
                    )
                ),
                "control_duplicate_cycles_excluded": "; ".join(
                    str(item)
                    for item in paired.get(
                        "control_duplicate_cycle_keys_excluded", []
                    )
                ),
                "context_mismatch_cycles_excluded": "; ".join(
                    f"{key}: {value}"
                    for key, value in sorted(
                        dict(
                            paired.get(
                                "context_mismatch_cycle_keys_excluded", {}
                            )
                        ).items()
                    )
                ),
                "stable_region_id": region.get("region_id"),
                "stable_region_size": region.get("size"),
                "stable_region_supported": region.get("supported"),
                "stable_region_center": region.get("is_center"),
                "instability_reasons_text": "; ".join(
                    str(item) for item in summary.instability_reasons
                ),
            }
        )
        rows.append(row)
    return rows


def _execution_model_rows(ticker: TickerAnalysis) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for leg in ("buy", "sell"):
        model = dict(ticker.execution_model.get(leg) or {})
        rows.append({"leg": leg.upper(), **model})
    return rows


def _primary_evaluation_section(ticker: TickerAnalysis) -> str:
    """Render the single conservative ATR profile proposed for paper evaluation."""

    selected = ticker.primary_evaluation_setting
    if not selected:
        return (
            "<section><h2>One settings set to evaluate next</h2>"
            "<div class='notice'><strong>No settings set could be selected.</strong> "
            "The analysis did not produce a valid evaluation control.</div></section>"
        )

    setting_rows = [
        {"setting": label, "value": _format(selected.get(key), decimals=2)}
        for key, label in (
            ("atr_adaptive_enabled", "ATR adaptation enabled"),
            ("atr_adapt_minimum_profit_enabled", "ATR adapts minimum profit"),
            ("atr_block_new_buy_until_ready", "Block new BUY until ATR is ready"),
            ("atr_period", "ATR period"),
            ("atr_bar_seconds", "ATR bar duration (seconds)"),
            ("atr_initial_drop_multiplier", "Initial drop × ATR"),
            ("atr_buy_rebound_multiplier", "BUY rebound × ATR"),
            ("atr_minimum_profit_multiplier", "Minimum profit × ATR"),
            ("atr_sell_trail_multiplier", "SELL trail × ATR"),
            ("atr_adapt_protective_sell_enabled", "ATR adapts protective SELL"),
            ("atr_protective_sell_multiplier", "Protective SELL × ATR"),
            ("atr_min_pct", "Minimum ATR clamp (%)"),
            ("atr_max_pct", "Maximum ATR clamp (%)"),
        )
    ]
    settings_table = _table(setting_rows, [("setting", "Setting"), ("value", "Value")])
    is_change = bool(selected.get("selection_is_change"))
    selected_leg = str(selected.get("selection_scored_legs") or "none")
    prefix = "entry" if selected_leg == "BUY" else "exit" if selected_leg == "normal SELL" else ""
    paired = dict(selected.get(f"{prefix}_paired_evidence") or {}) if prefix else {}
    bootstrap = dict(paired.get("bootstrap") or {})
    influence = dict(paired.get("leave_one_day_out") or {})
    region = dict(selected.get("selection_stable_region") or {})
    model = dict(selected.get("selection_execution_model") or {})
    candidate_km = dict(paired.get("candidate_km_trigger_probability") or {})
    control_km = dict(paired.get("control_km_trigger_probability") or {})

    metric_items = [
        ("Selected profile", selected.get("profile")),
        ("Changed from control", is_change),
        ("Evidence leg", selected_leg),
        ("Same-cycle pairs", selected.get("selection_paired_windows")),
        ("Both settings triggered", selected.get("selection_paired_both_triggered")),
        (
            "Execution-adjusted price pairs",
            selected.get("selection_paired_execution_adjusted_cycles"),
        ),
        (
            "Minimum execution-model samples",
            selected.get("selection_minimum_execution_model_samples"),
        ),
        ("Independent UTC trading days", selected.get("selection_independent_trading_days")),
        ("Execution-adjusted paired median (bps)", selected.get("selection_paired_execution_adjusted_median_bps")),
        ("Bootstrap probability positive", _pct(selected.get("selection_bootstrap_probability_positive_pct"))),
    ]
    metric_cards = "".join(
        f"<div class='card'><div class='muted'>{_escape(label)}</div><div class='metric'>{_escape(_format(value))}</div></div>"
        for label, value in metric_items
    )

    evidence_rows = [
        {
            "measure": "Identical-cycle paired windows",
            "candidate": paired.get("paired_windows"),
            "control": "same cycles",
            "meaning": "Only cycles replayable for both this candidate and the exact evaluation control are compared.",
        },
        {
            "measure": "Both-triggered paired windows",
            "candidate": paired.get("paired_both_triggered"),
            "control": "same pairs",
            "meaning": "Only these pairs contribute a direct execution-adjusted price delta.",
        },
        {
            "measure": "Censoring-aware trigger probability",
            "candidate": {
                "all_horizons": candidate_km,
                "selected_horizon": paired.get("selected_km_horizon"),
                "selected_probability_pct": paired.get(
                    "selected_candidate_km_trigger_probability_pct"
                ),
            },
            "control": {
                "all_horizons": control_km,
                "selected_probability_pct": paired.get(
                    "selected_control_km_trigger_probability_pct"
                ),
                "selected_delta_pct": paired.get(
                    "selected_km_trigger_probability_delta_pct"
                ),
            },
            "meaning": (
                "Kaplan-Meier estimates retain capture-end non-triggers as right-censored. "
                "The stability gate uses the longest 15-, 5-, or 1-minute horizon supported for both settings."
            ),
        },
        {
            "measure": "Trading-day bootstrap 80% interval (bps)",
            "candidate": [bootstrap.get("ci80_lower_bps"), bootstrap.get("ci80_upper_bps")],
            "control": "delta versus control",
            "meaning": "Two thousand deterministic resamples of whole UTC trading-day clusters.",
        },
        {
            "measure": "Trading-day bootstrap 95% interval (bps)",
            "candidate": [bootstrap.get("ci95_lower_bps"), bootstrap.get("ci95_upper_bps")],
            "control": "delta versus control",
            "meaning": "A wider uncertainty interval; it is descriptive and not a guarantee.",
        },
        {
            "measure": "Leave-one-day-out minimum / positive rate",
            "candidate": {
                "minimum_bps": influence.get("minimum_bps"),
                "positive_pct": influence.get("positive_pct"),
                "most_influential_day": influence.get("most_influential_day"),
            },
            "control": "delta versus control",
            "meaning": "The comparison is recomputed after removing each trading day to expose one-day dependence.",
        },
        {
            "measure": "Empirical execution model",
            "candidate": {
                "fills_considered": model.get("fills_considered"),
                "touch_residual_samples": model.get("touch_residual_samples"),
                "trigger_to_fill_samples": model.get("trigger_to_fill_samples"),
                "fresh_reference_quote_samples": model.get(
                    "fresh_reference_quote_samples"
                ),
                "crossed_touch_quotes_excluded": model.get(
                    "crossed_touch_quotes_excluded"
                ),
                "evidence": model.get("evidence"),
            },
            "control": "same model policy",
            "meaning": "Saved bid/ask and adverse historical fill residuals convert trigger prices into conservative estimated fills.",
        },
        {
            "measure": "Stable adjacent parameter region",
            "candidate": region,
            "control": "isolated peaks rejected",
            "meaning": "A changed setting must be the center of the preferred connected near-best region, not a single winning grid point.",
        },
    ]
    evidence_table = _table(
        evidence_rows,
        [("measure", "Robustness measure"), ("candidate", "Candidate result"), ("control", "Comparison"), ("meaning", "How to interpret it")],
    )

    changed_fields = [str(value) for value in selected.get("selection_changed_fields") or []]
    retained_text = "None — the unchanged evaluation control is retained"
    changed_text = ", ".join(changed_fields) if changed_fields else retained_text
    status_class = "good" if is_change else "warn"
    status_text = (
        "A changed setting passed every v1.3 robustness gate."
        if is_change
        else "No changed setting passed every v1.3 robustness gate; the control is the single set proposed for continued paper evaluation."
    )
    rejected = selected.get("selection_rejected_candidate_count")
    return (
        "<section><h2>One settings set to evaluate next</h2>"
        "<div class='notice'><strong>Paper-evaluation suggestion only.</strong> The optimizer always emits exactly one complete ATR profile per ticker. "
        "It changes the control only when paired same-cycle evidence is positive after censoring, execution-cost, uncertainty, influence, and stable-region checks.</div>"
        f"<p class='{status_class}'><strong>{_escape(status_text)}</strong></p>"
        f"<div class='grid'>{metric_cards}</div>"
        f"<p><strong>Changed fields relative to the evaluation control:</strong> {_escape(changed_text)}</p>"
        f"<p><strong>Why this set was selected:</strong> {_escape(selected.get('selection_reason'))}</p>"
        f"<p><strong>Deterministic selection rule:</strong> {_escape(selected.get('selection_method'))}</p>"
        f"<p><strong>Changed candidates rejected as unstable or unsupported:</strong> {_escape(_format(rejected))}</p>"
        f"<h3>Robustness evidence for the selected set</h3>{evidence_table}"
        f"<h3>Complete settings profile to enter for paper evaluation</h3>{settings_table}"
        f"<p class='warn'><strong>Required caution:</strong> {_escape(selected.get('selection_warning'))}</p>"
        "<p>The price-delta result is calculated only on identical cycles where both candidate and control triggered. Trigger availability is assessed separately with censoring-aware probabilities. "
        "BUY and normal-SELL changes are never combined into the highlighted set, alternate ATR windows are never highlighted, and initial-drop changes remain unscored because the saved data does not contain the complete counterfactual entry-selection path.</p>"
        "</section>"
    )


def _ticker_html(ticker: TickerAnalysis, relative_files: dict[str, str]) -> str:
    summaries = _candidate_evidence_rows(ticker)
    priority = [
        row
        for row in summaries
        if row.get("control_candidate")
        or row.get("priority") in {"evaluate first", "secondary evaluation"}
    ][:40]
    candidate_table = _table(
        priority,
        [
            ("leg", "Leg"),
            ("period", "ATR period"),
            ("bar_seconds", "Bar seconds"),
            ("baseline_window", "Evaluation-control ATR window"),
            ("minimum_profit_multiplier", "Min-profit × ATR"),
            ("multiplier", "Trail/rebound × ATR"),
            ("observations", "Windows"),
            ("scoreable_observations", "Complete-context ATR windows"),
            ("historical_atr_profile_count", "Historical profiles represented"),
            ("historical_atr_profile_observations", "Profile/window counts"),
            ("candidate_atr_observations", "ATR-usable windows"),
            ("candidate_atr_coverage_pct", "ATR coverage %"),
            ("trigger_rate_pct", "Observed trigger %"),
            ("scoreable_triggered", "Scoreable triggers"),
            ("right_censored_observations", "Right-censored windows"),
            ("km_trigger_probability_15m_pct", "KM trigger probability by 15m %"),
            (
                "median_adjusted_improvement_bps",
                "Median execution-adjusted improvement (bps)",
            ),
            ("paired_windows", "Same-cycle control pairs"),
            ("paired_both_triggered", "Pairs triggered for both"),
            ("paired_execution_adjusted_cycles", "Execution-adjusted pairs"),
            ("minimum_execution_model_samples", "Minimum model samples used"),
            ("independent_trading_days", "Independent delta days"),
            (
                "paired_execution_adjusted_median_bps",
                "Paired adjusted delta (bps)",
            ),
            ("bootstrap_ci80_lower_bps", "Bootstrap 80% low (bps)"),
            ("bootstrap_ci80_upper_bps", "Bootstrap 80% high (bps)"),
            (
                "bootstrap_probability_positive_pct",
                "Bootstrap P(delta > 0) %",
            ),
            ("lodo_minimum_bps", "Leave-one-day-out minimum (bps)"),
            ("lodo_positive_pct", "Leave-one-day-out positive %"),
            ("candidate_km_15m_pct", "Paired candidate KM 15m %"),
            ("control_km_15m_pct", "Paired control KM 15m %"),
            ("selected_km_horizon", "Longest shared KM horizon"),
            (
                "selected_km_trigger_probability_delta_pct",
                "KM probability delta at shared horizon (%)",
            ),
            (
                "paired_median_absolute_delay_delta_seconds",
                "Paired median absolute-delay change (s)",
            ),
            ("median_delay_seconds", "Signed median delay (s)"),
            ("median_absolute_delay_seconds", "Median absolute delay (s)"),
            ("median_mfe_bps", "Median post-trigger favorable excursion (bps)"),
            ("median_mae_bps", "Median post-trigger adverse excursion (bps)"),
            ("left_censored_observations", "Left-censored windows"),
            ("screening_score", "Screening score (legacy local)"),
            ("stable_region_id", "Stable region"),
            ("stable_region_size", "Stable-region points"),
            ("stable_region_center", "Region center"),
            ("evidence_stable", "Robust evidence stable"),
            ("instability_reasons_text", "Instability reasons"),
            ("evidence", "Evidence"),
            ("priority", "Priority"),
        ],
        empty="No replay candidate had enough usable data to rank.",
    )
    profile_breakdown_rows: list[dict[str, Any]] = []
    for summary_row in priority:
        for breakdown in summary_row.get("historical_atr_profile_breakdown") or []:
            profile_breakdown_rows.append(
                {
                    "leg": summary_row.get("leg"),
                    "candidate_key": summary_row.get("candidate_key"),
                    "period": summary_row.get("period"),
                    "bar_seconds": summary_row.get("bar_seconds"),
                    "minimum_profit_multiplier": summary_row.get(
                        "minimum_profit_multiplier"
                    ),
                    "multiplier": summary_row.get("multiplier"),
                    **breakdown,
                }
            )
    profile_breakdown_table = _table(
        profile_breakdown_rows[:500],
        [
            ("leg", "Leg"),
            ("candidate_key", "Candidate"),
            ("period", "ATR period"),
            ("bar_seconds", "Bar seconds"),
            ("minimum_profit_multiplier", "Min-profit × ATR"),
            ("multiplier", "Trail/rebound × ATR"),
            ("historical_atr_profile_id", "Historical profile subgroup"),
            ("observations", "Windows"),
            ("scoreable_observations", "Complete-context ATR windows"),
            ("candidate_atr_observations", "ATR-usable windows"),
            ("candidate_atr_coverage_pct", "ATR coverage %"),
            ("triggered", "Triggered"),
            ("scoreable_triggered", "Scoreable triggers"),
            ("trigger_rate_pct", "Trigger %"),
            ("median_improvement_bps", "Median local improvement (bps)"),
            ("median_delay_seconds", "Signed median delay (s)"),
            ("median_absolute_delay_seconds", "Median absolute delay (s)"),
            ("median_mfe_bps", "Median favorable excursion (bps)"),
            ("median_mae_bps", "Median adverse excursion (bps)"),
            ("left_censored_observations", "Left-censored windows"),
            ("screening_score", "Subgroup screening score"),
        ],
        empty="No candidate/profile subgroup rows were available.",
    )
    settings_table = _table(
        ticker.suggested_settings,
        [
            ("evaluation_priority", "Priority"),
            ("profile", "Profile"),
            ("evaluation_status", "Status"),
            ("settings_source", "Source"),
            ("primary_eligible", "Eligible for one highlighted set"),
            ("changed_fields", "Changed fields"),
            ("changed_legs", "Changed replay legs"),
            ("alternate_atr_window", "Alternate ATR window"),
            ("independently_combined_legs", "Independently combined legs"),
            ("atr_adaptive_enabled", "ATR adaptive"),
            ("atr_adapt_minimum_profit_enabled", "ATR adapts min profit"),
            ("atr_period", "Period"),
            ("atr_bar_seconds", "Bar seconds"),
            ("atr_initial_drop_multiplier", "Initial drop × ATR"),
            ("atr_buy_rebound_multiplier", "BUY rebound × ATR"),
            ("atr_minimum_profit_multiplier", "Minimum profit × ATR"),
            ("atr_sell_trail_multiplier", "SELL trail × ATR"),
            ("atr_min_pct", "Min clamp %"),
            ("atr_max_pct", "Max clamp %"),
            ("candidate_atr_coverage_pct", "Candidate ATR coverage %"),
            ("entry_screening_score", "BUY score"),
            ("entry_control_screening_score", "BUY control score"),
            ("entry_score_delta_vs_control", "BUY score delta"),
            ("entry_scoreable_windows", "BUY complete-context windows"),
            ("entry_scoreable_triggers", "BUY triggers"),
            ("entry_left_censored_rate_pct", "BUY left-censored %"),
            ("entry_right_censored_rate_pct", "BUY right-censored %"),
            ("entry_evidence_stable", "BUY robust evidence stable"),
            ("entry_paired_evidence", "BUY paired evidence"),
            ("entry_stable_region", "BUY stable region"),
            ("entry_instability_reasons", "BUY instability reasons"),
            ("exit_screening_score", "SELL score"),
            ("exit_control_screening_score", "SELL control score"),
            ("exit_score_delta_vs_control", "SELL score delta"),
            ("exit_scoreable_windows", "SELL complete-context windows"),
            ("exit_scoreable_triggers", "SELL triggers"),
            ("exit_left_censored_rate_pct", "SELL left-censored %"),
            ("exit_right_censored_rate_pct", "SELL right-censored %"),
            ("exit_evidence_stable", "SELL robust evidence stable"),
            ("exit_paired_evidence", "SELL paired evidence"),
            ("exit_stable_region", "SELL stable region"),
            ("exit_instability_reasons", "SELL instability reasons"),
            ("evidence", "Why it is shown"),
        ],
    )
    profile_table = _table(
        ticker.atr_settings_profiles,
        [
            ("profile_id", "Exact profile ID"),
            ("cycle_count", "Cycles"),
            ("first_cycle_number", "First cycle"),
            ("last_cycle_number", "Last cycle"),
            ("atr_adaptive_enabled", "ATR adaptive"),
            ("atr_adapt_minimum_profit_enabled", "Adaptive min profit"),
            ("atr_period", "Period"),
            ("atr_bar_seconds", "Bar seconds"),
            ("atr_initial_drop_multiplier", "Initial drop ×"),
            ("atr_buy_rebound_multiplier", "BUY rebound ×"),
            ("atr_minimum_profit_multiplier", "Min profit ×"),
            ("atr_sell_trail_multiplier", "SELL trail ×"),
            ("atr_min_pct", "Min clamp"),
            ("atr_max_pct", "Max clamp"),
            ("cycles_with_missing_fields", "Cycles with missing ATR fields"),
        ],
        empty="No cycle row contained a usable ATR settings snapshot.",
    )
    regime_table = _table(
        ticker.atr_settings_regimes,
        [
            ("regime_number", "Regime"),
            ("profile_id", "Exact profile ID"),
            ("cycle_count", "Consecutive cycles"),
            ("first_cycle_number", "First cycle"),
            ("last_cycle_number", "Last cycle"),
            ("first_created_at", "First cycle time"),
            ("last_created_at", "Last cycle time"),
        ],
        empty="No chronological ATR regime could be reconstructed.",
    )
    history_table = _table(
        ticker.atr_settings_history[:1000],
        [
            ("cycle_number", "Cycle"),
            ("cycle_id", "Cycle ID"),
            ("created_at", "Created UTC"),
            ("profile_id", "Exact profile ID"),
            ("atr_adaptive_enabled", "ATR adaptive"),
            ("atr_period", "Period"),
            ("atr_bar_seconds", "Bar seconds"),
            ("atr_initial_drop_multiplier", "Drop ×"),
            ("atr_buy_rebound_multiplier", "BUY ×"),
            ("atr_minimum_profit_multiplier", "Profit ×"),
            ("atr_sell_trail_multiplier", "SELL ×"),
            ("missing_atr_fields", "Missing fields"),
        ],
        empty="No cycles were found.",
    )
    score_components = [
        {"component": name.replace("_", " "), "points": value}
        for name, value in sorted(
            (ticker.coverage.get("coverage_score_components") or {}).items()
        )
    ]
    score_table = _table(score_components, [("component", "Coverage component"), ("points", "Points")])
    capture_table = _table(
        ticker.capture_inventory[:1000],
        [
            ("cycle_number", "Cycle"),
            ("cycle_id", "Cycle ID"),
            ("event_type", "Event"),
            ("event_time_utc", "Event time UTC"),
            ("rows_read", "Rows read"),
            ("usable_points", "Usable points"),
            ("rows_with_saved_atr", "Rows with saved bot ATR"),
            ("invalid_rows", "Invalid rows"),
            ("duplicate_rows", "Duplicate rows"),
            ("pre_window_seconds", "Pre seconds"),
            ("post_window_seconds", "Post seconds"),
            ("issues", "Issues"),
            ("path", "Relative path"),
        ],
        empty="No capture archives were found for this ticker.",
    )
    execution_model_table = _table(
        _execution_model_rows(ticker),
        [
            ("leg", "Leg"),
            ("fills_considered", "Fills considered"),
            ("touch_residual_samples", "Beyond-touch residual samples"),
            ("trigger_to_fill_samples", "Last-to-fill residual samples"),
            ("trading_days", "Trading days"),
            ("fresh_touch_quote_samples", "Fresh fill-touch quote samples"),
            (
                "fresh_reference_quote_samples",
                "Fresh fill-Last reference samples",
            ),
            ("crossed_touch_quotes_excluded", "Crossed fill quotes excluded"),
            (
                "stale_or_unknown_touch_quote_samples",
                "Stale/unknown fill-touch quote samples",
            ),
            (
                "stale_or_unknown_reference_quote_samples",
                "Stale/unknown fill-Last reference samples",
            ),
            (
                "median_fill_quote_age_seconds",
                "Median fill-quote age (seconds)",
            ),
            (
                "maximum_touch_quote_age_seconds",
                "Maximum touch-quote age used (seconds)",
            ),
            (
                "maximum_reference_quote_age_seconds",
                "Maximum Last-reference age used (seconds)",
            ),
            ("median_spread_bps", "Median saved spread (bps)"),
            (
                "p75_adverse_beyond_touch_bps",
                "75th percentile adverse beyond-touch residual (bps)",
            ),
            (
                "p75_adverse_trigger_to_fill_bps",
                "75th percentile adverse Last-to-fill residual (bps)",
            ),
            ("evidence", "Execution-model evidence"),
        ],
    )
    summary = ticker.atr_settings_summary
    current_settings = [
        {"setting": key, "value": value}
        for key, value in sorted((summary.get("current_app_settings") or {}).items())
    ]
    current_table = _table(
        current_settings,
        [("setting", "Current saved setting"), ("value", "Exact SQLite value")],
        empty="No applicable current app_settings.strategy ATR snapshot was available.",
    )
    source_control_values = dict(
        summary.get("evaluation_control_source_values")
        or summary.get("evaluation_control_settings")
        or {}
    )
    replay_control_values = dict(
        summary.get("counterfactual_replay_control_settings")
        or summary.get("evaluation_control_settings")
        or {}
    )
    control_keys = sorted(set(source_control_values) | set(replay_control_values))
    control_settings = [
        {
            "setting": key,
            "source_value": source_control_values.get(key),
            "replay_value": replay_control_values.get(key),
            "normalized": key
            in (summary.get("evaluation_control_normalization_adjustments") or {}),
        }
        for key in control_keys
    ]
    control_table = _table(
        control_settings,
        [
            ("setting", "Evaluation-control setting"),
            ("source_value", "Saved/source value"),
            ("replay_value", "Value used for replay and suggestions"),
            ("normalized", "Normalized to current GUI range"),
        ],
        empty="No evaluation control was available.",
    )
    normalization_rows = [
        {
            "setting": key,
            "saved": details.get("saved") if isinstance(details, dict) else None,
            "used": details.get("used") if isinstance(details, dict) else None,
        }
        for key, details in sorted(
            (summary.get("evaluation_control_normalization_adjustments") or {}).items()
        )
    ]
    normalization_table = _table(
        normalization_rows,
        [
            ("setting", "Normalized setting"),
            ("saved", "Saved/source value"),
            ("used", "Replay/suggestion value"),
        ],
        empty="No normalization was required; all evaluation-control values were preserved exactly.",
    )
    median_settings = [
        {
            "setting": key,
            "value": value,
            "cycles_with_value": (summary.get("historical_median_observed_counts") or {}).get(key, 0),
            "used_default": key in (summary.get("historical_median_default_fallback_fields") or []),
        }
        for key, value in sorted((summary.get("historical_median_settings") or {}).items())
    ]
    median_table = _table(
        median_settings,
        [
            ("setting", "Historical median setting"),
            ("value", "Median/majority value"),
            ("cycles_with_value", "Cycles contributing"),
            ("used_default", "Default fallback used"),
        ],
    )
    issues = "".join(f"<li>{_escape(issue)}</li>" for issue in ticker.issues) or "<li>None recorded.</li>"
    limits = "".join(f"<li>{_escape(item)}</li>" for item in ticker.limitations)
    downloads = " | ".join(
        f"<a href='{html.escape(path, quote=True)}'>{_escape(label)}</a>"
        for label, path in relative_files.items()
    )
    primary_evaluation_section = _primary_evaluation_section(ticker)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{_escape(ticker.ticker)} coverage and replay</title><style>{_CSS}</style></head>
<body><header><h1>{_escape(APP_NAME)} — {_escape(ticker.ticker)}</h1><div>Version {_escape(APP_VERSION)} · deterministic read-only fill-window analysis</div></header>
<main>
<section><h2>How to use this report</h2><div class="notice"><strong>These are experiments to evaluate, not optimized live settings.</strong> The saved data is centred on actual fills and is selection-biased. A candidate can look better inside the recorded 30-minute windows while performing worse on full sessions, missed trades, gaps, or real order-book liquidity.</div><p>{downloads}</p></section>
<section><h2>Data coverage</h2>{_coverage_cards(ticker)}<p>The score measures whether the saved evidence is broad and usable; it does not measure strategy quality or future profitability. Components are capped and sum to at most 100 points. The ATR-evidence component is awarded when at least one replay window has candidate ATR reconstructed from saved prices or a valid matching-window bot ATR fallback; a saved ATR column is not required when reconstruction succeeds. Grades additionally require completed-cycle depth: A needs score ≥80 and ≥20 completed cycles; B ≥65 and ≥10; C ≥45 and ≥3; otherwise D.</p>{score_table}</section>
{primary_evaluation_section}
<section><h2>Actual ATR settings and changes between cycles</h2><div class="info"><strong>The “Historical median baseline (derived from actual stored cycle ATR settings)” uses values actually stored on this ticker's cycle rows, except for fields explicitly marked as fallback defaults.</strong> It is a descriptive actual-settings summary, not a fitted recommendation and not necessarily one complete configuration that ever ran. Numeric fields use medians; booleans use deterministic majority values. The evaluation control used to center candidate generation is shown separately and prefers one complete applicable saved profile when available.</div>{_settings_explanation(ticker)}<h3>Evaluation control actually used for replay</h3><p>The control hierarchy is: applicable complete current <code>app_settings.strategy</code>; otherwise the latest complete historical cycle snapshot; otherwise the historical median/default summary. Candidate generation and the highlighted paper-evaluation set use the normalized replay values below. Valid source values are preserved exactly. Malformed, legacy, or out-of-range values are repaired only to current BouncyBot GUI-enterable limits and disclosed instead of being silently replayed under one value while the report displays another.</p><p><strong>Chosen source:</strong> {_escape(summary.get('evaluation_control_label'))} ({_escape(summary.get('evaluation_control_source'))}).</p>{control_table}<h4>Normalization adjustments</h4>{normalization_table}<h3>Current saved app settings</h3><p>{_escape(summary.get('current_app_settings_applicability'))} Updated UTC: {_escape(summary.get('current_app_settings_updated_at') or 'not recorded')}. Current settings are shown separately because they may have been saved after older cycles completed and therefore cannot be retroactively assigned to those cycles.</p>{current_table}<h3>Historical median baseline inputs</h3><p>“Cycles contributing” is counted independently for each field. A documented default is used only when no cycle contains a valid value for that field. The median row is descriptive evidence; when settings changed, it can combine field values that never ran together on one cycle.</p>{median_table}<h3>Distinct exact historical profiles</h3><p>An exact profile is a content-derived identifier for the ATR values stored together on one or more cycle rows. Missing values remain missing; they are not silently filled before profile identification.</p>{profile_table}<h3>Chronological settings regimes</h3><p>A regime is a consecutive run of cycles with the same exact stored ATR snapshot. If profile A was used, then B, then A again, this table shows three regimes. This makes changes and later reversions visible.</p>{regime_table}<h3>Cycle-by-cycle settings provenance</h3>{history_table}</section>
<section><h2>ATR settings to evaluate</h2><p>The first row is the normalized evaluation control used to construct the candidate grid: current applicable saved settings when complete, otherwise the latest complete historical cycle profile, otherwise the documented historical-median/default summary. The historical median remains visible as descriptive actual-settings evidence when it differs. Replay-screened rows change only parameters that can be examined inside saved fill windows.</p><p>A row is eligible for the single highlighted changed set only when it uses the evaluation-control ATR window, changes exactly one independently replayed BUY or normal-SELL leg, compares candidate and control on the same cycles, has adequate empirical execution samples and independent trading days, retains a positive deterministic bootstrap interval, remains positive whenever one day is removed, does not materially reduce censoring-aware trigger probability or worsen adverse excursion, and is the center of a supported adjacent parameter region. If no row passes every gate, the unchanged evaluation control remains the one proposed set. Alternate-window and combined rows remain comparison experiments; initial-drop sensitivity rows remain unscored because the anchor-to-drop path is usually missing.</p>{settings_table}</section>
<section><h2>How ATR is reconstructed</h2><p>Price rows are grouped into fixed UTC buckets of the candidate bar duration. For each bar the optimizer calculates high, low and close. True range is the maximum of: high − low, |high − previous close|, and |low − previous close|. Candidate ATR is the simple mean of the latest <em>period</em> true ranges and is converted to a percentage of the latest close.</p><div class="formula">effective percentage = round(clamp(ATR% × candidate multiplier, evaluation-control minimum clamp %, evaluation-control maximum clamp %), 2)</div><p>For every candidate, its ATR period, bar duration, multipliers, and clamps are fixed across all replayable cycles. The fixed replay clamps for this ticker are {_escape(summary.get('counterfactual_replay_atr_min_pct'))}% and {_escape(summary.get('counterfactual_replay_atr_max_pct'))}%. This avoids testing a nominally identical candidate with a different clamp on every cycle. A zero BUY-rebound or SELL-trail multiplier is preserved as BouncyBot's immediate-market mode and is not raised to the minimum clamp. The bot-captured ATR value is used as a fallback only when the candidate period and bar duration exactly match those stored on that cycle; alternate windows require reconstruction from captured prices. If the evaluation control disables ATR-adaptive minimum profit, the optimizer does not vary that multiplier: it retains each cycle's saved manual rise_trigger_pct while screening only the ATR-derived SELL trail, and labels that limitation.</p></section>
<section><h2>How counterfactual replay works</h2><h3>BUY replay</h3><ol><li>Start at the recorded BUY-order time, or the first saved point when the order began before the capture.</li><li>Reconstruct ATR for the candidate period/bar and lock the candidate BUY trail percentage.</li><li>Track the running low of captured Last/trigger prices.</li><li>Trigger when Last/trigger price reaches running low × (1 + trail%).</li><li>Compare that local trigger with the recorded average BUY fill. No new initial-drop trade is invented.</li></ol><h3>Normal SELL replay</h3><ol><li>Start with the actual average BUY price and saved SELL capture.</li><li>Before activation, reconstruct candidate ATR and calculate minimum-profit and trailing percentages.</li><li>Calculate the required activation price as <code>rounded minimum stop ÷ (1 − SELL trail fraction)</code>, preserving the cycle's saved manual-minimum-profit and slippage-buffer behavior where applicable.</li><li>Use the saved selected app strategy price for activation. After activation, lock the percentages and use captured Last/trigger price for the native SELL trail.</li><li>Track the running high and trigger when Last/trigger price falls to running high × (1 − trail%).</li><li>Compare the local trigger with the recorded normal SELL fill. Protective exits are excluded.</li></ol><h3>Unknown outcomes at capture end</h3><p>When a valid candidate has not triggered by the final saved row, the optimizer records a <strong>right-censored</strong> result: it knows the candidate did not trigger during the observed interval, but it does not claim that it would never trigger later. Left-censored windows start after required earlier state and are excluded from ranking. Right-censored complete-context windows remain in Kaplan-Meier trigger-probability estimates and are not charged as confirmed misses.</p></section>
<section><h2>Empirical spread and slippage adjustment</h2><p>Trigger price is not execution price. For each ticker and leg, the optimizer measures the saved quote spread at the historical fill and adverse fill residuals relative to the contemporaneous bid/ask touch or Last reference. A candidate BUY fill is estimated from its trigger ask (or Last when no quote exists) plus the 75th-percentile non-negative adverse residual. A candidate SELL fill uses its trigger bid minus the corresponding residual. Fill-time bid/ask quotes and Last references older than five seconds are excluded from execution residuals; impossible crossed quotes are also excluded. The candidate's own cycle is excluded from its empirical model whenever another sample is available. If rows for one cycle disagree on immutable historical fill or quote context, that cycle is excluded fail-closed and a changed primary suggestion is blocked. A per-pair residual-sample count of zero remains zero rather than inheriting a larger ticker aggregate. This is conservative top-of-book evidence, not an order-book or market-impact simulation.</p>{execution_model_table}</section>
<section><h2>Paired counterfactual evidence and robustness screening</h2><p>The headline table pools usable windows for the ticker but every changed-candidate conclusion is based on the intersection of cycle IDs where both that candidate and the exact evaluation control are replayable. This prevents a candidate from appearing better merely because difficult control or candidate windows are absent. Duplicate cycle/candidate rows or immutable cycle-context disagreements are excluded and make the candidate unstable.</p><h3>How the v1.3 evidence is constructed</h3><ol><li><strong>Same-cycle pairing:</strong> candidate and control are compared only on identical cycles. Price deltas require both to trigger; trigger availability uses all paired triggered and right-censored windows.</li><li><strong>Execution adjustment:</strong> candidate and control trigger prices are converted to conservative estimated fills using saved spread and empirical adverse slippage.</li><li><strong>Right-censoring:</strong> capture-end non-triggers are not called misses. Kaplan-Meier estimates report trigger probability at supported 1-, 5-, and 15-minute horizons without extrapolating beyond available follow-up. The longest shared horizon is used, and a changed suggestion requires at least five minutes of shared support.</li><li><strong>Trading-day bootstrap:</strong> UTC trading days are resampled as clusters for 2,000 deterministic replicates. The report shows 80% and 95% intervals and the percentage of replicates with a positive paired median.</li><li><strong>Leave-one-day-out:</strong> each contributing day is removed in turn. A changed primary suggestion is rejected if any available omission makes the paired median zero or negative.</li><li><strong>Timing and adverse movement:</strong> a changed suggestion is rejected when median absolute trigger-time error deteriorates by more than five minutes or median post-trigger adverse excursion deteriorates by more than 25 bps versus control.</li><li><strong>Stable parameter region:</strong> isolated peaks are rejected. The optimizer finds near-best connected components around local peaks and selects the component with the strongest worst-point paired result; only its deterministic center can be highlighted.</li></ol>{candidate_table}<h3>Candidate results split by settings originally stored on each cycle</h3><p>The same fixed candidate is also summarized separately for each historical ATR profile. Small subgroups are descriptive only; differences can reflect date, volatility, or historical trade selection rather than a causal effect of the old settings.</p>{profile_breakdown_table}<h3>Screening score (legacy local diagnostic)</h3><p>The older local score remains as a descriptive tie-break and diagnostic, not the primary decision method:</p><div class="formula">Screening score = median execution-adjusted local improvement (bps) − median absolute trigger-time error (minutes) − 0.05 × median adverse excursion (bps)</div><p>Only complete-context triggered windows enter that local formula. Explicitly confirmed no-trigger outcomes can receive a miss penalty, but ordinary capture-window exhaustion is right-censoring and receives no miss penalty. The highlighted v1.3 setting is controlled by paired evidence, uncertainty, influence, censoring, execution-model, and stable-region gates.</p></section>
<section><h2>Capture matching and inventory</h2><p>Explicit cycle IDs are authoritative. A capture with a conflicting explicit cycle ID is never matched merely because its ticker and cycle number happen to agree. Within a matching cycle and leg, an exact non-empty order-reference match outranks a blank legacy reference; a conflicting non-empty order reference is rejected. Timestamp distance to the recorded fill is used only after that reference ranking. If the nearest matching archive contains no usable market rows, the next valid candidate is tried. Every otherwise valid ZIP is parsed for compact coverage statistics, but full price rows are retained in memory only while its selected cycle is replayed.</p>{capture_table}</section>
<section><h2>Data-quality issues</h2><ul>{issues}</ul></section>
<section><h2>Limitations</h2><ul>{limits}</ul><p>The optimizer still cannot observe full-session counterfactual trades, trades another initial-drop setting would have created, complete order-book depth, queue position, partial-fill sequencing, commissions, gaps, or market impact. The empirical fill adjustment is based on this ticker's saved fills and top-of-book quotes and can remain weak or biased when samples are sparse. A SELL capture centred on the final fill commonly starts after BUY fill or activation; such left-censored windows are excluded. Bootstrap and leave-one-day-out checks reduce sensitivity to the observed sample but do not remove historical selection bias. Validate the single proposed set in forward paper trading.</p></section>
<section><h2>Column glossary</h2><ul><li><strong>ATR coverage:</strong> percentage of complete-context candidate windows where ATR was reconstructed or validly recovered for the exact historical window.</li><li><strong>Right-censored:</strong> the candidate had not triggered when the saved capture ended; later behavior is unknown.</li><li><strong>KM trigger probability:</strong> Kaplan-Meier estimate that incorporates triggered and right-censored observations and is shown only when the requested horizon is supported.</li><li><strong>Paired adjusted delta:</strong> candidate minus control execution-adjusted local improvement, computed only for identical cycles where both triggered.</li><li><strong>Bootstrap interval:</strong> deterministic percentile interval from resampling whole UTC trading-day clusters.</li><li><strong>Leave-one-day-out minimum:</strong> worst paired median after omitting each contributing day in turn.</li><li><strong>Stable region:</strong> a connected group of adjacent candidate-grid points with positive near-best paired evidence; only the preferred region center can be highlighted.</li><li><strong>MFE/MAE:</strong> maximum favorable/adverse movement after the simulated trigger inside the remaining saved capture.</li><li><strong>Historical profile:</strong> exact ATR settings snapshot stored on each contributing cycle.</li></ul></section>
</main></body></html>"""


def _index_html(result: AnalysisResult, ticker_links: list[tuple[TickerAnalysis, str]]) -> str:
    rows = []
    for ticker, link in ticker_links:
        coverage = ticker.coverage
        rows.append(
            {
                "ticker": ticker.ticker,
                "coverage_grade": coverage.get("coverage_grade"),
                "coverage_score": coverage.get("coverage_score"),
                "completed_cycles": coverage.get("completed_cycles"),
                "buy_capture_match_pct": coverage.get("buy_capture_match_pct"),
                "sell_capture_match_pct": coverage.get("sell_capture_match_pct"),
                "profiles": ticker.atr_settings_summary.get("distinct_atr_profiles"),
                "changes": ticker.atr_settings_summary.get("configuration_change_count"),
                "replayable_buy_windows": coverage.get("replayable_buy_windows"),
                "replayable_sell_windows": coverage.get("replayable_sell_windows"),
                "link": f"<a href='{html.escape(link, quote=True)}'>Open report</a>",
            }
        )
    if rows:
        labels = (
            "Ticker",
            "Grade",
            "Score",
            "Completed",
            "BUY match",
            "SELL match",
            "ATR profiles",
            "Setting changes",
            "BUY replay",
            "SELL replay",
            "Report",
        )
        header = "".join(f"<th>{_escape(label)}</th>" for label in labels)
        body = "".join(
            "<tr>"
            f"<td>{_escape(row['ticker'])}</td>"
            f"<td>{_escape(row['coverage_grade'])}</td>"
            f"<td>{_escape(row['coverage_score'])}</td>"
            f"<td>{_escape(row['completed_cycles'])}</td>"
            f"<td>{_escape(_pct(row['buy_capture_match_pct']))}</td>"
            f"<td>{_escape(_pct(row['sell_capture_match_pct']))}</td>"
            f"<td>{_escape(row['profiles'])}</td>"
            f"<td>{_escape(row['changes'])}</td>"
            f"<td>{_escape(row['replayable_buy_windows'])}</td>"
            f"<td>{_escape(row['replayable_sell_windows'])}</td>"
            f"<td>{row['link']}</td></tr>"
            for row in rows
        )
        table = f"<div class='scroll'><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>"
    else:
        table = "<p>No tickers were found.</p>"
    issues = "".join(f"<li>{_escape(issue)}</li>" for issue in result.global_issues) or "<li>None recorded.</li>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{_escape(APP_NAME)} report</title><style>{_CSS}</style></head>
<body><header><h1>{_escape(APP_NAME)}</h1><div>Version {_escape(APP_VERSION)} · analysis {_escape(result.analysis_id[:16])}</div></header>
<main>
<section><h2>Analysis summary</h2><div class="grid"><div class="card"><div class="muted">Evidence data through UTC</div><div>{_escape(result.data_through_utc or 'No timestamped evidence')}</div></div><div class="card"><div class="muted">Tickers</div><div class="metric">{len(result.tickers)}</div></div><div class="card"><div class="muted">Input fingerprint</div><div class="code small">{_escape(result.input_fingerprint)}</div></div><div class="card"><div class="muted">Database content fingerprint</div><div class="code small">{_escape(result.database_sha256)}</div></div></div><p>This report is content-addressed: identical database/capture bytes and identical analysis limits produce the same analysis ID, directory name, and report bytes. Wall-clock run time and absolute source/output paths are deliberately excluded.</p></section>
<section><h2>Per-ticker reports</h2>{table}</section>
<section><h2>What the optimizer does</h2><p>It copies the stopped bot's SQLite/WAL files through ordinary read-only file access, reads capture ZIP members without extraction, reconstructs each ticker's historical ATR settings, matches fill-centred captures to recorded fills, replays a bounded candidate grid, and writes explanatory HTML/JSON/CSV evidence. It does not connect to IBKR, place orders, write settings, or modify the source database/captures.</p></section>
<section><h2>Run-level issues</h2><ul>{issues}</ul></section>
<section><h2>Safety and interpretation boundary</h2><div class="notice">Treat every proposed setting as a paper-trading experiment. A higher local replay score is not evidence of future profitability. The optimizer cannot recreate trades that never occurred or model full market liquidity.</div></section>
</main></body></html>"""


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _hash(path)
        for path in sorted(
            (item for item in root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(root).as_posix(),
        )
    }


def _candidate_profile_rows(ticker: TickerAnalysis) -> list[dict[str, Any]]:
    """Flatten candidate/profile metrics for direct CSV inspection."""
    rows: list[dict[str, Any]] = []
    for summary in ticker.candidate_summaries:
        for breakdown in summary.historical_atr_profile_breakdown:
            rows.append(
                {
                    "leg": summary.leg,
                    "candidate_key": summary.candidate_key,
                    "period": summary.period,
                    "bar_seconds": summary.bar_seconds,
                    "minimum_profit_multiplier": summary.minimum_profit_multiplier,
                    "multiplier": summary.multiplier,
                    "baseline_window": summary.baseline_window,
                    "pooled_priority": summary.priority,
                    **breakdown,
                }
            )
    return rows


def write_reports(result: AnalysisResult) -> AnalysisResult:
    """Publish a complete deterministic report directory atomically and idempotently."""
    output = result.output_dir.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.partial-", dir=output.parent)
    )
    relative_written: list[Path] = []
    ticker_links: list[tuple[TickerAnalysis, str]] = []
    issue_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    try:
        used_folders: set[str] = set()
        for ticker in result.tickers:
            folder_name = ticker_folder_name(ticker.ticker)
            folded = folder_name.casefold()
            if folded in used_folders:
                raise ValueError(f"Ticker report folder collision: {folder_name}")
            used_folders.add(folded)
            folder = partial / folder_name
            folder.mkdir(parents=True, exist_ok=False)
            prefix = folder_name
            coverage_json = folder / f"{prefix}_coverage_and_replay.json"
            candidates_csv = folder / f"{prefix}_candidate_screening.csv"
            paired_evidence_csv = folder / f"{prefix}_paired_candidate_evidence.csv"
            execution_model_csv = folder / f"{prefix}_execution_model.csv"
            candidate_profiles_csv = (
                folder / f"{prefix}_candidate_profile_breakdown.csv"
            )
            primary_settings_csv = folder / f"{prefix}_primary_settings_to_evaluate.csv"
            settings_csv = folder / f"{prefix}_atr_settings_to_evaluate.csv"
            profiles_csv = folder / f"{prefix}_historical_atr_profiles.csv"
            regimes_csv = folder / f"{prefix}_atr_settings_regimes.csv"
            history_csv = folder / f"{prefix}_atr_settings_by_cycle.csv"
            captures_csv = folder / f"{prefix}_capture_inventory.csv"
            observations_csv = folder / f"{prefix}_replay_observations.csv"
            report_html = folder / f"{prefix}_coverage_and_replay.html"

            _write_json(coverage_json, ticker.to_jsonable())
            _write_csv(candidates_csv, [asdict(row) for row in ticker.candidate_summaries])
            _write_csv(paired_evidence_csv, _candidate_evidence_rows(ticker))
            _write_csv(execution_model_csv, _execution_model_rows(ticker))
            _write_csv(candidate_profiles_csv, _candidate_profile_rows(ticker))
            _write_csv(primary_settings_csv, [ticker.primary_evaluation_setting])
            _write_csv(settings_csv, ticker.suggested_settings)
            _write_csv(profiles_csv, ticker.atr_settings_profiles)
            _write_csv(regimes_csv, ticker.atr_settings_regimes)
            _write_csv(history_csv, ticker.atr_settings_history)
            _write_csv(captures_csv, ticker.capture_inventory)
            _write_csv(observations_csv, [asdict(row) for row in ticker.replay_observations])
            relative_files = {
                "JSON evidence": coverage_json.name,
                "Candidate screening CSV": candidates_csv.name,
                "Paired candidate evidence CSV": paired_evidence_csv.name,
                "Execution model CSV": execution_model_csv.name,
                "Candidate/profile breakdown CSV": candidate_profiles_csv.name,
                "Primary settings suggestion CSV": primary_settings_csv.name,
                "ATR settings to evaluate CSV": settings_csv.name,
                "Historical ATR profiles CSV": profiles_csv.name,
                "ATR regimes CSV": regimes_csv.name,
                "Cycle settings history CSV": history_csv.name,
                "Capture inventory CSV": captures_csv.name,
                "Replay observations CSV": observations_csv.name,
            }
            report_html.write_text(_ticker_html(ticker, relative_files), encoding="utf-8")
            files = (
                coverage_json,
                candidates_csv,
                paired_evidence_csv,
                execution_model_csv,
                candidate_profiles_csv,
                primary_settings_csv,
                settings_csv,
                profiles_csv,
                regimes_csv,
                history_csv,
                captures_csv,
                observations_csv,
                report_html,
            )
            relative_written.extend(path.relative_to(partial) for path in files)
            ticker_links.append((ticker, f"{folder_name}/{report_html.name}"))
            summary_rows.append({"ticker": ticker.ticker, **ticker.coverage})
            for issue in ticker.issues:
                issue_rows.append({"scope": ticker.ticker, "issue": issue})

        for issue in result.global_issues:
            issue_rows.append({"scope": "run", "issue": issue})

        summary_csv = partial / "tickers_summary.csv"
        issues_csv = partial / "data_quality_issues.csv"
        index_html = partial / "index.html"
        methodology = partial / "README_REPORT.txt"
        manifest = partial / "analysis_manifest.json"
        checksums = partial / "SHA256SUMS.txt"
        _write_csv(summary_csv, summary_rows)
        _write_csv(issues_csv, issue_rows)
        index_html.write_text(_index_html(result, ticker_links), encoding="utf-8")
        methodology.write_text(
            f"{APP_NAME} {APP_VERSION}\n\n"
            "Open index.html first. Each ticker folder contains a detailed HTML explanation plus machine-readable JSON/CSV evidence.\n\n"
            "ACTUAL SETTINGS\n"
            "The row labelled 'Historical median baseline (derived from actual stored cycle ATR settings)' is calculated from values actually saved on cycle rows, except for fields explicitly listed as defaults. It is a descriptive actual-settings summary, not a fitted recommendation and not necessarily one complete profile that ever ran. Candidate generation instead uses an evaluation control: current applicable complete app settings, otherwise the latest complete historical cycle profile, otherwise the median/default summary. Exact profiles, chronological regimes, and cycle-by-cycle settings are exported separately.\n\n"
            "ONE SETTINGS SET TO EVALUATE NEXT\n"
            "Each ticker report highlights exactly one paper-evaluation set between Data coverage and Actual ATR settings. A changed profile must use the evaluation-control ATR window, change exactly one independently replayed BUY or normal-SELL leg, and pass all paired-evidence gates: at least five same-cycle pairs, at least three pairs that triggered for both settings, at least five independent UTC trading days contributing price deltas, at least five empirical execution samples, a positive 80% deterministic trading-day bootstrap interval, at least 80% bootstrap probability of a positive paired delta, positive leave-one-day-out estimates, at least five minutes of shared censoring-aware follow-up, no trigger-probability deficit larger than five percentage points, no median absolute timing-error increase larger than five minutes, no median adverse-excursion increase larger than 25 bps, and support from the center of a preferred adjacent parameter region. Combined BUY/SELL and alternate-window profiles cannot be highlighted. If no changed profile passes every gate, the unchanged normalized evaluation control is retained.\n\n"
            "CHANGED SETTINGS BETWEEN CYCLES\n"
            "Each ATR candidate's period, bar duration, multipliers, and evaluation-control clamps are held constant across every replayable cycle. Each observation is tagged with the exact ATR profile originally stored on its cycle. When the evaluation control disables adaptive minimum profit, each cycle's saved manual rise_trigger_pct is retained as a historical input and the minimum-profit ATR multiplier is not varied. The pooled table shows overall behavior; candidate_profile_breakdown.csv shows the same candidate separately for every historical profile subgroup. Subgroup differences can be confounded by date and market regime and are not causal evidence.\n\n"
            "REPLAY\n"
            "BUY replay starts at the recorded order time, reconstructs candidate ATR, tracks the running low of captured Last/trigger prices, and triggers on the candidate rebound. Normal SELL replay mirrors the trading bot's slippage-aware four-decimal activation calculation, tests activation with the selected app strategy price, and after activation follows captured Last/trigger price. Protective exits are inventoried but excluded from normal-profit SELL ranking. A valid non-trigger at capture end is right-censored rather than a confirmed miss. Left-censored windows are excluded from ranking.\n\n"
            "PAIRED EVIDENCE, UNCERTAINTY, AND EXECUTION\n"
            "Every changed candidate is compared with the exact control only on identical cycle IDs. Price deltas use pairs where both triggered; Kaplan-Meier trigger probabilities use the paired triggered/right-censored set at the longest horizon supported by both settings (15, then 5, then 1 minute), and a changed suggestion requires at least five minutes. Simulated trigger prices are adjusted to conservative estimated fills using saved bid/ask spread and ticker/leg empirical adverse residuals, excluding the candidate cycle from its own model when possible. Fill-touch quotes and Last references older than five seconds are excluded from execution residuals; impossible crossed quotes are also excluded. Conflicting immutable fill/quote context for one cycle is excluded fail-closed and blocks a changed suggestion; a real per-pair residual-sample count of zero is never replaced by a ticker-wide aggregate. UTC trading days are resampled as clusters for 2,000 deterministic bootstrap replicates and are removed one at a time for influence analysis. Median absolute timing-error deterioration above five minutes and median adverse-excursion deterioration above 25 bps are rejection conditions. Isolated best grid points are rejected. Among supported adjacent near-best plateaus around local peaks, the plateau with the strongest worst-point paired result is preferred and only its deterministic center can be highlighted. The legacy local score remains descriptive; it no longer decides the primary suggestion. Screening score = median execution-adjusted local improvement (bps) − median absolute trigger-time error (minutes) − 0.05 × median adverse excursion (bps).\n\n"
            "DETERMINISM AND SAFETY\n"
            "The report is content-addressed: its analysis ID is derived from database/WAL content hashes, capture relative paths/content hashes, optimizer version, and analysis limits. Run time and absolute paths are excluded. The source database/captures are checked before and after analysis; reports are published atomically and repeated identical runs reuse byte-identical output.\n\n"
            "LIMITATION\n"
            "Fill-centred captures cannot recreate full-session trades, initial-drop trades that never happened, broker tick-size normalization, exchange-specific stop behavior, full order-book depth, queue position, partial-fill sequencing, commissions, gaps, or market impact. Kaplan-Meier estimates assume censoring is non-informative conditional on the observed data; a capture ending for a market-dependent reason can violate that assumption. Empirical fill adjustment, bootstrap uncertainty, and stability screening are only as representative as the saved historical sample, and testing many candidates can still select noise. BUY and SELL candidate rows are not a jointly simulated full-strategy result. Evaluate the single proposed set in forward paper trading.\n",
            encoding="utf-8",
        )
        relative_written.extend(
            path.relative_to(partial)
            for path in (summary_csv, issues_csv, index_html, methodology)
        )

        manifest_relative = manifest.relative_to(partial)
        checksum_relative = checksums.relative_to(partial)
        final_relatives = sorted(
            relative_written + [manifest_relative, checksum_relative],
            key=lambda path: path.as_posix(),
        )
        result.files_written = [output / path for path in final_relatives]
        _write_json(manifest, result.to_jsonable())
        relative_written.append(manifest_relative)

        checksum_lines = [
            f"{_hash(partial / relative)}  {relative.as_posix()}"
            for relative in sorted(relative_written, key=lambda path: path.as_posix())
        ]
        checksums.write_text("\n".join(checksum_lines) + "\n", encoding="ascii")

        if output.exists():
            if not output.is_dir():
                raise FileExistsError(f"Report output exists and is not a directory: {output}")
            if _tree_hashes(output) != _tree_hashes(partial):
                raise FileExistsError(
                    "A report directory with the same content-derived analysis ID exists but differs; "
                    f"refusing to overwrite possible corruption: {output}"
                )
            shutil.rmtree(partial)
        else:
            atomic_publish_directory(partial, output)
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return result
