from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_reported_ruff_diagnostics_remain_corrected() -> None:
    atr_source = (ROOT / "optimizer/atr.py").read_text(encoding="utf-8")
    assert "from .utils import finite_float, median\n\n# These are" in atr_source
    assert "from .utils import finite_float, median\n\n\n# These are" not in atr_source

    ibrec_source = (ROOT / "optimizer/ibrec.py").read_text(encoding="utf-8")
    ibrec_tree = ast.parse(ibrec_source)
    integrity_loops = [
        node
        for node in ast.walk(ibrec_tree)
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Tuple)
        and node.target.elts
        and isinstance(node.target.elts[0], ast.Name)
        and node.target.elts[0].id in {"tick_count", "_tick_count"}
    ]
    assert len(integrity_loops) == 1
    assert isinstance(integrity_loops[0].target.elts[0], ast.Name)
    assert integrity_loops[0].target.elts[0].id == "_tick_count"
    assert "if _tick_count != int(manifest[\"row_count\"]):" in ibrec_source

    market_source = (ROOT / "optimizer/market_replay.py").read_text(encoding="utf-8")
    market_tree = ast.parse(market_source)
    imported_names = {
        alias.asname or alias.name.split(".")[-1]
        for node in market_tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert {"asdict", "IbrecError", "mean", "median"}.isdisjoint(imported_names)
    assert "Path" in imported_names
    assert "control = summaries[control_profile.key()]" not in market_source

    safety_source = (ROOT / "tests/test_safety.py").read_text(encoding="utf-8")
    assert (
        "    import ctypes\n"
        "    from ctypes import wintypes\n\n"
        "    import optimizer.safety as safety\n"
    ) in safety_source

    audit_source = (ROOT / "tests/test_v120_architecture_audit.py").read_text(encoding="utf-8")
    assert audit_source.index("from optimizer.replay import summarize_observations") < audit_source.index(
        "from optimizer.reports import write_reports"
    )


def test_quality_gates_run_source_checks_before_coverage() -> None:
    powershell = (ROOT / "scripts/run_tests.ps1").read_text(encoding="utf-8-sig")
    shell = (ROOT / "scripts/run_tests.sh").read_text(encoding="utf-8-sig")

    for script in (powershell, shell):
        compile_index = script.index("-m compileall")
        ruff_index = script.index("-m ruff check")
        pyright_index = script.index("-m pyright")
        coverage_index = script.index("-m coverage run")
        assert compile_index < ruff_index < pyright_index < coverage_index
