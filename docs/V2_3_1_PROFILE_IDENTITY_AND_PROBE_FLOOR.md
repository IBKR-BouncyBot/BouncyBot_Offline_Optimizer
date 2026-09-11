# BouncyBot Offline Optimizer v2.3.1

## Profile identity and outward-probe correction

Version 2.3.1 corrects three defects found in a deep audit of the v2.3.0 tree.
None of them could produce an incorrect trading recommendation.

### 1. `AtrProfile.key()` was not lossless (medium)

`AtrProfile.key()` renders every float field with two decimals and is the
identity used for the summary and session dictionaries, the effective-array
equivalence catalog, the stable-region hash, and the bootstrap seed.
`MarketReplayConfig.normalized()` nevertheless stored the ATR clamps with four
decimals, and `_coarse_profiles` unions the configured minimum clamp with the
fixed `(0.01, 0.05, 0.10, 0.20)` candidate set. A configured `min_atr_pct` of
`0.105` therefore produced two distinct `AtrProfile` objects sharing the key
`...-min0.10-max20.00-...`: 406 coarse profiles collapsed to 325 distinct keys.

The consequence depended on which engine ran, which is selected automatically
from recording size and worker count:

- the reference path evaluated both profiles and let the second overwrite the
  first in `summaries`, so the searched grid silently shrank with no warning;
- the compact path aborted the analysis with
  `Distinct ATR profiles produced the same nominal profile key`.

Identical input therefore either under-searched or crashed.

The fix enforces the invariant at the one object every producer constructs:
`AtrProfile.__post_init__` now rounds `initial_drop_multiplier`,
`buy_rebound_multiplier`, `minimum_profit_multiplier`, `sell_trail_multiplier`,
`min_atr_pct`, `max_atr_pct`, and `protective_sell_value` to two decimals. This
covers the configuration, the coarse grid, refinement, window profiles, and
outward boundary probes at once. `MarketReplayConfig.normalized()` now rounds
the clamps *before* validating them, so the reported contract matches the values
actually replayed and a pair that collapses once rounded is rejected rather than
accepted with a sub-hundredth gap.

`_evaluate_profiles` also gained the duplicate-key guard the compact evaluator
already had, so the two engines now fail closed identically.

Note: `protective_sell_value` previously stored four decimals. It is normalized
to two because `key()`, `protective_policy_label`, and BouncyBot's own GUI all
express it with two. Every value the search generates is already two-decimal, so
no searched profile changes.

The final review additionally canonicalizes rounded negative zero to positive
zero, enforces the non-zero floors of the initial-drop and minimum-profit
multipliers, and validates the rounded ATR-clamp ordering on every directly
constructed profile. These checks prevent equal values from acquiring different
`-0.00`/`0.00` keys and prevent an invalid direct candidate from bypassing the
configuration-level validation.

### 2. Outward probes below the GUI multiplier floor (low)

`_boundary_extension_profiles` floored every outward lower probe at `0.0`.
BouncyBot accepts zero only for the BUY-rebound and SELL-trail multipliers,
where it selects immediate market-style behavior; the initial-drop and
minimum-profit multipliers have a GUI minimum of `0.01`. Probes with a `0.0`
initial-drop or minimum-profit multiplier became full candidates and appeared in
`candidate_results.csv` and the near-best and Pareto analysis.

The existing clamp-saturation gate already prevented such a profile from being
recommended — at multiplier `0.0` the effective percentage is permanently
minimum-clamp-bound, so the 90% saturation gate rejects it — but a value no user
can enter should never enter the candidate tables. Probes now respect the
per-dimension floor and are rounded to two decimals.

Probe evidence now records the value after `AtrProfile` normalization. This is
important for clamp probes: a raw half-step such as `0.025` is replayed and keyed
as `0.03`, and the CSV/JSON evidence now reports `0.03` rather than the
pre-normalization input.

### 3. Dead `_subset_candidate` helper (low)

`_subset_candidate` had no remaining callers. It derived a leave-one-day-out
summary by filtering cached session results, which is precisely the approach the
v2.3 exact leave-one-day-out rework replaced with a full subset re-replay so
overnight continuity is rebuilt. It has been removed so a future edit cannot
reintroduce the stale-state defect.

## Verification

- Complete test collection: 522 passed, 1 skipped (GUI/PySide6), 0 failures,
  against 501 passed / 0 failures on the unmodified v2.3.0 tree. The 21
  additional tests are in
  `tests/test_v231_profile_identity_and_probe_floor.py`.
- Combined statement/branch coverage: 85.7%, above the configured 85% floor.
- Market Replay reports for the default configuration republished
  byte-identically against the pre-change content-addressed output before the
  version string was bumped.
- Serial and three-process spawned execution remain exactly equivalent:
  identical analysis identity, identical recommendation, and identical scores
  across all candidates with zero session-field differences.
- The formerly colliding `min_atr_pct=0.105` configuration now completes in both
  engines, agrees exactly between them, reports an effective clamp of `0.10`,
  and produces a candidate set whose keys are all distinct.

The release contains no intended analytical-methodology change. The Market
Replay analysis contract remains at version 16.
