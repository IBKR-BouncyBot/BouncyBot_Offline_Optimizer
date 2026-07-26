# BouncyBot Offline Optimizer v1.9.2

## Market Replay preflight presentation and Windows test clarification

Version 1.9.2 corrects the one failing native-PySide6 test reported by the
Windows `run_all_tests.bat` gate for v1.9.1. The multi-recording preflight had
regressed from a human-readable container description to the opaque text
`format(s) [3]`. The GUI now reports verified inputs as `v2 ZIP`, `v3 SQLite`,
or `v2 ZIP / v3 SQLite` as applicable.

The six skipped tests in the supplied Windows run were not application
failures. They are security integration tests that must create filesystem
symbolic links. Windows returned error 1314 because the test account did not
have symbolic-link creation privilege. Those tests continue to execute on
systems where symbolic links are available; the test suite now also audits
that runtime skips are limited to the approved optional PySide6 and
symbolic-link capability checks.

The GUI regression test now checks the human-readable format semantically and
case-insensitively, while rejecting the old numeric-list wording. Pure
presentation tests cover v2, v3, mixed-format, duplicate-input, and defensive
fallback labels without requiring PySide6.

This release does not change ATR reconstruction, the three-stage ATR-window
search, multiplier or clamp search, replay state, execution calibration,
bootstrap analysis, exact leave-one-day-out reselection, walk-forward
validation, scoring, stable-region selection, reports, or recommendation
authorization.
