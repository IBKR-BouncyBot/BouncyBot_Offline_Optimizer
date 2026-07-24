"""Compare two directory trees by relative path and SHA-256 content."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def manifest(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise ValueError(f"directory does not exist: {root}")
    return {
        path.relative_to(root).as_posix(): _hash(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    args = parser.parse_args()
    left = manifest(args.left)
    right = manifest(args.right)
    if left == right:
        print(f"Directory trees are byte-identical across {len(left)} files.")
        return 0
    print("Directory trees differ.")
    for name in sorted(set(left) | set(right)):
        if left.get(name) != right.get(name):
            print(f"  {name}: {left.get(name, '<missing>')} != {right.get(name, '<missing>')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
