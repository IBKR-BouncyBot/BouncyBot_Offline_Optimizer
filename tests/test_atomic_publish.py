from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from optimizer import atomic_publish
from optimizer.analysis import run_analysis
from optimizer.atomic_publish import atomic_publish_directory
from optimizer.models import AnalysisConfig
from optimizer.reports import write_reports
from tests.conftest import create_source_fixture


def test_atomic_publish_retries_transient_permission_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / ".report.partial"
    destination = tmp_path / "report"
    source.mkdir()
    (source / "evidence.txt").write_text("complete", encoding="utf-8")
    calls = 0
    injected_failures = 0
    completed_moves: list[tuple[Path, Path]] = []
    sleeps: list[float] = []

    def flaky_replace(left: Path, right: Path) -> None:
        nonlocal calls, injected_failures
        calls += 1
        if injected_failures < 2:
            injected_failures += 1
            raise PermissionError(errno.EACCES, "simulated Windows scanner lock")
        completed_moves.append((Path(left), Path(right)))

    monkeypatch.setattr(atomic_publish, "_replace_directory", flaky_replace)
    monkeypatch.setattr(atomic_publish.time, "sleep", sleeps.append)

    atomic_publish_directory(source, destination, attempts=4)

    # This unit test uses a fully simulated move so the retry sequence is
    # deterministic and independent of antivirus/indexer activity.
    assert injected_failures == 2
    assert calls == 3
    assert sleeps == [0.025, 0.05]
    assert completed_moves == [(source, destination)]


def test_atomic_publish_does_not_hide_non_transient_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / ".report.partial"
    destination = tmp_path / "report"
    source.mkdir()
    sleeps: list[float] = []

    def broken_replace(left: Path, right: Path) -> None:
        del left, right
        raise FileNotFoundError(errno.ENOENT, "simulated non-transient failure")

    monkeypatch.setattr(atomic_publish, "_replace_directory", broken_replace)
    monkeypatch.setattr(atomic_publish.time, "sleep", sleeps.append)

    with pytest.raises(FileNotFoundError):
        atomic_publish_directory(source, destination, attempts=4)

    assert sleeps == []
    assert source.is_dir()
    assert not destination.exists()


def test_sqlite_report_publication_retries_windows_directory_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = create_source_fixture(tmp_path / "bot", cycles=2)
    real_replace = os.replace
    attempts: list[tuple[Path, Path]] = []
    injected_failures = 0

    def flaky_replace(left: Path, right: Path) -> None:
        nonlocal injected_failures
        attempts.append((Path(left), Path(right)))
        if injected_failures < 2:
            injected_failures += 1
            raise PermissionError(errno.EACCES, "simulated Windows scanner lock")
        real_replace(left, right)

    monkeypatch.setattr(atomic_publish, "_replace_directory", flaky_replace)

    result = write_reports(run_analysis(AnalysisConfig(source, tmp_path / "reports")))

    # This is an integration test, so do not assume that the real Windows move
    # succeeds on the first non-injected attempt. Antivirus, indexing, and sync
    # software may add another legitimate transient denial. Verify the injected
    # failures, the bounded retry behavior, and that every attempt targeted the
    # one final report publication instead of asserting an environment-specific
    # exact call count.
    assert injected_failures == 2
    assert 3 <= len(attempts) <= atomic_publish._DEFAULT_ATTEMPTS
    assert len({left for left, _right in attempts}) == 1
    assert {right for _left, right in attempts} == {result.output_dir}
    assert os.replace is real_replace
    assert result.output_dir.is_dir()
    assert (result.output_dir / "index.html").is_file()


def test_atomic_publish_retries_windows_sharing_violation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / ".report.partial"
    destination = tmp_path / "report"
    source.mkdir()
    calls = 0
    injected_failures = 0
    completed_moves: list[tuple[Path, Path]] = []

    def sharing_violation_once(left: Path, right: Path) -> None:
        nonlocal calls, injected_failures
        calls += 1
        if injected_failures == 0:
            injected_failures += 1
            error = OSError("simulated Windows sharing violation")
            error.winerror = 32
            raise error
        completed_moves.append((Path(left), Path(right)))

    monkeypatch.setattr(atomic_publish, "_replace_directory", sharing_violation_once)
    monkeypatch.setattr(atomic_publish.time, "sleep", lambda _seconds: None)

    atomic_publish_directory(source, destination, attempts=2)

    assert injected_failures == 1
    assert calls == 2
    assert completed_moves == [(source, destination)]


def test_atomic_publish_accepts_observed_success_after_wrapper_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / ".report.partial"
    destination = tmp_path / "report"
    source.mkdir()
    (source / "complete.txt").write_text("yes", encoding="utf-8")
    real_replace = os.replace

    def move_then_report_error(left: Path, right: Path) -> None:
        real_replace(left, right)
        raise PermissionError(errno.EACCES, "simulated post-move wrapper error")

    monkeypatch.setattr(atomic_publish, "_replace_directory", move_then_report_error)

    atomic_publish_directory(source, destination, attempts=16)

    assert not source.exists()
    assert (destination / "complete.txt").read_text(encoding="utf-8") == "yes"


def test_atomic_publish_validates_arguments_and_paths(tmp_path: Path) -> None:
    source = tmp_path / ".report.partial"
    destination = tmp_path / "report"

    with pytest.raises(ValueError, match="positive integer"):
        atomic_publish_directory(source, destination, attempts=0)
    with pytest.raises(ValueError, match="positive integer"):
        atomic_publish_directory(source, destination, attempts=True)
    with pytest.raises(FileNotFoundError, match="does not exist or is unsafe"):
        atomic_publish_directory(source, destination)

    source.mkdir()
    destination.mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        atomic_publish_directory(source, destination)


def test_atomic_publish_exhausts_transient_retries_without_partial_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / ".report.partial"
    destination = tmp_path / "report"
    source.mkdir()
    (source / "complete.txt").write_text("yes", encoding="utf-8")
    sleeps: list[float] = []

    def always_locked(left: Path, right: Path) -> None:
        del left, right
        raise PermissionError(errno.EACCES, "simulated persistent Windows lock")

    monkeypatch.setattr(atomic_publish, "_replace_directory", always_locked)
    monkeypatch.setattr(atomic_publish.time, "sleep", sleeps.append)

    with pytest.raises(PermissionError, match="persistent Windows lock"):
        atomic_publish_directory(source, destination, attempts=3)

    assert sleeps == [0.025, 0.05]
    assert source.is_dir()
    assert not destination.exists()
