"""Pure presentation helpers for the GUI result table.

Keeping the text generation independent of PySide6 makes every tooltip and
report-path rule testable in environments that do not have a GUI runtime.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import TickerAnalysis
from .utils import finite_float, finite_int, ticker_folder_name


@dataclass(slots=True, frozen=True)
class ResultColumn:
    key: str
    heading: str
    heading_tooltip: str


@dataclass(slots=True, frozen=True)
class ResultCell:
    text: str
    tooltip: str


RESULT_COLUMNS = (
    ResultColumn(
        "ticker",
        "Ticker",
        "Ticker symbol reconstructed from cycle rows and capture manifests. Analysis is performed independently per ticker.",
    ),
    ResultColumn(
        "coverage",
        "Coverage",
        "Letter grade derived from score and completed-cycle count: A requires ≥80 and ≥20 cycles; B ≥65 and ≥10; C ≥45 and ≥3; otherwise D.",
    ),
    ResultColumn(
        "score",
        "Score",
        "Weighted 0–100 data-coverage score. It measures evidence completeness, not strategy profitability.",
    ),
    ResultColumn(
        "completed",
        "Completed",
        "Completed normal or protective-exit cycles found in SQLite for this ticker.",
    ),
    ResultColumn(
        "buy_match",
        "BUY match",
        "Percentage of recorded BUY fills that could be matched to a usable BUY_FILL capture archive.",
    ),
    ResultColumn(
        "sell_match",
        "SELL match",
        "Percentage of recorded normal SELL fills that could be matched to a usable SELL_FILL capture archive. Protective exits are reported separately.",
    ),
    ResultColumn(
        "replay_windows",
        "Replay windows",
        "Number of matched capture windows with usable price rows for counterfactual BUY / normal-SELL replay.",
    ),
    ResultColumn(
        "report",
        "Report",
        "Open the detailed deterministic HTML report for this ticker.",
    ),
)


_IBREC_CONTAINER_LABELS = {
    "sqlite": "SQLite",
    "zip": "ZIP",
}


def market_replay_format_label(details: Mapping[str, Any]) -> str:
    """Return a concise human-readable label for verified .ibrec inputs.

    ``inspect_ibrec_set`` returns one record per selected file, which is the
    authoritative source for matching a format version to its container.  The
    aggregate ``format_versions`` and ``containers`` fields are used only as a
    defensive fallback so the GUI does not regress to an opaque list such as
    ``[3]`` if a caller supplies a reduced summary.
    """

    labels: set[tuple[int, str]] = set()
    recordings = details.get("recordings")
    if isinstance(recordings, (list, tuple)):
        for recording in recordings:
            if not isinstance(recording, Mapping):
                continue
            version = finite_int(recording.get("format_version"))
            container = str(recording.get("container_format") or "").strip().lower()
            if version in {2, 3} and container in _IBREC_CONTAINER_LABELS:
                labels.add((version, container))

    if not labels:
        versions_raw = details.get("format_versions")
        versions: set[int] = set()
        if isinstance(versions_raw, (list, tuple, set, frozenset)):
            for value in versions_raw:
                version = finite_int(value)
                if version in {2, 3}:
                    versions.add(version)
        containers_raw = details.get("containers")
        containers: set[str] = set()
        if isinstance(containers_raw, (list, tuple, set, frozenset)):
            for value in containers_raw:
                container = str(value).strip().lower()
                if container in _IBREC_CONTAINER_LABELS:
                    containers.add(container)
        for version in versions:
            expected = "zip" if version == 2 else "sqlite"
            if not containers or expected in containers:
                labels.add((version, expected))

    if not labels:
        return "unknown .ibrec format"
    return " / ".join(
        f"v{version} {_IBREC_CONTAINER_LABELS[container]}"
        for version, container in sorted(labels)
    )


def _percent(value: Any) -> str:
    number = finite_float(value)
    if number is None:
        return "—"
    return f"{number:.1f}%"


def _count(value: Any) -> int:
    number = finite_float(value)
    if number is None:
        return 0
    return int(number) if number >= 0 else 0


def result_cells(ticker: TickerAnalysis) -> list[ResultCell]:
    """Return table text and row-specific derivation tooltips."""
    coverage = ticker.coverage
    completed = _count(coverage.get("completed_cycles"))
    buy_fills = _count(coverage.get("buy_fills"))
    buy_matched = _count(coverage.get("matched_buy_captures"))
    sell_fills = _count(coverage.get("normal_sell_fills"))
    sell_matched = _count(coverage.get("matched_sell_captures"))
    buy_replay = _count(coverage.get("replayable_buy_windows"))
    sell_replay = _count(coverage.get("replayable_sell_windows"))
    score_number = finite_float(coverage.get("coverage_score"))
    if score_number is None:
        score_number = math.nan
    score_text = f"{score_number:.1f}" if math.isfinite(score_number) else "—"
    grade = str(coverage.get("coverage_grade") or "—")
    components = coverage.get("coverage_score_components") or {}
    component_parts: list[str] = []
    for name, value in sorted(components.items()):
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            component_parts.append(f"{name.replace('_', ' ')} {number:.1f}")
    component_text = ", ".join(component_parts) or "No score components were available."
    return [
        ResultCell(
            ticker.ticker,
            f"All SQLite cycles and capture manifests normalized to ticker {ticker.ticker} are grouped into this independent analysis.",
        ),
        ResultCell(
            grade,
            f"Grade {grade} comes from score {score_text} plus sample depth: A requires ≥80 and ≥20 completed cycles; B ≥65 and ≥10; C ≥45 and ≥3; otherwise D.",
        ),
        ResultCell(
            score_text,
            "Coverage score is a weighted evidence-completeness measure. "
            f"Components: {component_text}. It is not a return, confidence level, or trading score.",
        ),
        ResultCell(
            str(completed),
            f"{completed} cycle row(s) had a complete stage or bought quantity fully covered by a normal/protective SELL quantity.",
        ),
        ResultCell(
            _percent(coverage.get("buy_capture_match_pct")),
            f"{buy_matched} usable BUY capture(s) matched {buy_fills} recorded BUY fill(s). Percentage = matched ÷ fills × 100.",
        ),
        ResultCell(
            _percent(coverage.get("sell_capture_match_pct")),
            f"{sell_matched} usable normal-SELL capture(s) matched {sell_fills} recorded normal SELL fill(s). Protective SELL captures are not included here.",
        ),
        ResultCell(
            f"{buy_replay} / {sell_replay}",
            "BUY / normal-SELL replayable windows. A matched archive is replayable only when it contains at least one valid timestamped positive price row.",
        ),
        ResultCell(
            "Open report",
            f"Open the detailed {ticker.ticker} coverage, settings-provenance, counterfactual-replay, and limitations report.",
        ),
    ]


def ticker_report_path(output_dir: Path, ticker: str) -> Path:
    folder = ticker_folder_name(ticker)
    return Path(output_dir) / folder / f"{folder}_coverage_and_replay.html"
