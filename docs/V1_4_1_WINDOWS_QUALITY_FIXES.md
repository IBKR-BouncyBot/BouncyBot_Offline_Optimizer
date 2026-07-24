# BouncyBot Offline Optimizer v1.4.1 — Windows quality fixes

Version 1.4.1 is a corrective release for the v1.4.0 Market Replay release. It does not change the SQLite/capture optimization algorithm, the Market Replay ATR candidate grid, scoring, stable-region selection, or report interpretation.

## Corrected Windows report publication

A completed report is still written to a private same-filesystem staging directory and published only after all HTML, JSON, CSV, manifest, and checksum files are complete. On Windows, antivirus scanners, indexers, or synchronization software can briefly hold that newly written directory and make the final atomic directory rename fail with `WinError 5`, `WinError 32`, or `WinError 33`.

The publisher now retries only those transient access-denied, sharing-violation, and lock-violation errors while repeating the same atomic rename. It never falls back to copying a partially completed report into the final content-addressed directory. Non-transient errors still fail immediately. The same helper is used by both the SQLite/capture and Market Replay report writers.

## Corrected Python 3.11 source-quality regression test

The literal-only f-string regression test previously walked every nested `JoinedStr` AST node. Python 3.11 can expose nested format-specification nodes differently from newer Python versions, causing valid strings such as nested dynamic format specifications to be reported as dozens of false positives.

The test now examines actual lexical f-string tokens and supports both the Python 3.11 single-token representation and the Python 3.12+ split f-string token representation. It still detects genuine Ruff `F541` cases, including escaped-brace f-strings without replacement fields.

The audit also found and removed two real literal-only f-string prefixes in `optimizer/analysis.py` and `optimizer/replay.py` that had been hidden by the previous version-dependent test behavior.

## Regression coverage

New tests simulate transient Windows directory locks, confirm retry timing and eventual atomic publication, verify immediate propagation of non-transient filesystem errors, exercise the complete SQLite report writer through the retry helper, and verify the f-string detector against nested dynamic format specifications and genuine literal-only f-strings.
