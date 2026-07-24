"""Create a path-ordered ZIP with normalized timestamps and permissions."""

from __future__ import annotations

import argparse
import os
import stat
import time
import zipfile
from pathlib import Path, PurePosixPath


_EXECUTABLE_BINARY_SUFFIXES = {".com", ".exe"}


def portable_executable(path: Path) -> bool:
    """Return a host-independent executable classification for release metadata.

    Windows does not preserve POSIX executable mode bits through ``chmod`` or a
    normal source extraction. Keep an existing executable bit when one is
    available, and otherwise recognize conventional shebang scripts and native
    Windows executable binaries. PowerShell and batch files are deliberately
    not marked executable merely because of their suffix.
    """

    mode = path.stat().st_mode
    if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return True
    if path.suffix.lower() in _EXECUTABLE_BINARY_SUFFIXES:
        return True
    with path.open("rb") as stream:
        return stream.read(2) == b"#!"


def _zip_datetime(epoch: int) -> tuple[int, int, int, int, int, int]:
    value = time.gmtime(max(epoch, 315532800))  # ZIP timestamps start in 1980.
    second = value.tm_sec - value.tm_sec % 2
    return value.tm_year, value.tm_mon, value.tm_mday, value.tm_hour, value.tm_min, second


def _info(name: str, *, epoch: int, directory: bool, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_zip_datetime(epoch))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o755 if directory or executable else 0o644
    file_type = stat.S_IFDIR if directory else stat.S_IFREG
    info.external_attr = (file_type | mode) << 16
    if directory:
        info.external_attr |= 0x10
    info.extra = b""
    info.comment = b""
    return info


def create_zip(source: Path, output: Path, *, root_name: str, epoch: int) -> None:
    source = source.resolve()
    output = output.resolve()
    if not source.is_dir():
        raise ValueError(f"source directory does not exist: {source}")
    if output == source or source in output.parents:
        raise ValueError("output ZIP cannot be inside the source directory")
    if not root_name or PurePosixPath(root_name).name != root_name:
        raise ValueError("root_name must be one safe path component")
    entries = sorted(source.rglob("*"), key=lambda path: path.relative_to(source).as_posix())
    for path in entries:
        if path.is_symlink():
            raise ValueError(f"refusing to archive symbolic link: {path}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
        ) as archive:
            archive.writestr(_info(f"{root_name}/", epoch=epoch, directory=True), b"")
            for path in entries:
                relative = path.relative_to(source).as_posix()
                archive_name = f"{root_name}/{relative}"
                if path.is_dir():
                    archive.writestr(
                        _info(archive_name.rstrip("/") + "/", epoch=epoch, directory=True),
                        b"",
                    )
                    continue
                executable = portable_executable(path)
                archive.writestr(
                    _info(archive_name, epoch=epoch, directory=False, executable=executable),
                    path.read_bytes(),
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--root-name", required=True)
    parser.add_argument(
        "--epoch",
        type=int,
        default=int(os.environ.get("SOURCE_DATE_EPOCH", "315532800")),
    )
    args = parser.parse_args()
    create_zip(args.source, args.output, root_name=args.root_name, epoch=args.epoch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
