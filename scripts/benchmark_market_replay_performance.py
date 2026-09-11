"""Benchmark exact Market Replay worker counts and verify result equivalence."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from optimizer.market_replay import run_market_replay_analysis
from optimizer.market_replay_models import MarketReplayConfig


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark exact Market Replay execution for one or more worker counts. "
            "Every run must produce the same analytical result."
        )
    )
    parser.add_argument("recordings", type=Path, nargs="+")
    parser.add_argument(
        "--workers",
        type=int,
        nargs="+",
        default=[1, 0],
        help="Worker settings to compare; default: 1 and Automatic(0)",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Optional path for the deterministic benchmark summary",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    recordings = tuple(path.expanduser().absolute() for path in args.recordings)
    rows: list[dict[str, object]] = []
    reference: dict[str, object] | None = None
    with tempfile.TemporaryDirectory(prefix="bouncybot-performance-benchmark-") as root:
        output_root = Path(root)
        for index, workers in enumerate(args.workers):
            started = time.perf_counter()
            result = run_market_replay_analysis(
                MarketReplayConfig(
                    recording_path=recordings,
                    output_root=output_root / f"workers-{workers}-{index}",
                    worker_processes=workers,
                )
            )
            elapsed = time.perf_counter() - started
            analytical = {
                "analysis_id": result.analysis_id,
                "recommendation": result.recommendation.to_dict(),
                "candidates": [candidate.to_dict() for candidate in result.candidates],
            }
            if reference is None:
                reference = analytical
            elif analytical != reference:
                raise RuntimeError(
                    f"Worker setting {workers} changed the analytical result."
                )
            rows.append(
                {
                    "workers": workers,
                    "elapsed_seconds": round(elapsed, 6),
                    "analysis_id": result.analysis_id,
                    "candidate_count": len(result.candidates),
                }
            )
    payload = {
        "recordings": [str(path) for path in recordings],
        "runs": rows,
        "analytical_results_identical": True,
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output_json is not None:
        output = args.output_json.expanduser().absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8", newline="\n")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
