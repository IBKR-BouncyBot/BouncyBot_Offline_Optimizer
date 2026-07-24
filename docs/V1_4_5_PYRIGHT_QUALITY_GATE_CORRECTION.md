# BouncyBot Offline Optimizer v1.4.5 — Pyright quality-gate correction

Version 1.4.5 corrects the 16 Pyright 1.1.411 errors reported by the Windows `run_all_tests.bat` quality gate for v1.4.4. Ruff completed successfully before Pyright stopped the gate. The same Pyright run also reported three unresolved PySide6 imports because the checker was not explicitly given the virtual-environment interpreter used by the quality script.

## Type-safety corrections

- ATR multiplier, period, bar-duration, clamp, and captured-ATR normalization now route untyped SQLite/JSON values through the shared fail-closed finite-number parser before arithmetic.
- Captured ATR selection carries a proven non-optional float beside each candidate row instead of repeatedly converting an optional model attribute.
- Stable-region ranking stores the optional paired delta once and narrows it before applying the descending sort key.
- Format-3 integrity, RTH-only validation, and preflight provenance use a typed dictionary-copy helper for optional manifest `source` metadata.
- Market Replay end-of-session marking derives a separately narrowed open-entry price before division.
- GUI result formatting uses the shared finite-number parser for optional coverage values and rejects booleans as numeric evidence.

These changes preserve the existing fail-closed analysis behavior while making the invariants explicit to Pyright.

## Virtual-environment discovery

The Windows quality script now invokes:

```powershell
& $python -m pyright --pythonpath $python
```

The project configuration also declares the repository-local `.venv`. Pyright therefore resolves the same installed PySide6 package used by compilation, pytest, and the Windows build rather than an unrelated interpreter.

## Regression protection

The v1.4.5 tests verify:

- malformed untyped ATR values fall back safely while valid values are unchanged;
- optional and non-finite captured ATR rows are filtered before selection;
- missing and zero paired deltas remain distinct during stable-region ranking;
- absent or malformed format-3 manifest-source metadata becomes an empty typed dictionary;
- presentation helpers and result rows handle missing or boolean numeric values safely;
- the Windows script passes its exact `.venv` interpreter to Pyright; and
- the project-local Pyright virtual-environment configuration remains present.

## Scope

This release does not change ATR formulas, candidate grids, Market Replay v2/v3 parsing semantics, cycle replay, candidate scoring, stable-region selection, SQLite/capture analysis, recommendation policy, or report interpretation.
