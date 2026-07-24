"""Write path- and time-independent release build provenance."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _named_hashes(paths: list[Path]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in sorted(paths, key=lambda item: item.name.lower()):
        if path.name in values:
            raise ValueError(f"duplicate provenance input name: {path.name}")
        values[path.name] = _hash(path)
    return values


def installed_distributions() -> dict[str, str]:
    """Return the installed distribution set with stable, validated names."""

    distributions: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        # Use Distribution.name rather than treating PackageMetadata as a
        # regular dict.  This is supported by Python 3.11 and by Pyright's
        # importlib.metadata protocol.
        name = distribution.name
        if not isinstance(name, str) or not name.strip():
            continue
        previous = distributions.get(name)
        if previous is not None and previous != distribution.version:
            raise RuntimeError(f"multiple installed versions detected for {name}")
        distributions[name] = distribution.version
    return dict(sorted(distributions.items(), key=lambda item: item[0].lower()))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--application-version", required=True)
    parser.add_argument("--source-date-epoch", required=True, type=int)
    parser.add_argument("--lock", action="append", type=Path, default=[])
    parser.add_argument("--script", action="append", type=Path, default=[])
    parser.add_argument("--input", action="append", type=Path, default=[])
    args = parser.parse_args()
    payload = {
        "application_version": args.application_version,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "architecture": platform.machine(),
        "source_date_epoch": args.source_date_epoch,
        "lock_files": _named_hashes(args.lock),
        "build_scripts": _named_hashes(args.script),
        "build_inputs": _named_hashes(args.input),
        "installed_distributions": installed_distributions(),
    }
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
