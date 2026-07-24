from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import zipfile
from pathlib import Path
from typing import Any

import pytest

import optimizer.market_replay as market_replay
from optimizer.market_replay import (
    _candidate_robustness,
    _market_replay_bootstrap_seed,
    _paired_sessions_by_day,
    _stable_region_centers,
    _summary,
    run_market_replay_analysis,
)
from optimizer.market_replay_models import (
    AtrProfile,
    MarketReplayCandidateSummary,
    MarketReplayConfig,
    MarketReplaySessionResult,
)
from scripts.create_reproducible_zip import create_zip
from scripts.create_source_manifest import (
    is_release_source_path,
    release_source_files,
    source_manifest,
)
from tests.market_replay_fixtures import make_ticks, write_v3

ROOT = Path(__file__).resolve().parents[1]
CONTROL = AtrProfile(14, 60, 1.50, 0.75, 1.00, 1.00)


def _session(day: str, return_bps: float) -> MarketReplaySessionResult:
    return MarketReplaySessionResult(
        session_date=day,
        period_id=int(day[-2:]),
        scheduled_open_utc=f"{day}T13:30:00Z",
        scheduled_close_utc=f"{day}T20:00:00Z",
        observed_start_utc=f"{day}T13:30:00Z",
        observed_end_utc=f"{day}T20:00:00Z",
        ticks=100,
        trades=1,
        completed_trades=1,
        no_trade=False,
        open_position=False,
        realized_return_bps=return_bps,
        marked_return_bps=return_bps,
        conservative_return_bps=return_bps,
        max_drawdown_bps=0.0,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_market_replay_uses_three_stage_two_dimensional_window_search(
    tmp_path: Path,
) -> None:
    ticks, periods = make_ticks(sessions=5)
    recording = write_v3(tmp_path / "week.ibrec", ticks, periods)
    result = run_market_replay_analysis(
        MarketReplayConfig(recording, tmp_path / "reports")
    )

    stage1 = [row for row in result.window_search if row["stage"] == "1_bar_duration"]
    stage2 = [row for row in result.window_search if row["stage"] == "2_period"]
    stage3 = [
        row
        for row in result.window_search
        if row["stage"] == "3_multiplier_search_windows"
    ]
    assert {(row["period"], row["bar_seconds"]) for row in stage1} == {
        (14, 15),
        (14, 30),
        (14, 60),
        (14, 120),
    }
    advanced_bars = {
        row["bar_seconds"]
        for row in stage1
        if row["selected_for_next_stage"]
    }
    assert 60 in advanced_bars
    assert 2 <= len(advanced_bars) <= 3
    assert {row["period"] for row in stage2}.issubset({5, 7, 10, 14, 21, 28})
    assert (14, 60) in {
        (row["period"], row["bar_seconds"])
        for row in stage2
        if row["selected_for_next_stage"]
    }
    selected_windows = {(row["period"], row["bar_seconds"]) for row in stage3}
    assert len(selected_windows) == 3
    assert (14, 60) in selected_windows
    assert {
        (candidate.profile.period, candidate.profile.bar_seconds)
        for candidate in result.candidates
    }.issubset(selected_windows)
    assert result.recommendation.leave_one_day_out_selection_runs == 5
    assert (
        result.recommendation.leave_one_day_out_same_window_selection_pct
        is not None
    )


def test_trading_day_bootstrap_and_leave_one_out_are_deterministic() -> None:
    days = [f"2026-07-{day:02d}" for day in range(6, 11)]
    control_sessions = [_session(day, 0.0) for day in days]
    candidate_sessions = [_session(day, 20.0 + index) for index, day in enumerate(days)]
    candidate_profile = AtrProfile(14, 60, 1.50, 0.75, 1.00, 0.75)
    candidate = _summary(candidate_profile, candidate_sessions)
    control = _summary(CONTROL, control_sessions)

    first, first_rows = _candidate_robustness(
        candidate,
        candidate_sessions,
        control,
        control_sessions,
        seed="deterministic-week",
    )
    second, second_rows = _candidate_robustness(
        candidate,
        list(reversed(candidate_sessions)),
        control,
        list(reversed(control_sessions)),
        seed="deterministic-week",
    )
    assert first == second
    assert first_rows == second_rows
    assert first["bootstrap_replicates"] == 2_000
    assert first["bootstrap_ci80_low"] > 0.0
    assert first["bootstrap_probability_positive_pct"] == 100.0
    assert first["leave_one_day_out_min_delta"] > 0.0
    assert first["leave_one_day_out_sign_reversals"] == 0
    assert first["passed"] is True


def test_market_replay_centers_share_one_candidate_independent_bootstrap_schedule() -> None:
    first = _market_replay_bootstrap_seed("a" * 64, CONTROL)
    second = _market_replay_bootstrap_seed("a" * 64, CONTROL)
    other_recording = _market_replay_bootstrap_seed("b" * 64, CONTROL)
    assert first == second
    assert first != other_recording
    assert "shared-trading-day-bootstrap" in first


def test_large_near_best_multiplier_plateau_is_one_stable_region() -> None:
    profiles = [
        AtrProfile(14, 60, initial, buy, profit, sell)
        for initial in (0.75, 1.00, 1.25, 1.50)
        for buy in (0.00, 0.25, 0.50, 0.75)
        for profit in (0.50, 0.75, 1.00, 1.25)
        for sell in (0.00, 0.25, 0.50, 0.75)
    ]
    candidates = [
        MarketReplayCandidateSummary(
            profile=profile,
            score=100.0,
            sessions=5,
            completed_sessions=5,
            sessions_with_trades=5,
            completed_trades=5,
            open_position_sessions=0,
            no_trade_sessions=0,
            median_return_bps=100.0,
            mean_return_bps=100.0,
            worst_return_bps=100.0,
            maximum_drawdown_bps=0.0,
            open_position_rate_pct=0.0,
            no_trade_rate_pct=0.0,
        )
        for profile in profiles
    ]
    centers = _stable_region_centers(candidates, sessions=5)
    assert len(centers) == 1
    selected, _reason = centers[0]
    assert selected.stable_region_size == len(profiles)
    assert selected.stable_region_center is True


def test_bootstrap_and_leave_one_out_cluster_multiple_periods_by_trading_day() -> None:
    days = [f"2026-07-{day:02d}" for day in range(6, 11)]
    control_sessions: list[MarketReplaySessionResult] = []
    candidate_sessions: list[MarketReplaySessionResult] = []
    for day_index, day in enumerate(days):
        for period_offset in (0, 100):
            control = _session(day, 0.0)
            control.period_id += period_offset
            candidate = _session(day, 10.0 + day_index)
            candidate.period_id += period_offset
            control_sessions.append(control)
            candidate_sessions.append(candidate)

    clusters = _paired_sessions_by_day(candidate_sessions, control_sessions)
    assert list(clusters) == days
    assert all(len(cluster) == 2 for cluster in clusters.values())
    evidence, rows = _candidate_robustness(
        _summary(AtrProfile(14, 60, 1.50, 0.75, 1.00, 0.75), candidate_sessions),
        candidate_sessions,
        _summary(CONTROL, control_sessions),
        control_sessions,
        seed="two-period-day-clusters",
    )
    assert evidence["paired_sessions"] == 10
    assert evidence["paired_trading_days"] == 5
    assert len(rows) == 5
    assert all(row["remaining_trading_days"] == 4 for row in rows)


def test_leave_one_day_out_window_instability_rejects_changed_profile() -> None:
    days = [f"2026-07-{day:02d}" for day in range(6, 11)]
    control_sessions = [_session(day, 0.0) for day in days]
    candidate_sessions = [_session(day, 20.0) for day in days]
    candidate_profile = AtrProfile(21, 30, 1.50, 0.75, 1.00, 0.75)
    selection_rows = [
        {
            "omitted_trading_day": day,
            "selected_windows": (
                [{"period": 21, "bar_seconds": 30}]
                if index < 2
                else [{"period": 14, "bar_seconds": 60}]
            ),
            "selected_profile_key": CONTROL.key(),
        }
        for index, day in enumerate(days)
    ]
    evidence, _ = _candidate_robustness(
        _summary(candidate_profile, candidate_sessions),
        candidate_sessions,
        _summary(CONTROL, control_sessions),
        control_sessions,
        seed="window-instability",
        selection_rows=selection_rows,
    )
    assert evidence["same_atr_window_selection_pct"] == 40.0
    assert evidence["passed"] is False
    assert any(
        "atr period/bar window" in reason.lower()
        for reason in evidence["failure_reasons"]
    )


def test_leave_one_day_out_rejects_a_single_day_driven_result() -> None:
    days = [f"2026-07-{day:02d}" for day in range(6, 11)]
    control_sessions = [_session(day, 0.0) for day in days]
    candidate_sessions = [
        _session(day, 100.0 if index == 4 else 0.0)
        for index, day in enumerate(days)
    ]
    candidate_profile = AtrProfile(14, 60, 1.50, 0.75, 1.00, 0.75)
    evidence, rows = _candidate_robustness(
        _summary(candidate_profile, candidate_sessions),
        candidate_sessions,
        _summary(CONTROL, control_sessions),
        control_sessions,
        seed="one-day-dominates",
    )
    friday = rows[-1]
    assert friday["omitted_trading_day"] == days[-1]
    assert friday["score_delta"] == 0.0
    assert evidence["leave_one_day_out_min_delta"] == 0.0
    assert evidence["passed"] is False
    assert any(
        "leave-one-day-out" in reason.lower()
        for reason in evidence["failure_reasons"]
    )


def test_unstable_changed_candidate_falls_back_to_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ticks, periods = make_ticks(sessions=5)
    recording = write_v3(tmp_path / "week.ibrec", ticks, periods)

    def reject_all(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return (
            {
                "paired_trading_days": 5,
                "observed_score_delta": 10.0,
                "bootstrap_replicates": 2_000,
                "bootstrap_ci80_low": -1.0,
                "bootstrap_ci80_high": 20.0,
                "bootstrap_probability_positive_pct": 70.0,
                "leave_one_day_out_estimates": 5,
                "leave_one_day_out_min_delta": -2.0,
                "leave_one_day_out_median_delta": 5.0,
                "leave_one_day_out_max_delta": 12.0,
                "leave_one_day_out_positive_pct": 80.0,
                "leave_one_day_out_sign_reversals": 1,
                "leave_one_day_out_most_influential_day": "2026-07-10",
                "leave_one_day_out_largest_change": 12.0,
                "passed": False,
                "failure_reasons": ["Injected unstable evidence."],
            },
            [],
        )

    monkeypatch.setattr(market_replay, "_candidate_robustness", reject_all)
    result = run_market_replay_analysis(
        MarketReplayConfig(recording, tmp_path / "reports")
    )
    assert result.recommendation.profile == CONTROL
    assert result.recommendation.evidence_stable is False
    assert "unchanged BouncyBot control" in result.recommendation_reason


def test_reproducible_zip_ignores_source_mtimes(tmp_path: Path) -> None:
    source = tmp_path / "tree"
    (source / "nested").mkdir(parents=True)
    (source / "alpha.txt").write_text("alpha\n", encoding="utf-8")
    (source / "nested" / "beta.txt").write_text("beta\n", encoding="utf-8")
    executable = source / "run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    # Windows cannot persist a POSIX execute bit. A shebang therefore needs to
    # produce the same portable ZIP metadata even when stat reports 0644.
    executable.chmod(0o644)
    plain_shell = source / "plain.sh"
    plain_shell.write_text("echo plain\n", encoding="utf-8")
    plain_shell.chmod(0o644)
    windows_executable = source / "app.exe"
    windows_executable.write_bytes(b"MZ\x00\x00")
    windows_script = source / "build.ps1"
    windows_script.write_text("Write-Host 'build'\n", encoding="utf-8")
    windows_script.chmod(0o644)
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    create_zip(source, first, root_name="release", epoch=1_784_505_600)
    for index, path in enumerate(sorted(source.rglob("*")), start=1):
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
    create_zip(source, second, root_name="release", epoch=1_784_505_600)
    assert _sha256(first) == _sha256(second)
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == sorted(archive.namelist())
        assert all(info.date_time == (2026, 7, 20, 0, 0, 0) for info in archive.infolist())
        modes = {
            info.filename: (info.external_attr >> 16) & 0o777
            for info in archive.infolist()
        }
        assert modes["release/run.sh"] == 0o755
        assert modes["release/app.exe"] == 0o755
        assert modes["release/plain.sh"] == 0o644
        assert modes["release/build.ps1"] == 0o644


def test_source_manifest_is_deterministic_and_excludes_private_generated_data(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "optimizer").mkdir(parents=True)
    (source / "optimizer" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "README.md").write_text("read me\n", encoding="utf-8")
    script = source / "run.sh"
    script.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o644)
    (source / ".venv" / "Lib" / "site-packages" / "package").mkdir(parents=True)
    (source / ".venv" / "Lib" / "site-packages" / "package" / "readme.md").write_text(
        "third-party trailing whitespace   \n",
        encoding="utf-8",
    )
    (source / ".pytest_cache").mkdir()
    (source / ".pytest_cache" / "state").write_text("private", encoding="utf-8")
    (source / "recording.ibrec").write_bytes(b"private recording")
    (source / "bot_state.sqlite-wal").write_bytes(b"private database sidecar")
    (source / "ibkr_trading_bot.lock").write_text("private lock", encoding="utf-8")
    (source / ".env").write_text("TOKEN=private\n", encoding="utf-8")
    (source / ".env.local").write_text("TOKEN=private\n", encoding="utf-8")
    (source / "certificate.crt").write_text("private certificate", encoding="utf-8")
    (source / "certificate.cer").write_text("private certificate", encoding="utf-8")
    (source / "certificate.der").write_bytes(b"private certificate")
    (source / "signing.keystore").write_bytes(b"private signing material")
    (source / "debug_captures").mkdir()
    (source / "debug_captures" / "capture.zip").write_bytes(b"private capture")
    (source / "market_replay_deadbeef").mkdir()
    (source / "market_replay_deadbeef" / "index.html").write_text(
        "private report", encoding="utf-8"
    )
    first = source_manifest(source)
    for index, path in enumerate(sorted(source.rglob("*")), start=1):
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
    second = source_manifest(source)
    assert first == second
    assert [row["path"] for row in first["files"]] == [
        "README.md",
        "optimizer/module.py",
        "run.sh",
    ]
    rows = {row["path"]: row for row in first["files"]}
    assert rows["run.sh"]["executable"] is True


def test_release_source_filter_excludes_local_environments_and_generated_trees() -> None:
    for relative in (
        Path(".venv/Lib/site-packages/package/readme.md"),
        Path(".venv-release/Lib/site-packages/package/readme.md"),
        Path(".venv-custom/Lib/site-packages/package/readme.md"),
        Path("build/generated.txt"),
        Path("release/generated.txt"),
        Path("optimizer_reports/report/index.html"),
    ):
        assert is_release_source_path(relative) is False
    assert is_release_source_path(Path("docs/README.md")) is True


def test_release_lock_gitignore_license_and_build_contracts() -> None:
    for name in ("requirements-bootstrap.lock", "requirements-release-win64.lock"):
        lines = [
            line.strip()
            for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert lines
        assert all(line.count("==") == 1 for line in lines)
        assert not any(any(token in line for token in (">=", "<=", "~=", "!=")) for line in lines)

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (
        "*.ibrec",
        "*.sqlite",
        "debug_captures/",
        "optimizer_reports/",
        "*.pfx",
        "*.keystore",
        ".ruff_cache/",
        "market_replay_*/",
        "SOURCE_MANIFEST.json",
        "BouncyBot_Offline_Optimizer_*_Synthetic_Multi_Recording_Example_Report.zip",
    ):
        assert pattern in gitignore

    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in attributes
    assert "*.ibrec binary" in attributes

    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert license_text.startswith("# PolyForm Noncommercial License 1.0.0")
    assert 'license = { file = "LICENSE" }' in pyproject

    build = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    assert '$requiredPythonVersion = "3.11.9"' in build
    assert '$sourceDateEpoch = "1784592000"' in build
    assert "requirements-release-win64.lock" in build
    assert "--no-deps" in build
    assert "--only-binary=:all:" in build
    assert '"--noupx"' in build
    assert "SOURCE_DATE_EPOCH" in build
    assert "compare_trees.py" in build
    assert "create_reproducible_zip.py" in build
    assert "create_source_manifest.py" in build
    assert "-m pip check" in build
    assert "SOURCE_MANIFEST.json" in build
    assert "& $Command | Out-Host" in build
    assert "$candidates += [PSCustomObject]@{" in build
    assert "$actual.Count -eq 2" in build
    assert "@candidateArguments" in build
    assert "@launcherArguments" in build
    assert "$launcher.Length" not in build
    assert "Compress-Archive" not in build


def test_release_helper_scripts_are_importable() -> None:
    for name in (
        "compare_trees.py",
        "create_reproducible_zip.py",
        "create_source_manifest.py",
        "verify_release_environment.py",
        "write_build_provenance.py",
    ):
        path = ROOT / "scripts" / name
        spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)


def _python_sources() -> list[Path]:
    return sorted(
        [
            ROOT / "main.py",
            *(ROOT / "optimizer").glob("*.py"),
            *(ROOT / "scripts").glob("*.py"),
            *(ROOT / "tests").glob("*.py"),
        ]
    )


def test_python_sources_have_no_duplicate_literal_dict_keys() -> None:
    offenders: list[str] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            seen: set[tuple[type[Any], Any]] = set()
            for key in node.keys:
                if not isinstance(key, ast.Constant):
                    continue
                value = key.value
                try:
                    marker = (type(value), value)
                    duplicate = marker in seen
                    seen.add(marker)
                except TypeError:
                    continue
                if duplicate:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{key.lineno}:{value!r}"
                    )
    assert offenders == []


def test_python_sources_have_no_duplicate_definitions_or_mutable_defaults() -> None:
    offenders: list[str] = []

    def inspect_body(path: Path, body: list[ast.stmt], scope: str) -> None:
        definitions: dict[str, int] = {}
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                previous = definitions.get(node.name)
                if previous is not None:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:duplicate {scope}.{node.name} (first {previous})"
                    )
                definitions[node.name] = node.lineno
                inspect_body(path, node.body, f"{scope}.{node.name}")
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defaults = [
                        *node.args.defaults,
                        *(value for value in node.args.kw_defaults if value is not None),
                    ]
                    if any(isinstance(value, (ast.Dict, ast.List, ast.Set)) for value in defaults):
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}:mutable default in {scope}.{node.name}"
                        )

    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        inspect_body(path, tree.body, path.stem)
    assert offenders == []


def test_source_text_has_no_control_characters_or_trailing_whitespace() -> None:
    offenders: list[str] = []
    extensions = {".bat", ".md", ".ps1", ".py", ".sh", ".toml", ".txt"}
    for path in release_source_files(ROOT):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        relative = path.relative_to(ROOT)
        text = path.read_text(encoding="utf-8-sig")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line.rstrip() != line:
                offenders.append(
                    f"{relative}:{line_number}:trailing whitespace"
                )
            if any(ord(character) < 32 and character != "\t" for character in line):
                offenders.append(
                    f"{relative}:{line_number}:control character"
                )
    assert offenders == []
