"""Verify that a release virtual environment exactly matches pinned lock files."""

from __future__ import annotations

import argparse
import importlib.metadata
import re
from pathlib import Path

_NAME_RE = re.compile(r"[-_.]+")


def canonical_name(value: str) -> str:
    return _NAME_RE.sub("-", value).lower().strip()


def parse_lock(path: Path) -> dict[str, str]:
    expected: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") or "==" not in line or line.count("==") != 1:
            raise ValueError(f"{path}:{line_number}: expected one exact name==version pin")
        name, version = (part.strip() for part in line.split("==", 1))
        if not name or not version or any(token in version for token in (";", " ", "\t")):
            raise ValueError(f"{path}:{line_number}: invalid exact pin")
        canonical = canonical_name(name)
        previous = expected.get(canonical)
        if previous is not None and previous != version:
            raise ValueError(f"{path}:{line_number}: conflicting pin for {name}")
        expected[canonical] = version
    if not expected:
        raise ValueError(f"{path}: lock file contains no exact pins")
    return expected


def installed_distributions() -> dict[str, str]:
    installed: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        # ``PackageMetadata`` intentionally exposes mapping subscription but
        # not ``dict.get`` in its public typing protocol.  The Distribution
        # property is the supported, typed way to read the package name.
        name = distribution.name
        if not isinstance(name, str) or not name.strip():
            continue
        canonical = canonical_name(name)
        version = distribution.version
        previous = installed.get(canonical)
        if previous is not None and previous != version:
            raise RuntimeError(f"multiple installed versions detected for {name}")
        installed[canonical] = version
    return installed


def verify(lock_paths: list[Path], *, strict: bool) -> list[str]:
    expected: dict[str, str] = {}
    for path in lock_paths:
        for name, version in parse_lock(path).items():
            previous = expected.get(name)
            if previous is not None and previous != version:
                raise ValueError(f"conflicting lock files for {name}: {previous} versus {version}")
            expected[name] = version
    installed = installed_distributions()
    errors: list[str] = []
    for name, version in sorted(expected.items()):
        actual = installed.get(name)
        if actual is None:
            errors.append(f"missing {name}=={version}")
        elif actual != version:
            errors.append(f"{name}: expected {version}, installed {actual}")
    if strict:
        for name, version in sorted(installed.items()):
            if name not in expected:
                errors.append(f"unexpected installed distribution {name}=={version}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("locks", nargs="+", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    errors = verify(args.locks, strict=args.strict)
    if errors:
        print("Release environment does not match the lock files:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("Release environment exactly matches the pinned lock files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
