from __future__ import annotations

from pathlib import Path

from optimizer.atr import (
    captured_atr_near,
    normalize_atr_clamps,
    normalize_atr_multiplier,
    normalize_atr_window,
)
from optimizer.evidence import _region_center
from optimizer.ibrec import _object_dict
from optimizer.models import CandidateSummary, PricePoint, TickerAnalysis
from optimizer.presentation import _count, _percent, result_cells

ROOT = Path(__file__).resolve().parents[1]


def _summary(key: str, delta: float | None) -> CandidateSummary:
    summary = CandidateSummary(
        leg="buy",
        candidate_key=key,
        multiplier=0.75,
        minimum_profit_multiplier=None,
        period=14,
        bar_seconds=60,
        baseline_window=True,
        observations=20,
        scoreable_observations=20,
        triggered=20,
        scoreable_triggered=20,
        candidate_atr_observations=20,
        candidate_atr_coverage_pct=100.0,
        trigger_rate_pct=100.0,
        median_improvement_bps=delta,
        median_delay_seconds=0.0,
        median_absolute_delay_seconds=0.0,
        median_mfe_bps=0.0,
        median_mae_bps=0.0,
        left_censored_observations=0,
        left_censored_rate_pct=0.0,
        screening_score=delta,
        evidence="strong local-window sample",
        priority="evaluate first",
        rationale="type-narrowing regression",
    )
    if delta is not None:
        summary.paired_evidence["median_execution_adjusted_delta_bps"] = delta
    return summary


def test_untyped_atr_inputs_fail_closed_without_changing_valid_values() -> None:
    invalid = object()
    assert normalize_atr_multiplier(invalid, allow_zero=False) == 0.01
    assert normalize_atr_multiplier(invalid, allow_zero=True) == 0.0
    assert normalize_atr_window(invalid, invalid) == (14, 60)
    assert normalize_atr_clamps(invalid, invalid) == (0.10, 20.00)

    assert normalize_atr_multiplier("0.75", allow_zero=False) == 0.75
    assert normalize_atr_window("21", "120") == (21, 120)
    assert normalize_atr_clamps("0.25", "15") == (0.25, 15.0)


def test_captured_atr_optional_values_are_narrowed_before_selection() -> None:
    rows = [
        PricePoint(90.0, "", 100.0, 100.0, atr_pct=None),
        PricePoint(95.0, "", 100.0, 100.0, atr_pct=float("nan")),
        PricePoint(99.0, "", 100.0, 100.0, atr_pct=0.55),
    ]
    assert captured_atr_near(rows, 100.0) == 0.55


def test_region_center_handles_missing_and_zero_paired_deltas() -> None:
    missing = _summary("missing", None)
    zero = _summary("zero", 0.0)
    assert _region_center([missing, zero]) is zero


def test_manifest_source_and_presentation_values_fail_closed() -> None:
    assert _object_dict(None) == {}
    assert _object_dict([("type", "invalid")]) == {}
    assert _object_dict({"type": "fixture"}) == {"type": "fixture"}
    assert _object_dict({1: "fixture"}) == {"1": "fixture"}
    assert _percent(None) == "—"
    assert _percent(True) == "—"
    assert _count(None) == 0
    assert _count(True) == 0


def test_result_cells_handles_a_missing_coverage_score() -> None:
    ticker = TickerAnalysis(
        ticker="AAPL",
        coverage={"coverage_score": None},
        execution_model={},
        evidence_methodology={},
        cycles=[],
        atr_settings_summary={},
        atr_settings_profiles=[],
        atr_settings_regimes=[],
        atr_settings_history=[],
        capture_inventory=[],
        replay_observations=[],
        candidate_summaries=[],
        suggested_settings=[],
        primary_evaluation_setting={},
        issues=[],
        limitations=[],
    )
    assert result_cells(ticker)[2].text == "—"


def test_windows_pyright_uses_the_created_virtual_environment() -> None:
    script = (ROOT / "scripts/run_tests.ps1").read_text(encoding="utf-8-sig")
    posix_script = (ROOT / "scripts/run_tests.sh").read_text(encoding="utf-8-sig")
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8-sig")
    assert '& $python -m pyright --pythonpath $python' in script
    assert '"$PYTHON_BIN" -m pyright --pythonpath "$PYTHON_BIN"' in posix_script
    assert 'venvPath = "."' in config
    assert 'venv = ".venv"' in config
