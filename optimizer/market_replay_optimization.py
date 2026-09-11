"""Exact refinement acceleration for Market Replay profile evaluation.

This module contains only calculation-preserving optimizations:

* exact profile-equivalence signatures after ATR multiplication, clamping, and
  two-decimal rounding;
* read-only memory-mapped effective-percentage and clamp-state arrays shared by
  serial and spawned evaluation workers;
* adaptive profile batching for large recordings; and
* memory-aware automatic worker selection.

Nominal profiles are always restored before ranking, stable-region analysis,
boundary extension, robustness validation, and reporting.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

import numpy as np
from numpy.typing import NDArray

from .market_replay_models import (
    AtrProfile,
    MarketReplayCandidateSummary,
    MarketReplaySessionResult,
)
from .utils import finite_float

_STATE_UNAVAILABLE = 0
_STATE_MINIMUM = 1
_STATE_MAXIMUM = 2
_STATE_RAW = 3
_STATE_ZERO = 4

_STATE_NAME = {
    _STATE_UNAVAILABLE: "unavailable",
    _STATE_MINIMUM: "min",
    _STATE_MAXIMUM: "max",
    _STATE_RAW: "raw",
    _STATE_ZERO: "zero",
}

_UNAVAILABLE_BASIS_POINTS = np.int16(-32_768)
_DEFAULT_AUTO_WORKER_CEILING = 16
_MIN_FREE_MEMORY_BYTES = 1 << 30
_MIN_ESTIMATED_WORKER_BYTES = 128 << 20
# A mostly unique ATR stream can otherwise grow one Python dictionary entry
# per retained row while preparing each effective behavior.  Capping this
# opportunistic cache changes only recomputation, never the emitted arrays.
_MAX_SCALAR_CACHE_ENTRIES = 131_072


class AtrValueSeries(Protocol):
    """Minimal read-only ATR vector accepted by both replay engines.

    Python lists and NumPy arrays do not share the nominal ``Sequence`` type
    in NumPy's type declarations, even though both provide the only two
    operations required by the replay engine: length and integer indexing.
    The broad ``object`` item type is intentional; values are narrowed and
    converted at the one consumption boundary below.
    """

    def __len__(self) -> int: ...

    def __getitem__(self, index: int, /) -> object: ...


def effective_percentage_state_scalar(
    atr_pct: float | None,
    multiplier: float,
    minimum: float,
    maximum: float,
    *,
    allow_zero: bool,
) -> tuple[float | None, int]:
    """Mirror BouncyBot's effective-percentage calculation exactly."""

    if allow_zero and multiplier <= 0:
        return 0.0, _STATE_ZERO
    if atr_pct is None or not math.isfinite(atr_pct) or atr_pct <= 0:
        return None, _STATE_UNAVAILABLE
    raw = atr_pct * multiplier
    if raw <= minimum:
        return round(minimum, 2), _STATE_MINIMUM
    if raw >= maximum:
        return round(maximum, 2), _STATE_MAXIMUM
    return round(raw, 2), _STATE_RAW


def _effective_scalar(
    atr_pct: float | None,
    multiplier: float,
    profile: AtrProfile,
    *,
    allow_zero: bool,
) -> tuple[float | None, int]:
    return effective_percentage_state_scalar(
        atr_pct,
        multiplier,
        profile.min_atr_pct,
        profile.max_atr_pct,
        allow_zero=allow_zero,
    )


def _float_token(value: float) -> str:
    return struct.pack(">d", float(value)).hex()


def _behavior_token(
    profile: AtrProfile,
    multiplier: float,
    *,
    allow_zero: bool,
) -> str:
    return (
        f"p{profile.period}-b{profile.bar_seconds}"
        f"-min{_float_token(profile.min_atr_pct)}"
        f"-max{_float_token(profile.max_atr_pct)}"
        f"-mul{_float_token(multiplier)}-z{int(allow_zero)}"
    )


def _component_behaviors(
    profile: AtrProfile,
) -> tuple[tuple[str, float, bool], ...]:
    values: list[tuple[str, float, bool]] = [
        ("initial_drop", profile.initial_drop_multiplier, False),
        ("buy_rebound", profile.buy_rebound_multiplier, True),
        ("minimum_profit", profile.minimum_profit_multiplier, False),
        ("sell_trail", profile.sell_trail_multiplier, True),
    ]
    if profile.protective_sell_mode == "atr":
        values.append(("protective_sell", profile.protective_sell_value, False))
    return tuple(values)


def _available_memory_bytes() -> int | None:
    """Return currently available physical memory without a third-party dependency."""

    if os.name == "nt":
        try:
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong),
                    ("avail_phys", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("avail_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("avail_virtual", ctypes.c_ulonglong),
                    ("avail_extended_virtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatus()
            status.length = ctypes.sizeof(_MemoryStatus)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.avail_phys)
        except (AttributeError, OSError, TypeError, ValueError):
            return None
    sysconf = getattr(os, "sysconf", None)
    if not callable(sysconf):
        return None
    try:
        raw_pages = sysconf("SC_AVPHYS_PAGES")
        raw_page_size = sysconf("SC_PAGE_SIZE")
        if (
            isinstance(raw_pages, bool)
            or not isinstance(raw_pages, int)
            or isinstance(raw_page_size, bool)
            or not isinstance(raw_page_size, int)
        ):
            return None
        pages = raw_pages
        page_size = raw_page_size
    except (OSError, TypeError, ValueError):
        return None
    value = pages * page_size
    return value if value > 0 else None


def memory_aware_worker_count(
    *,
    cpu_count: int,
    available_memory_bytes: int | None,
    estimated_replay_context_bytes: int,
    pending_profiles: int,
    automatic_ceiling: int = _DEFAULT_AUTO_WORKER_CEILING,
) -> int:
    """Choose an automatic process count while reserving CPU and memory."""

    logical = max(1, int(cpu_count))
    pending = max(1, int(pending_profiles))
    ceiling = max(1, min(64, int(automatic_ceiling)))
    cpu_limit = min(ceiling, pending, logical - 1 if logical > 1 else 1)
    if available_memory_bytes is None or available_memory_bytes <= 0:
        return max(1, cpu_limit)

    context = max(1, int(estimated_replay_context_bytes))
    # Read-only mmap pages are shared by the operating system, but each worker
    # still needs private interpreter/state-machine memory and can fault a large
    # working set. This estimate is deliberately conservative.
    per_worker = max(_MIN_ESTIMATED_WORKER_BYTES, context // 3)
    usable = max(0, int(available_memory_bytes) - _MIN_FREE_MEMORY_BYTES)
    memory_limit = max(1, usable // per_worker) if usable else 1
    return max(1, min(cpu_limit, memory_limit))


def adaptive_profile_batch_size(
    *,
    total_rows: int,
    profile_count: int,
    worker_count: int,
    configured_maximum: int = 16,
) -> int:
    """Return a small deterministic task size for expensive profile replays."""

    rows = max(0, int(total_rows))
    profiles = max(1, int(profile_count))
    workers = max(1, int(worker_count))
    configured = max(1, min(256, int(configured_maximum)))
    if rows >= 1_500_000:
        row_cap = 2
    elif rows >= 500_000:
        row_cap = 4
    elif rows >= 100_000:
        row_cap = 8
    else:
        row_cap = 16
    balancing = max(1, math.ceil(profiles / max(1, workers * 6)))
    return max(1, min(configured, row_cap, balancing))


@dataclass(slots=True, frozen=True)
class _EntryPaths:
    values: Path
    states: Path


class EffectiveAtrSeries(Sequence[float | None]):
    """Read-only ATR vector with optional precomputed effective lookups."""

    __slots__ = ("base", "catalog", "session_index", "_behavior_cache")

    def __init__(
        self,
        base: AtrValueSeries,
        catalog: "EffectiveArrayCatalog | None",
        session_index: int,
    ):
        self.base = base
        self.catalog = catalog
        self.session_index = int(session_index)
        self._behavior_cache: dict[
            tuple[int, int, float, float, float, bool],
            tuple[NDArray[np.int16], NDArray[np.uint8]] | None,
        ] = {}

    def __len__(self) -> int:
        return len(self.base)

    def __getitem__(self, index: int) -> float | None:
        return finite_float(self.base[index])

    def effective_percentage_state(
        self,
        index: int,
        multiplier: float,
        profile: AtrProfile,
        *,
        allow_zero: bool,
    ) -> tuple[float | None, str]:
        if self.catalog is None:
            value, state = _effective_scalar(
                self[index],
                multiplier,
                profile,
                allow_zero=allow_zero,
            )
            return value, _STATE_NAME[state]
        behavior = (
            profile.period,
            profile.bar_seconds,
            profile.min_atr_pct,
            profile.max_atr_pct,
            float(multiplier),
            bool(allow_zero),
        )
        loaded = self._behavior_cache.get(behavior)
        if behavior not in self._behavior_cache:
            loaded = self.catalog.loaded_entry(
                self.session_index,
                profile,
                multiplier,
                allow_zero=allow_zero,
            )
            self._behavior_cache[behavior] = loaded
        normalized_index = index if index >= 0 else len(self.base) + index
        if normalized_index < 0 or normalized_index >= len(self.base):
            raise IndexError(index)
        if loaded is not None and (
            len(loaded[0]) != len(self.base) or len(loaded[1]) != len(self.base)
        ):
            raise ValueError(
                "Prepared effective percentage/state arrays do not match the ATR series length."
            )
        if loaded is None:
            value, state = _effective_scalar(
                self[normalized_index],
                multiplier,
                profile,
                allow_zero=allow_zero,
            )
            return value, _STATE_NAME[state]
        basis_points = int(loaded[0][normalized_index])
        state = int(loaded[1][normalized_index])
        if state not in _STATE_NAME:
            raise ValueError("Unknown effective clamp-state code.")
        value = (
            None
            if basis_points == int(_UNAVAILABLE_BASIS_POINTS)
            else basis_points / 100.0
        )
        return value, _STATE_NAME[state]


class EffectiveArrayCatalog:
    """Prepare and reuse exact effective-percentage arrays across profiles."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._loaded: dict[str, tuple[NDArray[np.int16], NDArray[np.uint8]]] = {}
        self._digests: dict[str, bytes] = {}
        self._prepared: set[str] = set()
        self._expected_lengths: dict[str, int] = {}
        self._atr_lengths: dict[str, int] = {}
        self._path_cache: dict[
            tuple[int, int, int, float, float, float, bool],
            _EntryPaths,
        ] = {}
        self._closed = False

    def _atr_length(self, atr_path: Path) -> int:
        key = str(atr_path)
        cached = self._atr_lengths.get(key)
        if cached is not None:
            return cached
        base = np.load(atr_path, mmap_mode="r", allow_pickle=False)
        try:
            if base.ndim != 1 or base.dtype != np.dtype(np.float64):
                raise ValueError(
                    "Prepared ATR data must be a one-dimensional float64 array."
                )
            length = len(base)
        finally:
            self._close_array(base)
        self._atr_lengths[key] = length
        return length

    def _validate_entry_files(
        self,
        paths: _EntryPaths,
        *,
        expected_length: int,
    ) -> None:
        if paths.values.exists() != paths.states.exists():
            raise ValueError("Prepared effective percentage/state files are incomplete.")
        if not paths.values.exists():
            return
        values = np.load(paths.values, mmap_mode="r", allow_pickle=False)
        try:
            states = np.load(paths.states, mmap_mode="r", allow_pickle=False)
            try:
                if values.dtype != np.dtype(np.int16) or states.dtype != np.dtype(np.uint8):
                    raise ValueError("Unsupported effective-array dtype.")
                if values.ndim != 1 or states.ndim != 1:
                    raise ValueError(
                        "Effective percentage/state arrays must be one-dimensional."
                    )
                if len(values) != len(states) or len(values) != expected_length:
                    raise ValueError(
                        "Effective percentage/state arrays do not match their ATR source length."
                    )
            finally:
                self._close_array(states)
        finally:
            self._close_array(values)

    def _paths(
        self,
        session_index: int,
        profile: AtrProfile,
        multiplier: float,
        *,
        allow_zero: bool,
    ) -> _EntryPaths:
        cache_key = (
            int(session_index),
            profile.period,
            profile.bar_seconds,
            profile.min_atr_pct,
            profile.max_atr_pct,
            float(multiplier),
            bool(allow_zero),
        )
        cached = self._path_cache.get(cache_key)
        if cached is not None:
            return cached
        token = _behavior_token(profile, multiplier, allow_zero=allow_zero)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()[:24]
        stem = f"s{int(session_index):04d}-{digest}"
        paths = _EntryPaths(
            values=self.root / f"{stem}-values.npy",
            states=self.root / f"{stem}-states.npy",
        )
        self._path_cache[cache_key] = paths
        return paths

    @staticmethod
    def _close_array(values: NDArray[Any]) -> None:
        mmap_object = getattr(values, "_mmap", None)
        if mmap_object is not None and not getattr(mmap_object, "closed", False):
            mmap_object.close()

    def _prepare_entry(
        self,
        session_index: int,
        atr_path: Path,
        profile: AtrProfile,
        multiplier: float,
        *,
        allow_zero: bool,
    ) -> _EntryPaths:
        paths = self._paths(
            session_index,
            profile,
            multiplier,
            allow_zero=allow_zero,
        )
        cache_key = str(paths.values)
        expected_length = self._atr_length(atr_path)
        self._expected_lengths[cache_key] = expected_length
        if cache_key in self._prepared:
            return paths
        if paths.values.exists() or paths.states.exists():
            self._validate_entry_files(paths, expected_length=expected_length)
            self._prepared.add(cache_key)
            return paths

        value_partial = paths.values.with_suffix(".partial.npy")
        state_partial = paths.states.with_suffix(".partial.npy")
        base: NDArray[Any] | None = None
        values: NDArray[np.int16] | None = None
        states: NDArray[np.uint8] | None = None
        scalar_cache: dict[int, tuple[np.int16, np.uint8]] = {}
        try:
            try:
                loaded_base = np.load(atr_path, mmap_mode="r", allow_pickle=False)
                base = loaded_base
                if loaded_base.ndim != 1 or loaded_base.dtype != np.dtype(np.float64):
                    raise ValueError(
                        "Prepared ATR data must be a one-dimensional float64 array."
                    )
                if len(loaded_base) != expected_length:
                    raise ValueError(
                        "Prepared ATR source changed while effective arrays were built."
                    )
                created_values = np.lib.format.open_memmap(
                    value_partial,
                    mode="w+",
                    dtype=np.int16,
                    shape=(len(loaded_base),),
                )
                values = created_values
                created_states = np.lib.format.open_memmap(
                    state_partial,
                    mode="w+",
                    dtype=np.uint8,
                    shape=(len(loaded_base),),
                )
                states = created_states
                chunk_size = 250_000
                for start in range(0, len(loaded_base), chunk_size):
                    stop = min(len(loaded_base), start + chunk_size)
                    source = loaded_base[start:stop]
                    out_values = np.empty(stop - start, dtype=np.int16)
                    out_states = np.empty(stop - start, dtype=np.uint8)
                    for offset, raw_value in enumerate(source):
                        parsed = float(raw_value)
                        bits = struct.unpack(">Q", struct.pack(">d", parsed))[0]
                        cached = scalar_cache.get(bits)
                        if cached is None:
                            atr_pct = None if math.isnan(parsed) else parsed
                            effective, state = _effective_scalar(
                                atr_pct,
                                multiplier,
                                profile,
                                allow_zero=allow_zero,
                            )
                            if effective is None:
                                basis_points = _UNAVAILABLE_BASIS_POINTS
                            else:
                                integer_basis_points = int(round(effective * 100.0))
                                if not (
                                    np.iinfo(np.int16).min
                                    < integer_basis_points
                                    <= np.iinfo(np.int16).max
                                ):
                                    raise ValueError(
                                        "Effective percentage exceeds the prepared-array range."
                                    )
                                basis_points = np.int16(integer_basis_points)
                            cached = (basis_points, np.uint8(state))
                            if len(scalar_cache) < _MAX_SCALAR_CACHE_ENTRIES:
                                scalar_cache[bits] = cached
                        out_values[offset], out_states[offset] = cached
                    created_values[start:stop] = out_values
                    created_states[start:stop] = out_states
                values_flush = getattr(created_values, "flush", None)
                states_flush = getattr(created_states, "flush", None)
                if not callable(values_flush) or not callable(states_flush):
                    raise TypeError(
                        "NumPy open_memmap returned an object without flush()."
                    )
                values_flush()
                states_flush()
            finally:
                if values is not None:
                    self._close_array(values)
                if states is not None:
                    self._close_array(states)
                if base is not None:
                    self._close_array(base)
        except Exception:
            value_partial.unlink(missing_ok=True)
            state_partial.unlink(missing_ok=True)
            raise
        try:
            os.replace(value_partial, paths.values)
            os.replace(state_partial, paths.states)
        except Exception:
            # The pair is one logical cache entry. Never retain one published
            # half if the second atomic move fails on Windows.
            paths.values.unlink(missing_ok=True)
            paths.states.unlink(missing_ok=True)
            value_partial.unlink(missing_ok=True)
            state_partial.unlink(missing_ok=True)
            raise
        self._validate_entry_files(paths, expected_length=expected_length)
        self._prepared.add(cache_key)
        return paths

    def prepare(
        self,
        profiles: Iterable[AtrProfile],
        *,
        session_atr_paths: Mapping[tuple[int, int, int], Path],
    ) -> None:
        """Prepare every distinct behavior used by ``profiles``."""

        unique_profiles = list(dict.fromkeys(profiles))
        for profile in sorted(unique_profiles, key=lambda value: value.key()):
            for session_index, period, bar_seconds in sorted(session_atr_paths):
                if period != profile.period or bar_seconds != profile.bar_seconds:
                    continue
                atr_path = session_atr_paths[(session_index, period, bar_seconds)]
                for _component, multiplier, allow_zero in _component_behaviors(profile):
                    self._prepare_entry(
                        session_index,
                        atr_path,
                        profile,
                        multiplier,
                        allow_zero=allow_zero,
                    )

    def _load(
        self,
        paths: _EntryPaths,
    ) -> tuple[NDArray[np.int16], NDArray[np.uint8]] | None:
        key = str(paths.values)
        loaded = self._loaded.get(key)
        if loaded is not None:
            return loaded
        if not paths.values.exists() or not paths.states.exists():
            return None
        values = np.load(paths.values, mmap_mode="r", allow_pickle=False)
        try:
            states = np.load(paths.states, mmap_mode="r", allow_pickle=False)
        except Exception:
            self._close_array(values)
            raise
        if values.dtype != np.dtype(np.int16) or states.dtype != np.dtype(np.uint8):
            self._close_array(values)
            self._close_array(states)
            raise ValueError("Unsupported effective-array dtype.")
        if values.ndim != 1 or states.ndim != 1:
            self._close_array(values)
            self._close_array(states)
            raise ValueError("Effective percentage/state arrays must be one-dimensional.")
        if len(values) != len(states):
            self._close_array(values)
            self._close_array(states)
            raise ValueError("Effective percentage/state arrays are not aligned.")
        expected_length = self._expected_lengths.get(key)
        if expected_length is not None and len(values) != expected_length:
            self._close_array(values)
            self._close_array(states)
            raise ValueError(
                "Effective percentage/state arrays do not match their ATR source length."
            )
        loaded = (values, states)
        self._loaded[key] = loaded
        return loaded

    def lookup(
        self,
        session_index: int,
        index: int,
        profile: AtrProfile,
        multiplier: float,
        *,
        allow_zero: bool,
        fallback_atr_pct: float | None,
    ) -> tuple[float | None, str]:
        paths = self._paths(
            session_index,
            profile,
            multiplier,
            allow_zero=allow_zero,
        )
        loaded = self._load(paths)
        if loaded is None or index < 0 or index >= len(loaded[0]):
            value, state = _effective_scalar(
                fallback_atr_pct,
                multiplier,
                profile,
                allow_zero=allow_zero,
            )
            return value, _STATE_NAME[state]
        basis_points = int(loaded[0][index])
        state = int(loaded[1][index])
        if state not in _STATE_NAME:
            raise ValueError("Unknown effective clamp-state code.")
        value = (
            None
            if basis_points == int(_UNAVAILABLE_BASIS_POINTS)
            else basis_points / 100.0
        )
        return value, _STATE_NAME[state]

    def loaded_entry(
        self,
        session_index: int,
        profile: AtrProfile,
        multiplier: float,
        *,
        allow_zero: bool,
    ) -> tuple[NDArray[np.int16], NDArray[np.uint8]] | None:
        """Return one read-only prepared entry, loading it at most once."""

        return self._load(
            self._paths(
                session_index,
                profile,
                multiplier,
                allow_zero=allow_zero,
            )
        )

    def _file_digest(self, path: Path) -> bytes:
        key = str(path)
        cached = self._digests.get(key)
        if cached is not None:
            return cached
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
        value = digest.digest()
        self._digests[key] = value
        return value

    def profile_signature(
        self,
        profile: AtrProfile,
        *,
        session_indices: Sequence[int],
    ) -> str | None:
        """Return an exact effective-behavior signature or ``None`` fail-closed."""

        ordered_session_indices = tuple(int(value) for value in session_indices)
        if not ordered_session_indices:
            # With no observed ATR rows, equal effective behavior cannot be
            # proven.  Fail closed instead of collapsing every multiplier.
            return None
        digest = hashlib.sha256()
        digest.update(f"p{profile.period}-b{profile.bar_seconds}".encode("ascii"))
        digest.update(profile.protective_sell_mode.encode("ascii"))
        if profile.protective_sell_mode == "manual":
            digest.update(struct.pack(">d", profile.protective_sell_value))
        for component, multiplier, allow_zero in _component_behaviors(profile):
            digest.update(component.encode("ascii"))
            for session_index in ordered_session_indices:
                paths = self._paths(
                    session_index,
                    profile,
                    multiplier,
                    allow_zero=allow_zero,
                )
                if not paths.values.exists() or not paths.states.exists():
                    return None
                digest.update(self._file_digest(paths.values))
                digest.update(self._file_digest(paths.states))
        return digest.hexdigest()

    def close(self) -> None:
        if self._closed:
            return
        # Mark closed only after every map is released. If one close fails on
        # Windows, retain the mappings so the caller can retry cleanup rather
        # than turning a partial failure into a permanent no-op.
        for values, states in self._loaded.values():
            self._close_array(values)
            self._close_array(states)
        self._loaded.clear()
        self._digests.clear()
        self._expected_lengths.clear()
        self._atr_lengths.clear()
        self._path_cache.clear()
        self._closed = True


_RUNTIME_EFFECTIVE_CATALOG: EffectiveArrayCatalog | None = None


def set_runtime_effective_catalog(
    catalog: EffectiveArrayCatalog | None,
) -> EffectiveArrayCatalog | None:
    """Install a process-local catalog and return the previous value."""

    global _RUNTIME_EFFECTIVE_CATALOG
    previous = _RUNTIME_EFFECTIVE_CATALOG
    _RUNTIME_EFFECTIVE_CATALOG = catalog
    return previous


def lookup_effective_percentage_state(
    atr_values: AtrValueSeries,
    index: int,
    multiplier: float,
    profile: AtrProfile,
    *,
    allow_zero: bool,
) -> tuple[float | None, str]:
    """Use a prepared exact array when available, otherwise use scalar logic."""

    # This is a replay hot path. A concrete-class check avoids repeated
    # dynamic attribute lookup while retaining the scalar fallback for lists
    # and NumPy arrays.
    if isinstance(atr_values, EffectiveAtrSeries):
        return atr_values.effective_percentage_state(
            index,
            multiplier,
            profile,
            allow_zero=allow_zero,
        )
    parsed = finite_float(atr_values[index])
    effective, state = _effective_scalar(
        parsed,
        multiplier,
        profile,
        allow_zero=allow_zero,
    )
    return effective, _STATE_NAME[state]


def _collapse_profiles(
    profiles: Sequence[AtrProfile],
    catalog: EffectiveArrayCatalog,
    *,
    session_indices: Sequence[int],
) -> tuple[list[AtrProfile], list[int]]:
    """Collapse only profiles proven identical by complete effective signatures."""

    representatives: list[AtrProfile] = []
    representative_by_signature: dict[str, int] = {}
    mapping: list[int] = []
    for profile in profiles:
        signature = catalog.profile_signature(
            profile,
            session_indices=session_indices,
        )
        if signature is None:
            representative_index = len(representatives)
            representatives.append(profile)
            mapping.append(representative_index)
            continue
        representative_index = representative_by_signature.get(signature)
        if representative_index is None:
            representative_index = len(representatives)
            representative_by_signature[signature] = representative_index
            representatives.append(profile)
        mapping.append(representative_index)
    return representatives, mapping


def exact_equivalence_group_count(
    profiles: Sequence[AtrProfile],
    catalog: EffectiveArrayCatalog,
    *,
    session_indices: Sequence[int],
) -> int:
    representatives, _mapping = _collapse_profiles(
        profiles,
        catalog,
        session_indices=session_indices,
    )
    return len(representatives)


def expand_evaluation_results(
    profiles: Sequence[AtrProfile],
    representatives: Sequence[AtrProfile],
    mapping: Sequence[int],
    representative_results: Sequence[
        tuple[str, MarketReplayCandidateSummary, list[MarketReplaySessionResult]]
    ],
) -> list[
    tuple[str, MarketReplayCandidateSummary, list[MarketReplaySessionResult]]
]:
    """Restore every nominal profile in original order after representative replay."""

    if len(mapping) != len(profiles):
        raise RuntimeError("Equivalence expansion mapping does not match profile count.")
    if len(representative_results) != len(representatives):
        raise RuntimeError("Representative evaluation returned an incomplete result set.")
    by_key = {key: (summary, sessions) for key, summary, sessions in representative_results}
    if len(by_key) != len(representative_results):
        raise RuntimeError("Representative evaluation returned duplicate profile keys.")
    output: list[
        tuple[str, MarketReplayCandidateSummary, list[MarketReplaySessionResult]]
    ] = []
    for profile, representative_index in zip(profiles, mapping, strict=True):
        if representative_index < 0 or representative_index >= len(representatives):
            raise RuntimeError("Equivalence expansion referenced an invalid representative.")
        representative = representatives[representative_index]
        try:
            summary, sessions = by_key[representative.key()]
        except KeyError as exc:
            raise RuntimeError(
                "Representative evaluation omitted a required profile."
            ) from exc
        cloned = deepcopy(summary)
        cloned.profile = profile
        output.append((profile.key(), cloned, list(sessions)))
    return output


def validate_nominal_profile_results(
    profiles: Sequence[AtrProfile],
    results: Sequence[
        tuple[str, MarketReplayCandidateSummary, list[MarketReplaySessionResult]]
    ],
) -> None:
    """Fail closed when scheduling or alias expansion loses/reorders a profile."""

    if len(results) != len(profiles):
        raise RuntimeError(
            "Profile evaluation returned an incomplete result set: "
            f"expected {len(profiles)}, received {len(results)}."
        )
    for index, (profile, result) in enumerate(zip(profiles, results, strict=True)):
        key, summary, _sessions = result
        if key != profile.key() or summary.profile != profile:
            raise RuntimeError(
                "Profile evaluation changed nominal profile order at index "
                f"{index}."
            )
