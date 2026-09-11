"""Regression tests for the v2.2.2 Ruff quality-gate corrections."""

from __future__ import annotations

import ast
from pathlib import Path

import optimizer.market_replay_optimization as optimization

ROOT = Path(__file__).resolve().parents[1]


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_market_replay_optimization_has_no_unused_replace_import() -> None:
    path = Path(optimization.__file__)
    assert "replace" not in _imported_names(path)


def test_v220_private_imports_precede_public_imports() -> None:
    path = ROOT / "tests" / "test_v220_exact_refinement_acceleration.py"
    source = path.read_text(encoding="utf-8")
    private_position = source.index("    _STATE_MAXIMUM,")
    public_position = source.index("    EffectiveArrayCatalog,")
    assert private_position < public_position


def test_v221_does_not_import_unused_atr_cache_key() -> None:
    path = ROOT / "tests" / "test_v221_deep_bug_audit.py"
    assert "_atr_cache_key" not in _imported_names(path)
