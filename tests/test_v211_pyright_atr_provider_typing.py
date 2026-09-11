"""Regression tests for the v2.1.1 ATR-provider Pyright correction."""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np

from optimizer.market_replay import _AtrValueSeries

ROOT = Path(__file__).resolve().parents[1]
MARKET_REPLAY_SOURCE = ROOT / "optimizer/market_replay.py"


def _annotation(source: str, node: ast.arg) -> str:
    assert node.annotation is not None
    return ast.get_source_segment(source, node.annotation) or ""


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_atr_value_protocol_supports_reference_and_compact_vectors() -> None:
    """The core uses only length and integer indexing for either engine."""

    def first(values: _AtrValueSeries) -> tuple[int, float | None]:
        return len(values), values[0]

    assert first([1.25, None]) == (2, 1.25)
    compact = np.asarray([1.25, 2.50], dtype=np.float64)
    length, value = first(compact)
    assert length == 2
    assert float(value) == 1.25


def test_core_atr_annotations_use_the_minimal_read_only_protocol() -> None:
    source = MARKET_REPLAY_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    for function_name in ("_simulate_session_stateful", "_simulate_session"):
        function = _function(tree, function_name)
        argument = next(argument for argument in function.args.args if argument.arg == "atr_values")
        assert _annotation(source, argument) == "_AtrValueSeries"

    for function_name in ("_evaluate_period_sequence_core", "_evaluate_period_sequence"):
        function = _function(tree, function_name)
        argument = next(argument for argument in function.args.args if argument.arg == "atr_provider")
        annotation = _annotation(source, argument)
        assert "Callable" in annotation
        assert "_AtrValueSeries" in annotation
        assert "Sequence[float | None]" not in annotation


def test_reference_provider_callbacks_accept_the_core_sequence_contract() -> None:
    """Callbacks must not narrow Sequence[Any] to list[IbrecTick]."""

    source = MARKET_REPLAY_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    for parent_name in ("_evaluate_profile", "_evaluate_profile_phase"):
        parent = _function(tree, parent_name)
        provider = next(
            node
            for node in parent.body
            if isinstance(node, ast.FunctionDef) and node.name == "atr_provider"
        )
        assert len(provider.args.args) >= 2
        assert _annotation(source, provider.args.args[1]) == "Sequence[Any]"
