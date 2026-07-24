from __future__ import annotations

import ast
from pathlib import Path

from scripts.create_source_manifest import _excluded

ROOT = Path(__file__).resolve().parents[1]


def test_source_manifest_suffix_check_uses_direct_boolean_return() -> None:
    source = (ROOT / "scripts/create_source_manifest.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_excluded"
    )
    final_statement = function.body[-1]
    assert isinstance(final_statement, ast.Return)
    comparison = final_statement.value
    assert isinstance(comparison, ast.Compare)
    assert len(comparison.ops) == 1
    assert isinstance(comparison.ops[0], ast.In)


def test_source_manifest_suffix_exclusions_are_behaviorally_unchanged() -> None:
    for name in (
        "bot_state.sqlite",
        "bot_state.sqlite-wal",
        "recording.ibrec",
        "recording.ibrec-shm",
        "certificate.crt",
        "signing.key",
        "analysis.log",
        "module.pyc",
    ):
        assert _excluded(Path(name)) is True

    for name in ("module.py", "README.md", "settings.toml", "build.ps1"):
        assert _excluded(Path(name)) is False
