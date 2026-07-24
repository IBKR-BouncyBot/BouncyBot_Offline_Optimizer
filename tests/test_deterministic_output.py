from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from optimizer.analysis import AnalysisError, run_analysis
from optimizer.models import AnalysisConfig
from optimizer.reports import write_reports
from tests.conftest import create_source_fixture


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_identical_input_reuses_identical_content_addressed_report(
    tmp_path: Path,
) -> None:
    source = create_source_fixture(tmp_path / "bot", cycles=3)
    config = AnalysisConfig(source, tmp_path / "reports")

    first = write_reports(run_analysis(config))
    first_bytes = _tree_bytes(first.output_dir)
    second = write_reports(run_analysis(config))

    assert second.analysis_id == first.analysis_id
    assert second.run_id == first.run_id
    assert second.output_dir == first.output_dir
    assert _tree_bytes(second.output_dir) == first_bytes
    assert [path.name for path in (tmp_path / "reports").iterdir()] == [first.run_id]


def test_report_bytes_do_not_depend_on_source_or_output_absolute_path(
    tmp_path: Path,
) -> None:
    source_a = create_source_fixture(tmp_path / "source-a", cycles=2)
    source_b = tmp_path / "different parent" / "source-b"
    shutil.copytree(source_a, source_b)

    first = write_reports(
        run_analysis(AnalysisConfig(source_a, tmp_path / "reports-a"))
    )
    second = write_reports(
        run_analysis(AnalysisConfig(source_b, tmp_path / "other" / "reports-b"))
    )

    assert first.analysis_id == second.analysis_id
    assert first.run_id == second.run_id
    assert _tree_bytes(first.output_dir) == _tree_bytes(second.output_dir)
    combined = b"\n".join(_tree_bytes(first.output_dir).values())
    assert str(source_a).encode() not in combined
    assert str(first.output_dir.parent).encode() not in combined


def test_report_identity_and_bytes_ignore_source_mtime_only_changes(
    tmp_path: Path,
) -> None:
    source = create_source_fixture(tmp_path / "bot-mtime", cycles=2)
    first = write_reports(
        run_analysis(AnalysisConfig(source, tmp_path / "reports-before"))
    )

    paths = [source / "bot_state.sqlite", *sorted((source / "debug_captures").rglob("*.zip"))]
    for index, path in enumerate(paths, start=1):
        stat = path.stat()
        os.utime(
            path,
            ns=(stat.st_atime_ns + index * 1_000_000, stat.st_mtime_ns + index * 1_000_000),
        )

    second = write_reports(
        run_analysis(AnalysisConfig(source, tmp_path / "reports-after"))
    )

    assert second.analysis_id == first.analysis_id
    assert second.run_id == first.run_id
    assert _tree_bytes(second.output_dir) == _tree_bytes(first.output_dir)


def test_existing_content_addressed_directory_must_be_byte_identical(
    source_fixture: Path,
    tmp_path: Path,
) -> None:
    result = write_reports(
        run_analysis(AnalysisConfig(source_fixture, tmp_path / "reports"))
    )
    (result.output_dir / "index.html").write_text("tampered", encoding="utf-8")

    with pytest.raises(FileExistsError, match="same content-derived analysis ID"):
        write_reports(
            run_analysis(AnalysisConfig(source_fixture, tmp_path / "reports"))
        )


def test_manifest_identity_is_content_derived_and_has_relative_file_names(
    source_fixture: Path,
    tmp_path: Path,
) -> None:
    result = write_reports(
        run_analysis(AnalysisConfig(source_fixture, tmp_path / "reports"))
    )
    manifest_bytes = (result.output_dir / "analysis_manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["analysis_id"] == result.input_fingerprint
    assert manifest["run_id"] == f"optimizer_{result.input_fingerprint[:16]}"
    assert manifest["generated_at_utc"] == manifest["data_through_utc"]
    assert all(not Path(path).is_absolute() for path in manifest["files_written"])
    assert hashlib.sha256(manifest_bytes).hexdigest()


def test_capture_set_change_during_analysis_aborts(
    source_fixture: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    import optimizer.analysis as analysis_module

    real_state = analysis_module.capture_source_state(source_fixture / "debug_captures")
    states = iter((real_state, real_state + (("new.zip", 1, 1, "0" * 64),)))
    monkeypatch.setattr(analysis_module, "capture_source_state", lambda root: next(states))

    with pytest.raises(AnalysisError, match="debug_captures archive set changed"):
        run_analysis(AnalysisConfig(source_fixture, tmp_path / "reports"))
    assert not (source_fixture / "ibkr_trading_bot.lock").exists()


def test_identical_input_is_byte_identical_across_process_hash_seeds(
    tmp_path: Path,
) -> None:
    source = create_source_fixture(tmp_path / "bot-subprocess", cycles=3)
    project_root = Path(__file__).resolve().parents[1]
    report_trees: list[dict[str, bytes]] = []

    for seed in ("1", "987654"):
        output_root = tmp_path / f"reports-{seed}"
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        # Keep the child process independent from whichever coverage runner is
        # executing this test. Some Python environments install coverage or
        # tracing startup hooks that react to these inherited variables and
        # can overwrite the parent process's coverage data file at child exit.
        for variable in tuple(environment):
            if (
                variable == "COVERAGE_RUN"
                or variable == "COVERAGE_FILE"
                or variable.startswith("COVERAGE_PROCESS_")
                or variable.startswith("COV_CORE_")
            ):
                environment.pop(variable, None)
        completed = subprocess.run(
            [
                sys.executable,
                str(project_root / "main.py"),
                "--no-gui",
                "--yes",
                "--json-result",
                "--source-dir",
                str(source),
                "--output-dir",
                str(output_root),
            ],
            cwd=project_root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        payload = json.loads(completed.stdout)
        report_trees.append(_tree_bytes(Path(payload["output_dir"])))

    assert report_trees[0] == report_trees[1]
