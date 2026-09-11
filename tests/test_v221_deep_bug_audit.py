"""Deep regression coverage for the exact v2.2 performance engine."""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest

from optimizer.market_replay_models import AtrProfile, MarketReplayCandidateSummary
from optimizer.market_replay_optimization import (
    EffectiveArrayCatalog,
    validate_nominal_profile_results,
)


def _profile() -> AtrProfile:
    return AtrProfile(14, 60, 1.5, 0.75, 1.0, 1.0)


def _summary(profile: AtrProfile) -> MarketReplayCandidateSummary:
    return MarketReplayCandidateSummary(
        profile=profile,
        score=0.0,
        sessions=0,
        completed_sessions=0,
        sessions_with_trades=0,
        completed_trades=0,
        open_position_sessions=0,
        no_trade_sessions=0,
        median_return_bps=0.0,
        mean_return_bps=0.0,
        worst_return_bps=0.0,
        maximum_drawdown_bps=0.0,
        open_position_rate_pct=0.0,
        no_trade_rate_pct=0.0,
    )


def test_nominal_result_guard_rejects_missing_and_reordered_profiles() -> None:
    left = _profile()
    right = AtrProfile(14, 60, 1.5, 1.0, 1.0, 1.0)
    correct = [
        (left.key(), _summary(left), []),
        (right.key(), _summary(right), []),
    ]
    validate_nominal_profile_results([left, right], correct)
    with pytest.raises(RuntimeError, match="incomplete result set"):
        validate_nominal_profile_results([left, right], correct[:1])
    with pytest.raises(RuntimeError, match="changed nominal profile order"):
        validate_nominal_profile_results([left, right], list(reversed(correct)))


def test_catalog_close_releases_loaded_memory_maps(tmp_path: Path) -> None:
    value_path = tmp_path / "values.npy"
    state_path = tmp_path / "states.npy"
    np.save(value_path, np.array([10, 20], dtype=np.int16), allow_pickle=False)
    np.save(state_path, np.array([1, 3], dtype=np.uint8), allow_pickle=False)
    catalog = EffectiveArrayCatalog(tmp_path)
    values = np.load(value_path, mmap_mode="r", allow_pickle=False)
    states = np.load(state_path, mmap_mode="r", allow_pickle=False)
    value_mmap = values._mmap
    state_mmap = states._mmap
    catalog._loaded[str(value_path)] = (values, states)
    catalog.close()
    assert value_mmap.closed
    assert state_mmap.closed


def test_equivalence_code_uses_no_process_randomized_hash() -> None:
    import optimizer.market_replay_optimization as optimization

    source = Path(optimization.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "hash"
    ]
    assert offenders == []


def test_shared_arrays_are_never_opened_writable() -> None:
    import optimizer.market_replay_optimization as optimization

    source = Path(optimization.__file__).read_text(encoding="utf-8")
    forbidden = (
        'mmap_mode="r+"',
        "mmap_mode='r+'",
        'mmap_mode="w+"',
        "mmap_mode='w+'",
    )
    # Creation uses open_memmap(mode="w+") for a new temporary file; loading
    # an existing shared array must remain read-only.
    loading_lines = [
        line for line in source.splitlines() if "np.load(" in line or "mmap_mode" in line
    ]
    assert not any(token in line for line in loading_lines for token in forbidden)
