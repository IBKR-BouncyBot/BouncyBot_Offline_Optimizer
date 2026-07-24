"""Cross-platform atomic publication helpers for completed report directories."""

from __future__ import annotations

import errno
import gc
import os
import time
from pathlib import Path

_TRANSIENT_ERRNOS = {errno.EACCES, errno.EBUSY, errno.EPERM}
_TRANSIENT_WINDOWS_ERRORS = {5, 32, 33}
_DEFAULT_ATTEMPTS = 16
_INITIAL_DELAY_SECONDS = 0.025
_MAX_DELAY_SECONDS = 0.5

# Keep the move callable module-local. Fault-injection tests can then replace
# only the publication operation instead of mutating the process-wide
# ``os.replace`` attribute shared by unrelated standard-library code.
_replace_directory = os.replace


def _is_transient_move_error(error: OSError) -> bool:
    """Return whether a directory move can reasonably succeed after a short retry."""

    return (
        isinstance(error, PermissionError)
        or error.errno in _TRANSIENT_ERRNOS
        or getattr(error, "winerror", None) in _TRANSIENT_WINDOWS_ERRORS
    )


def atomic_publish_directory(
    source: Path,
    destination: Path,
    *,
    attempts: int = _DEFAULT_ATTEMPTS,
) -> None:
    """Atomically rename a completed staging directory, retrying transient Windows locks.

    Antivirus scanners, indexers, and sync clients can hold a newly created report
    directory briefly on Windows and make an otherwise valid ``os.replace`` fail
    with access-denied or sharing-violation errors. Retrying the *same atomic rename*
    preserves publication semantics; this function never falls back to a partial
    copy into the final content-addressed destination.
    """

    source = Path(source)
    destination = Path(destination)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise ValueError("attempts must be a positive integer.")
    if not source.is_dir() or source.is_symlink():
        raise FileNotFoundError(f"Report staging directory does not exist or is unsafe: {source}")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Report destination already exists: {destination}")
    delay = _INITIAL_DELAY_SECONDS
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            _replace_directory(source, destination)
            return
        except OSError as error:
            # A platform wrapper could report an error after the underlying move
            # completed. Treat the observable final state as authoritative.
            if destination.is_dir() and not destination.is_symlink() and not source.exists():
                return
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(
                    f"Report destination appeared during publication: {destination}"
                ) from error
            if not _is_transient_move_error(error) or attempt + 1 >= attempts:
                raise
            last_error = error
            # CPython normally closes directory iterators promptly, but forcing a
            # collection is harmless here and releases any delayed scandir handle
            # before the next Windows rename attempt.
            gc.collect()
            time.sleep(delay)
            delay = min(_MAX_DELAY_SECONDS, delay * 2.0)

    # The loop always returns or raises. Keep a defensive terminal branch for
    # type checkers and future edits.
    if last_error is not None:  # pragma: no cover
        raise last_error
    raise RuntimeError("Atomic report publication did not run.")  # pragma: no cover
