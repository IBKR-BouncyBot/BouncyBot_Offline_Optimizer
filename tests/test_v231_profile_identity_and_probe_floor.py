"""Regression tests for v2.3.1 profile identity and outward-probe floors.

Three defects are covered:

* ``AtrProfile.key()`` renders every float field with two decimals, but the
  stored values could carry more precision.  Two distinct profiles could
  therefore share one nominal key, which silently shrank the searched grid in
  the reference engine and aborted the compact engine.
* Outward boundary probes could synthesize multiplier values below BouncyBot's
  GUI floor for the initial-drop and minimum-profit multipliers.
* ``_subset_candidate`` was dead code that reimplemented the cached-session
  leave-one-day-out approach the exact rework deliberately replaced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from optimizer import market_replay
from optimizer.market_replay import (
    MarketReplayAnalysisError,
    _boundary_extension_profiles,
    _coarse_profiles,
    _evaluate_profiles,
)
from optimizer.market_replay_models import (
    AtrProfile,
    MarketReplayCandidateSummary,
    MarketReplayConfig,
)


def _config(tmp_path: Path, **overrides: object) -> MarketReplayConfig:
    values: dict[str, object] = {
        "recording_path": tmp_path / "recording.ibrec",
        "output_root": tmp_path / "reports",
    }
    values.update(overrides)
    return MarketReplayConfig(**values)  # type: ignore[arg-type]


def _profile(**overrides: object) -> AtrProfile:
    values: dict[str, object] = {
        "period": 14,
        "bar_seconds": 60,
        "initial_drop_multiplier": 1.50,
        "buy_rebound_multiplier": 0.75,
        "minimum_profit_multiplier": 1.00,
        "sell_trail_multiplier": 1.00,
        "min_atr_pct": 0.10,
        "max_atr_pct": 20.00,
    }
    values.update(overrides)
    return AtrProfile(**values)  # type: ignore[arg-type]


def _candidate(profile: AtrProfile, *, score: float) -> MarketReplayCandidateSummary:
    return MarketReplayCandidateSummary(
        profile=profile,
        score=score,
        sessions=5,
        completed_sessions=5,
        sessions_with_trades=5,
        completed_trades=5,
        open_position_sessions=0,
        no_trade_sessions=0,
        median_return_bps=1.0,
        mean_return_bps=1.0,
        worst_return_bps=1.0,
        maximum_drawdown_bps=1.0,
        open_position_rate_pct=0.0,
        no_trade_rate_pct=0.0,
    )


def test_profile_key_is_lossless_for_every_rendered_float() -> None:
    """Stored floats must match exactly what ``key()`` renders."""

    profile = AtrProfile(
        period=14,
        bar_seconds=60,
        initial_drop_multiplier=1.499,
        buy_rebound_multiplier=0.751,
        minimum_profit_multiplier=1.004,
        sell_trail_multiplier=0.996,
        min_atr_pct=0.105,
        max_atr_pct=19.999,
        protective_sell_mode="atr",
        protective_sell_value=3.001,
    )
    assert profile.initial_drop_multiplier == 1.50
    assert profile.buy_rebound_multiplier == 0.75
    assert profile.minimum_profit_multiplier == 1.00
    assert profile.sell_trail_multiplier == 1.00
    assert profile.min_atr_pct == 0.10
    assert profile.max_atr_pct == 20.00
    assert profile.protective_sell_value == 3.00
    assert profile.key() == (
        "p14-b60-d1.50-buy0.75-profit1.00-sell1.00-min0.10-max20.00-protective-atr3.00"
    )


def test_distinct_profiles_never_share_a_nominal_key() -> None:
    """Two clamp values that render alike must collapse to one profile."""

    left = _profile(min_atr_pct=0.105)
    right = _profile(min_atr_pct=0.10)
    assert left.key() == right.key()
    assert left == right


def test_profile_key_canonicalizes_negative_zero() -> None:
    """Equal zero values must not acquire different rendered identities."""

    negative = _profile(buy_rebound_multiplier=-0.004)
    positive = _profile(buy_rebound_multiplier=0.0)
    assert negative.buy_rebound_multiplier == 0.0
    assert negative.key() == positive.key()
    assert "buy-0.00" not in negative.key()


@pytest.mark.parametrize(
    "overrides",
    [
        {"initial_drop_multiplier": 0.0},
        {"minimum_profit_multiplier": 0.0},
        {"buy_rebound_multiplier": -0.01},
        {"sell_trail_multiplier": -0.01},
        {"min_atr_pct": 0.0},
        {"min_atr_pct": 0.104, "max_atr_pct": 0.102},
        {"max_atr_pct": 100.0},
    ],
)
def test_profile_rejects_values_outside_the_gui_contract(
    overrides: dict[str, float],
) -> None:
    """A directly constructed candidate must remain reproducible in BouncyBot."""

    with pytest.raises(ValueError):
        _profile(**overrides)


def test_coarse_grid_has_no_key_collision_for_a_four_decimal_clamp(
    tmp_path: Path,
) -> None:
    """The configured clamp can no longer duplicate a coarse clamp candidate."""

    config = _config(tmp_path, min_atr_pct=0.105, max_atr_pct=20.0).normalized()
    assert config.min_atr_pct == 0.10
    profiles = _coarse_profiles(config, [(14, 60)], search_clamps=True)
    keys = [profile.key() for profile in profiles]
    assert len(keys) == len(set(keys))


def test_clamp_bounds_are_validated_on_the_stored_two_decimal_values(
    tmp_path: Path,
) -> None:
    """A pair that collapses onto one value once rounded must be rejected.

    ``0.102`` and ``0.104`` are distinct raw values and were accepted before
    v2.3.1, but both store as ``0.10``, leaving a maximum that is not above the
    minimum.  A pair that still separates once rounded stays valid.
    """

    with pytest.raises(ValueError):
        _config(tmp_path, min_atr_pct=0.102, max_atr_pct=0.104).normalized()
    separated = _config(tmp_path, min_atr_pct=0.104, max_atr_pct=0.109).normalized()
    assert (separated.min_atr_pct, separated.max_atr_pct) == (0.10, 0.11)


def test_reference_engine_fails_closed_on_a_duplicate_profile_key(
    tmp_path: Path,
) -> None:
    """The serial path must refuse a colliding grid instead of overwriting."""

    class _Collides(AtrProfile):
        def key(self) -> str:  # type: ignore[override]
            return "duplicate-key"

    config = _config(tmp_path).normalized()
    profiles = [
        _Collides(
            period=14,
            bar_seconds=60,
            initial_drop_multiplier=1.50,
            buy_rebound_multiplier=0.75,
            minimum_profit_multiplier=1.00,
            sell_trail_multiplier=multiplier,
            min_atr_pct=0.10,
            max_atr_pct=20.00,
        )
        for multiplier in (1.00, 1.25)
    ]
    with pytest.raises(MarketReplayAnalysisError, match="same nominal profile key"):
        _evaluate_profiles(
            None,  # type: ignore[arg-type]
            [],
            {},
            profiles,
            {},
            {},
            config,
            progress=None,
            message="regression",
        )


def test_reference_engine_rejects_collision_with_an_existing_summary(
    tmp_path: Path,
) -> None:
    """A later batch cannot reuse a populated key for a different profile."""

    class _Collides(AtrProfile):
        def key(self) -> str:  # type: ignore[override]
            return "duplicate-key"

    left = _Collides(
        period=14,
        bar_seconds=60,
        initial_drop_multiplier=1.50,
        buy_rebound_multiplier=0.75,
        minimum_profit_multiplier=1.00,
        sell_trail_multiplier=1.00,
        min_atr_pct=0.10,
        max_atr_pct=20.00,
    )
    right = _Collides(
        period=14,
        bar_seconds=60,
        initial_drop_multiplier=1.50,
        buy_rebound_multiplier=0.75,
        minimum_profit_multiplier=1.00,
        sell_trail_multiplier=1.25,
        min_atr_pct=0.10,
        max_atr_pct=20.00,
    )
    summaries = {left.key(): _candidate(left, score=1.0)}
    with pytest.raises(MarketReplayAnalysisError, match="existing Market Replay summary"):
        _evaluate_profiles(
            None,  # type: ignore[arg-type]
            [],
            {},
            [right],
            summaries,
            {},
            _config(tmp_path).normalized(),
            progress=None,
            message="regression",
        )


@pytest.mark.parametrize(
    ("dimension", "expected_floor"),
    [
        ("initial_drop_multiplier", 0.01),
        ("minimum_profit_multiplier", 0.01),
        ("buy_rebound_multiplier", 0.0),
        ("sell_trail_multiplier", 0.0),
    ],
)
def test_outward_probes_respect_the_gui_multiplier_floor(
    dimension: str,
    expected_floor: float,
) -> None:
    """Only the BUY/SELL trail multipliers may be probed down to zero."""

    center_profile = _profile(**{dimension: 0.25})
    center = _candidate(center_profile, score=20.0)
    center.clamp_min_rate_pct = 0.0
    candidates = [
        center,
        _candidate(_profile(**{dimension: 0.50}), score=20.0),
    ]
    _probes, rows = _boundary_extension_profiles(center, candidates)
    lower = [
        row
        for row in rows
        if row["dimension"] == dimension and row["direction"] == "lower"
    ]
    assert lower, f"expected a lower probe for {dimension}"
    assert float(lower[0]["probe_value"]) == pytest.approx(expected_floor)


def test_outward_probe_values_stay_two_decimal() -> None:
    """A probe value must be expressible by the profile key that names it."""

    center_profile = _profile(sell_trail_multiplier=1.25)
    center = _candidate(center_profile, score=20.0)
    center.clamp_min_rate_pct = 0.0
    candidates = [
        _candidate(_profile(sell_trail_multiplier=value), score=20.0)
        for value in (0.75, 1.00, 1.25)
    ]
    probes, rows = _boundary_extension_profiles(center, candidates)
    for row in rows:
        value = row["probe_value"]
        if isinstance(value, float):
            assert value == round(value, 2)
    assert any(profile.sell_trail_multiplier == 1.50 for profile in probes)


def test_clamp_probe_evidence_uses_the_normalized_replayed_value() -> None:
    """Clamp evidence must not retain a higher-precision pre-normalization input."""

    center_profile = _profile(min_atr_pct=0.05)
    center = _candidate(center_profile, score=20.0)
    center.clamp_min_rate_pct = 100.0
    probes, rows = _boundary_extension_profiles(center, [center])
    lower = next(
        row
        for row in rows
        if row["dimension"] == "min_atr_pct" and row["direction"] == "lower"
    )
    assert lower["probe_value"] == 0.03
    assert any(profile.min_atr_pct == 0.03 for profile in probes)
    assert "min0.03" in str(lower["probe_profile_key"])


def test_dead_leave_one_day_out_subset_helper_is_removed() -> None:
    """The cached-session LOO helper must not reappear beside the exact rework."""

    assert not hasattr(market_replay, "_subset_candidate")
