from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import ModuleType

from optimizer.cli import main
from tests.market_replay_fixtures import make_ticks, write_v3


def test_cli_version(capsys) -> None:
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "1.9.3" in capsys.readouterr().out


def test_packaged_smoke_test_imports_gui_without_starting_it(monkeypatch) -> None:
    gui_module = ModuleType("optimizer.gui")
    gui_module.MainWindow = object
    monkeypatch.setitem(sys.modules, "optimizer.gui", gui_module)

    assert main(["--packaged-smoke-test"]) == 0


def test_cli_noninteractive_analysis(source_fixture: Path, tmp_path: Path, capsys) -> None:
    exit_code = main(
        [
            "--no-gui",
            "--yes",
            "--source-dir",
            str(source_fixture),
            "--output-dir",
            str(tmp_path / "reports"),
            "--json-result",
        ]
    )
    assert exit_code == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["tickers"][0]["ticker"] == "AAPL"
    assert Path(payload["output_dir"]).joinpath("index.html").exists()


def test_cli_rejects_disabling_capture_hashes(
    source_fixture: Path,
    tmp_path: Path,
    capsys,
) -> None:
    exit_code = main(
        [
            "--no-gui",
            "--yes",
            "--source-dir",
            str(source_fixture),
            "--output-dir",
            str(tmp_path / "reports"),
            "--no-capture-hashes",
        ]
    )
    assert exit_code == 3
    assert "cannot be disabled" in capsys.readouterr().err


def test_cli_refuses_lock(source_fixture: Path, tmp_path: Path, capsys) -> None:
    (source_fixture / "ibkr_trading_bot.lock").write_text("1", encoding="ascii")
    exit_code = main(
        [
            "--no-gui",
            "--yes",
            "--source-dir",
            str(source_fixture),
            "--output-dir",
            str(tmp_path / "reports"),
        ]
    )
    assert exit_code == 2
    assert "SAFETY CHECK FAILED" in capsys.readouterr().err


def test_cli_cancel_and_missing_source(tmp_path: Path, monkeypatch, capsys) -> None:
    from tests.conftest import create_database

    source = tmp_path / "bot"
    create_database(source, cycles=0)
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert main(["--no-gui", "--source-dir", str(source), "--output-dir", str(tmp_path / "out")]) == 1
    assert "cancelled" in capsys.readouterr().out.lower()

    missing = tmp_path / "missing"
    assert main(["--no-gui", "--yes", "--source-dir", str(missing)]) == 2
    assert "SAFETY CHECK FAILED" in capsys.readouterr().err


def test_cli_reports_incompatible_sqlite_without_a_traceback(
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "bot"
    source.mkdir()
    with closing(sqlite3.connect(source / "bot_state.sqlite")) as connection:
        connection.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)")
        connection.commit()

    exit_code = main(
        [
            "--no-gui",
            "--yes",
            "--source-dir",
            str(source),
            "--output-dir",
            str(tmp_path / "reports"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "ANALYSIS FAILED" in captured.err
    assert "missing required table" in captured.err.lower()
    assert "Traceback" not in captured.err
    assert not (source / "ibkr_trading_bot.lock").exists()


def test_cli_market_replay_v3_is_independent_of_bot_lock(
    tmp_path: Path,
    capsys,
) -> None:
    rows, periods = make_ticks()
    recording = write_v3(tmp_path / "recordings" / "AAPL.ibrec", rows, periods)
    lock = recording.parent / "ibkr_trading_bot.lock"
    lock.write_text("trading-bot-lock", encoding="ascii")

    exit_code = main(
        [
            "--no-gui",
            "--ibrec",
            str(recording),
            "--output-dir",
            str(tmp_path / "replay-reports"),
            "--json-result",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ticker"] == "AAPL"
    assert payload["recording_format_version"] == 3
    assert payload["recording_container_format"] == "sqlite"
    assert Path(payload["output_dir"], "index.html").is_file()
    assert lock.read_text(encoding="ascii") == "trading-bot-lock"


def test_cli_market_replay_failure_has_no_traceback(tmp_path: Path, capsys) -> None:
    recording = tmp_path / "invalid.ibrec"
    recording.write_bytes(b"not a recording")

    exit_code = main(
        [
            "--no-gui",
            "--ibrec",
            str(recording),
            "--output-dir",
            str(tmp_path / "reports"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "ANALYSIS FAILED" in captured.err
    assert "Traceback" not in captured.err
