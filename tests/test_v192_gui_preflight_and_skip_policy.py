from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _call_name(node: ast.Call) -> str:
    function = node.func
    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        return f"{function.value.id}.{function.attr}"
    return ""


def test_runtime_skips_are_limited_to_optional_platform_capabilities() -> None:
    skip_sites: list[tuple[str, str]] = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in {"pytest.skip", "pytest.importorskip"}:
                continue
            argument = node.args[0] if node.args else None
            text = ""
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                text = argument.value
            elif isinstance(argument, ast.JoinedStr):
                text = "".join(
                    value.value
                    for value in argument.values
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                )
            skip_sites.append((path.relative_to(ROOT).as_posix(), f"{name}:{text}"))

    assert sorted(skip_sites) == sorted([
        ("tests/test_gui_optional.py", "pytest.importorskip:PySide6"),
        (
            "tests/test_ibrec_v2_v3.py",
            "pytest.skip:symlinks are unavailable in this environment: ",
        ),
        ("tests/test_ibrec_v2_v3.py", "pytest.skip:symlinks unavailable"),
        (
            "tests/test_safety.py",
            "pytest.skip:symlinks are unavailable in this environment",
        ),
        (
            "tests/test_safety.py",
            "pytest.skip:symlinks are unavailable in this environment",
        ),
        (
            "tests/test_safety.py",
            "pytest.skip:symlinks are unavailable in this environment",
        ),
    ])


def test_gui_preflight_uses_human_readable_format_helper() -> None:
    source = (ROOT / "optimizer/gui.py").read_text(encoding="utf-8")
    assert "market_replay_format_label(details)" in source
    assert "format(s) {details['format_versions']}" not in source
