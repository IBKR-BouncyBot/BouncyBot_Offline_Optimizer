# Changelog

## 2.3.1 — 2026-08-28

Profile-identity and outward-probe correction release.

- Enforces the two-decimal invariant that `AtrProfile.key()` already assumed: every rendered float field is normalized in `__post_init__`, so two distinct profiles can no longer share one nominal key.
- Validates and stores the Market Replay ATR clamps as the exact two-decimal values that are replayed and reported, instead of validating a raw value and storing four decimals. A minimum/maximum pair that collapses onto one value once rounded is now rejected.
- Gives the reference evaluation path the same fail-closed duplicate-key guard the compact evaluator already had, so both engines fail identically instead of one silently shrinking the searched grid.
- Keeps outward search-boundary probes inside BouncyBot's GUI-enterable range: only the BUY-rebound and SELL-trail multipliers may be probed to zero; the initial-drop and minimum-profit multipliers stop at 0.01.
- Rounds outward probe values to two decimals so a probe is always expressible by the profile key that names it.
- Canonicalizes negative zero in profile identity, validates rounded candidate floors and clamp ordering, and records the exact post-normalization value in every outward-probe evidence row.
- Rejects a later profile batch when an already-populated summary key belongs to a different profile.
- Removes the dead `_subset_candidate` helper, whose cached-session leave-one-day-out approach the exact rework deliberately replaced.
- No analytical-methodology change: the Market Replay analysis contract remains at version 16, and reports for every previously valid configuration are byte-identical apart from the version string.

## 2.3.0 — 2026-08-28

Analysis-wide process-parallel robustness-validation release.

- Keeps one compact replay store and one spawned process pool alive from Stage 3 through recommendation authorization.
- Parallelizes exact leave-one-day-out selector reruns, fixed-profile omission replays, selection-aware bootstrap replicates, chronological walk-forward folds, ATR bar-phase replays, and assumption-stress scenarios.
- Preserves one-profile chronological replay order and prohibits nested process pools.
- Restores every high-level task result in deterministic caller order and fails closed on missing, duplicate, unexpected, or mismatched task identifiers.
- Adds explicit post-Stage-3 progress transitions with worker mode, completed count, total count, and pending count.
- Retains byte-identical serial/parallel analytical output and leaves the Market Replay analysis contract at version 16 because formulas, samples, search space, and authorization semantics are unchanged.

## 2.2.3 — 2026-08-28

Pyright compatibility and replay-resource hardening release.

- Unified the reference and compact replay engines on one minimal read-only ATR-vector protocol, resolving the nine reported Pyright errors for Python lists and NumPy memory maps without weakening the public calculation contract.
- Replaced platform-specific direct `os.sysconf` typing with a fail-closed optional capability probe and rejected malformed, Boolean, NaN, or infinite ATR values at the shared conversion boundary.
- Prevented equivalence collapsing when no observed sessions exist; exact behavioral equivalence must now be proven from complete effective-percentage evidence.
- Bounded the opportunistic scalar memoization used while preparing effective arrays so high-cardinality multi-million-row ATR streams cannot grow an unbounded Python dictionary.
- Hardened temporary NumPy storage: compact tick files, ATR files, and effective arrays now close memory maps before cleanup, remove failed partial files, close a first map when a paired map fails to open, and release already-opened serial sessions when a later session fails.
- Ensured evaluator cleanup continues even if process-pool shutdown raises, preventing a worker failure from leaving parent-process mappings open.
- Added focused regression tests for protocol compatibility, Pyright-sensitive paths, fail-closed memory probing, empty-evidence equivalence, bounded effective-array preparation, and injected serialization/dtype failures.

## 2.2.2 — 2026-08-27

Ruff quality-gate correction and exact refinement acceleration release.

- Removed the unused `dataclasses.replace` import from the refinement-optimization module.
- Corrected the v2.2 exact-refinement test import ordering required by Ruff 0.16.0.
- Removed the unused `_atr_cache_key` test import from the deep-audit regression module.
- Includes the v2.2.0 exact effective-profile equivalence collapsing, shared effective-percentage/clamp-state arrays, adaptive refinement batches, and memory-aware automatic worker ceiling above eight.
- Includes the v2.2.1 fail-closed nominal-result validation and explicit NumPy memory-map lifecycle hardening.
- Preserves the complete nominal candidate grid and restores every nominal profile before ranking, stable-region analysis, outward boundary probes, robustness validation, and reporting.

## 2.2.1 — 2026-08-27

Deep performance-engine audit and lifecycle hardening.

- Added fail-closed validation that process scheduling and equivalence expansion return exactly one result for each nominal profile in deterministic order.
- Explicitly closes parent-process effective-array memory maps before temporary replay-store cleanup, avoiding Windows file-handle retention.
- Added regression tests for missing/reordered profile results, duplicate nominal profiles, read-only mappings, and deterministic grouping.

## 2.2.0 — 2026-08-27

Exact Stage 3 refinement acceleration.

- Collapses only profiles proven equivalent after exact ATR multiplication, clamp classification, and two-decimal rounding over the complete prepared ATR evidence.
- Stores effective percentages as exact integer hundredths and clamp states in read-only memory-mapped arrays shared by serial and spawned workers.
- Restores all nominal profile aliases before candidate ranking and all downstream recommendation logic.
- Uses smaller adaptive task batches for large recordings and raises the memory-aware automatic worker ceiling from eight to sixteen.

## 2.1.1 — 2026-08-27

Pyright ATR-provider contract and clean-source release.

- Corrected four Pyright 1.1.411 errors introduced by the compact NumPy replay engine. The replay core now accepts one minimal read-only ATR-vector protocol implemented by both ordinary Python lists and read-only NumPy arrays/memory maps.
- Widened the reference ATR-provider callback parameters from concrete `list[IbrecTick]` values to the same `Sequence[Any]` contract accepted by the replay core. This is a type-contract correction only; the runtime objects, ATR values, indexing order, and calculations are unchanged.
- Added regression checks for the protocol, callback annotations, NumPy/list compatibility, and the exact source patterns that caused the Windows Pyright failure.
- Rebuilt the source archive without the accidentally bundled Windows `.venv`, Ruff cache, Python bytecode, or other generated local files.

## 2.1.0 — 2026-08-26

Exact compact and process-parallel Market Replay performance release.

- Added compact read-only NumPy replay arrays that precompute strategy price, valid quote state, genuine Last/bid/ask event flags, and millisecond-exact receipt timestamps once per retained row.
- Added file-backed read-only ATR arrays shared by spawned worker processes without serializing or duplicating the full Python tick-object graph.
- Parallelized independent Stage 3 coarse profiles, local refinements, and outward search-boundary probes with one persistent process pool. Candidate generation, ranking, stable-region selection, boundary decisions, bootstrap schedules, and report writing remain serial and deterministic.
- Added GUI and terminal worker controls: automatic bounded selection, exact single-process execution, or an explicit 2–64 process count. Worker count is excluded from analytical identity and report content.
- Replaced the retained equity list and second drawdown pass with exact online drawdown accounting in chronological order.
- Replaced per-component/per-second clamp-observation tuple sets with exact last-second counters.
- Added an internal normalized-configuration path so compact/profile workers do not repeatedly revalidate immutable settings.
- Kept broad screening output compact and reruns only selected profiles with full detailed evidence where the existing workflow requires it.
- Added `multiprocessing.freeze_support()` before application startup and a packaged smoke import of the compact replay module for Windows/PyInstaller spawn compatibility.
- Added NumPy to runtime and pinned Windows release dependencies.
- Added exact equivalence tests comparing the original object engine, compact serial engine, and spawned process engine down to candidate and session dictionaries; worker-count and report-determinism tests prove identical analytical output.
- Added a reproducible benchmark harness and documented measured local speedups without changing the search grid, formulas, robustness gates, or recommendation.

## 2.0.2 — 2026-08-26

Unlimited Market Replay row and recording-count release.

- Removed the 64-recording ceiling from Market Replay configuration, preflight, loading, and terminal options.
- Removed the 2,000,000 aggregate/per-recording row ceiling from format-v2 and format-v3 import, preflight, and multi-recording combination.
- Removed the obsolete `--max-ibrec-files` and `--max-ibrec-rows` terminal options.
- Advanced the Market Replay analysis contract to version 16 and records the row/count limits as absent.
- Retained byte-size, archive-expansion, ZIP-member, field-size, integrity-chain, duplicate-input, and source-mutation protections.
- Added regression coverage for more than 64 selected recordings, aggregate row totals above the former ceiling, CLI/help contracts, and source-level absence of hidden row/count checks.

## 2.0.1 — 2026-08-01

Ruff F841 quality-gate correction release.

- Removed the unused `selected_windows` local and its now-redundant aggregation from the top-level Market Replay analysis after policy-specific ATR windows had already been retained in `policy_window_sets`.
- Added an AST regression test that protects the exact correction.
- Kept ATR reconstruction, protective-policy selection, staged search, replay, calibration, robustness validation, scoring, reports, and recommendations unchanged.
- Cleaned generated pytest and bytecode cache files from the published source archive.

## 2.0.0 — 2026-07-31

Protective-SELL policy optimization release.

- Added a separate bounded Market Replay risk-policy stage that compares protective SELL disabled with manual 1–5% and ATR-adaptive 1.5–4.5x native trailing SELL policies at the unchanged ATR control.
- Requires an adjacent multi-point policy region, minimum protective-exit/day evidence, practical score improvement, tail-risk non-inferiority, and ATR-clamp identifiability before one enabled policy can advance.
- Runs the three-stage ATR-window and multiplier search separately for the disabled control and the one supported enabled policy, preventing one policy from borrowing another policy's selected ATR windows.
- Requires the final complete ATR-plus-protective-policy profile to pass all existing paired, bootstrap, leave-one-day-out, phase, policy, Pareto, boundary, stress, continuity, selection-aware, and walk-forward authorization gates.
- Models protective placement immediately after BUY, controller-style stop normalization, genuine-Last native trailing behavior, fresh-bid market fills, overnight carry, pending fills, and cancellation before normal profit-taking SELL replacement.
- Adds descriptive stop-out recovery, later normal-activation, additional-loss-avoided, recovery-regret, and overnight-exit diagnostics without using those hindsight diagnostics to rank candidates.
- Adds `protective_sell_policy_comparison.csv`, `protective_sell_trade_diagnostics.csv`, corresponding HTML/JSON evidence, and protective fields throughout candidate, session, continuity, and recommendation exports.
- Advances the Market Replay analysis contract to version 15 and adds focused regression tests for protective order semantics, policy-region selection, policy-specific ATR-window search, overnight behavior, report contracts, and conservative fallback.

## 1.9.4 — 2026-07-28

Same-date Market Replay fragment-merging and diagnostic-evidence release.

- Stitches verified non-overlapping same-date RTH fragments instead of excluding the complete date. The deterministic selector prioritizes wall-clock coverage, then live-only coverage, normal-close evidence, retained strategy rows, fewer stitches, and a content-derived tie-break.
- Preserves real inter-fragment outages in the rebased monotonic clock so ATR freshness, quote age, event-gap, and Last-density quality gates remain authoritative. Overlapping tick streams are never interleaved.
- Excludes a date when retained same-date fragments disagree on scheduled RTH boundaries; empty fragments cannot invalidate otherwise usable evidence.
- Deduplicates an identical shared boundary row, rejects conflicting equal-time boundaries, and never stitches a fragment whose receipt clock reverses.
- Corrected the interval selector so boundary compatibility is checked against the actual final fragment in each candidate chain rather than an unrelated best prefix with the same end time.
- Resolves format, manifest, and connectivity provenance across every recording contributing to a merged period; incomplete provenance fails closed.
- Adds `recording_fragment_evidence.csv` plus HTML/JSON fragment diagnostics and individual recording hashes in quality exclusions.
- Names the offending file for multi-recording preflight/load failures and rejects zero-row recordings during preflight.
- Advances the Market Replay analysis contract to version 14 and adds regression tests for coverage-first selection, schedule conflicts, empty fragments, merged gaps, clock reversal, exact boundary handling, chain compatibility, provenance, report evidence, input-order determinism, and filename diagnostics.

## 1.9.3 — 2026-07-28

Calibration date-key correction and report-integrity release.

- Corrected the silent defect where date-cross-fitted BUY/SELL execution-cost and trade-notional calibration was never applied: recording periods use `YYYYMMDD` session dates while calibration emitted ISO `YYYY-MM-DD` override keys, so every per-date lookup missed and the uniform effective values were used instead. Producer and consumer now share one `canonical_session_date` helper, and override validation rejects canonically equivalent duplicate dates.
- Corrected follow-on calibration defects exposed by the date-key repair: disabled cost/notional calibration toggles now suppress both global and per-date replacements; date-cross-fitted trade notionals may move below the configured default instead of being incorrectly floored like execution costs; one notional sample is retained per completed BUY cycle even when several cycles have identical dates and values; and ambiguous multi-date BUY cycles are excluded from date-specific notional evidence.
- Execution timestamps are now assigned to the recorded exchange-session date, with contract-time-zone fallback, rather than to their UTC calendar date. This prevents same-session fills from leaking into prior-only calibration for exchanges whose RTH session crosses midnight UTC.
- Canonical override dates are now stored in normalized configuration, so semantically identical `YYYYMMDD` and ISO inputs produce the same deterministic configuration and report identity. Empty non-string date keys fail closed.
- Corrected Market Replay CSV exports that derived their header from the first row only: heterogeneous evidence rows (mixed combine-time and quality-gate session exclusions; gate-skipped versus evaluated robustness rows) silently lost columns for the whole file. Columns are now the union across all rows with the caller's order leading; homogeneous files remain byte-identical.
- Emitted the combined-recording exclusion summary only after both exclusion passes (overlap/fragment dates and no-retained-rows periods) so the reported count can no longer be understated or omitted.
- Loaded `kernel32` with `use_last_error=True` in the Windows process-liveness helper so the ACCESS_DENIED comparison reads a real saved error code instead of always zero.
- Moved the Market Replay GUI preflight (recording copy and content-hash verification) onto a worker thread so multi-gigabyte selections no longer freeze the window; the fail-closed checks and the analysis worker's own re-verification are unchanged.
- Hardened the asynchronous preflight against stale completion races: changing or clearing the recording set while hashing is in progress can no longer restore an obsolete Ready state, and unexpected worker exceptions are surfaced instead of leaving the interface stuck in Checking state.
- Single-sourced all interpolated quantiles through `optimizer.utils.percentile`, aligned the initial-drop sensitivity floor with the trading app's 0.01 GUI minimum, treated stored cycle number 0 as known in the capture-inventory ordering, replaced a disguised folder-name length constant with named constants, removed a shadowing smoke-test import, and documented the validation-only `normalized()` call in refinement.
- Added focused regression tests for calibration date-key application, canonical normalization, disabled-toggle behavior, cross-fitted notional direction and sample counting, stale preflight completion, unexpected preflight exceptions, and CSV column union.
- Bumped the application version so content-addressed analysis IDs change with this release; identical inputs re-analyzed under 1.9.3 publish new report directories instead of colliding with pre-fix output.
- Same-version release correction: validation now applies minimum/maximum bounds to the exact rounded notional and execution-cost values that are stored, and prior-only cross-fitted trade-notional estimates are clamped to the 0.01 currency-unit minimum. This prevents sub-cent notionals from rounding to zero and prevents near-10,000 bps values from rounding across the permitted execution-cost boundary.

## 1.9.2 — 2026-07-26

Market Replay preflight presentation and Windows test clarification release.

- Restored human-readable verified-format labels in the Market Replay preflight: `v2 ZIP`, `v3 SQLite`, or both.
- Corrected the native PySide6 regression test to check the semantic label case-insensitively and reject the obsolete numeric-list text.
- Added pure presentation coverage for single-format, mixed-format, duplicate, aggregate-fallback, and unknown-format summaries.
- Added a source-contract audit that limits runtime skips to the optional PySide6 module and operating-system symbolic-link capability checks.
- Documented that the six Windows skips are security integration tests blocked by missing symbolic-link privilege, not application failures.
- Kept ATR reconstruction, search, replay, robustness analysis, calibration, scoring, reporting, and recommendation behavior unchanged.

## 1.9.1 — 2026-07-26

Ruff quality-gate correction release.

- Removed the unused `control_profile` local assignment reported by Ruff 0.16.0 as `F841` in `tests/test_v190_robust_selection_validation.py`.
- Added a focused source-contract regression that protects the exact correction.
- Kept ATR calculations, staged search, bootstrap, leave-one-day-out, walk-forward validation, replay, scoring, stable-region selection, calibration, reports, and recommendation behavior unchanged.

## 1.9.0 — 2026-07-26

- Added exact raw-chronology leave-one-day-out reselection across all three ATR-search stages.
- Added expanding chronological walk-forward validation on unseen session blocks.
- Added deterministic selection-aware out-of-bag and circular moving-block bootstrap analysis.
- Added separate BUY/SELL p50, p75, and p90 execution calibration with date-cross-fitted assumptions.
- Single-sourced balanced scoring and added drawdown-focused, return-focused, and cost-stressed policy checks.
- Added Pareto-frontier rejection, search-boundary extension, deterministic assumption stress, and overnight economic-block evidence.
- Added named recommendation-quality gates and new HTML, JSON, and CSV evidence exports.
- Increased the Market Replay analysis contract to version 13 and added focused regression/property tests.

## 1.8.2 — 2026-07-25

Pyright and numeric-evidence hardening release.

- Corrected the ten optional numeric-conversion diagnostics reported by the native Windows Pyright 1.1.411 gate.
- Pinned Pyright 1.1.411 in the normal and reproducible Windows validation environments.
- Added shared exact finite-integer parsing and reused the existing finite-float boundary across database, model, recording, and quality code.
- Rejected Boolean, fractional, non-finite, overflowing, precision-losing, and pathological-exponent values at untrusted numeric boundaries.
- Required validated Market Replay contracts to contain a finite positive minimum tick instead of silently substituting a cent.
- Prevented fractional broker error codes from being truncated into connectivity codes.
- Prevented unknown component provenance from inheriting format-3 finalization behavior.
- Corrected text `false` connectivity evidence and configuration flags so they cannot become true through generic Python truthiness.
- Added regression coverage for every reported Pyright site and the additional numeric, provenance, and configuration cases found during the audit.
- Kept ATR calculations, staged search, continuous overnight replay, SQLite execution calibration, bootstrap, leave-one-day-out, scoring, stable-region selection, and recommendation behavior unchanged for valid inputs.

## 1.8.1 — 2026-07-25

Ruff quality-gate correction release.

- Corrected the five Ruff diagnostics reported by the native Windows v1.8.0 quality gate.
- Normalized import ordering in the GUI, reproducible-ZIP helper, and continuous-replay/calibration tests.
- Removed one unused type import and one unused leave-one-day-out local assignment.
- Pinned Ruff 0.16.0 in both normal and reproducible Windows dependency sets so the validated ruleset cannot drift between runs.
- Added regression coverage for the exact reported source conditions and the Ruff pin.
- Kept ATR calculations, staged search, continuous overnight replay, execution calibration, robustness analysis, scoring, stable-region selection, and recommendation behavior unchanged.

## 1.8.0 — 2026-07-25

Continuous overnight replay, actual-execution calibration, and real-recording methodology hardening release.

- Added default continuous replay of open long positions and already-submitted SELL trails across provably consecutive primary-eligible RTH recordings.
- Preserved modeled BUY basis, quantity, cumulative equity, continuity-chain drawdown, locked SELL trail, running high, pending bid-side fill, and detailed trade identity across supported boundaries.
- Re-warmed ATR after an overnight HOLD before deriving an unsubmitted normal SELL; kept already-submitted SELL trail parameters locked.
- Broke continuity conservatively on missing weekdays or ambiguous holidays, partial or failed-quality sessions, overlaps, absent closing bid marks, and disabled overnight mode.
- Added `continuity_evidence.csv` plus per-session and per-trade overnight/chain evidence.
- Added optional read-only execution calibration from a stopped BouncyBot `bot_state.sqlite` folder without requiring `debug_captures`.
- Preferred exact conId/currency cycle identity; used legacy ticker fallback only when no exact cycle was available.
- Derived broker-order commission, no-future same-side quote slippage, combined adverse-cost percentiles, and median actual BUY notional from deduplicated executions.
- Added cycle-level commission fallback for execution rows whose row-level commission was absent or less authoritative, without double-counting mirrored normal/protective SELL totals.
- Applied only supported calibration evidence, retained configured floors and values when sample counts were insufficient, and excluded incompatible commission currencies.
- Added GUI and CLI controls for the calibration folder, sample count, quote age, cost/notional replacement, and overnight replay.
- Added deterministic `execution_calibration.csv`; calibration content hashes, not absolute paths, participate in analysis identity.
- Incorporated the real-recording audit improvements developed after v1.6.0: primary session-quality gates, connectivity and Last-density evidence, top-of-book size checks, execution-cost and turnover penalties, clamp-saturation diagnostics, minimum-clamp search, representative window-screen mini-grids, and exploratory short-circuiting below the robust-day threshold.
- Added regression coverage for overnight exits, active trails, Friday-to-Monday continuity, missing-day breaks, ATR re-warm, pending fills, chain drawdown, source-lock safety, exact contract matching, quote chronology, commission currency, cycle-level commission fallback, path-independent calibration reports, and SQLite-only calibration folders.
- Made bootstrap resampling continuity-aware: flat outcomes remain trading-day units, while days linked by an overnight position or active SELL order form one indivisible block; changed recommendations require at least five independent units.
- Replayed candidate and control exactly after each omitted day and blocked changed selection when overnight-linked stage-search stability cannot be established exactly from omission-specific search evidence.

## 1.6.0 — 2026-07-21

Multi-recording Market Replay and recommendation-soundness release.

- Added simultaneous analysis of one or more format-v2/v3 `.ibrec` recordings for one provably identical instrument.
- Added duplicate path/content rejection, aggregate row/byte limits, canonical content identity, and deterministic input-order independence.
- Excluded complete dates with overlapping recordings or interrupted same-day fragments instead of splicing unproven ATR/order/position state.
- Added GUI add/remove/clear controls and multi-path terminal input.
- Added `input_recordings.csv` and `excluded_sessions.csv` evidence.
- Audited ATR reconstruction against BouncyBot's simple-average formula, freshness horizon, percentage clamps, rounding, and monotonic clock.
- Added five-second ATR bar-phase stress and adverse per-session phase aggregation.
- Rebuilt all three search stages in every leave-one-day-out run, including omission-specific multiplier refinement.
- Distributed stage-3 refinement seeds across selected ATR windows.
- Tightened fill modeling to require the recorded same-side touch and retained triggered orders as pending until executable evidence appears.
- Included immediate spread drawdown and prohibited stale pre-entry bids from marking later open positions.
- Added candidate/control same-day return, participation, tail-risk, complete-outcome, bootstrap, leave-one-day-out, and phase authorization gates.
- Kept exactly one complete profile per analysis; unsupported changes fall back to the unchanged control.
- Added deterministic multi-file, aggregate-limit, overlap, timing, fill, marking, stability, report, and regression tests.

## 1.5.3 — 2026-07-20

Windows portable-archive and source-audit correction release.

- Made deterministic ZIP executable metadata host-independent by recognizing existing execute bits, shebang scripts, and native Windows executables.
- Applied the same executable classification to source manifests so Windows and POSIX release preparation agree.
- Replaced full-tree `rglob` traversal with a pruned top-down source walk that does not descend into virtual environments or generated/private trees.
- Made the source text audit consume the release-source file list instead of scanning third-party packages inside `.venv`.
- Added regression tests for the exact Windows failures, executable metadata, source filtering, and deterministic modification-time behavior.
- Kept ATR calculations, Market Replay v2/v3 semantics, staged search, robustness evidence, recommendation selection, and report output unchanged.

## 1.5.2 — 2026-07-20

Windows release-gate correction release.

- Passed the Windows version-resource file to PyInstaller as an absolute source-tree path so generated pass-specific spec files cannot reinterpret it below `build/spec-*`.
- Added the version-resource file to the builder's required-input checks before creating the release environment.
- Replaced unsupported `PackageMetadata.get()` calls with the typed `Distribution.name` property in release-environment verification and deterministic build provenance.
- Added regression tests for the exact PyInstaller path failure, both Pyright diagnostics, invalid metadata names, and conflicting installed versions.
- Kept ATR calculations, Market Replay v2/v3 semantics, staged search, robustness evidence, recommendation selection, and report output unchanged.

## 1.5.1 — 2026-07-20

Ruff SIM103 quality-gate correction release.

- Replaced the final conditional-return block in `scripts/create_source_manifest.py` with the equivalent direct Boolean return required by Ruff 0.15.22.
- Added regression coverage for both the direct-return source shape and the manifest exclusion behavior.
- Kept source-manifest contents, private-data exclusions, ATR calculations, replay behavior, robustness analysis, recommendations, and report output unchanged.

## 1.5.0 — 2026-07-20

Three-stage ATR-window search, day-level robustness, and reproducible-release build.

- Replaced the four coupled ATR period/bar points in the Market Replay workflow with a three-stage screen that first compares bar duration, then ATR period, then entry/exit multipliers.
- Added periods 10 and 28 and retained the unchanged 14-period/60-second control throughout window narrowing.
- Added default 2,000-replicate whole-trading-day bootstrap evidence for stable-region centers versus the unchanged control on identical RTH sessions.
- Added default leave-one-trading-day-out influence analysis and blocked changed recommendations when any omission is non-positive or reverses the result.
- Added deterministic ATR-window, robustness, and recommendation-specific leave-one-day-out report exports.
- Kept exactly one complete ATR profile per recording and retained the unchanged control when changed evidence is unstable.
- Confirmed the optimizer uses the same PolyForm Noncommercial 1.0.0 license as BouncyBot and linked package metadata to the license file.
- Strengthened `.gitignore` for databases, `.ibrec` recordings, captures, reports, credentials, signing material, environments, caches, and release output.
- Added exact Windows dependency locks, strict environment verification, fixed reproducibility environment variables, two-pass PyInstaller tree comparison, deterministic build provenance, and repeat-verified deterministic ZIP creation.
- Incremented the Market Replay analysis contract to prevent reuse of reports generated under the previous search contract.

## 1.4.5 — 2026-07-20

Pyright quality-gate correction release.

- Corrected all 16 Pyright 1.1.411 errors reported by the v1.4.4 Windows quality gate after Ruff passed.
- Reused the shared fail-closed finite-number parser for untyped ATR and presentation values.
- Narrowed optional captured ATR values, paired-evidence deltas, Market Replay open-position prices, and format-3 manifest-source metadata before use.
- Made the Windows quality gate pass its exact virtual-environment interpreter to Pyright and declared the local `.venv` in project configuration so installed PySide6 modules can be resolved.
- Added focused runtime and source-contract tests for every reported diagnostic category.
- Kept optimizer algorithms, candidate grids, `.ibrec` v2/v3 semantics, scoring, recommendations, and report results unchanged.

## 1.4.4 — 2026-07-20

Ruff quality-gate correction release.

- Corrected all ten Ruff 0.15.22 diagnostics reported by the v1.4.3 Windows quality gate after its complete pytest suite passed.
- Removed five unused imports and one unused local assignment from the Market Replay optimizer without changing its calculations.
- Marked the format-3 integrity row counter as an intentionally ignored loop target while retaining the post-loop manifest count check.
- Normalized the three import blocks reported by Ruff in the ATR module and architecture/safety tests.
- Reordered the Windows and POSIX quality gates so compilation, Ruff, and Pyright run before the longer coverage test suite.
- Added source-contract regression coverage for the exact reported lint conditions and quality-gate ordering.
- Kept SQLite/capture analysis, Market Replay v2/v3 ingestion, ATR search, scoring, recommendation selection, report contents, and deterministic output semantics unchanged.

## 1.4.3 — 2026-07-20

Windows retry-test robustness release.

- Corrected the remaining Windows-only atomic-publication test failure reported against v1.4.2.
- Removed the invalid assumption that a real Windows directory move must succeed on the first non-injected retry.
- Made low-level retry unit tests fully simulated so antivirus, indexing, and synchronization software cannot alter their expected call counts.
- Updated the end-to-end report-publication test to verify injected failures, bounded retries, one staging source, one final destination, and successful publication rather than an environment-specific exact move count.
- Restored real bounded backoff in the end-to-end Windows-lock test instead of replacing sleep with a no-op.
- Kept both optimizer algorithms, Market Replay v2/v3 support, candidate scoring, report contents, and production atomic-publication behavior unchanged.

## 1.4.2 — 2026-07-20

Windows publication-test isolation release.

- Corrected the remaining Windows-only end-to-end atomic-publication test failure from v1.4.1.
- Replaced process-wide monkeypatching of the shared `os.replace` function with a module-local publication move hook.
- Kept the production operation as the same atomic `os.replace` directory move and retained all v1.4.1 transient-Windows retry behavior.
- Added an assertion that fault injection leaves the process-wide `os.replace` callable unchanged.
- Confirmed that optimizer algorithms, replay behavior, candidate scoring, report interpretation, and deterministic publication semantics are unchanged.

## 1.4.1 — 2026-07-20

Windows quality-fix release.

- Added retrying atomic directory publication for transient Windows access-denied, sharing-violation, and lock-violation errors.
- Kept publication atomic: no partial-copy fallback is used, and non-transient filesystem errors still fail immediately.
- Applied the same publication helper to SQLite/capture and Market Replay reports.
- Replaced the Python-version-sensitive nested-AST literal-f-string test with a lexical token check compatible with Python 3.11 and Python 3.12+.
- Removed two genuine literal-only f-string prefixes found by the corrected detector.
- Added focused regression tests for transient rename locks, non-transient failures, end-to-end report publication, nested dynamic format specifications, and genuine Ruff `F541` cases.

## 1.4.0 — 2026-07-20

Independent Market Replay ATR optimization release.

- Added a separate desktop tab and terminal workflow for one `.ibrec` recording.
- Added strict Market Replay format-2 ZIP and format-3 SQLite ingestion.
- Verified format-3 metadata, record hashes, tick/event chains, RTH digest, and committed checkpoint.
- Accepted valid format-3 recovery state with an active RTH period and blank committed observed end; derive a conservative observed end from committed ticks and retain right-censoring.
- Added strict format-3 lifecycle/source validation, rollback-journal copy/recovery, WAL/SHM rejection, source-component mutation checks, and final symlink revalidation.
- Preserved monotonic sequence/`elapsed_ns` ordering when the recorder receipt clock moves backwards; report the clock anomaly and prevent stable-evidence labelling.
- Validated every raw event row, then compressed only replay-irrelevant size/volume/high-low updates while retaining Last events, full snapshots, price/feed changes, first/final rows, and UTC-second state; report raw and retained row counts separately.
- Added synthetic, delayed-only, mixed live/delayed, frozen-interruption, crossed-quote, and receipt-clock evidence gates.
- Added path-independent, content-addressed deterministic input identity for renamed or moved recordings.
- Added full-RTH-period replay of anchor, initial drop, BUY rebound, holding, minimum-profit activation, and SELL trail.
- Added a bounded coarse ATR grid with deterministic local refinement.
- Added BouncyBot-compatible UTC ATR bars, percentage clamps, Last-event native-trail semantics, minimum-tick stop rounding, and minimum-profit stop protection.
- Added conservative quote-touch fill approximation and explicit reporting of execution-model limits.
- Added right-censoring for open positions, unresolved BUY trails, and recordings ending while future entries remain possible; treat a BUY trail as cancelled when the observed interval proves the configured cancellation boundary was reached.
- Added a right-censoring penalty and stability rejection above a 20% censored-session rate.
- Added connected near-best parameter-region selection and exactly one complete profile per recording.
- Added a separate deterministic HTML/JSON/CSV evidence report and CLI JSON summary.
- Added frozen-package smoke imports for the new parser, optimizer, report writer, and GUI tab.
- Added v2/v3 parser, tamper, safety, replay, report, determinism, CLI, and optional native-GUI tests.
- Added bundled `tzdata` support for portable Windows format-2 session reconstruction.

## 1.3.0 — 2026-07-19

Paired, censoring-aware, execution-adjusted robustness release.

- Compared every changed candidate with the exact evaluation control only on identical cycle IDs.
- Added fail-closed exclusion and reporting for duplicate candidate/control observations and immutable cycle-context mismatches.
- Reclassified valid capture-end non-triggers as right-censored and added Kaplan-Meier trigger probabilities at 1, 5, and 15 minutes.
- Added longest-mutually-supported censoring horizon selection; changed recommendations require at least five minutes of shared follow-up.
- Added deterministic 2,000-replicate trading-day cluster bootstrap intervals and probability-positive estimates.
- Added leave-one-trading-day-out influence analysis, sign-reversal detection, and most-influential-day reporting.
- Added empirical per-ticker, per-leg execution adjustment from saved bid/ask touches, spread, and adverse fill residuals, with cycle-level leave-one-out exclusion.
- Excluded fill-touch quotes older than five seconds and impossible crossed quotes from touch/spread residuals.
- Excluded a cycle from the execution model when candidate rows disagree on immutable fill or quote context, and made such conflicts block a changed primary suggestion.
- Corrected the stability gate so a paired estimate that actually used zero empirical residual samples cannot inherit a larger aggregate sample count.
- Added stable-parameter-region detection that rejects isolated grid peaks and highlights only the center of the preferred supported plateau.
- Added trigger-probability, timing-deterioration, adverse-excursion, bootstrap, influence, execution-sample, and stable-region gates.
- Continued to emit exactly one complete ATR settings set per ticker; retain the unchanged evaluation control whenever changed evidence is unstable.
- Added paired-evidence and execution-model CSV exports plus substantially expanded report explanations.
- Incremented the deterministic analysis contract so v1.3 evidence cannot reuse a pre-v1.3 content-addressed report directory.
- Added regression coverage for same-cycle pairing, censoring, shared horizons, deterministic bootstrap, leave-one-day-out behavior, execution quote handling, context mismatches, timing deterioration, stable plateaus, and non-future fill quotes.

## 1.2.0 — 2026-07-19

Primary per-ticker evaluation-profile release.

- Fixed the Ruff `F541` error in `optimizer/presentation.py` by removing the unnecessary `f` prefix from a literal tooltip string.
- Added a source-contract test that rejects literal-only f-strings throughout `main.py`, `optimizer/`, and `tests/`.
- Added one deterministic **settings set to evaluate next** to every ticker result.
- Placed the highlighted set between **Data coverage** and **Actual ATR settings and changes between cycles** in each ticker HTML report.
- Added a conservative selection rule: only a baseline-window profile that changes exactly one independently replayed BUY or normal-SELL leg can be highlighted; require five complete-context windows, three triggers, and a five-point improvement over the exact control when available.
- Added a fail-safe fallback: when no one-leg change meets the evidence rules, retain the normalized evaluation control rather than inventing an unsupported change; keep the actual saved source values separate when replay normalization was required.
- Added the selected set to per-ticker JSON and a new `<TICKER>_primary_settings_to_evaluate.csv` file.
- Separated actual saved evaluation-control evidence from the normalized, GUI-enterable counterfactual replay control so reports never claim that a repaired legacy value actually ran.
- Constrained ATR periods, bar durations, multipliers, and clamps to current BouncyBot GUI limits for replay and suggestions while preserving original SQLite values in provenance tables.
- Streamed capture CSV members instead of loading a complete large CSV into memory, deduplicated repeated archive diagnostics, loaded the optional audit-events table deterministically, and tightened integer/boolean parsing.
- Corrected coverage accounting so a structurally valid archive with zero usable price rows is not counted as a usable capture.
- Expanded architecture-audit, malformed-input, deterministic-output, capture-streaming, event-loading, and normalization tests.
- Expanded report and methodology documentation to explain why the highlighted set is a paper-evaluation experiment rather than an optimized live configuration.
- Added deterministic tie-breaking, changed-input-order, current-control fallback, historical-control fallback, report ordering, JSON/CSV export, and repeated-analysis tests.
- Corrected the highlighted-profile ranking so a legitimate screening score of exactly zero is not treated as missing or negative infinity; the control-relative delta is now recomputed from the displayed candidate and control scores instead of trusting a duplicated cached field.
- Made the highlighted report evidence table use the same canonical score and control delta as the selector.
- Added containment checks for SQLite WAL/SHM symlinks before fingerprinting or snapshot staging.
- Rejected duplicate and future-leaking evidence paths, preserved explicit zero-valued capture metadata, and made report JSON serialization fail closed on unsupported objects.
- Updated the methodology to describe the actual evaluation-control hierarchy, normalized replay clamps, candidate grid, and median absolute timing-error penalty.
- Added a hidden packaged-executable smoke mode and made the Windows build fail unless the frozen executable can load the complete PySide6 GUI runtime and exit successfully within 30 seconds.
- Isolated the cross-process determinism test from inherited coverage/tracing environment variables so child optimizer processes cannot overwrite or truncate the parent quality gate's coverage data.

## 1.1.0 — 2026-07-19

Explained, settings-aware, deterministic reporting release.

- Fixed the three Ruff `I001` import-formatting errors reported for `main.py`, `optimizer/reports.py`, and `tests/test_safety.py`.
- Added a GUI result-table glossary: every column header and every result cell has a derivation tooltip.
- Added an **Open report** button to every ticker row.
- Expanded the ticker HTML reports with coverage-score construction, ATR reconstruction, BUY/SELL replay, screening-score, settings-provenance, candidate-selection, capture-matching, limitations, and column-glossary explanations.
- Clarified that **Historical median baseline (derived from actual stored cycle ATR settings)** is the control summary calculated from cycle-row settings. It is not a fitted recommendation and can be a synthetic field-wise summary when settings varied.
- Added exact historical ATR profile IDs, chronological settings regimes, cycle-by-cycle settings provenance, and an exact current `app_settings.strategy` snapshot when applicable.
- Added pooled candidate results and per-historical-profile subgroup metrics so changed settings are visible instead of being silently treated as one configuration.
- Made candidate semantics consistent across cycles: period, bar duration, multipliers, and historical-median ATR clamps remain fixed for one candidate, while each observation retains its original historical profile as provenance.
- Added content-addressed report identity. Identical input bytes and analysis limits produce the same run ID, directory name, and report bytes, independent of wall-clock time, source path, output path, and Python hash seed.
- Added regression proof that modification-time-only changes do not alter report identity or bytes.
- Added idempotent report publication: an existing content-addressed report is reused only when every generated byte matches; divergent content is rejected.
- Prevented captured-ATR fallback from reading future rows.
- Prevented captured-ATR fallback from being attributed to a candidate unless the exact ATR period and bar duration are present on that cycle or in its capture-event strategy snapshot.
- Preserved same-timestamp quote/selected-price updates unless the full market-data row is semantically identical.
- Added CSV fallback when a present JSONL member has no usable price rows.
- Added source/capture symlink escape rejection, lock-file cleanup on partial acquisition failure, deterministic SQLite tie ordering, invalid-clamp rejection, and zero-multiplier immediate-market regression coverage.
- Rejected report output paths that replace or sit underneath the bot lock, SQLite main file, WAL, SHM, or capture directory.
- Converted incompatible-database CLI failures into a controlled exit-code-3 message without a traceback.
- Corrected the no-history report explanation so documented fallback defaults are never described as an observed ATR profile.
- Added deterministic-output, settings-history, presentation, optional GUI, capture-integrity, source-safety, replay-boundary, and regression tests.

## 1.0.0 — 2026-07-19

Initial release of BouncyBot Offline Optimizer.

- Added source-byte-preserving SQLite/WAL staging and temporary snapshot analysis without opening the production database in SQLite.
- Added safe capture-ZIP inventory and validation without extraction.
- Added per-ticker data-coverage grading.
- Added capture-window BUY rebound and SELL activation/trail counterfactual replay.
- Added chronological SELL activation replay that recalculates ATR settings until activation, then locks the effective trail and minimum-profit values.
- Added correct separation of normal and protective exits, including BouncyBot's mirrored final-SELL history fields.
- Added execution-row fallback for incomplete legacy cycle fill fields.
- Added candidate-specific ATR evidence rules so alternate windows cannot reuse an unrelated bot-captured ATR value.
- Added per-ticker ATR profiles to evaluate, with explicit evidence and limitation labels.
- Added local HTML, JSON, and CSV reports with formula-safe CSV text, atomic publication, and SHA-256 manifests.
- Added GUI and terminal workflows with explicit confirmation and trading-bot lock coordination.
- Added portable Windows PyInstaller build using a unique runtime directory for side-by-side placement.
- Added source SQLite/WAL state verification to abort if an unexpected writer ignores the lock.
- Added automated safety, replay-boundary, protective-exit, malformed-input, report-escaping, atomic-output, CLI, and end-to-end tests.
