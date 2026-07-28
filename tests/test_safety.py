from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from optimizer.safety import (
    BotFolderLease,
    SourceSafetyError,
    _pid_is_running_windows,
    read_lock_owner,
    readonly_database_snapshot,
    source_paths,
    source_state,
    validate_source,
)
from tests.conftest import create_database


def test_validate_source_requires_database_and_absent_lock(tmp_path: Path) -> None:
    paths = source_paths(tmp_path)
    with pytest.raises(SourceSafetyError, match="database was not found"):
        validate_source(paths)
    create_database(tmp_path, cycles=0)
    assert validate_source(paths) == ["Capture directory 'debug_captures' was not found."]
    paths.bot_lock.write_text("123", encoding="ascii")
    with pytest.raises(SourceSafetyError, match="lock file exists"):
        validate_source(paths)


def test_bot_folder_lease_is_atomic_and_removes_only_owned_lock(tmp_path: Path) -> None:
    lock = tmp_path / "ibkr_trading_bot.lock"
    with BotFolderLease(lock):
        assert lock.exists()
        assert read_lock_owner(lock) is not None
        with pytest.raises(SourceSafetyError, match="lock appeared"):
            BotFolderLease(lock).acquire()
    assert not lock.exists()

    lock.write_text("999999", encoding="ascii")
    lease = BotFolderLease(lock)
    lease.release()
    assert lock.exists()


def test_readonly_snapshot_contains_source_rows_without_modifying_source(tmp_path: Path) -> None:
    database = create_database(tmp_path, cycles=1)
    before = database.read_bytes()
    with readonly_database_snapshot(database) as snapshot:
        assert snapshot.exists()
        with closing(sqlite3.connect(snapshot)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM cycles").fetchone()[0] == 1
            connection.execute("INSERT INTO events(created_at,level,message) VALUES('x','I','snapshot only')")
            connection.commit()
    assert database.read_bytes() == before


def test_readonly_snapshot_includes_committed_wal_without_touching_sidecars(
    tmp_path: Path,
) -> None:
    database = create_database(tmp_path, cycles=0)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "INSERT INTO events(created_at,level,message) VALUES('x','I','wal row')"
        )
        connection.commit()
        wal = Path(f"{database}-wal")
        assert wal.exists()
        before = {
            path.name: path.read_bytes()
            for path in (database, wal, Path(f"{database}-shm"))
            if path.exists()
        }
        with readonly_database_snapshot(database) as snapshot:
            with closing(sqlite3.connect(snapshot)) as copied:
                assert copied.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        after = {
            path.name: path.read_bytes()
            for path in (database, wal, Path(f"{database}-shm"))
            if path.exists()
        }
        assert after == before
    finally:
        connection.close()


def test_readonly_snapshot_rejects_missing_database(tmp_path: Path) -> None:
    with pytest.raises(SourceSafetyError, match="does not exist"):
        with readonly_database_snapshot(tmp_path / "missing.sqlite"):
            pass


def test_additional_source_and_process_safety_paths(tmp_path: Path) -> None:
    import os

    from optimizer.safety import pid_is_running

    file_root = tmp_path / "not-a-directory"
    file_root.write_text("x", encoding="utf-8")
    with pytest.raises(SourceSafetyError, match="does not exist"):
        validate_source(source_paths(file_root))
    assert read_lock_owner(tmp_path / "missing.lock") is None
    invalid = tmp_path / "invalid.lock"
    invalid.write_text("not-a-pid", encoding="ascii")
    assert read_lock_owner(invalid) is None
    assert not pid_is_running(-1)
    assert pid_is_running(os.getpid())


def test_lease_rejects_double_acquire_and_snapshot_rejects_invalid_sqlite(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    lease = BotFolderLease(lock)
    lease.acquire()
    try:
        with pytest.raises(SourceSafetyError, match="already held"):
            lease.acquire()
    finally:
        lease.release()
    invalid = tmp_path / "invalid.sqlite"
    invalid.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(SourceSafetyError, match="read-only"):
        with readonly_database_snapshot(invalid):
            pass


def test_source_state_includes_database_wal_and_shared_memory(tmp_path: Path) -> None:
    database = create_database(tmp_path, cycles=0)
    initial = source_state(database)
    assert initial[0] is not None
    assert initial[1:] == (None, None)

    Path(f"{database}-wal").write_bytes(b"wal")
    Path(f"{database}-shm").write_bytes(b"shared-memory")
    changed = source_state(database)
    assert changed != initial
    assert changed[1] is not None and changed[1][0].endswith("-wal")
    assert changed[2] is not None and changed[2][0].endswith("-shm")


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_sqlite_sidecar_symlink_cannot_escape_database_directory(
    tmp_path: Path,
    suffix: str,
) -> None:
    database = create_database(tmp_path / "bot", cycles=0)
    outside = tmp_path / f"outside{suffix}"
    outside.write_bytes(b"external sidecar")
    sidecar = Path(f"{database}{suffix}")
    try:
        sidecar.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable in this environment")

    with pytest.raises(SourceSafetyError, match="sidecar resolves outside"):
        source_state(database)
    if suffix == "-wal":
        with pytest.raises(SourceSafetyError, match="sidecar resolves outside"):
            with readonly_database_snapshot(database):
                pass


def test_source_state_fails_closed_when_a_file_changes_during_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optimizer.safety as safety

    database = create_database(tmp_path, cycles=0)

    def remove_while_hashing(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
        del chunk_size
        path.unlink()
        return "0" * 64

    monkeypatch.setattr(safety, "sha256_file", remove_while_hashing)
    with pytest.raises(SourceSafetyError, match="changed or became unreadable"):
        source_state(database)


def test_windows_process_probe_uses_pointer_safe_ctypes_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes
    from ctypes import wintypes

    import optimizer.safety as safety

    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    closed: list[int] = []
    large_handle = 0x1_0000_0001

    def get_exit_code(handle, output) -> bool:
        assert int(handle) == large_handle
        ctypes.cast(output, ctypes.POINTER(wintypes.DWORD)).contents.value = 259
        return True

    class FakeKernel32:
        OpenProcess = FakeFunction(lambda access, inherit, pid: large_handle)
        GetExitCodeProcess = FakeFunction(get_exit_code)
        CloseHandle = FakeFunction(lambda handle: closed.append(int(handle)) or True)

    loader_calls: list[tuple[str, bool]] = []

    def fake_windll(name: str, *args, **kwargs):
        loader_calls.append((name, bool(kwargs.get("use_last_error"))))
        return FakeKernel32()

    monkeypatch.setattr(safety.ctypes, "WinDLL", fake_windll, raising=False)
    monkeypatch.setattr(safety.ctypes, "get_last_error", lambda: 0, raising=False)

    assert _pid_is_running_windows(123)
    assert closed == [large_handle]
    # ``use_last_error=True`` is what makes ``ctypes.get_last_error`` observe
    # the saved error after each call; the plain ``windll`` loader never did.
    assert loader_calls == [("kernel32", True)]
    assert FakeKernel32.OpenProcess.restype is wintypes.HANDLE
    assert FakeKernel32.GetExitCodeProcess.argtypes[0] is wintypes.HANDLE


def test_windows_process_probe_reads_saved_last_error_for_denied_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optimizer.safety as safety

    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    class FakeKernel32:
        OpenProcess = FakeFunction(lambda access, inherit, pid: 0)
        GetExitCodeProcess = FakeFunction(lambda handle, output: True)
        CloseHandle = FakeFunction(lambda handle: True)

    monkeypatch.setattr(
        safety.ctypes, "WinDLL", lambda name, **kwargs: FakeKernel32(), raising=False
    )

    # ACCESS_DENIED(5) proves a live process the caller may not open: running.
    monkeypatch.setattr(safety.ctypes, "get_last_error", lambda: 5, raising=False)
    assert _pid_is_running_windows(123) is True

    # Any other saved error (invalid parameter) means no such process.
    monkeypatch.setattr(safety.ctypes, "get_last_error", lambda: 87, raising=False)
    assert _pid_is_running_windows(123) is False


def test_capture_state_rejects_archive_symlink_outside_source(tmp_path: Path) -> None:
    from optimizer.safety import capture_source_state

    root = tmp_path / "debug_captures"
    root.mkdir()
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"outside")
    link = root / "linked.zip"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")
    with pytest.raises(SourceSafetyError, match="outside debug_captures"):
        capture_source_state(root)


def test_lease_write_failure_removes_the_newly_created_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    lock = tmp_path / "ibkr_trading_bot.lock"

    def fail_write(fd: int, data: bytes) -> int:
        raise OSError("simulated write failure")

    monkeypatch.setattr(os, "write", fail_write)
    with pytest.raises(OSError, match="simulated write failure"):
        BotFolderLease(lock).acquire()
    assert not lock.exists()


def test_validate_source_rejects_database_or_capture_symlink_escape(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bot"
    source.mkdir()
    outside_database = create_database(tmp_path / "outside-database", cycles=0)
    database_link = source / "bot_state.sqlite"
    try:
        database_link.symlink_to(outside_database)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable in this environment")

    with pytest.raises(SourceSafetyError, match="database resolves outside"):
        validate_source(source_paths(source))

    database_link.unlink()
    create_database(source, cycles=0)
    outside_captures = tmp_path / "outside-captures"
    outside_captures.mkdir()
    (source / "debug_captures").symlink_to(outside_captures, target_is_directory=True)
    with pytest.raises(SourceSafetyError, match="Capture directory resolves outside"):
        validate_source(source_paths(source))
