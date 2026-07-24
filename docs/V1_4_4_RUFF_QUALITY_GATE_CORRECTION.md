# BouncyBot Offline Optimizer v1.4.4 — Ruff quality-gate correction

Version 1.4.4 corrects the ten Ruff 0.15.22 diagnostics reported by the Windows `run_all_tests.bat` quality gate for v1.4.3. The same Windows run had already completed all 236 pytest cases with 230 passes and six expected environment-dependent symlink skips; the failure occurred only when Ruff ran after coverage.

## Corrections

- Removed unused `asdict`, `Path`, `IbrecError`, `mean`, and `median` imports from `optimizer/market_replay.py`.
- Removed the unused `control` local assignment after stable recommendation selection.
- Changed the format-3 integrity row counter to an underscore-prefixed loop target while preserving the manifest row-count validation after iteration.
- Corrected the import boundary in `optimizer/atr.py`.
- Separated the standard-library and first-party local imports in the Windows process-probe test.
- Corrected alphabetical first-party import ordering in the v1.2 architecture-audit tests.

No optimizer algorithm, candidate profile, simulation rule, score, stability gate, report calculation, safety rule, or input format changed.

## Faster failure ordering

The Windows and POSIX quality scripts now run these source checks before the longer branch-coverage suite:

1. Python compilation
2. Ruff
3. Pyright
4. pytest under branch coverage with `ResourceWarning` promoted to an error
5. coverage reports and threshold enforcement

The gate remains read-only with respect to source files: Ruff is invoked in check mode, not automatic-fix mode.

## Regression protection

A v1.4.4 source-contract test verifies the exact corrected imports, the intentional format-3 count target, removal of the unused assignment, and quality-gate ordering. The authoritative Windows release gate remains:

```powershell
.\run_all_tests.bat
.\scripts\build_windows.ps1 -RunTests
```
