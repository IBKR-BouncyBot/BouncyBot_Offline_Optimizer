from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

from optimizer.determinism import ANALYSIS_CONTRACT_VERSION
from optimizer.evidence import evidence_contract
from optimizer.market_replay import market_replay_search_contract
from optimizer.market_replay_models import MarketReplayConfig
from optimizer.paths import app_dir
from optimizer.version import APP_NAME, APP_VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_consistent_across_release_files() -> None:
    assert APP_VERSION == "2.3.1"
    assert APP_NAME == "BouncyBot Offline Optimizer"
    for relative in [
        "pyproject.toml",
        "README.md",
        "CHANGELOG.md",
        "scripts/build_windows.ps1",
        "scripts/windows_version_info.txt",
        "docs/README.md",
        "docs/V2_3_1_PROFILE_IDENTITY_AND_PROBE_FLOOR.md",
    ]:
        assert APP_VERSION in (ROOT / relative).read_text(encoding="utf-8-sig")

    assert "v1.2.0" in (
        ROOT / "docs/V1_2_0_PRIMARY_EVALUATION_PROFILE.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.1.0" in (
        ROOT / "docs/V1_1_0_EXPLAINED_DETERMINISTIC_REPORTS.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.0.0" in (ROOT / "docs/V1_0_0_INITIAL_RELEASE.md").read_text(
        encoding="utf-8-sig"
    )
    assert "v1.3.0" in (
        ROOT / "docs/V1_3_0_PAIRED_ROBUSTNESS.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.4.0" in (
        ROOT / "docs/V1_4_0_MARKET_REPLAY_OPTIMIZATION.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.4.1" in (
        ROOT / "docs/V1_4_1_WINDOWS_QUALITY_FIXES.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.4.2" in (
        ROOT / "docs/V1_4_2_WINDOWS_TEST_ISOLATION_FIX.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.4.3" in (
        ROOT / "docs/V1_4_3_WINDOWS_RETRY_TEST_ROBUSTNESS.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.4.4" in (
        ROOT / "docs/V1_4_4_RUFF_QUALITY_GATE_CORRECTION.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.4.5" in (
        ROOT / "docs/V1_4_5_PYRIGHT_QUALITY_GATE_CORRECTION.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.5.0" in (
        ROOT / "docs/V1_5_0_THREE_STAGE_ATR_ROBUSTNESS_AND_REPRODUCIBLE_BUILDS.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.5.1" in (
        ROOT / "docs/V1_5_1_RUFF_SIM103_QUALITY_GATE_CORRECTION.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.5.2" in (
        ROOT / "docs/V1_5_2_WINDOWS_RELEASE_GATE_FIXES.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.5.3" in (
        ROOT / "docs/V1_5_3_WINDOWS_PORTABLE_ARCHIVE_AND_SOURCE_AUDIT_FIXES.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.8.0" in (
        ROOT / "docs/V1_8_0_CONTINUOUS_REPLAY_AND_EXECUTION_CALIBRATION.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.8.1" in (
        ROOT / "docs/V1_8_1_RUFF_QUALITY_GATE_CORRECTION.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.8.2" in (
        ROOT / "docs/V1_8_2_PYRIGHT_AND_NUMERIC_EVIDENCE_HARDENING.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.9.0" in (
        ROOT / "docs/V1_9_0_ROBUST_SELECTION_VALIDATION.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.9.1" in (
        ROOT / "docs/V1_9_1_RUFF_F841_QUALITY_GATE_CORRECTION.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.9.2" in (
        ROOT / "docs/V1_9_2_GUI_PREFLIGHT_AND_WINDOWS_TEST_CLARIFICATION.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.9.3" in (
        ROOT / "docs/V1_9_3_CALIBRATION_DATE_KEY_AND_REPORT_INTEGRITY.md"
    ).read_text(encoding="utf-8-sig")
    assert "v1.9.4" in (
        ROOT / "docs/V1_9_4_SAME_DATE_FRAGMENT_MERGING_AND_DIAGNOSTICS.md"
    ).read_text(encoding="utf-8-sig")
    assert "v2.0.0" in (
        ROOT / "docs/V2_0_0_PROTECTIVE_SELL_POLICY_OPTIMIZATION.md"
    ).read_text(encoding="utf-8-sig")
    assert "v2.0.1" in (
        ROOT / "docs/V2_0_1_RUFF_F841_QUALITY_GATE_CORRECTION.md"
    ).read_text(encoding="utf-8-sig")
    assert "v2.0.2" in (
        ROOT / "docs/V2_0_2_UNLIMITED_IBREC_ROWS_AND_FILES.md"
    ).read_text(encoding="utf-8-sig")
    assert "v2.1.0" in (
        ROOT / "docs/V2_1_0_EXACT_PARALLEL_REPLAY_PERFORMANCE.md"
    ).read_text(encoding="utf-8-sig")
    assert "v2.1.1" in (
        ROOT / "docs/V2_1_1_PYRIGHT_ATR_PROVIDER_TYPING_CORRECTION.md"
    ).read_text(encoding="utf-8-sig")
    assert "v2.2.0" in (
        ROOT / "docs/V2_2_0_EXACT_REFINEMENT_ACCELERATION.md"
    ).read_text(encoding="utf-8-sig")
    assert "v2.2.1" in (
        ROOT / "docs/V2_2_1_DEEP_PERFORMANCE_AUDIT.md"
    ).read_text(encoding="utf-8-sig")
    assert "v2.2.2" in (
        ROOT / "docs/V2_2_2_RUFF_QUALITY_GATE_CORRECTION.md"
    ).read_text(encoding="utf-8-sig")
    assert "v2.2.3" in (
        ROOT / "docs/V2_2_3_PYRIGHT_AND_RESOURCE_HARDENING.md"
    ).read_text(encoding="utf-8-sig")
    windows_version = (ROOT / "scripts/windows_version_info.txt").read_text(
        encoding="utf-8-sig"
    )
    assert "filevers=(2, 3, 1, 0)" in windows_version
    assert "prodvers=(2, 3, 1, 0)" in windows_version


def test_v191_reported_f841_condition_remains_corrected() -> None:
    source = (ROOT / "tests/test_v190_robust_selection_validation.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_selection_aware_bootstrap_is_deterministic_with_mocked_selector"
    )
    assigned_names = {
        target.id
        for node in ast.walk(function)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
        for target in (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
        )
        if isinstance(target, ast.Name)
    }
    assert "control_profile" not in assigned_names


def test_v130_analysis_and_evidence_contracts_are_versioned() -> None:
    assert ANALYSIS_CONTRACT_VERSION == 6
    contract = evidence_contract()
    assert contract["contract_version"] == 2
    assert contract["bootstrap_replicates"] == 2_000
    assert contract["minimum_paired_windows"] == 5
    assert contract["minimum_shared_km_horizon_seconds"] == 300.0


def test_v140_market_replay_contract_supports_v2_and_v3(tmp_path: Path) -> None:
    contract = market_replay_search_contract(
        MarketReplayConfig(tmp_path / "recording.ibrec", tmp_path / "reports")
    )
    assert contract["contract_version"] == 16
    assert contract["supported_ibrec_versions"] == [2, 3]
    assert contract["atr_window_search"]["stage_1"]["fixed_period"] == 14
    assert contract["atr_window_search"]["stage_2"]["periods"] == [5, 7, 10, 14, 21, 28]
    assert contract["bootstrap_replicates"] == 2_000
    assert contract["trading_day_bootstrap_replicates"] == 2_000
    assert "right_censored_session_fraction" in contract["scoring"]
    assert "synthetic source" in contract["evidence_stability_gates"]
    assert contract["input_recordings"]["recording_count_limit"] is None
    assert contract["input_recordings"]["aggregate_row_limit"] is None
    assert contract["input_recordings"]["same_day_overlap_policy"].startswith(
        "stitch the deterministic coverage-first"
    )
    assert "5-second phase offsets" in contract["atr_clock"]
    assert "recorded non-crossed ask" in contract["fill_model"]
    assert contract["continuous_overnight_replay"] is True
    assert contract["selection_aware_bootstrap_replicates"] == 32
    assert contract["moving_block_bootstrap_replicates"] == 2_000
    assert contract["minimum_advanced_validation_days"] == 20
    assert contract["walk_forward"] == {
        "minimum_training_days": 15,
        "validation_block_days": 5,
        "method": (
            "expanding chronological training windows; profile selection uses training data only, "
            "then the frozen selected profile is evaluated on the following unseen block"
        ),
    }
    assert {row["key"] for row in contract["score_policies"]} == {
        "balanced",
        "drawdown_focused",
        "return_focused",
        "cost_stressed",
    }
    calibration = contract["execution_calibration"]
    assert calibration["enabled"] is False
    assert calibration["maximum_quote_age_seconds"] == 5.0
    assert calibration["minimum_samples"] == 5
    protective = contract["protective_sell_policy_search"]
    assert protective["enabled"] is True
    assert protective["disabled_control"] is True
    assert protective["manual_trailing_percentages"] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert protective["atr_adaptive_multipliers"] == [
        1.5,
        2.0,
        2.5,
        3.0,
        3.5,
        4.0,
        4.5,
    ]
    assert protective["maximum_policies_advanced"] == 2
    assert "cancel" in protective["normal_sell_replacement"].lower()
    assert any(
        "protective SELL policy" in requirement
        for requirement in contract["changed_recommendation_requires"]
    )


def test_gui_contains_lock_check_and_explicit_confirmation() -> None:
    source = (ROOT / "optimizer/gui.py").read_text(encoding="utf-8")
    assert "Confirm offline read-only analysis" in source
    assert "QMessageBox.StandardButton.Cancel" in source
    assert "ibkr_trading_bot.lock" in source
    assert "copy SQLite/WAL through read-only file access" in source
    assert "QAbstractItemView.EditTrigger.NoEditTriggers" in source
    assert "QTableWidget.EditTrigger" not in source
    assert "self._busy = bool(busy)" in source
    assert "for widget in (" in source
    assert "widget.setEnabled(enabled)" in source
    assert '"Market Replay (.ibrec v2/v3)"' in source
    assert "No BouncyBot database will be read and no bot lock is required" in source
    assert "temporarily acquire" in source
    assert "read-only SQLite calibration ready" in source
    assert "Carry open long positions and active SELL trails across consecutive RTH recordings" in source
    assert "Optional execution calibration" in source
    assert "inspect_ibrec" in source
    assert "self._selected_ibrec_paths()" in source
    assert "QListWidget" in source
    assert "getOpenFileNames" in source
    assert 'Path(self.ibrec_edit.text()).expanduser().resolve()' not in source


def test_build_is_onedir_with_unique_runtime_directory() -> None:
    script = (ROOT / "scripts/build_windows.ps1").read_text(encoding="utf-8-sig")
    assert '"--onedir"' in script
    assert '"--contents-directory"' in script
    assert '"BouncyBotOptimizerRuntime"' in script
    assert '"--noconsole"' in script
    assert '"--packaged-smoke-test"' in script
    assert '"--collect-data", "tzdata"' in script
    assert "WaitForExit(30000)" in script
    assert "Packaged executable smoke test failed" in script


def test_windows_timezone_data_is_an_explicit_runtime_dependency() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8-sig")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8-sig")
    assert "tzdata>=2025.2" in requirements
    assert '"tzdata>=2025.2"' in pyproject


def test_batch_wrappers_use_optimizer_specific_no_pause_variable() -> None:
    for relative in ("run_all_tests.bat", "build_windows.bat"):
        source = (ROOT / relative).read_text(encoding="utf-8-sig")
        assert "BOUNCYBOT_OPTIMIZER_NO_PAUSE" in source
        assert "IBKR_BOT_NO_PAUSE" not in source


def test_runtime_code_has_no_network_client_imports() -> None:
    forbidden = {"requests", "httpx", "urllib.request", "socket"}
    found: set[str] = set()
    for path in (ROOT / "optimizer").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names if alias.name in forbidden)
            elif isinstance(node, ast.ImportFrom) and node.module in forbidden:
                found.add(str(node.module))
    assert found == set()


def test_source_mode_app_dir_is_project_root() -> None:
    assert app_dir() == ROOT


def test_reported_ruff_import_blocks_remain_normalized() -> None:
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    reports_source = (ROOT / "optimizer/reports.py").read_text(encoding="utf-8")
    safety_test_source = (ROOT / "tests/test_safety.py").read_text(encoding="utf-8")

    assert "from optimizer.cli import main\n\nif __name__" in main_source
    assert "from .version import APP_NAME, APP_VERSION\n\n_CSS" in reports_source
    assert (
        "    read_lock_owner,\n"
        "    readonly_database_snapshot,\n"
        "    source_paths,\n"
        "    source_state,\n"
        "    validate_source,\n"
        ")"
    ) in safety_test_source


def _is_literal_only_f_string_token(token_text: str) -> bool:
    quote_positions = [
        index for quote in ("'", '"') if (index := token_text.find(quote)) >= 0
    ]
    if not quote_positions:
        return False
    prefix = token_text[: min(quote_positions)].lower()
    if "f" not in prefix:
        return False
    expression = ast.parse(token_text, mode="eval").body
    return isinstance(expression, ast.JoinedStr) and not any(
        isinstance(node, ast.FormattedValue) for node in ast.walk(expression)
    )


def _literal_only_f_string_lines(source: str) -> list[int]:
    """Return real F541-style string-token locations across Python versions."""

    offenders: list[int] = []
    fstring_start = getattr(tokenize, "FSTRING_START", None)
    fstring_end = getattr(tokenize, "FSTRING_END", None)
    active_fstrings: list[dict[str, int | bool]] = []
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for token in tokens:
        if fstring_start is not None and token.type == fstring_start:
            active_fstrings.append({"line": token.start[0], "has_expression": False})
            continue
        if active_fstrings and token.type == tokenize.OP and token.string == "{":
            active_fstrings[-1]["has_expression"] = True
            continue
        if fstring_end is not None and token.type == fstring_end:
            current = active_fstrings.pop()
            if not current["has_expression"]:
                offenders.append(int(current["line"]))
            continue
        # Python 3.11 and earlier expose an entire f-string as one STRING token.
        if token.type != tokenize.STRING:
            continue
        if _is_literal_only_f_string_token(token.string):
            offenders.append(token.start[0])
    return offenders


def test_python_sources_have_no_literal_only_f_strings() -> None:
    offenders: list[str] = []
    for path in sorted([ROOT / "main.py", *(ROOT / "optimizer").glob("*.py"), *(ROOT / "tests").glob("*.py")]):
        source = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.relative_to(ROOT)}:{line}"
            for line in _literal_only_f_string_lines(source)
        )
    assert offenders == []


def test_literal_only_f_string_check_is_python_311_compatible() -> None:
    # Python 3.11 tokenizes each complete f-string as one STRING token. Exercise
    # that branch directly even when this suite runs under Python 3.12+.
    assert _is_literal_only_f_string_token('f"literal"')
    assert _is_literal_only_f_string_token('fr"{{literal braces}}"')
    assert not _is_literal_only_f_string_token('f"{value:.{decimals}f}"')
    assert not _is_literal_only_f_string_token('"not an f-string"')

    source = (
        'formatted = f"{value:.{decimals}f}"\n'
        'escaped = f"{{literal braces}}"\n'
        'plain = "not an f-string"\n'
    )
    assert _literal_only_f_string_lines(source) == [2]


def test_packaged_path_branch(monkeypatch, tmp_path: Path) -> None:
    import optimizer.paths as paths_module

    executable = tmp_path / "BouncyBotOfflineOptimizer.exe"
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr(paths_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths_module.sys, "executable", str(executable))
    assert paths_module.app_dir() == tmp_path
