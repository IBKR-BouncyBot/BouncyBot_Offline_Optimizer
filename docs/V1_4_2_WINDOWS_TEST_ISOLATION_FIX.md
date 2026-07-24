# BouncyBot Offline Optimizer v1.4.2 — Windows publication-test isolation fix

Version 1.4.2 is a narrowly scoped corrective release for the remaining Windows test failure reported against v1.4.1. It does not change either optimizer algorithm, the ATR search grids, replay semantics, candidate scoring, evidence gates, stable-region selection, or report interpretation.

## Reported Windows failure

The complete v1.4.1 Windows suite collected 236 tests. It completed 229 tests successfully, skipped six tests that require Windows symlink privileges, and failed one end-to-end atomic-publication test:

```text
assert calls == 2
E       assert 4 == 2
```

The report itself was generated successfully. The failure was in the test's call counter, not in report publication.

## Root cause

The test replaced `atomic_publish.os.replace`. Python modules share the same imported `os` module object, so assigning to that attribute also replaced the process-wide `os.replace` function seen by unrelated code during the complete report run. Windows performed additional replace operations outside the atomic-publication helper, causing the test to count four process-wide calls instead of the two publication attempts it intended to measure.

## Correction

The publication helper now keeps its rename callable in a module-local `_replace_directory` hook initialized from `os.replace`. Fault-injection tests replace only that hook. They no longer mutate the shared standard-library `os` module.

The end-to-end regression test now verifies both conditions:

- the publication hook is called exactly twice when the first simulated Windows access-denied error is retried;
- the real process-wide `os.replace` callable remains unchanged.

The production operation remains the same atomic `os.replace` directory move with the bounded transient-Windows retry policy introduced in v1.4.1.

## Release impact

The fix affects test isolation only. Runtime report content, source-read safety, deterministic-output rules, and atomic all-or-nothing publication behavior are unchanged apart from the normal version-dependent analysis identity.
