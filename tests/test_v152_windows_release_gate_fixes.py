from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.verify_release_environment as verify_release_environment
import scripts.write_build_provenance as write_build_provenance

ROOT = Path(__file__).resolve().parents[1]


class _Distribution:
    def __init__(self, name: object, version: str) -> None:
        self.name = name
        self.version = version

    @property
    def metadata(self) -> object:
        raise AssertionError("release helpers must not treat PackageMetadata as a dict")


def test_pyinstaller_receives_absolute_version_info_source_path() -> None:
    build = (ROOT / "scripts/build_windows.ps1").read_text(encoding="utf-8")

    assert '$versionInfoPath = [System.IO.Path]::GetFullPath((Join-Path $root "scripts\\windows_version_info.txt"))' in build
    assert '"--version-file", $versionInfoPath' in build
    assert '"--version-file", "scripts\\windows_version_info.txt"' not in build
    assert "@($bootstrapLock, $releaseLock, $versionInfoPath)" in build


def test_release_environment_uses_distribution_name_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    distributions = [
        _Distribution("Example_Package", "1.2.3"),
        _Distribution("Other.Package", "4.5.6"),
        _Distribution(None, "9.9.9"),
    ]
    monkeypatch.setattr(
        verify_release_environment.importlib.metadata,
        "distributions",
        lambda: distributions,
    )

    assert verify_release_environment.installed_distributions() == {
        "example-package": "1.2.3",
        "other-package": "4.5.6",
    }


def test_build_provenance_uses_distribution_name_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    distributions = [
        _Distribution("Zulu", "2.0"),
        _Distribution("alpha", "1.0"),
        _Distribution("", "3.0"),
    ]
    monkeypatch.setattr(
        write_build_provenance.importlib.metadata,
        "distributions",
        lambda: distributions,
    )

    assert write_build_provenance.installed_distributions() == {
        "alpha": "1.0",
        "Zulu": "2.0",
    }


def test_build_provenance_rejects_conflicting_installed_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        write_build_provenance.importlib.metadata,
        "distributions",
        lambda: [
            SimpleNamespace(name="duplicate", version="1.0"),
            SimpleNamespace(name="duplicate", version="2.0"),
        ],
    )

    with pytest.raises(RuntimeError, match="multiple installed versions"):
        write_build_provenance.installed_distributions()
