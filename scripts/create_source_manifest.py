"""Create a deterministic SHA-256 manifest for release-relevant source files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

_EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".venv-release",
    "__pycache__",
    "build",
    "dist",
    "debug_captures",
    "htmlcov",
    "optimizer_reports",
    "release",
    "scratch",
    "secrets",
    "temp",
    "tmp",
    "wheelhouse",
}
_EXCLUDED_FILE_NAMES = {
    ".coverage",
    ".env",
    "BUILD_PROVENANCE.json",
    "SHA256SUMS.txt",
    "SOURCE_MANIFEST.json",
    "coverage.json",
    "coverage.xml",
    "ibkr_trading_bot.lock",
}
_EXCLUDED_SUFFIXES = {
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".ibrec",
    ".ibrec-journal",
    ".ibrec-shm",
    ".ibrec-wal",
    ".cer",
    ".crt",
    ".csr",
    ".der",
    ".key",
    ".keystore",
    ".log",
    ".p12",
    ".p7b",
    ".p7c",
    ".pem",
    ".pfx",
    ".jks",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
}
_EXECUTABLE_BINARY_SUFFIXES = {".com", ".exe"}


def _excluded_directory(name: str) -> bool:
    return (
        name in _EXCLUDED_DIRECTORY_NAMES
        or name.startswith(".market_replay_")
        or name.startswith(".optimizer_")
        or name.startswith(".venv-")
        or name.startswith("market_replay_")
        or name.startswith("optimizer_")
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _excluded(relative: Path) -> bool:
    if any(_excluded_directory(part) for part in relative.parts[:-1]):
        return True
    if relative.name in _EXCLUDED_FILE_NAMES:
        return True
    if relative.name.startswith(".coverage."):
        return True
    if relative.name.startswith(".env."):
        return True
    return relative.suffix.lower() in _EXCLUDED_SUFFIXES


def is_release_source_path(relative: Path) -> bool:
    """Return whether a relative path belongs to the release-relevant source tree."""

    return bool(relative.parts) and not relative.is_absolute() and not _excluded(relative)


def release_source_files(root: Path) -> list[Path]:
    """Return release-relevant files without descending into generated trees."""

    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"source root does not exist: {root}")
    files: list[Path] = []
    for current, directory_names, file_names in os.walk(root, topdown=True):
        current_path = Path(current)
        relative_directory = current_path.relative_to(root)
        retained_directories: list[str] = []
        for name in sorted(directory_names):
            path = current_path / name
            relative = relative_directory / name
            if _excluded_directory(name):
                continue
            if path.is_symlink():
                raise ValueError(
                    f"refusing to manifest symbolic link: {relative.as_posix()}"
                )
            retained_directories.append(name)
        directory_names[:] = retained_directories
        for name in sorted(file_names):
            path = current_path / name
            relative = relative_directory / name
            if is_release_source_path(relative):
                files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def portable_executable(path: Path) -> bool:
    """Return the same host-independent executable classification used by ZIPs."""

    mode = path.stat().st_mode
    if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return True
    if path.suffix.lower() in _EXECUTABLE_BINARY_SUFFIXES:
        return True
    with path.open("rb") as stream:
        return stream.read(2) == b"#!"


def source_manifest(root: Path, *, excluded_output: Path | None = None) -> dict[str, Any]:
    """Return a path- and time-independent manifest for one source tree."""

    root = root.resolve()
    output = excluded_output.resolve() if excluded_output is not None else None
    files: list[dict[str, Any]] = []
    for path in release_source_files(root):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValueError(f"refusing to manifest symbolic link: {relative.as_posix()}")
        if output is not None and path.resolve() == output:
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"unsupported source-tree entry: {relative.as_posix()}")
        before = path.stat()
        digest = _sha256(path)
        executable = portable_executable(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"source file changed while hashing: {relative.as_posix()}")
        files.append(
            {
                "path": relative.as_posix(),
                "sha256": digest,
                "size": after.st_size,
                "executable": executable,
            }
        )
    if not files:
        raise ValueError("source tree contains no release-relevant files")
    aggregate = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    ).hexdigest()
    return {
        "format_version": 1,
        "source_tree_sha256": aggregate,
        "file_count": len(files),
        "files": files,
    }


def write_source_manifest(root: Path, output: Path) -> dict[str, Any]:
    payload = source_manifest(root, excluded_output=output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = write_source_manifest(args.root, args.output)
    print(
        "Source manifest written for "
        f"{payload['file_count']} files: {payload['source_tree_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
