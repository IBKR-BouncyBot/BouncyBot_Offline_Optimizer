# BouncyBot Offline Optimizer v1.5.3 — Windows portable-archive and source-audit fixes

Version 1.5.3 corrects two Windows-only failures reported by the native v1.5.2 quality gate. The ATR algorithms, `.ibrec` v2/v3 interpretation, staged search, bootstrap, leave-one-day-out analysis, stable-region selection, and generated recommendations are unchanged.

## Portable executable metadata

Windows does not preserve POSIX executable bits when a test calls `chmod` or when a source tree is extracted normally. The deterministic ZIP helper previously relied only on `st_mode`, so a shebang script such as `run.sh` could be archived as mode `0644` on Windows even though the same file was archived as `0755` on POSIX.

Release metadata now uses a host-independent classification:

- retain any existing POSIX execute bit;
- treat native `.exe` and `.com` binaries as executable; and
- treat files beginning with a shebang (`#!`) as executable.

PowerShell and batch files are not marked executable merely because of their suffix. The source-manifest helper uses the same classification, so executable metadata is consistent between Windows and POSIX release preparation.

## Source-audit isolation

The source text audit previously used `Path.rglob()` over the complete repository and filtered `.venv` files only after traversal. A normal Windows `run_all_tests.bat` invocation creates `.venv`, so the audit inspected third-party package documentation and reported trailing whitespace outside this project.

The source manifest now walks the tree top-down and prunes virtual environments, build output, release output, report trees, caches, captures, and other generated/private directories before descending into them. The source text audit consumes that same release-source file list. This prevents third-party files from affecting project quality checks and also avoids traversing large virtual environments during source-manifest creation.

## Regression protection

Tests now verify that:

- a shebang script receives mode `0755` even when the host reports `0644`;
- a Windows `.exe` receives executable archive metadata;
- a non-shebang `.sh`, `.ps1`, or `.bat` file is not made executable solely by suffix;
- source manifests use the same portable executable classification;
- `.venv`, `.venv-release`, custom `.venv-*`, build, release, and generated report trees are excluded before source auditing; and
- deterministic ZIP bytes remain independent of source modification times.
