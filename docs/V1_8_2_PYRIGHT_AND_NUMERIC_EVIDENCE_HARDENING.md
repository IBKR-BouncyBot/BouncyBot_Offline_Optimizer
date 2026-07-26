# BouncyBot Offline Optimizer v1.8.2

## Purpose

Version 1.8.2 corrects the optional-value conversion failures reported by the native Windows Pyright 1.1.411 quality gate and extends the review into a broader fail-closed audit of numeric and provenance boundaries.

The ATR formula, three-stage ATR-window search, multiplier search, continuous overnight replay, execution calibration, candidate scoring, whole-day bootstrap, leave-one-day-out analysis, stable-region selection, and recommendation authorization rules are unchanged.

## Reported Pyright corrections

The Windows quality gate identified direct `float()` and `int()` calls whose source values were typed as optional untrusted metadata. The affected paths were:

- instrument minimum-tick identity and compatibility checks in `optimizer/ibrec.py`;
- source format-version classification in `optimizer/market_replay_quality.py`;
- connectivity-event timestamp parsing in `optimizer/market_replay_quality.py`.

These paths now narrow values through shared finite-number parsers before comparison or arithmetic. Missing, Boolean, fractional, non-finite, overflowing, and unsupported values fail closed rather than reaching a direct conversion.

Both the normal Windows quality environment and reproducible Windows release environment now pin Pyright 1.1.411, the version that reported the diagnostics. Validation can therefore be repeated without silently changing the type-checker ruleset.

## Additional numeric hardening

The audit also corrected related behaviors that could produce a valid-looking but incorrect analysis:

- Integer metadata is parsed exactly instead of first passing through binary floating point. Large conIds, sequences, byte counts, and identifiers are therefore not rounded.
- Fractional values are rejected for integer fields. For example, broker error code `1100.5` can no longer be truncated into disconnect code `1100`.
- Binary-float integers beyond the exact `2**53` range are rejected. Exact larger values remain supported through integer or textual representations.
- Pathological decimal exponents are rejected before they can request an excessive integer allocation.
- Booleans are rejected as prices, tick sizes, periods, identifiers, counts, and safety limits.
- Overflow raised by custom or unusual numeric objects is handled as invalid evidence.

## Recording identity and minimum tick

A Market Replay recording must now provide a finite positive minimum tick in its validated contract metadata. Missing, zero, negative, Boolean, NaN, infinity, and malformed values are rejected during import.

The optimizer no longer silently substitutes `0.01` for malformed imported evidence. Such a substitution could alter stop rounding and make two different instrument identities appear compatible. The defensive `0.01` property fallback remains only for manually constructed in-memory objects; validated recordings cannot reach it with invalid metadata.

## Provenance and finalization

A combined recording has no single container format version. The owner component of an RTH period must therefore resolve to a supported format-2 or format-3 source before that period can be treated as finalized.

Unknown or malformed component-version provenance now fails closed. It cannot inherit format-3 finalization semantics merely because another part of a combined object is valid.

## Connectivity evidence

Connectivity-event interpretation now uses the optimizer's explicit truth parser. Text such as `"false"` is no longer truthy merely because it is a non-empty Python string.

Event timestamps must be finite. Missing, NaN, infinite, or malformed timestamps are ignored and cannot classify a session as disconnected or connected at an invented time.

## Configuration booleans

The following Market Replay controls must be actual Boolean values:

- continuous overnight replay;
- execution-cost calibration;
- trade-notional calibration.

Text such as `"false"` is rejected instead of becoming `True` through Python's general Boolean coercion.

## Verification intent

Regression tests cover the exact Pyright-reported source patterns and the broader malformed-input cases above. The source release should still receive the normal native Windows gate:

```powershell
.\run_all_tests.bat
.\scripts\build_windows.ps1 -RunTests
```
