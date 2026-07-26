from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        alias.asname or alias.name.split(".")[-1]
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }


def test_v180_reported_ruff_conditions_remain_corrected() -> None:
    gui_source = (ROOT / "optimizer/gui.py").read_text(encoding="utf-8")
    assert gui_source.index("    QDoubleSpinBox,\n") < gui_source.index("    QFileDialog,\n")

    market_path = ROOT / "optimizer/market_replay.py"
    market_source = market_path.read_text(encoding="utf-8")
    market_tree = ast.parse(market_source)
    assert "MarketReplaySessionQuality" not in _imported_names(market_path)
    leave_one_out = next(
        node
        for node in market_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_leave_one_day_out_selection_rows"
    )
    assigned_names = {
        target.id
        for node in ast.walk(leave_one_out)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "selected_window_set" not in assigned_names

    zip_source = (ROOT / "scripts/create_reproducible_zip.py").read_text(encoding="utf-8")
    assert (
        "from pathlib import Path, PurePosixPath\n\n"
        '_EXECUTABLE_BINARY_SUFFIXES = {".com", ".exe"}'
    ) in zip_source
    assert "from pathlib import Path, PurePosixPath\n\n\n_EXECUTABLE" not in zip_source

    test_source = (
        ROOT / "tests/test_v180_continuous_replay_and_calibration.py"
    ).read_text(encoding="utf-8")
    assert test_source.index("    _paired_bootstrap_units,\n") < test_source.index(
        "    _ReplayCarryState,\n"
    )


def test_v181_uses_the_reported_ruff_version_for_repeatable_validation() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    release_lock = (ROOT / "requirements-release-win64.lock").read_text(encoding="utf-8")
    assert "ruff==0.16.0" in requirements.splitlines()
    assert "ruff==0.16.0" in release_lock.splitlines()
