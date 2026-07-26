"""Command-line entry point and GUI dispatch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .analysis import AnalysisError, run_analysis
from .database import DatabaseFormatError
from .ibrec import IbrecError
from .market_replay import MarketReplayAnalysisError, run_market_replay_analysis
from .market_replay_models import MarketReplayConfig
from .market_replay_reports import write_market_replay_report
from .models import AnalysisConfig
from .paths import default_output_root, default_source_dir
from .reports import write_reports
from .safety import SourceSafetyError, source_paths, validate_source
from .version import APP_NAME, APP_VERSION


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            f"{APP_NAME}: analyze BouncyBot SQLite/captures or independently optimize one or more "
            "Market Replay Lab .ibrec v2/v3 recordings"
        )
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    parser.add_argument(
        "--packaged-smoke-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--no-gui", action="store_true", help="Run in the terminal instead of opening the desktop interface")
    parser.add_argument("--source-dir", type=Path, default=default_source_dir(), help="Folder containing bot_state.sqlite and debug_captures")
    parser.add_argument(
        "--ibrec",
        type=Path,
        nargs="+",
        help=(
            "Run the separate Market Replay workflow on one or more .ibrec format-v2 or format-v3 recordings "
            "for the same instrument"
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=default_output_root(), help="Root folder for versioned analysis runs")
    parser.add_argument("--yes", action="store_true", help="Confirm the bot is closed without an interactive terminal prompt")
    parser.add_argument(
        "--no-capture-hashes",
        action="store_true",
        help=(
            "Legacy compatibility switch; analysis rejects it because deterministic "
            "report identity requires capture hashes"
        ),
    )
    parser.add_argument("--max-capture-rows", type=int, default=200_000, help="Safety limit for rows read from one capture")
    parser.add_argument(
        "--max-archive-mib",
        type=int,
        default=256,
        help="Safety limit for total uncompressed bytes in one capture ZIP",
    )
    parser.add_argument("--json-result", action="store_true", help="Print the final run summary as JSON")
    parser.add_argument(
        "--max-ibrec-rows",
        type=int,
        default=2_000_000,
        help="Aggregate safety limit for rows across the selected Market Replay recordings",
    )
    parser.add_argument(
        "--max-ibrec-mib",
        type=int,
        default=4096,
        help="Aggregate safety limit for selected .ibrec and rollback-journal input size",
    )
    parser.add_argument(
        "--max-ibrec-zip-mib",
        type=int,
        default=8192,
        help="Safety limit for total uncompressed content in a version-2 ZIP recording",
    )
    parser.add_argument(
        "--max-ibrec-files",
        type=int,
        default=64,
        help="Maximum number of Market Replay recordings accepted in one analysis",
    )
    parser.add_argument(
        "--ibrec-notional",
        type=float,
        default=10_000.0,
        help="Assumed trade notional used for recorded top-of-book liquidity checks",
    )
    parser.add_argument(
        "--ibrec-cost-bps-per-side",
        type=float,
        default=1.0,
        help="Fixed execution-cost reserve charged on every modeled BUY and SELL side",
    )
    parser.add_argument(
        "--ibrec-turnover-penalty-bps",
        type=float,
        default=0.25,
        help="Additional screening-score penalty per completed simulated trade",
    )
    parser.add_argument(
        "--calibration-source-dir",
        type=Path,
        help=(
            "Optional stopped BouncyBot portable-data folder. Actual executions and commissions "
            "are read from a private SQLite snapshot to calibrate notional and a conservative "
            "per-side execution-cost reserve."
        ),
    )
    parser.add_argument(
        "--no-overnight-replay",
        action="store_true",
        help="Disable continuous position/SELL-trail carry across consecutive complete RTH recordings",
    )
    parser.add_argument(
        "--calibration-max-quote-age-seconds",
        type=float,
        default=5.0,
        help="Maximum age of a same-side .ibrec quote matched to an actual BouncyBot execution",
    )
    parser.add_argument(
        "--calibration-min-samples",
        type=int,
        default=5,
        help="Minimum per-side/cycle sample count before calibrated assumptions can replace configured values",
    )
    parser.add_argument(
        "--no-calibrated-cost",
        action="store_true",
        help="Read calibration evidence but retain the configured per-side execution-cost reserve",
    )
    parser.add_argument(
        "--no-calibrated-notional",
        action="store_true",
        help="Read calibration evidence but retain the configured assumed trade notional",
    )
    return parser


def _confirm(source_dir: Path, *, calibration_only: bool = False) -> bool:
    activity = (
        "copy SQLite/WAL through read-only file access, create a private temporary snapshot, "
        "derive execution-cost and trade-notional evidence, and release the lock before replay"
        if calibration_only
        else (
            "copy SQLite/WAL through read-only file access, create a private temporary snapshot, "
            "read capture ZIP files, and write only to the selected report directory"
        )
    )
    message = (
        "The trading-bot lock file is absent. Confirm that BouncyBot is fully closed.\n"
        f"The optimizer will temporarily acquire the same lock, {activity}.\n"
        f"Source: {source_dir}\n"
        "Continue [y/N]? "
    )
    try:
        response = input(message)
    except (EOFError, KeyboardInterrupt):
        return False
    return response.strip().lower() in {"y", "yes"}


def _progress(message: str, current: int, total: int) -> None:
    suffix = f" [{current}/{total}]" if total else ""
    print(f"==> {message}{suffix}", flush=True)


def run_terminal(args: argparse.Namespace) -> int:
    paths = source_paths(args.source_dir)
    try:
        warnings = validate_source(paths)
    except SourceSafetyError as exc:
        print(f"SAFETY CHECK FAILED: {exc}", file=sys.stderr)
        return 2
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if not args.yes and not _confirm(paths.root):
        print("Analysis cancelled.")
        return 1
    config = AnalysisConfig(
        source_dir=paths.root,
        output_root=Path(args.output_dir),
        max_archive_uncompressed_bytes=max(1, int(args.max_archive_mib)) * 1024 * 1024,
        max_rows_per_capture=max(100, int(args.max_capture_rows)),
        hash_capture_files=not bool(args.no_capture_hashes),
    )
    try:
        progress_callback = (
            (lambda message, current, total: print(
                f"==> {message}{f' [{current}/{total}]' if total else ''}",
                file=sys.stderr,
                flush=True,
            ))
            if args.json_result
            else _progress
        )
        result = run_analysis(config, progress=progress_callback)
        result = write_reports(result)
    except (
        SourceSafetyError,
        AnalysisError,
        DatabaseFormatError,
        OSError,
        ValueError,
    ) as exc:
        print(f"ANALYSIS FAILED: {exc}", file=sys.stderr)
        return 3
    summary = {
        "run_id": result.run_id,
        "output_dir": str(result.output_dir),
        "tickers": [
            {
                "ticker": ticker.ticker,
                "coverage_grade": ticker.coverage.get("coverage_grade"),
                "coverage_score": ticker.coverage.get("coverage_score"),
            }
            for ticker in result.tickers
        ],
        "global_issues": result.global_issues,
    }
    if args.json_result:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"Analysis complete: {result.output_dir}")
        print(f"Open: {result.output_dir / 'index.html'}")
    return 0


def run_market_replay_terminal(args: argparse.Namespace) -> int:
    """Run recording analysis, optionally with read-only execution calibration."""

    raw_inputs = args.ibrec if isinstance(args.ibrec, (list, tuple)) else [args.ibrec]
    config = MarketReplayConfig(
        recording_path=tuple(Path(value) for value in raw_inputs),
        output_root=Path(args.output_dir),
        max_rows=max(100, int(args.max_ibrec_rows)),
        max_input_bytes=max(1, int(args.max_ibrec_mib)) * 1024 * 1024,
        max_zip_uncompressed_bytes=max(1, int(args.max_ibrec_zip_mib)) * 1024 * 1024,
        max_recordings=max(1, int(args.max_ibrec_files)),
        assumed_trade_notional=float(args.ibrec_notional),
        execution_cost_bps_per_side=float(args.ibrec_cost_bps_per_side),
        turnover_penalty_bps_per_completed_trade=float(
            args.ibrec_turnover_penalty_bps
        ),
        continuous_overnight_replay=not bool(args.no_overnight_replay),
        calibration_source_dir=(
            Path(args.calibration_source_dir)
            if args.calibration_source_dir is not None
            else None
        ),
        calibration_max_quote_age_seconds=float(
            args.calibration_max_quote_age_seconds
        ),
        calibration_min_samples=max(1, int(args.calibration_min_samples)),
        calibration_use_execution_cost=not bool(args.no_calibrated_cost),
        calibration_use_trade_notional=not bool(args.no_calibrated_notional),
    )
    if args.calibration_source_dir is not None and not args.yes:
        calibration_root = Path(args.calibration_source_dir).expanduser().resolve()
        if not _confirm(calibration_root, calibration_only=True):
            print("Analysis cancelled.")
            return 1
    try:
        progress_callback = (
            (
                lambda message, current, total: print(
                    f"==> {message}{f' [{current}/{total}]' if total else ''}",
                    file=sys.stderr,
                    flush=True,
                )
            )
            if args.json_result
            else _progress
        )
        result = run_market_replay_analysis(config, progress=progress_callback)
        result = write_market_replay_report(result)
    except (
        IbrecError,
        MarketReplayAnalysisError,
        SourceSafetyError,
        DatabaseFormatError,
        OSError,
        ValueError,
    ) as exc:
        print(f"ANALYSIS FAILED: {exc}", file=sys.stderr)
        return 3
    summary = {
        "run_id": result.run_id,
        "analysis_id": result.analysis_id,
        "output_dir": str(result.output_dir),
        "recording_format_version": result.recording.format_version,
        "recording_format_versions": list(result.recording.format_versions),
        "recording_container_format": result.recording.container_format,
        "recording_count": result.recording.input_recording_count,
        "ticker": result.recording.symbol,
        "recommended_profile": result.recommendation.profile.to_dict(),
        "recommendation_score": result.recommendation.score,
        "evidence_stable": result.recommendation.evidence_stable,
        "continuous_overnight_replay": bool(
            result.search_contract.get("continuous_overnight_replay")
        ),
        "execution_calibration": result.execution_calibration,
        "global_issues": result.global_issues,
    }
    if args.json_result:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"Market Replay analysis complete: {result.output_dir}")
        print(f"Open: {result.output_dir / 'index.html'}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    if args.packaged_smoke_test:
        # Import the complete GUI entry module without creating a QApplication.
        # The Windows build invokes this hidden mode to prove that the frozen
        # executable, PySide6 runtime, and application modules can all load.
        try:
            from .gui import MainWindow
            from .ibrec import inspect_ibrec
            from .market_replay_reports import write_market_replay_report
        except ImportError:
            return 5
        return 0 if MainWindow is not None and inspect_ibrec is not None and write_market_replay_report is not None else 5
    if args.no_gui:
        return run_market_replay_terminal(args) if args.ibrec is not None else run_terminal(args)
    try:
        from .gui import launch_gui
    except ImportError as exc:
        print(f"GUI dependencies are unavailable: {exc}", file=sys.stderr)
        print("Run with --no-gui or install the requirements.", file=sys.stderr)
        return 4
    return int(
        launch_gui(
            source_dir=Path(args.source_dir),
            output_dir=Path(args.output_dir),
            ibrec_path=(
                tuple(Path(value) for value in args.ibrec)
                if args.ibrec is not None
                else None
            ),
        )
    )
