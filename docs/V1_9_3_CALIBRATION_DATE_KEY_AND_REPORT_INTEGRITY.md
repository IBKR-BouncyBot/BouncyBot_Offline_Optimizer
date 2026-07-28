# BouncyBot Offline Optimizer v1.9.3

## Calibration date-key correction and report integrity

Version 1.9.3 corrects a silent defect in the v1.9.0 date-cross-fitted
execution calibration. Recording RTH periods store session dates as
`YYYYMMDD`, while the calibration module emitted its per-date BUY/SELL
execution-cost and trade-notional overrides keyed by ISO `YYYY-MM-DD` dates.
The replay simulator looked overrides up by the raw period date, so every
date-specific lookup missed and the uniform effective values were used for
every session, while the calibration evidence and report could still describe
the date-specific values as applied. Producer and consumer now normalize
through one shared `canonical_session_date` helper, override validation
rejects canonically equivalent duplicate dates, and a regression test drives
an ISO-keyed override through the simulator against a `YYYYMMDD` period.
Normalized configuration stores that canonical ISO key as well, so equivalent
date spellings cannot create different content-addressed identities.

Review of the now-active per-date path found three masked calibration defects.
The cost and notional replacement toggles previously disabled only the uniform
assumption while still allowing per-date overrides to be emitted; both layers
now honor the toggle. Trade-notional cross-fitting also reused the conservative
execution-cost floor and therefore could never reduce a configured notional,
even though the documented behavior permits the median actual BUY notional to
replace it. Costs remain floor-increasing, while notionals now use a distinct
median/shrinkage path that can move in either direction. Finally, date-specific
notional evidence is counted once per completed BUY cycle, preserving distinct
cycles with equal values and excluding cycles whose BUY executions span more
than one UTC date.

Execution rows are now dated by the matching recorded RTH period, with the
contract's exchange time zone as the fallback. Using the raw UTC calendar date
would otherwise classify a fill in the same exchange session as earlier-day
evidence when that market's RTH interval crosses midnight UTC.

Market Replay CSV exports previously derived their header from the first row
alone. Tables that legitimately mix row schemas — session exclusions recorded
at combine time next to quality-gate exclusions, and gate-skipped robustness
rows next to evaluated ones — silently lost the extra columns for the whole
file. The writer now emits the union of columns across all rows with the
caller's order leading; files whose rows share one schema remain
byte-identical. The combined-recording exclusion summary is also emitted only
after both exclusion passes, so its count can no longer be understated or
omitted, and the Windows process-liveness helper now loads `kernel32` with
`use_last_error=True` so its ACCESS_DENIED comparison reads a real saved
error code.

The Market Replay GUI preflight, which copies and hash-verifies every
selected recording, now runs on a worker thread so multi-gigabyte selections
no longer freeze the window. The fail-closed checks are unchanged and the
analysis worker still re-copies and re-verifies everything itself. Smaller
consistency corrections single-source all interpolated quantiles through
`optimizer.utils.percentile`, align the initial-drop sensitivity floor with
the trading application's 0.01 GUI minimum, treat a stored cycle number of
zero as known in the capture-inventory ordering, name the report-folder
length constants, and remove a shadowing smoke-test import.

The asynchronous preflight also ignores results that became stale while a user
changed or cleared the selection. Unexpected worker exceptions are converted
to a blocked diagnostic instead of leaving the GUI indefinitely in its amber
Checking state.

Because report content changes, the application version participates in the
content-addressed analysis identifiers: identical inputs re-analyzed under
1.9.3 publish new report directories instead of colliding with pre-fix
output. This release does not change ATR reconstruction, the three-stage
ATR-window search, replay state, bootstrap analysis, exact leave-one-day-out
reselection, walk-forward validation, scoring, stable-region selection, or
recommendation authorization beyond applying the calibrated date-specific
assumptions as originally documented.

## Same-version notional-bound correction

The re-released v1.9.3 source also validates the exact rounded values that the
normalized Market Replay configuration stores. Trade notionals are rounded to
cents before enforcing the 0.01 minimum, and execution-cost values are rounded
to their stored precision before enforcing the strict 10,000 bps upper bound.
The prior-only date-cross-fitted trade-notional path now applies the same 0.01
minimum as the global and shrinkage paths. These changes affect only malformed
or pathological boundary inputs; normal calibration and ATR recommendation
behavior is unchanged.
