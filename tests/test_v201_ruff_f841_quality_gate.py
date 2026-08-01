"""Regression tests for the v2.0.1 Ruff F841 correction."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _assigned_names(function: ast.FunctionDef) -> set[str]:
    return {
        target.id
        for node in ast.walk(function)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }


def test_top_level_analysis_does_not_recreate_unused_selected_windows() -> None:
    tree = ast.parse(
        (ROOT / "optimizer/market_replay.py").read_text(encoding="utf-8")
    )
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }

    top_level = functions["run_market_replay_analysis"]
    assigned = _assigned_names(top_level)
    assert "selected_windows" not in assigned
    assert "selected_window_set" not in assigned

    bounded = functions["_run_bounded_selector"]
    bounded_assigned = _assigned_names(bounded)
    assert {"selected_windows", "selected_window_set"} <= bounded_assigned
