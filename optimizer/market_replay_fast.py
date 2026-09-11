"""Exact compact replay storage and deterministic process-parallel profile evaluation.

The Market Replay optimizer evaluates the same immutable market path for many
independent ATR profiles.  This module stores the replay-relevant tick state in
read-only NumPy memory maps and lets spawned worker processes attach to those
files without pickling or duplicating millions of Python ``IbrecTick`` objects.

Profile evaluation and the expensive independent post-search validation tasks
share one persistent process pool. Candidate generation, ranking, stable-region
selection, boundary decisions, bootstrap schedules, and report writing remain
serial and deterministic in the parent process.
"""

from __future__ import annotations

import errno
import gc
import math
import multiprocessing
import os
import shutil
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence, cast

import numpy as np
from numpy.typing import NDArray

from .market_replay_models import (
    AtrProfile,
    IbrecPeriod,
    IbrecRecording,
    IbrecTick,
    MarketReplayCandidateSummary,
    MarketReplayConfig,
    MarketReplaySessionResult,
)
from .market_replay_optimization import (
    EffectiveArrayCatalog,
    EffectiveAtrSeries,
    _available_memory_bytes,
    _collapse_profiles,
    adaptive_profile_batch_size,
    expand_evaluation_results,
    memory_aware_worker_count,
    set_runtime_effective_catalog,
    validate_nominal_profile_results,
)

_FLAG_FULL_SNAPSHOT = 1 << 0
_FLAG_LAST_EVENT = 1 << 1
_FLAG_BID_EVENT = 1 << 2
_FLAG_ASK_EVENT = 1 << 3
_TRANSIENT_CLEANUP_ERRNOS = {errno.EACCES, errno.EBUSY, errno.EPERM}
_TRANSIENT_CLEANUP_WINERRORS = {5, 32, 33}
_CLEANUP_ATTEMPTS = 8

_TICK_DTYPE = np.dtype(
    [
        ("sequence", "<i8"),
        ("timestamp", "<f8"),
        # The importer normalizes receipt timestamps to milliseconds.  Keep
        # that integer representation as well as the float epoch used by the
        # state machine so report timestamps remain byte-for-byte identical
        # even on platforms where a float round-trip lands either side of a
        # millisecond boundary.
        ("captured_epoch_ms", "<i8"),
        ("elapsed_ns", "<i8"),
        ("selected", "<f8"),
        ("bid", "<f8"),
        ("ask", "<f8"),
        ("last", "<f8"),
        ("mark", "<f8"),
        ("bid_size", "<f8"),
        ("ask_size", "<f8"),
        ("flags", "u1"),
    ],
    align=False,
)


def _close_memmap(values: NDArray[Any]) -> None:
    """Close a NumPy memory map when one backs ``values``."""

    mmap_object = getattr(values, "_mmap", None)
    if mmap_object is not None and not getattr(mmap_object, "closed", False):
        mmap_object.close()


def _remove_tree_with_retries(path: Path) -> None:
    """Remove a replay store after bounded transient Windows handle delays."""

    delay = 0.025
    for attempt in range(_CLEANUP_ATTEMPTS):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return
        except FileNotFoundError:
            return
        except OSError as error:
            transient = (
                isinstance(error, PermissionError)
                or error.errno in _TRANSIENT_CLEANUP_ERRNOS
                or getattr(error, "winerror", None) in _TRANSIENT_CLEANUP_WINERRORS
            )
            if not transient or attempt + 1 >= _CLEANUP_ATTEMPTS:
                raise
            gc.collect()
            time.sleep(delay)
            delay = min(0.5, delay * 2.0)


def _load_readonly_atr_array(
    path: str | Path,
    *,
    expected_length: int,
) -> NDArray[np.float64]:
    """Load one aligned read-only float64 ATR vector or fail closed."""

    values = np.load(path, mmap_mode="r", allow_pickle=False)
    if (
        values.dtype != np.dtype(np.float64)
        or values.ndim != 1
        or len(values) != int(expected_length)
        or values.flags.writeable
    ):
        _close_memmap(values)
        raise ValueError(
            "Prepared ATR data is not a read-only, aligned float64 vector."
        )
    return values


def _missing(value: float | None) -> float:
    return math.nan if value is None else float(value)


def _flush_memmap(array: NDArray[Any]) -> None:
    """Flush an array created by ``open_memmap`` through its runtime API."""

    flush = getattr(array, "flush", None)
    if not callable(flush):
        raise TypeError("NumPy open_memmap returned an object without flush().")
    flush()


def _captured_epoch_ms(tick: IbrecTick) -> int:
    # The importer normalizes ``captured_at_utc`` with millisecond precision by
    # truncating the source datetime, while ``tick.timestamp`` retains the
    # source's sub-millisecond precision for event ordering. Rounding the float
    # epoch can therefore advance the compact report timestamp by one
    # millisecond. Preserve the importer's exact normalized millisecond.
    text = tick.captured_at_utc
    if len(text) < 5 or not text.endswith("Z") or text[-5] != ".":
        raise ValueError("Normalized captured_at_utc must end in '.mmmZ'.")
    try:
        milliseconds = int(text[-4:-1])
    except ValueError as exc:
        raise ValueError("Normalized captured_at_utc has invalid milliseconds.") from exc
    return math.floor(float(tick.timestamp)) * 1_000 + milliseconds


def _tick_record(tick: IbrecTick) -> tuple[object, ...]:
    """Return one exact compact-row record in ``_TICK_DTYPE`` field order."""

    return (
        tick.sequence,
        tick.timestamp,
        _captured_epoch_ms(tick),
        tick.elapsed_ns,
        _missing(tick.selected_price()),
        _missing(tick.valid_bid()),
        _missing(tick.valid_ask()),
        _missing(tick.last),
        _missing(tick.mark_price),
        _missing(tick.bid_size),
        _missing(tick.ask_size),
        (_FLAG_FULL_SNAPSHOT if tick.full_snapshot else 0)
        | (_FLAG_LAST_EVENT if tick.has_last_event() else 0)
        | (_FLAG_BID_EVENT if tick.has_bid_event() else 0)
        | (_FLAG_ASK_EVENT if tick.has_ask_event() else 0),
    )


def _period_identity(period: IbrecPeriod) -> tuple[str, str, int]:
    return (
        str(period.source_recording_sha256 or ""),
        str(period.session_date),
        int(period.period_id),
    )


def _safe_token(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value)


@dataclass(slots=True, frozen=True)
class PreparedSessionSpec:
    """Pickle-safe description of one compact replay session."""

    period: IbrecPeriod
    tick_path: str
    session_index: int

    @property
    def identity(self) -> tuple[str, str, int]:
        return _period_identity(self.period)


@dataclass(slots=True, frozen=True)
class PreparedWorkerSpec:
    """Immutable process-pool initializer payload."""

    sessions: tuple[PreparedSessionSpec, ...]
    atr_root: str
    effective_root: str
    min_tick: float
    config: MarketReplayConfig
    recording: IbrecRecording | None = None


@dataclass(slots=True, frozen=True)
class AnalysisWorkerTask:
    """One deterministic post-search task executed against shared replay data."""

    task_id: str
    kind: str
    payload: tuple[Any, ...] = ()


class FastTickProxy:
    """Reusable view of one row in a :class:`FastTickSequence`."""

    __slots__ = ("_owner", "_index")

    def __init__(self, owner: "FastTickSequence", index: int = 0):
        self._owner = owner
        self._index = index

    def _set_index(self, index: int) -> "FastTickProxy":
        self._index = index
        return self

    @property
    def sequence(self) -> int:
        return int(self._owner._sequence[self._index])

    @property
    def timestamp(self) -> float:
        return float(self._owner._timestamp[self._index])

    @property
    def captured_at_utc(self) -> str:
        epoch_ms = int(self._owner._captured_epoch_ms[self._index])
        seconds, milliseconds = divmod(epoch_ms, 1_000)
        base = datetime.fromtimestamp(seconds, tz=timezone.utc)
        return f"{base:%Y-%m-%dT%H:%M:%S}.{milliseconds:03d}Z"

    @property
    def elapsed_ns(self) -> int:
        return int(self._owner._elapsed_ns[self._index])

    @property
    def bid(self) -> float | None:
        return self._owner._optional(self._owner._bid, self._index)

    @property
    def ask(self) -> float | None:
        return self._owner._optional(self._owner._ask, self._index)

    @property
    def last(self) -> float | None:
        return self._owner._optional(self._owner._last, self._index)

    @property
    def mark_price(self) -> float | None:
        return self._owner._optional(self._owner._mark, self._index)

    @property
    def bid_size(self) -> float | None:
        return self._owner._optional(self._owner._bid_size, self._index)

    @property
    def ask_size(self) -> float | None:
        return self._owner._optional(self._owner._ask_size, self._index)

    @property
    def full_snapshot(self) -> bool:
        return bool(int(self._owner._flags[self._index]) & _FLAG_FULL_SNAPSHOT)

    @property
    def changed_fields(self) -> tuple[str, ...]:
        """Compatibility view; the hot path uses the precomputed event flags."""

        flags = int(self._owner._flags[self._index])
        values: list[str] = []
        if flags & _FLAG_BID_EVENT:
            values.append("bid")
        if flags & _FLAG_ASK_EVENT:
            values.append("ask")
        if flags & _FLAG_LAST_EVENT:
            values.append("last")
        return tuple(values)

    def selected_price(self) -> float | None:
        return self._owner._optional(self._owner._selected, self._index)

    def valid_bid(self) -> float | None:
        return self._owner._optional(self._owner._bid, self._index)

    def valid_ask(self) -> float | None:
        return self._owner._optional(self._owner._ask, self._index)

    def has_last_event(self) -> bool:
        return bool(int(self._owner._flags[self._index]) & _FLAG_LAST_EVENT)

    def has_bid_event(self) -> bool:
        return bool(int(self._owner._flags[self._index]) & _FLAG_BID_EVENT)

    def has_ask_event(self) -> bool:
        return bool(int(self._owner._flags[self._index]) & _FLAG_ASK_EVENT)


class _FastTickIterator:
    __slots__ = ("_owner", "_index", "_proxy")

    def __init__(self, owner: "FastTickSequence"):
        self._owner = owner
        self._index = 0
        self._proxy = FastTickProxy(owner)

    def __iter__(self) -> "_FastTickIterator":
        return self

    def __next__(self) -> FastTickProxy:
        if self._index >= len(self._owner):
            raise StopIteration
        value = self._proxy._set_index(self._index)
        self._index += 1
        return value


class FastTickSequence(Sequence[FastTickProxy]):
    """Read-only memory-mapped replay tick sequence.

    Iteration deliberately reuses one proxy object.  The replay state machine
    consumes the current tick synchronously and never retains the input object.
    Avoiding per-row proxy allocation is material when millions of rows are
    replayed across hundreds of profiles.
    """

    __slots__ = (
        "path",
        "_array",
        "_sequence",
        "_timestamp",
        "_captured_epoch_ms",
        "_elapsed_ns",
        "_selected",
        "_bid",
        "_ask",
        "_last",
        "_mark",
        "_bid_size",
        "_ask_size",
        "_flags",
    )

    def __init__(self, path: str | Path):
        self.path = Path(path)
        array = np.load(self.path, mmap_mode="r", allow_pickle=False)
        if array.dtype != _TICK_DTYPE or array.ndim != 1 or array.flags.writeable:
            _close_memmap(array)
            raise ValueError(f"Unsupported compact replay dtype in {self.path}.")
        self._array = array
        self._sequence = self._array["sequence"]
        self._timestamp = self._array["timestamp"]
        self._captured_epoch_ms = self._array["captured_epoch_ms"]
        self._elapsed_ns = self._array["elapsed_ns"]
        self._selected = self._array["selected"]
        self._bid = self._array["bid"]
        self._ask = self._array["ask"]
        self._last = self._array["last"]
        self._mark = self._array["mark"]
        self._bid_size = self._array["bid_size"]
        self._ask_size = self._array["ask_size"]
        self._flags = self._array["flags"]

    @staticmethod
    def _optional(values: NDArray[np.float64], index: int) -> float | None:
        value = float(values[index])
        return None if math.isnan(value) else value

    def __len__(self) -> int:
        return int(self._array.shape[0])

    def __iter__(self) -> Iterator[FastTickProxy]:
        return _FastTickIterator(self)

    def __getitem__(self, index: int | slice) -> FastTickProxy | list[FastTickProxy]:
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            return [FastTickProxy(self, item) for item in range(start, stop, step)]
        normalized = index if index >= 0 else len(self) + index
        if normalized < 0 or normalized >= len(self):
            raise IndexError(index)
        return FastTickProxy(self, normalized)

    def close(self) -> None:
        """Release the underlying memory map when the platform exposes it."""

        array = self._array
        mmap_object = getattr(array, "_mmap", None)
        # Field views retain a reference to the same mmap.  Rebind every view
        # before closing so Windows can release the file immediately rather
        # than keeping the temporary replay directory locked until process
        # teardown.
        empty = np.empty(0, dtype=_TICK_DTYPE)
        self._array = empty
        self._sequence = empty["sequence"]
        self._timestamp = empty["timestamp"]
        self._captured_epoch_ms = empty["captured_epoch_ms"]
        self._elapsed_ns = empty["elapsed_ns"]
        self._selected = empty["selected"]
        self._bid = empty["bid"]
        self._ask = empty["ask"]
        self._last = empty["last"]
        self._mark = empty["mark"]
        self._bid_size = empty["bid_size"]
        self._ask_size = empty["ask_size"]
        self._flags = empty["flags"]
        if mmap_object is not None and not getattr(mmap_object, "closed", False):
            mmap_object.close()


class PreparedReplayStore:
    """Temporary compact replay/ATR storage shared by spawned worker processes."""

    def __init__(
        self,
        root: Path,
        sessions: tuple[PreparedSessionSpec, ...],
        *,
        total_rows: int,
    ):
        self.root = root
        self.sessions = sessions
        self.atr_root = root / "atr"
        self.atr_root.mkdir(parents=True, exist_ok=True)
        self.effective_root = root / "effective"
        self.effective_root.mkdir(parents=True, exist_ok=True)
        self._session_by_identity = {spec.identity: spec for spec in sessions}
        self._total_rows = int(total_rows)
        self._closed = False

    @classmethod
    def create(
        cls,
        period_ticks: Sequence[tuple[IbrecPeriod, Sequence[IbrecTick]]],
    ) -> "PreparedReplayStore":
        root = Path(tempfile.mkdtemp(prefix="bouncybot-replay-fast-"))
        sessions: list[PreparedSessionSpec] = []
        try:
            ordered = sorted(
                period_ticks,
                key=lambda item: (
                    item[0].open_timestamp,
                    item[0].session_date,
                    item[0].source_recording_sha256,
                    item[0].period_id,
                ),
            )
            for index, (period, ticks) in enumerate(ordered):
                path = root / f"ticks-{index:04d}.npy"
                cls._write_ticks(path, ticks)
                sessions.append(
                    PreparedSessionSpec(
                        period=period,
                        tick_path=str(path),
                        session_index=index,
                    )
                )
            return cls(
                root,
                tuple(sessions),
                total_rows=sum(len(ticks) for _period, ticks in ordered),
            )
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise

    @staticmethod
    def _write_ticks(path: Path, ticks: Sequence[IbrecTick]) -> None:
        temporary = path.with_suffix(".partial.npy")
        array: NDArray[Any] | None = None
        try:
            try:
                created_array = np.lib.format.open_memmap(
                    temporary,
                    mode="w+",
                    dtype=_TICK_DTYPE,
                    shape=(len(ticks),),
                )
                array = created_array
                chunk_size = 100_000
                for start in range(0, len(ticks), chunk_size):
                    chunk = ticks[start : start + chunk_size]
                    stop = start + len(chunk)
                    # One pass over the Python objects is materially faster than one
                    # generator pass per field, especially for multi-million-row
                    # recordings. NumPy writes the same binary values in field order.
                    created_array[start:stop] = np.fromiter(
                        (_tick_record(tick) for tick in chunk),
                        dtype=_TICK_DTYPE,
                        count=len(chunk),
                    )
                _flush_memmap(created_array)
            finally:
                if array is not None:
                    _close_memmap(array)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        try:
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @property
    def total_rows(self) -> int:
        return self._total_rows

    def session_spec(self, period: IbrecPeriod) -> PreparedSessionSpec:
        try:
            return self._session_by_identity[_period_identity(period)]
        except KeyError as exc:
            raise KeyError(
                f"No compact replay session exists for {period.session_date}/{period.period_id}."
            ) from exc

    def open_ticks(self, period: IbrecPeriod) -> FastTickSequence:
        return FastTickSequence(self.session_spec(period).tick_path)

    def atr_path(self, period: IbrecPeriod, atr_period: int, bar_seconds: int) -> Path:
        spec = self.session_spec(period)
        source = _safe_token(period.source_recording_sha256[:16] or "combined")
        return self.atr_root / (
            f"atr-{spec.session_index:04d}-{source}-{_safe_token(period.session_date)}-"
            f"{period.period_id}-p{atr_period}-b{bar_seconds}.npy"
        )

    def persist_atr(
        self,
        period: IbrecPeriod,
        atr_period: int,
        bar_seconds: int,
        values: Sequence[float | None],
    ) -> Path:
        path = self.atr_path(period, atr_period, bar_seconds)
        if path.exists():
            existing = _load_readonly_atr_array(
                path,
                expected_length=len(values),
            )
            _close_memmap(existing)
            return path
        temporary = path.with_suffix(".partial.npy")
        array: NDArray[np.float64] | None = None
        try:
            try:
                created_array = np.lib.format.open_memmap(
                    temporary,
                    mode="w+",
                    dtype=np.dtype("<f8"),
                    shape=(len(values),),
                )
                array = created_array
                for start in range(0, len(values), 250_000):
                    chunk = values[start : start + 250_000]
                    created_array[start : start + len(chunk)] = np.fromiter(
                        (
                            math.nan
                            if value is None or not math.isfinite(float(value))
                            else float(value)
                            for value in chunk
                        ),
                        dtype=np.float64,
                        count=len(chunk),
                    )
                _flush_memmap(created_array)
            finally:
                if array is not None:
                    _close_memmap(array)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        try:
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return path

    def persist_atr_cache(
        self,
        atr_cache: Mapping[tuple[str, str, int, int, int], Sequence[float | None]],
        *,
        windows: set[tuple[int, int]] | None = None,
    ) -> None:
        for key, values in atr_cache.items():
            window = (key[3], key[4])
            if windows is not None and window not in windows:
                continue
            spec = self._session_by_identity.get(key[:3])
            if spec is not None:
                self.persist_atr(spec.period, key[3], key[4], values)

    def close(self) -> None:
        if self._closed:
            return
        _remove_tree_with_retries(self.root)
        self._closed = True

    def __enter__(self) -> "PreparedReplayStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            self.close()
        except OSError:
            if exc_type is None:
                # A successful analysis must not silently leak a potentially
                # multi-gigabyte replay store. Surface the remaining handle.
                raise
            # Do not mask a primary analysis failure with a secondary cleanup
            # error after all bounded retries have already been attempted.
            shutil.rmtree(self.root, ignore_errors=True)


_WORKER_SPEC: PreparedWorkerSpec | None = None
_WORKER_RECORDING: IbrecRecording | None = None
_WORKER_TICKS: list[tuple[IbrecPeriod, FastTickSequence]] = []
_WORKER_ATR: dict[str, NDArray[np.float64]] = {}
_WORKER_SESSION_INDEX: dict[tuple[str, str, int], int] = {}
_WORKER_EFFECTIVE_CATALOG: EffectiveArrayCatalog | None = None


def _worker_initialize(spec: PreparedWorkerSpec) -> None:
    global _WORKER_SPEC, _WORKER_RECORDING, _WORKER_TICKS, _WORKER_ATR
    global _WORKER_SESSION_INDEX
    global _WORKER_EFFECTIVE_CATALOG
    _WORKER_SPEC = spec
    _WORKER_RECORDING = spec.recording
    opened: list[tuple[IbrecPeriod, FastTickSequence]] = []
    try:
        for session in spec.sessions:
            opened.append((session.period, FastTickSequence(session.tick_path)))
    except Exception:
        for _period, ticks in opened:
            ticks.close()
        raise
    _WORKER_TICKS = opened
    _WORKER_ATR = {}
    _WORKER_SESSION_INDEX = {
        session.identity: session.session_index for session in spec.sessions
    }
    _WORKER_EFFECTIVE_CATALOG = EffectiveArrayCatalog(spec.effective_root)
    set_runtime_effective_catalog(_WORKER_EFFECTIVE_CATALOG)


def _worker_analysis_task(task: AnalysisWorkerTask) -> tuple[str, Any]:
    """Execute one high-level deterministic task in an initialized worker."""

    if _WORKER_SPEC is None or _WORKER_RECORDING is None:
        raise RuntimeError(
            "Market Replay analysis worker was not initialized with recording metadata."
        )
    from .market_replay import (
        _assumption_stress_scenario_row,
        _atr_phase_task_result,
        _fixed_leave_one_out_task_result,
        _leave_one_day_out_selection_row,
        _selection_aware_bootstrap_replicate_row,
        _walk_forward_fold_row,
    )

    period_ticks = cast(Any, _WORKER_TICKS)
    if task.kind == "leave_one_out_selector":
        (omitted_day,) = task.payload
        result = _leave_one_day_out_selection_row(
            _WORKER_RECORDING,
            period_ticks,
            _WORKER_SPEC.config,
            str(omitted_day),
        )
    elif task.kind == "fixed_leave_one_out":
        omitted_day, candidate_profile, control_profile = task.payload
        result = _fixed_leave_one_out_task_result(
            _WORKER_RECORDING,
            period_ticks,
            _WORKER_SPEC.config,
            str(omitted_day),
            cast(AtrProfile, candidate_profile),
            cast(AtrProfile, control_profile),
        )
    elif task.kind == "atr_phase":
        profile, phase_seconds = task.payload
        result = _atr_phase_task_result(
            _WORKER_RECORDING,
            period_ticks,
            cast(AtrProfile, profile),
            int(phase_seconds),
            _WORKER_SPEC.config,
        )
    elif task.kind == "assumption_stress":
        stress_key, variant, candidate_profile, control_profile = task.payload
        result = _assumption_stress_scenario_row(
            _WORKER_RECORDING,
            period_ticks,
            str(stress_key),
            cast(MarketReplayConfig, variant),
            cast(AtrProfile, candidate_profile),
            cast(AtrProfile, control_profile),
        )
    elif task.kind == "walk_forward":
        fold_number, training_days, validation_days, candidate_profile = task.payload
        result = _walk_forward_fold_row(
            _WORKER_RECORDING,
            period_ticks,
            _WORKER_SPEC.config,
            int(fold_number),
            tuple(str(day) for day in cast(Sequence[Any], training_days)),
            tuple(str(day) for day in cast(Sequence[Any], validation_days)),
            cast(AtrProfile, candidate_profile),
        )
    elif task.kind == "selection_bootstrap":
        replicate, indices, units, candidate_profile = task.payload
        result = _selection_aware_bootstrap_replicate_row(
            _WORKER_RECORDING,
            period_ticks,
            _WORKER_SPEC.config,
            int(replicate),
            tuple(int(index) for index in cast(Sequence[Any], indices)),
            tuple(
                tuple(str(day) for day in cast(Sequence[Any], unit))
                for unit in cast(Sequence[Any], units)
            ),
            cast(AtrProfile, candidate_profile),
        )
    else:
        raise ValueError(f"Unknown Market Replay worker task kind: {task.kind!r}.")
    gc.collect()
    return task.task_id, result


def _worker_atr_path(period: IbrecPeriod, profile: AtrProfile) -> Path:
    if _WORKER_SPEC is None:
        raise RuntimeError("Market Replay worker was not initialized.")
    try:
        session_index = _WORKER_SESSION_INDEX[_period_identity(period)]
    except KeyError as exc:
        raise RuntimeError(
            "Market Replay worker received an unknown compact session."
        ) from exc
    source = _safe_token(period.source_recording_sha256[:16] or "combined")
    return Path(_WORKER_SPEC.atr_root) / (
        f"atr-{session_index:04d}-{source}-{_safe_token(period.session_date)}-"
        f"{period.period_id}-p{profile.period}-b{profile.bar_seconds}.npy"
    )


def _worker_evaluate_batch(
    task: tuple[tuple[AtrProfile, ...], dict[str, int] | None],
) -> list[
    tuple[str, MarketReplayCandidateSummary, list[MarketReplaySessionResult]]
]:
    if _WORKER_SPEC is None:
        raise RuntimeError("Market Replay worker was not initialized.")
    profiles, day_weights = task
    from .market_replay import (
        _evaluate_period_sequence_core,
        _summary,
    )

    output: list[
        tuple[str, MarketReplayCandidateSummary, list[MarketReplaySessionResult]]
    ] = []
    for profile in profiles:
        def atr_provider(
            period: IbrecPeriod,
            _ticks: Sequence[Any],
        ) -> EffectiveAtrSeries:
            path = _worker_atr_path(period, profile)
            key = str(path)
            values = _WORKER_ATR.get(key)
            if values is None:
                values = _load_readonly_atr_array(
                    path,
                    expected_length=len(_ticks),
                )
                _WORKER_ATR[key] = values
            try:
                session_index = _WORKER_SESSION_INDEX[_period_identity(period)]
            except KeyError as exc:
                raise RuntimeError(
                    "Market Replay worker received an unknown effective-array session."
                ) from exc
            return EffectiveAtrSeries(
                values,
                _WORKER_EFFECTIVE_CATALOG,
                session_index,
            )

        sessions, _ = _evaluate_period_sequence_core(
            _WORKER_SPEC.min_tick,
            _WORKER_TICKS,
            profile,
            _WORKER_SPEC.config,
            atr_provider,
            keep_details=False,
            config_is_normalized=True,
        )
        summary_sessions = sessions
        if day_weights is not None:
            summary_sessions = [
                session
                for session in sessions
                for _ in range(
                    max(0, int(day_weights.get(session.session_date, 0)))
                )
            ]
        output.append(
            (
                profile.key(),
                _summary(profile, summary_sessions, _WORKER_SPEC.config),
                sessions,
            )
        )
    return output


def _packaged_spawn_probe(size: int) -> int:
    """Top-level frozen-worker probe used by the Windows release gate."""

    values = np.arange(int(size), dtype=np.int64)
    return int(values.sum())


def packaged_spawn_smoke_test() -> bool:
    """Prove that the frozen executable can spawn and import NumPy workers."""

    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=1, mp_context=context) as pool:
        result = pool.submit(_packaged_spawn_probe, 11).result(timeout=30.0)
    return result == 55


class ProfileBatchEvaluator:
    """Evaluate independent profiles using compact arrays and a persistent pool."""

    def __init__(
        self,
        store: PreparedReplayStore,
        *,
        min_tick: float,
        config: MarketReplayConfig,
        requested_workers: int,
        recording: IbrecRecording | None = None,
    ):
        self.store = store
        self.min_tick = float(min_tick)
        self.config = config.normalized()
        self.recording = recording
        cpu_count = max(1, os.cpu_count() or 1)
        self.requested_workers = int(requested_workers)
        automatic_ceiling = 16
        try:
            automatic_ceiling = int(
                os.environ.get(
                    "BOUNCYBOT_OPTIMIZER_AUTO_WORKER_CEILING",
                    "16",
                )
            )
        except ValueError:
            automatic_ceiling = 16
        estimated_context = max(1, self.store.total_rows * (_TICK_DTYPE.itemsize + 16))
        self.worker_count = (
            memory_aware_worker_count(
                cpu_count=cpu_count,
                available_memory_bytes=_available_memory_bytes(),
                estimated_replay_context_bytes=estimated_context,
                pending_profiles=64,
                automatic_ceiling=automatic_ceiling,
            )
            if requested_workers == 0
            else max(1, min(64, int(requested_workers)))
        )
        self._auto_mode = requested_workers == 0
        if not self._auto_mode:
            self._worker_limit_reason = "manual setting"
        elif self.store.total_rows < 50_000:
            self._worker_limit_reason = "automatic small-workload threshold"
        else:
            self._worker_limit_reason = "automatic CPU/memory limit"
        self._pool: ProcessPoolExecutor | None = None
        self._closed = False
        self._serial_ticks: list[tuple[IbrecPeriod, FastTickSequence]] | None = None
        self._serial_atr: dict[str, NDArray[np.float64]] = {}
        self._effective_catalog = EffectiveArrayCatalog(self.store.effective_root)

    @property
    def parallel_enabled(self) -> bool:
        if self.worker_count <= 1:
            return False
        if not self._auto_mode:
            return True
        return self.store.total_rows >= 50_000

    def _worker_spec(self) -> PreparedWorkerSpec:
        return PreparedWorkerSpec(
            sessions=self.store.sessions,
            atr_root=str(self.store.atr_root),
            effective_root=str(self.store.effective_root),
            min_tick=self.min_tick,
            config=self.config,
            recording=self._worker_recording(),
        )

    def _worker_recording(self) -> IbrecRecording | None:
        """Return compact, pickle-safe recording metadata for generic tasks."""

        recording = self.recording
        if recording is None:
            return None
        return IbrecRecording(
            path=recording.path,
            sha256=recording.sha256,
            size_bytes=recording.size_bytes,
            input_components=[dict(row) for row in recording.input_components],
            container_format=recording.container_format,
            format_version=recording.format_version,
            manifest=dict(recording.manifest),
            contract=dict(recording.contract),
            ticks=[],
            periods=[session.period for session in self.store.sessions],
            issues=list(recording.issues),
            feed_counts=dict(recording.feed_counts),
            raw_row_count=recording.raw_row_count,
            retained_row_count=recording.retained_row_count,
            data_start_utc=recording.data_start_utc,
            data_end_utc=recording.data_end_utc,
            excluded_sessions=[dict(row) for row in recording.excluded_sessions],
            quality_events=[dict(row) for row in recording.quality_events],
            fragment_evidence=[dict(row) for row in recording.fragment_evidence],
        )

    @property
    def execution_label(self) -> str:
        if self.parallel_enabled:
            return (
                f"{self.worker_count} worker processes "
                f"({self._worker_limit_reason})"
            )
        return f"serial execution ({self._worker_limit_reason})"

    def run_analysis_tasks(
        self,
        tasks: Sequence[AnalysisWorkerTask],
        *,
        serial_handler: Callable[[AnalysisWorkerTask], Any],
        progress: Callable[[str, int, int], None] | None,
        message: str,
    ) -> list[Any]:
        """Run independent high-level tasks through the persistent pool.

        Results are restored in the caller's task order. Missing, duplicate,
        unexpected, or mismatched task identifiers abort the analysis.
        """

        if self._closed:
            raise RuntimeError("Market Replay profile evaluator is already closed.")
        ordered = list(tasks)
        if not ordered:
            return []
        task_ids = [task.task_id for task in ordered]
        if any(not task_id for task_id in task_ids) or len(set(task_ids)) != len(task_ids):
            raise ValueError("Market Replay analysis task identifiers must be unique.")

        total = len(ordered)

        def emit(completed: int) -> None:
            if progress is None:
                return
            pending = max(0, total - completed)
            progress(
                f"{message} · {self.execution_label} · {completed}/{total} complete · {pending} pending",
                completed,
                total,
            )

        emit(0)
        results_by_id: dict[str, Any] = {}
        if not self.parallel_enabled or total < 2:
            for index, task in enumerate(ordered, start=1):
                results_by_id[task.task_id] = serial_handler(task)
                emit(index)
        else:
            pool = self._ensure_pool()
            futures = {
                pool.submit(_worker_analysis_task, task): task.task_id
                for task in ordered
            }
            completed = 0
            try:
                for future in as_completed(futures):
                    expected_id = futures[future]
                    returned_id, value = future.result()
                    if returned_id != expected_id:
                        raise RuntimeError(
                            "A Market Replay worker returned a mismatched task identifier."
                        )
                    if returned_id in results_by_id:
                        raise RuntimeError(
                            f"A Market Replay worker returned duplicate task {returned_id!r}."
                        )
                    results_by_id[returned_id] = value
                    completed += 1
                    emit(completed)
            except BaseException:
                for future in futures:
                    future.cancel()
                raise

        missing = [task_id for task_id in task_ids if task_id not in results_by_id]
        extra = sorted(set(results_by_id) - set(task_ids))
        if missing or extra or len(results_by_id) != total:
            raise RuntimeError(
                "Market Replay analysis tasks returned incomplete evidence: "
                f"missing={missing!r}, extra={extra!r}."
            )
        return [results_by_id[task_id] for task_id in task_ids]

    def _ensure_pool(self) -> ProcessPoolExecutor:
        if self._pool is None:
            context = multiprocessing.get_context("spawn")
            self._pool = ProcessPoolExecutor(
                max_workers=self.worker_count,
                mp_context=context,
                initializer=_worker_initialize,
                initargs=(self._worker_spec(),),
            )
        return self._pool

    def _serial_period_ticks(
        self,
    ) -> list[tuple[IbrecPeriod, FastTickSequence]]:
        if self._serial_ticks is None:
            opened: list[tuple[IbrecPeriod, FastTickSequence]] = []
            try:
                for session in self.store.sessions:
                    opened.append(
                        (session.period, FastTickSequence(session.tick_path))
                    )
            except Exception:
                for _period, ticks in opened:
                    ticks.close()
                raise
            self._serial_ticks = opened
        return self._serial_ticks

    def _serial_atr_values(
        self,
        period: IbrecPeriod,
        profile: AtrProfile,
        *,
        expected_length: int,
    ) -> NDArray[np.float64]:
        path = self.store.atr_path(period, profile.period, profile.bar_seconds)
        key = str(path)
        values = self._serial_atr.get(key)
        if values is None:
            values = _load_readonly_atr_array(
                path,
                expected_length=expected_length,
            )
            self._serial_atr[key] = values
        elif len(values) != expected_length:
            raise ValueError(
                "Prepared ATR data no longer matches its compact replay session."
            )
        return values

    def _session_atr_paths(
        self,
        profiles: Sequence[AtrProfile],
    ) -> dict[tuple[int, int, int], Path]:
        windows = sorted({(profile.period, profile.bar_seconds) for profile in profiles})
        return {
            (session.session_index, period, bar_seconds): self.store.atr_path(
                session.period,
                period,
                bar_seconds,
            )
            for session in self.store.sessions
            for period, bar_seconds in windows
        }

    def _prepare_effective_profiles(
        self,
        profiles: Sequence[AtrProfile],
    ) -> tuple[list[AtrProfile], list[int]]:
        self._effective_catalog.prepare(
            profiles,
            session_atr_paths=self._session_atr_paths(profiles),
        )
        return _collapse_profiles(
            profiles,
            self._effective_catalog,
            session_indices=[session.session_index for session in self.store.sessions],
        )

    def _evaluate_serial(
        self,
        profiles: Sequence[AtrProfile],
        summary_day_weights: dict[str, int] | None,
    ) -> list[
        tuple[str, MarketReplayCandidateSummary, list[MarketReplaySessionResult]]
    ]:
        from .market_replay import _evaluate_period_sequence_core, _summary

        output: list[
            tuple[str, MarketReplayCandidateSummary, list[MarketReplaySessionResult]]
        ] = []
        period_ticks = self._serial_period_ticks()
        for profile in profiles:
            def atr_provider(
                period: IbrecPeriod,
                _ticks: Sequence[Any],
            ) -> EffectiveAtrSeries:
                values = self._serial_atr_values(
                    period,
                    profile,
                    expected_length=len(_ticks),
                )
                session_index = self.store.session_spec(period).session_index
                return EffectiveAtrSeries(
                    values,
                    self._effective_catalog,
                    session_index,
                )

            sessions, _ = _evaluate_period_sequence_core(
                self.min_tick,
                period_ticks,
                profile,
                self.config,
                atr_provider,
                keep_details=False,
                config_is_normalized=True,
            )
            summary_sessions = sessions
            if summary_day_weights is not None:
                summary_sessions = [
                    session
                    for session in sessions
                    for _ in range(
                        max(
                            0,
                            int(
                                summary_day_weights.get(
                                    session.session_date,
                                    0,
                                )
                            ),
                        )
                    )
                ]
            output.append(
                (
                    profile.key(),
                    _summary(profile, summary_sessions, self.config),
                    sessions,
                )
            )
        return output

    def evaluate(
        self,
        profiles: Iterable[AtrProfile],
        *,
        summary_day_weights: dict[str, int] | None,
        progress: Callable[[int, int], None] | None,
    ) -> list[
        tuple[str, MarketReplayCandidateSummary, list[MarketReplaySessionResult]]
    ]:
        if self._closed:
            raise RuntimeError("Market Replay profile evaluator is already closed.")
        ordered = sorted(profiles, key=lambda profile: profile.key())
        if not ordered:
            return []
        keyed_profiles: dict[str, AtrProfile] = {}
        for profile in ordered:
            previous = keyed_profiles.setdefault(profile.key(), profile)
            if previous != profile:
                raise RuntimeError(
                    "Distinct ATR profiles produced the same nominal profile key."
                )
        representatives, mapping = self._prepare_effective_profiles(ordered)
        group_sizes = [0 for _profile in representatives]
        for representative_index in mapping:
            group_sizes[representative_index] += 1
        if not self.parallel_enabled or len(representatives) < 2:
            values = self._evaluate_serial(representatives, summary_day_weights)
            if progress is not None:
                completed = 0
                for group_size in group_sizes:
                    completed += group_size
                    progress(min(completed, len(ordered)), len(ordered))
            expanded = expand_evaluation_results(
                ordered,
                representatives,
                mapping,
                values,
            )
            validate_nominal_profile_results(ordered, expanded)
            return expanded

        pool = self._ensure_pool()
        configured_batch = 16
        try:
            configured_batch = int(
                os.environ.get("BOUNCYBOT_OPTIMIZER_PROFILE_BATCH", "16")
            )
        except ValueError:
            configured_batch = 16
        batch_size = adaptive_profile_batch_size(
            total_rows=self.store.total_rows,
            profile_count=len(representatives),
            worker_count=self.worker_count,
            configured_maximum=configured_batch,
        )
        batches = [
            tuple(representatives[start : start + batch_size])
            for start in range(0, len(representatives), batch_size)
        ]
        futures = {
            pool.submit(_worker_evaluate_batch, (batch, summary_day_weights)): batch
            for batch in batches
        }
        completed = 0
        representative_position = {
            profile.key(): index for index, profile in enumerate(representatives)
        }
        results: list[
            tuple[str, MarketReplayCandidateSummary, list[MarketReplaySessionResult]]
        ] = []
        for future in as_completed(futures):
            batch_results = future.result()
            results.extend(batch_results)
            completed += sum(
                group_sizes[representative_position[key]]
                for key, _summary, _sessions in batch_results
            )
            if progress is not None:
                progress(min(completed, len(ordered)), len(ordered))
        results.sort(key=lambda item: item[0])
        expanded = expand_evaluation_results(
            ordered,
            representatives,
            mapping,
            results,
        )
        validate_nominal_profile_results(ordered, expanded)
        return expanded

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pool = self._pool
        self._pool = None
        try:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
        finally:
            if self._serial_ticks is not None:
                for _period, ticks in self._serial_ticks:
                    ticks.close()
                self._serial_ticks = None
            for values in self._serial_atr.values():
                _close_memmap(values)
            self._serial_atr.clear()
            self._effective_catalog.close()

    def __enter__(self) -> "ProfileBatchEvaluator":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
