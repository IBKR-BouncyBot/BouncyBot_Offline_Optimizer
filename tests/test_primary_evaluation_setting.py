from __future__ import annotations

import csv
import json
from pathlib import Path

from optimizer.analysis import _primary_evaluation_setting, run_analysis
from optimizer.models import AnalysisConfig
from optimizer.reports import write_reports
from tests.conftest import create_source_fixture


def _profile(
    name: str,
    *,
    leg: str,
    score: float,
    control_score: float = 0.0,
    coverage: float | None = 100.0,
    priority: int = 1,
    windows: int = 8,
    triggers: int = 6,
    multiplier: float = 0.75,
) -> dict[str, object]:
    if leg not in {"BUY", "normal SELL"}:
        raise ValueError(f"Unsupported test leg: {leg}")
    is_buy = leg == "BUY"
    changed_field = (
        "atr_buy_rebound_multiplier"
        if is_buy
        else "atr_sell_trail_multiplier"
    )
    paired_delta = score - control_score
    stable = bool(windows >= 5 and triggers >= 3 and paired_delta > 0.0)
    paired = {
        "paired_cycles": windows,
        "paired_windows": windows,
        "both_triggered_cycles": triggers,
        "paired_both_triggered": triggers,
        "paired_trading_days": 5 if stable else min(windows, 4),
        "independent_trading_days": 5 if stable else min(windows, 4),
        "median_execution_adjusted_delta_bps": paired_delta,
        "paired_execution_adjusted_median_bps": paired_delta,
        "median_mae_delta_bps": 0.0,
        "candidate_km_trigger_probability": {
            "1m_pct": 40.0,
            "5m_pct": 80.0,
            "15m_pct": 95.0,
        },
        "control_km_trigger_probability": {
            "1m_pct": 40.0,
            "5m_pct": 80.0,
            "15m_pct": 95.0,
        },
        "bootstrap": {
            "probability_positive_pct": 95.0 if stable else 50.0,
            "ci80_lower_bps": paired_delta - 1.0 if stable else -1.0,
            "ci80_upper_bps": paired_delta + 1.0,
            "ci95_lower_bps": paired_delta - 2.0 if stable else -2.0,
            "ci95_upper_bps": paired_delta + 2.0,
        },
        "leave_one_day_out": {
            "positive_pct": 100.0 if stable else 50.0,
            "minimum_bps": paired_delta - 1.0 if stable else -1.0,
        },
    }
    stable_region = {
        "region_id": "test-region",
        "size": 2,
        "preferred": True,
        "supported": stable,
        "is_center": stable,
    }
    return {
        "profile": name,
        "evaluation_priority": priority,
        "evaluation_status": "screened on saved fill windows",
        "settings_source": "fixed counterfactual candidate around the evaluation control",
        "actual_settings_control": False,
        "evaluation_control": False,
        "primary_eligible": stable,
        "candidate_atr_coverage_pct": coverage,
        "entry_screening_score": score if is_buy else None,
        "exit_screening_score": score if not is_buy else None,
        "entry_control_screening_score": control_score if is_buy else None,
        "exit_control_screening_score": control_score if not is_buy else None,
        "entry_score_delta_vs_control": score - control_score if is_buy else None,
        "exit_score_delta_vs_control": score - control_score if not is_buy else None,
        "entry_scoreable_windows": windows if is_buy else 0,
        "exit_scoreable_windows": windows if not is_buy else 0,
        "entry_scoreable_triggers": triggers if is_buy else 0,
        "exit_scoreable_triggers": triggers if not is_buy else 0,
        "changed_fields": [changed_field],
        "changed_legs": [leg],
        "alternate_atr_window": False,
        "entry_paired_evidence": paired if is_buy else {},
        "exit_paired_evidence": paired if not is_buy else {},
        "entry_stable_region": stable_region if is_buy else {},
        "exit_stable_region": stable_region if not is_buy else {},
        "entry_evidence_stable": stable if is_buy else False,
        "exit_evidence_stable": stable if not is_buy else False,
        "entry_instability_reasons": [] if is_buy and stable else ["unstable"],
        "exit_instability_reasons": [] if not is_buy and stable else ["unstable"],
        "evidence": "test evidence",
        "atr_adaptive_enabled": True,
        "atr_adapt_minimum_profit_enabled": True,
        "atr_period": 14,
        "atr_bar_seconds": 60,
        "atr_initial_drop_multiplier": 1.5,
        "atr_buy_rebound_multiplier": multiplier if is_buy else 0.75,
        "atr_minimum_profit_multiplier": 1.0,
        "atr_sell_trail_multiplier": multiplier if not is_buy else 1.0,
        "atr_min_pct": 0.1,
        "atr_max_pct": 20.0,
    }


def _control(name: str, *, evaluation_control: bool) -> dict[str, object]:
    return {
        "profile": name,
        "settings_source": (
            "app_settings.strategy"
            if evaluation_control
            else "cycles table ATR columns"
        ),
        "actual_settings_control": True,
        "evaluation_control": evaluation_control,
        "evaluation_status": "control",
        "evidence": "control evidence",
        "atr_adaptive_enabled": True,
        "atr_adapt_minimum_profit_enabled": True,
        "atr_block_new_buy_until_ready": False,
        "atr_adapt_protective_sell_enabled": False,
        "atr_period": 14,
        "atr_bar_seconds": 60,
        "atr_initial_drop_multiplier": 1.5,
        "atr_buy_rebound_multiplier": 0.75,
        "atr_minimum_profit_multiplier": 1.0,
        "atr_sell_trail_multiplier": 1.0,
        "atr_protective_sell_multiplier": 3.0,
        "atr_min_pct": 0.1,
        "atr_max_pct": 20.0,
    }


def test_primary_selector_highlights_only_one_replayed_leg_deterministically() -> None:
    buy = _profile("BUY change", leg="BUY", score=12.0, priority=3)
    sell = _profile("SELL change", leg="normal SELL", score=7.0, priority=2)
    combined = dict(buy)
    combined.update(
        {
            "profile": "combined comparison",
            "primary_eligible": False,
            "changed_fields": [
                "atr_buy_rebound_multiplier",
                "atr_sell_trail_multiplier",
            ],
            "changed_legs": ["BUY", "normal SELL"],
            "entry_screening_score": 100.0,
            "exit_screening_score": 100.0,
        }
    )

    first = _primary_evaluation_setting(
        [sell, combined, buy],
        {"evidence_level": "moderate"},
    )
    second = _primary_evaluation_setting(
        [buy, sell, combined],
        {"evidence_level": "moderate"},
    )

    assert first == second
    assert first["profile"] == "BUY change"
    assert first["selection_scored_legs"] == "BUY"
    assert first["selection_changed_fields"] == ["atr_buy_rebound_multiplier"]
    assert first["selection_kind"] == "single-leg paired-and-robust paper-evaluation change"
    assert first["selection_candidate_count"] == 2


def test_primary_selector_uses_deterministic_priority_for_exact_ties() -> None:
    later = _profile("later", leg="BUY", score=5.0, priority=9)
    earlier = _profile("earlier", leg="BUY", score=5.0, priority=2)

    selected = _primary_evaluation_setting(
        [later, earlier],
        {"evidence_level": "moderate"},
    )

    assert selected["profile"] == "earlier"
    assert selected["selection_candidate_count"] == 2


def test_primary_selector_treats_zero_as_a_valid_score_and_recomputes_control_delta() -> None:
    zero = _profile(
        "zero but improved",
        leg="BUY",
        score=0.0,
        control_score=-10.0,
        priority=9,
    )
    positive = _profile(
        "positive but weaker delta",
        leg="BUY",
        score=1.0,
        control_score=-5.0,
        priority=1,
    )
    # A stale precomputed field must not override the scores that are displayed
    # and used by the eligibility rule.
    zero["entry_score_delta_vs_control"] = -999.0

    selected = _primary_evaluation_setting(
        [positive, zero],
        {"evidence_level": "moderate"},
    )

    assert selected["profile"] == "zero but improved"
    assert selected["selection_leg_screening_score"] == 0.0
    assert selected["selection_score_delta_vs_control"] == 10.0


def test_primary_selector_is_input_order_independent_with_nonfinite_coverage_and_duplicate_names() -> None:
    first = _profile(
        "same",
        leg="BUY",
        score=5.0,
        coverage=float("nan"),
        priority=2,
        multiplier=0.65,
    )
    second = _profile(
        "same",
        leg="BUY",
        score=5.0,
        coverage=float("nan"),
        priority=2,
        multiplier=0.85,
    )

    selected_forward = _primary_evaluation_setting(
        [first, second],
        {"evidence_level": "moderate"},
    )
    selected_reverse = _primary_evaluation_setting(
        [second, first],
        {"evidence_level": "moderate"},
    )

    assert selected_forward == selected_reverse
    assert selected_forward["atr_buy_rebound_multiplier"] in {0.65, 0.85}
    assert selected_forward["candidate_atr_coverage_pct"] is None


def test_primary_selector_rejects_alternate_windows_and_insufficient_evidence() -> None:
    alternate = _profile("alternate", leg="BUY", score=100.0)
    alternate["alternate_atr_window"] = True
    alternate["primary_eligible"] = False
    weak = _profile("weak", leg="BUY", score=100.0, windows=4, triggers=4)
    control = _control("Evaluation control", evaluation_control=True)

    selected = _primary_evaluation_setting(
        [alternate, weak, control],
        {"evidence_level": "limited"},
    )

    assert selected["profile"] == "Evaluation control"
    assert selected["selection_is_change"] is False
    assert selected["selection_rejected_candidate_count"] == 2


def test_primary_selector_rejects_an_unknown_changed_leg_even_if_marked_eligible() -> None:
    malformed = _profile("malformed", leg="BUY", score=100.0)
    malformed["changed_legs"] = ["initial entry selection"]
    control = _control("Evaluation control", evaluation_control=True)

    selected = _primary_evaluation_setting(
        [malformed, control],
        {"evidence_level": "moderate"},
    )

    assert selected["profile"] == "Evaluation control"
    assert selected["selection_rejected_candidate_count"] == 1


def test_primary_selector_falls_back_to_explicit_evaluation_control() -> None:
    historical = _control("Historical control", evaluation_control=False)
    current = _control("Current exact", evaluation_control=True)

    current_selected = _primary_evaluation_setting(
        [historical, current],
        {"evidence_level": "limited"},
    )
    historical_selected = _primary_evaluation_setting(
        [historical],
        {"evidence_level": "limited"},
    )

    assert current_selected["profile"] == "Current exact"
    assert historical_selected["profile"] == "Historical control"
    assert current_selected["selection_kind"] == "unchanged evaluation-control paper test"
    assert current_selected["selection_candidate_count"] == 0
    assert "does not support one changed ATR profile" in current_selected["selection_reason"]


def test_primary_selector_does_not_present_an_incomplete_noncontrol_snapshot_as_the_control() -> None:
    historical = _control("Historical control", evaluation_control=True)
    incomplete_current = {
        "profile": "Incomplete current",
        "settings_source": "app_settings.strategy",
        "actual_settings_control": True,
        "evaluation_control": False,
        "evaluation_status": "control",
        "evidence": "current",
        "atr_period": 14,
    }

    selected = _primary_evaluation_setting(
        [incomplete_current, historical],
        {"evidence_level": "limited"},
    )

    assert selected["profile"] == "Historical control"

def test_ticker_report_places_one_primary_setting_between_coverage_and_history(
    tmp_path: Path,
) -> None:
    source = create_source_fixture(tmp_path / "bot", cycles=6)
    result = write_reports(
        run_analysis(AnalysisConfig(source, tmp_path / "reports"))
    )
    ticker = result.tickers[0]
    folder = result.output_dir / "AAPL"
    report = (folder / "AAPL_coverage_and_replay.html").read_text(encoding="utf-8")

    coverage_index = report.index("<h2>Data coverage</h2>")
    primary_index = report.index("<h2>One settings set to evaluate next</h2>")
    history_index = report.index("<h2>Actual ATR settings and changes between cycles</h2>")
    assert coverage_index < primary_index < history_index
    assert ticker.primary_evaluation_setting["profile"] in report
    assert "Paper-evaluation suggestion only" in report
    assert "Deterministic selection rule" in report
    assert "not an optimized live configuration" in report
    selected_buy = float(ticker.primary_evaluation_setting["atr_buy_rebound_multiplier"])
    assert f"{selected_buy:.2f}" in report

    primary_csv = folder / "AAPL_primary_settings_to_evaluate.csv"
    with primary_csv.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["profile"] == ticker.primary_evaluation_setting["profile"]

    evidence = json.loads(
        (folder / "AAPL_coverage_and_replay.json").read_text(encoding="utf-8")
    )
    assert evidence["primary_evaluation_setting"] == ticker.primary_evaluation_setting


def test_primary_setting_is_identical_for_repeated_analysis_of_same_input(
    tmp_path: Path,
) -> None:
    source = create_source_fixture(tmp_path / "bot-repeat", cycles=6)
    first = run_analysis(AnalysisConfig(source, tmp_path / "reports-a"))
    second = run_analysis(AnalysisConfig(source, tmp_path / "reports-b"))

    assert first.analysis_id == second.analysis_id
    assert first.tickers[0].primary_evaluation_setting == second.tickers[0].primary_evaluation_setting
