# BouncyBot Offline Optimizer v1.5.2 — Windows release-gate fixes

Version 1.5.2 corrects the two failures reported by the native Windows v1.5.1 release checks. The optimizer algorithms and generated analysis results are unchanged.

## PyInstaller version-resource path

The reproducible Windows builder writes each generated PyInstaller specification under a pass-specific directory such as `build/spec-pass1`. PyInstaller resolves a relative `--version-file` path from that specification directory when it later evaluates the generated spec.

Version 1.5.1 passed `scripts/windows_version_info.txt` as a relative path. The generated spec therefore attempted to open `build/spec-pass1/scripts/windows_version_info.txt`, which does not exist.

Version 1.5.2 constructs the version-resource path from the absolute repository root, verifies that it exists before building, and passes that absolute path to both reproducibility passes.

## Pyright-compatible installed-distribution metadata

The release-environment and build-provenance helpers previously called `distribution.metadata.get("Name")`. The runtime metadata object accepts that call, but Python's public `importlib.metadata.PackageMetadata` typing protocol does not expose a `get` method. Pyright 1.1.411 correctly rejected both calls.

Both helpers now read the package name through the typed `Distribution.name` property. Empty or invalid names are still ignored, conflicting installed versions still fail closed, and the resulting dependency set remains deterministic.

## Regression protection

The release adds tests that verify:

- the Windows builder passes an absolute source-tree version-resource path;
- the relative path that failed under a generated spec is absent;
- the version-resource file is part of the builder's required-input checks;
- both release metadata helpers use `Distribution.name` without touching `PackageMetadata.get`; and
- conflicting installed versions are rejected by deterministic build provenance.

## Scope

This release does not change ATR reconstruction, the three-stage ATR-window search, Market Replay v2/v3 parsing, strategy replay, bootstrap or leave-one-day-out evidence, stable-region selection, recommendation policy, report contents, or analysis-contract versions.
