"""Read-only source validation and coordination with the live trading bot."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .models import SourcePaths


class SourceSafetyError(RuntimeError):
    """Raised when analysis cannot proceed without risking concurrent access."""


def source_paths(root: Path) -> SourcePaths:
    resolved = Path(root).expanduser().resolve()
    return SourcePaths(
        root=resolved,
        database=resolved / "bot_state.sqlite",
        captures=resolved / "debug_captures",
        bot_lock=resolved / "ibkr_trading_bot.lock",
    )


def validate_database_source(paths: SourcePaths) -> list[str]:
    """Validate a stopped BouncyBot folder when only SQLite is required.

    Market Replay execution calibration does not consume ``debug_captures``.
    Keeping that narrower boundary separate prevents an absent capture folder
    from being reported as a calibration defect while retaining the same
    database, symlink, and lock protections as the full bot-data workflow.
    """

    if not paths.root.exists() or not paths.root.is_dir():
        raise SourceSafetyError(f"Source directory does not exist: {paths.root}")
    if not paths.database.exists() or not paths.database.is_file():
        raise SourceSafetyError(f"SQLite database was not found: {paths.database}")
    root = paths.root.resolve()
    database = paths.database.resolve()
    if root not in database.parents:
        raise SourceSafetyError(
            f"SQLite database resolves outside the selected source directory: {paths.database}"
        )
    if paths.bot_lock.exists():
        owner = read_lock_owner(paths.bot_lock)
        suffix = f" (PID {owner})" if owner else ""
        raise SourceSafetyError(
            "The trading-bot lock file exists"
            f"{suffix}: {paths.bot_lock}. Close the bot and confirm the lock is gone before analysis."
        )
    return []


def validate_source(paths: SourcePaths) -> list[str]:
    """Validate the complete SQLite-and-captures analysis source."""

    validate_database_source(paths)
    root = paths.root.resolve()
    warnings: list[str] = []
    if not paths.captures.exists():
        warnings.append("Capture directory 'debug_captures' was not found.")
    elif not paths.captures.is_dir():
        warnings.append("Capture path 'debug_captures' is not a directory.")
    elif root not in paths.captures.resolve().parents:
        raise SourceSafetyError(
            f"Capture directory resolves outside the selected source directory: {paths.captures}"
        )
    return warnings


def read_lock_owner(path: Path) -> int | None:
    try:
        text = Path(path).read_text(encoding="ascii", errors="ignore").strip()
        value = int(text)
        return value if value > 0 else None
    except (OSError, TypeError, ValueError):
        return None


def _pid_is_running_windows(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
    except Exception:
        return True
    # Explicit signatures are required on 64-bit Windows.  Without them ctypes
    # assumes C ``int`` return values and can truncate a HANDLE before it is
    # passed to GetExitCodeProcess or CloseHandle.
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return bool(get_last_error() == error_access_denied)
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return int(exit_code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)


def pid_is_running(pid: int) -> bool:
    """Best-effort process existence check without sending Windows signals."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_is_running_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return getattr(exc, "errno", None) != errno.ESRCH
    return True


@dataclass(slots=True)
class BotFolderLease:
    """Temporarily own the bot's normal lock while making a read-only snapshot.

    The optimizer never removes a pre-existing lock, including a potentially
    stale one. It creates the same lock only after explicit user confirmation,
    preventing a normal bot launch from starting in the same portable folder
    during analysis. The file is removed only when owned by this process.
    """

    path: Path
    fd: int | None = None
    acquired: bool = False

    def acquire(self) -> None:
        if self.acquired:
            raise SourceSafetyError("The analysis lease is already held.")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            self.fd = os.open(str(self.path), flags, 0o600)
            self.acquired = True
            try:
                os.write(self.fd, str(os.getpid()).encode("ascii"))
                try:
                    os.fsync(self.fd)
                except OSError:
                    pass
            except Exception:
                # O_EXCL proves this process created the file. Remove it even
                # when writing the PID failed and ownership cannot be read back.
                if self.fd is not None:
                    os.close(self.fd)
                    self.fd = None
                self.path.unlink(missing_ok=True)
                self.acquired = False
                raise
        except FileExistsError as exc:
            owner = read_lock_owner(self.path)
            suffix = f" (PID {owner})" if owner else ""
            raise SourceSafetyError(
                f"The trading-bot lock appeared before analysis could start{suffix}: {self.path}"
            ) from exc
        except Exception:
            self.release()
            raise

    def release(self) -> None:
        try:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
        finally:
            if self.acquired:
                try:
                    owner = read_lock_owner(self.path)
                    if owner == os.getpid():
                        self.path.unlink(missing_ok=True)
                finally:
                    self.acquired = False

    def __enter__(self) -> "BotFolderLease":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _stable_file_state(path: Path) -> tuple[str, int, int, str]:
    """Hash one file and reject a change that occurred while it was read."""

    try:
        before = path.stat()
        digest = sha256_file(path)
        after = path.stat()
    except OSError as exc:
        raise SourceSafetyError(
            f"Source file changed or became unreadable during analysis: {path}"
        ) from exc
    before_key = (int(before.st_size), int(before.st_mtime_ns), int(before.st_ino))
    after_key = (int(after.st_size), int(after.st_mtime_ns), int(after.st_ino))
    if before_key != after_key:
        raise SourceSafetyError(
            f"Source file changed while it was being fingerprinted: {path}"
        )
    return path.name, int(after.st_size), int(after.st_mtime_ns), digest


def _safe_sqlite_sidecar(database: Path, suffix: str) -> Path:
    """Return an existing SQLite sidecar only when it stays beside the database.

    A portable-folder sidecar can technically be a symlink. Following a link
    outside the selected bot directory would violate the optimizer's read-only
    source boundary, so both fingerprinting and snapshot staging use this same
    containment check.
    """

    sidecar = Path(f"{database}{suffix}")
    if not sidecar.exists():
        return sidecar
    try:
        resolved = sidecar.resolve(strict=True)
    except OSError as exc:
        raise SourceSafetyError(f"Could not resolve SQLite sidecar: {sidecar}") from exc
    if resolved.parent != database.parent:
        raise SourceSafetyError(
            f"SQLite sidecar resolves outside the database directory: {sidecar}"
        )
    return sidecar


def source_state(database: Path) -> tuple[tuple[str, int, int, str] | None, ...]:
    """Return stable metadata for SQLite, WAL, and shared-memory files.

    The live bot lock is the primary concurrency control. This additional state
    check detects an unexpected writer that ignored the lock while analysis was
    reading the stopped portable folder.
    """
    database = Path(database).resolve()
    paths = (
        database,
        _safe_sqlite_sidecar(database, "-wal"),
        _safe_sqlite_sidecar(database, "-shm"),
    )
    result: list[tuple[str, int, int, str] | None] = []
    for path in paths:
        try:
            path.stat()
        except FileNotFoundError:
            result.append(None)
        except OSError as exc:
            raise SourceSafetyError(f"Could not inspect source file: {path}") from exc
        else:
            result.append(_stable_file_state(path))
    return tuple(result)


def capture_source_state(root: Path) -> tuple[tuple[str, int, int, str], ...]:
    """Fingerprint every capture archive without following data outside root."""
    root = Path(root).resolve()
    if not root.exists() or not root.is_dir():
        return ()
    result: list[tuple[str, int, int, str]] = []
    for path in sorted(
        (item for item in root.rglob("*.zip") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        resolved = path.resolve()
        if root not in resolved.parents:
            raise SourceSafetyError(
                f"Capture archive resolves outside debug_captures: {path.name}"
            )
        _, size, modified_ns, digest = _stable_file_state(path)
        result.append((path.relative_to(root).as_posix(), size, modified_ns, digest))
    return tuple(result)


@contextmanager
def readonly_database_snapshot(database: Path) -> Iterator[Path]:
    """Create a consistent temporary SQLite snapshot without opening source in SQLite.

    SQLite may create or update a ``-shm`` sidecar even for a ``mode=ro`` WAL
    connection when the WAL index must be rebuilt. To keep the bot folder
    byte-for-byte read-only, copy the main database and WAL into a private
    temporary directory first. SQLite recovery and the online-backup operation
    then touch only those temporary files.
    """
    database = Path(database).resolve()
    if not database.exists():
        raise SourceSafetyError(f"SQLite database does not exist: {database}")
    with tempfile.TemporaryDirectory(prefix="bouncybot_optimizer_") as temp_name:
        temp_root = Path(temp_name)
        staged = temp_root / database.name
        snapshot = temp_root / "bot_state_snapshot.sqlite"
        try:
            shutil.copyfile(database, staged)
            source_wal = _safe_sqlite_sidecar(database, "-wal")
            if source_wal.exists():
                shutil.copyfile(source_wal, Path(f"{staged}-wal"))
        except OSError as exc:
            raise SourceSafetyError(
                f"Could not copy the SQLite source files read-only: {exc}"
            ) from exc
        try:
            source = sqlite3.connect(staged, timeout=10.0)
        except sqlite3.Error as exc:
            raise SourceSafetyError(
                f"Could not open the temporary read-only source copy: {exc}"
            ) from exc
        try:
            source.execute("PRAGMA query_only = ON")
            destination = sqlite3.connect(snapshot, timeout=10.0)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
        except sqlite3.Error as exc:
            raise SourceSafetyError(
                f"Could not create the temporary read-only analysis snapshot: {exc}"
            ) from exc
        finally:
            source.close()
        yield snapshot
