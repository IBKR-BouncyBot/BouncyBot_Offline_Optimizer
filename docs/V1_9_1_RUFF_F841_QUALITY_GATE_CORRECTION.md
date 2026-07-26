# BouncyBot Offline Optimizer v1.9.1

## Ruff F841 quality-gate correction

Version 1.9.1 corrects the single Ruff 0.16.0 `F841` diagnostic reported by the native Windows `run_all_tests.bat` gate for v1.9.0. The diagnostic was an unused local assignment in the selection-aware-bootstrap regression test.

The unused assignment has been removed. The test behavior and all optimizer algorithms are unchanged. A focused AST-based regression confirms that the reported function no longer assigns the unused name.

This release does not change ATR reconstruction, the three-stage ATR-window search, multiplier or clamp search, fixed-profile or selection-aware bootstrap, moving-block bootstrap, exact leave-one-day-out reselection, walk-forward validation, replay state, scoring, stable-region selection, execution calibration, report calculations, or recommendation authorization.
