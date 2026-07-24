"""Historical ATR-setting provenance and regime reconstruction.

BouncyBot stores an ATR snapshot on every cycle row.  This module keeps those
snapshots distinct from the current ``app_settings.strategy`` record so reports
never imply that one current configuration was used for all historical trades.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from .database import safe_float, safe_int
from .utils import median, timestamp_seconds

ATR_SETTING_DEFAULTS: dict[str, Any] = {
    "atr_adaptive_enabled": True,
    "atr_adapt_minimum_profit_enabled": True,
    "atr_block_new_buy_until_ready": False,
    "atr_adapt_protective_sell_enabled": False,
    "atr_period": 14,
    "atr_bar_seconds": 60,
    "atr_initial_drop_multiplier": 1.5,
    "atr_buy_rebound_multiplier": 0.75,
    "atr_minimum_profit_multiplier": 1.0,
    "atr_sell_trail_multiplier": 1.0,
    "atr_protective_sell_multiplier": 3.0,
    "atr_min_pct": 0.1,
    "atr_max_pct": 20.0,
}

ATR_SETTING_FIELDS = tuple(ATR_SETTING_DEFAULTS)
HISTORICAL_MEDIAN_LABEL = (
    "Historical median baseline (derived from actual stored cycle ATR settings)"
)
ATR_BOOL_FIELDS = frozenset(
    {
        "atr_adaptive_enabled",
        "atr_adapt_minimum_profit_enabled",
        "atr_block_new_buy_until_ready",
        "atr_adapt_protective_sell_enabled",
    }
)
ATR_INT_FIELDS = frozenset({"atr_period", "atr_bar_seconds"})

# Fields that directly define the four adaptive strategy percentages and their
# ATR window.  Protective and readiness controls are reported too, but they do
# not change the BUY/normal-SELL counterfactual replay implemented here.
ATR_REPLAY_PROFILE_FIELDS = (
    "atr_adaptive_enabled",
    "atr_adapt_minimum_profit_enabled",
    "atr_period",
    "atr_bar_seconds",
    "atr_initial_drop_multiplier",
    "atr_buy_rebound_multiplier",
    "atr_minimum_profit_multiplier",
    "atr_sell_trail_multiplier",
    "atr_min_pct",
    "atr_max_pct",
)


def normalized_bool(value: Any) -> bool | None:
    """Parse a stored SQLite/JSON boolean without inventing a default."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        number = safe_float(value)
        if number == 1.0:
            return True
        if number == 0.0:
            return False
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return None


def normalized_setting(record: dict[str, Any], name: str) -> Any:
    """Return one actual stored value, or ``None`` when unavailable/invalid."""
    if name not in record or record.get(name) is None:
        return None
    if name in ATR_BOOL_FIELDS:
        return normalized_bool(record.get(name))
    if name in ATR_INT_FIELDS:
        value = safe_int(record.get(name))
        return value if value is not None and value > 0 else None
    value = safe_float(record.get(name))
    return value if value is not None else None


def atr_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    """Extract only ATR-related settings while preserving missing values."""
    return {name: normalized_setting(record, name) for name in ATR_SETTING_FIELDS}


def profile_id(snapshot: dict[str, Any]) -> str:
    """Return a stable identifier for an exact ATR snapshot.

    The identifier is content-derived, so it does not depend on cycle ordering,
    filesystem paths, or the time at which analysis runs.
    """
    canonical = json.dumps(
        {name: snapshot.get(name) for name in ATR_SETTING_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"ATR-{hashlib.sha256(canonical).hexdigest()[:10].upper()}"


def cycle_sort_key(cycle: dict[str, Any]) -> tuple[Any, ...]:
    created = timestamp_seconds(cycle.get("created_at"))
    updated = timestamp_seconds(cycle.get("updated_at"))
    timestamp = created if created is not None else updated
    cycle_number = safe_int(cycle.get("cycle_number"))
    return (
        timestamp is None,
        timestamp if timestamp is not None else 0.0,
        cycle_number is None,
        cycle_number if cycle_number is not None else 0,
        str(cycle.get("id") or ""),
    )


def historical_median_snapshot(
    cycles: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, int], list[str]]:
    """Summarize actual cycle snapshots field by field.

    Numeric fields use a median, boolean fields use a deterministic majority
    vote (ties resolve to ``True``), and a documented BouncyBot default is used
    only when no cycle contains that field.  The result can therefore combine
    values that never occurred together on one cycle; reports state that caveat.
    """
    snapshot: dict[str, Any] = {}
    observed_counts: dict[str, int] = {}
    fallback_fields: list[str] = []
    for name, default in ATR_SETTING_DEFAULTS.items():
        values = [normalized_setting(cycle, name) for cycle in cycles]
        values = [value for value in values if value is not None]
        observed_counts[name] = len(values)
        if not values:
            snapshot[name] = default
            fallback_fields.append(name)
        elif name in ATR_BOOL_FIELDS:
            snapshot[name] = sum(1 for value in values if value) >= len(values) / 2.0
        elif name in ATR_INT_FIELDS:
            value = median(float(item) for item in values)
            snapshot[name] = max(1, int(round(value if value is not None else int(default))))
        else:
            value = median(float(item) for item in values)
            snapshot[name] = float(value if value is not None else default)
    return snapshot, observed_counts, fallback_fields


def _evaluation_control(
    *,
    history: list[dict[str, Any]],
    current_snapshot: dict[str, Any],
    median_snapshot: dict[str, Any],
    median_matches_complete_profile: bool,
) -> dict[str, Any]:
    """Choose one production-like control from the best available provenance.

    A field-wise median is useful as a descriptive summary but can combine
    settings that never ran together. Candidate generation therefore starts
    from an exact replay-relevant profile whenever the saved data permits it:
    current applicable settings first, otherwise the latest complete cycle
    snapshot, and only then the historical median/default summary.
    """

    current_replay_complete = all(
        current_snapshot.get(name) is not None
        for name in ATR_REPLAY_PROFILE_FIELDS
    )
    latest_complete = next(
        (
            row
            for row in reversed(history)
            if all(row.get(name) is not None for name in ATR_REPLAY_PROFILE_FIELDS)
        ),
        None,
    )

    if current_replay_complete:
        raw = current_snapshot
        source = "app_settings.strategy"
        label = "Evaluation control: current saved app settings"
        source_cycle_id = ""
        source_profile_id = ""
        exact_for_replay = True
    elif latest_complete is not None:
        raw = latest_complete
        source = "latest complete historical cycle ATR snapshot"
        source_cycle_id = str(latest_complete.get("cycle_id") or "")
        source_profile_id = str(latest_complete.get("profile_id") or "")
        cycle_label = latest_complete.get("cycle_number")
        label = (
            "Evaluation control: latest complete historical ATR snapshot"
            + (f" (cycle {cycle_label})" if cycle_label is not None else "")
        )
        exact_for_replay = True
    else:
        raw = median_snapshot
        source = "historical median/default summary"
        label = "Evaluation control: historical median/default summary"
        source_cycle_id = ""
        source_profile_id = ""
        exact_for_replay = bool(median_matches_complete_profile)

    missing_before_fallback = [
        name for name in ATR_SETTING_FIELDS if raw.get(name) is None
    ]
    complete = {
        name: (
            raw.get(name)
            if raw.get(name) is not None
            else median_snapshot.get(name, ATR_SETTING_DEFAULTS[name])
        )
        for name in ATR_SETTING_FIELDS
    }
    exact_complete_profile = not missing_before_fallback and (
        source != "historical median/default summary"
        or median_matches_complete_profile
    )
    return {
        "evaluation_control_label": label,
        "evaluation_control_source": source,
        "evaluation_control_settings": complete,
        "evaluation_control_missing_fields_before_fallback": missing_before_fallback,
        "evaluation_control_exact_for_replay": exact_for_replay,
        "evaluation_control_exact_complete_profile": exact_complete_profile,
        "evaluation_control_source_cycle_id": source_cycle_id,
        "evaluation_control_source_profile_id": source_profile_id,
    }


def _current_strategy_for_ticker(
    settings: dict[str, Any],
    settings_updated_at: dict[str, str],
    ticker: str,
    *,
    ticker_count: int,
) -> tuple[dict[str, Any] | None, str, str]:
    raw = settings.get("strategy")
    updated_at = settings_updated_at.get("strategy", "")
    if not isinstance(raw, dict):
        return None, updated_at, "No structured app_settings.strategy record was found."
    configured_ticker = str(raw.get("ticker") or "").strip().upper()
    if configured_ticker and configured_ticker != ticker:
        return None, updated_at, f"Current app settings apply to {configured_ticker}, not {ticker}."
    if not configured_ticker and ticker_count != 1:
        return (
            None,
            updated_at,
            "Current app settings do not identify a ticker and the database contains multiple tickers.",
        )
    return dict(raw), updated_at, "Current app_settings.strategy applies to this ticker."


def build_settings_audit(
    cycles: list[dict[str, Any]],
    *,
    settings: dict[str, Any],
    settings_updated_at: dict[str, str],
    ticker: str,
    ticker_count: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return summary, exact profiles, contiguous regimes, and cycle history."""
    ordered = sorted(cycles, key=cycle_sort_key)
    history: list[dict[str, Any]] = []
    profile_builders: dict[str, dict[str, Any]] = {}
    complete_keys: set[tuple[Any, ...]] = set()

    previous_profile_id = ""
    configuration_changes = 0
    for cycle in ordered:
        snapshot = atr_snapshot(cycle)
        present = [name for name, value in snapshot.items() if value is not None]
        missing = [name for name, value in snapshot.items() if value is None]
        current_profile_id = profile_id(snapshot) if present else "UNKNOWN"
        if (
            previous_profile_id
            and current_profile_id != "UNKNOWN"
            and current_profile_id != previous_profile_id
        ):
            configuration_changes += 1
        if current_profile_id != "UNKNOWN":
            previous_profile_id = current_profile_id

        row = {
            "profile_id": current_profile_id,
            "cycle_id": str(cycle.get("id") or ""),
            "cycle_number": safe_int(cycle.get("cycle_number")),
            "created_at": str(cycle.get("created_at") or ""),
            "updated_at": str(cycle.get("updated_at") or ""),
            "stage": str(cycle.get("stage") or ""),
            "missing_atr_fields": missing,
            **snapshot,
        }
        history.append(row)
        if current_profile_id == "UNKNOWN":
            continue

        profile = profile_builders.get(current_profile_id)
        if profile is None:
            profile = {
                "profile_id": current_profile_id,
                "cycle_count": 0,
                "first_cycle_number": row["cycle_number"],
                "last_cycle_number": row["cycle_number"],
                "first_created_at": row["created_at"],
                "last_created_at": row["created_at"],
                "cycles_with_missing_fields": 0,
                **snapshot,
            }
            profile_builders[current_profile_id] = profile
        profile["cycle_count"] += 1
        profile["last_cycle_number"] = row["cycle_number"]
        profile["last_created_at"] = row["created_at"]
        if missing:
            profile["cycles_with_missing_fields"] += 1
        if not missing:
            complete_keys.add(tuple(snapshot.get(name) for name in ATR_SETTING_FIELDS))

    # Contiguous regimes preserve A -> B -> A as three regimes, instead of
    # collapsing both A periods into one row and hiding that settings changed.
    regimes: list[dict[str, Any]] = []
    for row in history:
        current_profile_id = str(row["profile_id"])
        if not regimes or regimes[-1]["profile_id"] != current_profile_id:
            regimes.append(
                {
                    "regime_number": len(regimes) + 1,
                    "profile_id": current_profile_id,
                    "cycle_count": 1,
                    "first_cycle_number": row["cycle_number"],
                    "last_cycle_number": row["cycle_number"],
                    "first_created_at": row["created_at"],
                    "last_created_at": row["created_at"],
                }
            )
        else:
            regimes[-1]["cycle_count"] += 1
            regimes[-1]["last_cycle_number"] = row["cycle_number"]
            regimes[-1]["last_created_at"] = row["created_at"]

    median_snapshot, observed_counts, fallback_fields = historical_median_snapshot(cycles)
    median_key = tuple(median_snapshot.get(name) for name in ATR_SETTING_FIELDS)
    current_strategy, current_updated_at, current_note = _current_strategy_for_ticker(
        settings,
        settings_updated_at,
        ticker,
        ticker_count=ticker_count,
    )
    current_snapshot = atr_snapshot(current_strategy or {})
    current_present = [name for name, value in current_snapshot.items() if value is not None]
    current_missing = [name for name, value in current_snapshot.items() if value is None]
    profile_counts = Counter(str(row["profile_id"]) for row in history)
    known_profile_counts = {key: value for key, value in sorted(profile_counts.items()) if key != "UNKNOWN"}
    control = _evaluation_control(
        history=history,
        current_snapshot=current_snapshot,
        median_snapshot=median_snapshot,
        median_matches_complete_profile=median_key in complete_keys,
    )

    summary = {
        "cycle_rows": len(cycles),
        "cycles_with_any_stored_atr_settings": sum(
            1 for row in history if row["profile_id"] != "UNKNOWN"
        ),
        "cycles_with_complete_stored_atr_settings": sum(
            1 for row in history if not row["missing_atr_fields"]
        ),
        "cycles_without_any_stored_atr_settings": sum(
            1 for row in history if row["profile_id"] == "UNKNOWN"
        ),
        "distinct_atr_profiles": len(profile_builders),
        "contiguous_setting_regimes": len(regimes),
        "configuration_change_count": configuration_changes,
        "settings_varied_between_cycles": len(profile_builders) > 1 or configuration_changes > 0,
        "profile_cycle_counts": known_profile_counts,
        "historical_median_label": HISTORICAL_MEDIAN_LABEL,
        "historical_median_settings": median_snapshot,
        "historical_median_observed_counts": observed_counts,
        "historical_median_default_fallback_fields": fallback_fields,
        "historical_median_matches_an_observed_complete_profile": median_key in complete_keys,
        "current_app_settings_available": bool(current_present),
        "current_app_settings_applicability": current_note,
        "current_app_settings_updated_at": current_updated_at,
        "current_app_settings": current_snapshot if current_present else {},
        "current_app_settings_missing_fields": current_missing if current_present else list(ATR_SETTING_FIELDS),
        "current_app_settings_complete_for_replay": all(
            current_snapshot.get(name) is not None for name in ATR_REPLAY_PROFILE_FIELDS
        ),
        **control,
    }
    profiles = sorted(
        profile_builders.values(),
        key=lambda row: (
            str(row.get("first_created_at") or ""),
            row.get("first_cycle_number") is None,
            row.get("first_cycle_number") or 0,
            str(row.get("profile_id") or ""),
        ),
    )
    return summary, profiles, regimes, history


def cycle_profile_lookup(history: list[dict[str, Any]]) -> dict[str, str]:
    """Map cycle IDs to stable historical ATR profile IDs."""
    return {
        str(row.get("cycle_id") or ""): str(row.get("profile_id") or "UNKNOWN")
        for row in history
        if str(row.get("cycle_id") or "")
    }
