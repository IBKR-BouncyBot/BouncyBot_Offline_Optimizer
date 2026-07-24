from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from optimizer.analysis import run_analysis
from optimizer.models import AnalysisConfig
from optimizer.reports import _write_csv, _write_json, write_reports


def test_reports_escape_untrusted_ticker_and_issue_text(source_fixture: Path, tmp_path: Path) -> None:
    result = run_analysis(AnalysisConfig(source_dir=source_fixture, output_root=tmp_path / "reports"))
    result.tickers[0].issues.append("<script>alert('x')</script>")
    result.global_issues.append("<b>unsafe</b>")
    write_reports(result)
    ticker_html = (result.output_dir / "AAPL" / "AAPL_coverage_and_replay.html").read_text(encoding="utf-8")
    index_html = (result.output_dir / "index.html").read_text(encoding="utf-8")
    assert "<script>alert" not in ticker_html
    assert "&lt;script&gt;" in ticker_html
    assert "<b>unsafe</b>" not in index_html
    assert "&lt;b&gt;unsafe&lt;/b&gt;" in index_html


def test_report_with_no_stored_atr_snapshot_does_not_claim_one_profile(
    source_fixture: Path,
    tmp_path: Path,
) -> None:
    atr_columns = (
        "atr_adaptive_enabled",
        "atr_adapt_minimum_profit_enabled",
        "atr_adapt_protective_sell_enabled",
        "atr_period",
        "atr_bar_seconds",
        "atr_initial_drop_multiplier",
        "atr_buy_rebound_multiplier",
        "atr_minimum_profit_multiplier",
        "atr_sell_trail_multiplier",
        "atr_protective_sell_multiplier",
        "atr_min_pct",
        "atr_max_pct",
    )
    assignment = ", ".join(f"{column}=NULL" for column in atr_columns)
    with closing(sqlite3.connect(source_fixture / "bot_state.sqlite")) as connection:
        connection.execute(f"UPDATE cycles SET {assignment}")
        connection.commit()

    result = write_reports(
        run_analysis(AnalysisConfig(source_fixture, tmp_path / "reports"))
    )
    report = (
        result.output_dir / "AAPL" / "AAPL_coverage_and_replay.html"
    ).read_text(encoding="utf-8")

    assert "No cycle row contained a usable ATR settings snapshot" in report
    assert "fallback defaults" in report
    assert "All cycle rows with ATR data resolve to one exact historical profile" not in report


def test_checksum_manifest_covers_report_files(source_fixture: Path, tmp_path: Path) -> None:
    result = write_reports(run_analysis(AnalysisConfig(source_dir=source_fixture, output_root=tmp_path / "reports")))
    lines = (result.output_dir / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines()
    assert any(line.endswith("  index.html") for line in lines)
    assert any("AAPL/AAPL_coverage_and_replay.json" in line for line in lines)
    assert not any(line.endswith("SHA256SUMS.txt") for line in lines)

    manifest = json.loads(
        (result.output_dir / "analysis_manifest.json").read_text(encoding="utf-8")
    )
    written = {Path(path).name for path in manifest["files_written"]}
    assert "analysis_manifest.json" in written
    assert "SHA256SUMS.txt" in written


def test_report_publish_is_atomic_and_cleans_partial_directory(
    source_fixture: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    import optimizer.reports as reports_module

    result = run_analysis(
        AnalysisConfig(source_dir=source_fixture, output_root=tmp_path / "reports")
    )

    def fail_write(path, value) -> None:
        raise RuntimeError("forced report failure")

    monkeypatch.setattr(reports_module, "_write_json", fail_write)
    with pytest.raises(RuntimeError, match="forced report failure"):
        reports_module.write_reports(result)
    assert not result.output_dir.exists()
    partials = list(
        result.output_dir.parent.glob(f".{result.output_dir.name}.partial-*")
    )
    assert partials == []


def test_csv_writer_neutralizes_spreadsheet_formulas(tmp_path: Path) -> None:
    output = tmp_path / "safe.csv"
    _write_csv(
        output,
        [
            {
                "equals": '=HYPERLINK("https://invalid.example")',
                "plus": "+1+1",
                "minus": "-2+3",
                "at": "@SUM(A1:A2)",
                "normal": "AAPL",
            }
        ],
    )
    text = output.read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in text
    assert "'+1+1" in text
    assert "'-2+3" in text
    assert "'@SUM" in text
    assert "AAPL" in text


def test_ticker_report_explains_settings_provenance_algorithm_and_score(
    source_fixture: Path,
    tmp_path: Path,
) -> None:
    result = write_reports(
        run_analysis(AnalysisConfig(source_fixture, tmp_path / "reports"))
    )
    html_text = (
        result.output_dir / "AAPL" / "AAPL_coverage_and_replay.html"
    ).read_text(encoding="utf-8")
    readme = (result.output_dir / "README_REPORT.txt").read_text(encoding="utf-8")

    assert "Historical median baseline (derived from actual stored cycle ATR settings)" in html_text
    assert "field-wise statistical summary" in html_text
    assert "How ATR is reconstructed" in html_text
    assert "How counterfactual replay works" in html_text
    assert "required activation price" in html_text.lower()
    assert "Screening score" in html_text
    assert "Explicit cycle IDs are authoritative" in html_text
    assert "ACTUAL SETTINGS" in readme
    assert "Screening score =" in readme
    assert "content-addressed" in readme


def test_reports_export_cycle_settings_profiles_and_regimes(
    source_fixture: Path,
    tmp_path: Path,
) -> None:
    result = write_reports(
        run_analysis(AnalysisConfig(source_fixture, tmp_path / "reports"))
    )
    folder = result.output_dir / "AAPL"
    assert (folder / "AAPL_historical_atr_profiles.csv").exists()
    assert (folder / "AAPL_atr_settings_regimes.csv").exists()
    assert (folder / "AAPL_atr_settings_by_cycle.csv").exists()


def test_json_writer_rejects_unsupported_nondeterministic_values(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        _write_json(tmp_path / "invalid.json", {"unsupported": object()})
