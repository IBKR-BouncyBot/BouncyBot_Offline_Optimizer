# Testing

## Automated coverage

The suite covers:

- absent, pre-existing, and race-created bot locks;
- atomic acquisition, double-acquire rejection, PID-write failure cleanup, and owned-lock-only removal;
- source database/capture symlink escape rejection;
- SQLite/WAL/shared-memory and complete capture-set mutation detection;
- read-only SQLite/WAL staging, committed-WAL inclusion, and source-byte preservation;
- schema-tolerant loading, deterministic tie ordering, large-event-table avoidance, and decision-event filtering;
- execution fallback for incomplete cycle fills and protective-exit separation;
- corrupt, oversized, malformed, duplicate-member, stale-only, JSONL, CSV, and unusable-JSONL-with-CSV capture handling;
- no-extraction ZIP traversal resistance and capture symlink boundaries;
- retention of distinct same-timestamp market/quote updates and removal of only semantically identical rows;
- ATR reconstruction, candidate clamps, zero immediate-market behavior, historical fallback, future-fallback rejection, and alternate-window fallback rejection;
- directionally valid faster/smoother windows at small and large settings boundaries;
- exact BUY and SELL trail boundaries, manual minimum profit, order-after-window behavior, and chronological SELL activation;
- explicit triggered/right-censored/unavailable outcomes, non-future fill-quote selection, and Kaplan-Meier horizon support;
- historical setting profiles, A → B → A regimes, current-settings separation, field-wise median summaries, missing-field defaults, and per-profile candidate metrics;
- candidate aggregation, zero/negative score ordering, evidence thresholds, and alternate-window ATR-coverage gating;
- identical-cycle candidate/control pairing, duplicate and immutable-context fail-closed exclusions, candidate-only/control-only triggers, and right-censored paired outcomes;
- deterministic trading-day cluster bootstrap intervals, input-order independence, and leave-one-day-out influence/sign-reversal detection;
- empirical BUY/SELL spread and slippage adjustment, cycle-level leave-one-out, stale and crossed quote exclusion, Last fallback, conflicting same-cycle fill-context rejection, and preservation of real zero per-pair sample counts;
- true-grid adjacency, isolated-peak rejection, lower robust plateau selection, deterministic stable-region center, and timing/MAE/trigger-probability gates;
- deterministic one-profile selection, one-leg/baseline-window eligibility, full robustness-gate enforcement, exact tie-breaking, and unchanged-control fallback;
- primary-profile HTML placement, JSON identity, single-row CSV export, and repeated-analysis equality;
- deterministic report identity and byte output in the same process, across source/output paths, and in subprocesses with different `PYTHONHASHSEED` values;
- deterministic identity and byte output after modification-time-only changes to otherwise identical source files;
- refusal to overwrite a divergent existing content-addressed directory;
- refusal to use the bot lock, SQLite main/WAL/SHM paths, or anything below those files as the report root;
- exact-period/bar requirements for bot-captured ATR fallback and no ticker-median substitution when the capture context is unknown;
- controlled CLI handling of an incompatible SQLite schema without a traceback or leaked lock;
- accurate no-history report wording that distinguishes documented defaults from observed settings;
- source-controlled HTML escaping and spreadsheet-formula neutralization;
- GUI tooltip definitions, ticker report paths, and—when PySide6 is installed—per-row report-button wiring;
- CLI success, cancellation, safety errors, JSON result, and rejection of disabled capture hashes;
- version, release-documentation, build, confirmation, no-network, and wrapper-variable contracts;
- absence of literal-only f-strings across application and test Python sources.

The cross-process determinism test removes inherited coverage/tracing startup
variables from its child environment. This keeps the child optimizer process
independent and prevents third-party coverage startup hooks from replacing the
parent quality gate's coverage data at child exit.

## Windows quality gate

```text
compileall
Ruff
Pyright
pytest with ResourceWarning=error
branch coverage with 85% minimum
```

The historical v1.1.0 Windows run completed 103 tests with two environment skips, then Ruff reported one `F541` literal-only f-string in `optimizer/presentation.py`. Version 1.2.0 removed that prefix and added a source-wide regression check. Version 1.4.1 makes that check lexical-token based so it behaves consistently under Python 3.11 and Python 3.12+, removes two additional genuine literal-only prefixes, and adds simulated Windows directory-lock tests for both the atomic helper and complete SQLite report publication. Version 1.4.2 isolates the injected directory-move callable inside the publication module so the end-to-end test no longer mutates or counts unrelated process-wide `os.replace` operations on Windows. Version 1.4.3 removes the remaining environment-dependent exact retry-count assumption: the integration test now verifies the injected failures, bounded retry behavior, single publication target, and completed report while allowing a real scanner or indexer to add another legitimate transient denial. Version 1.4.4 corrects the ten Ruff diagnostics reported after the complete v1.4.3 pytest run and moves compilation, Ruff, and Pyright ahead of coverage so source-quality failures stop the gate before the longer test pass. Version 1.4.5 corrects the subsequent Pyright scalar/optional diagnostics, passes the exact project-local interpreter through `--pythonpath`, and declares the same `.venv` in project configuration so installed PySide6 modules are resolved without disabling missing-import checks. The full Windows gate remains the authoritative confirmation because Ruff, Pyright, and native PySide6 may not be installed in every offline build environment.

The Windows release builder also launches the generated `BouncyBotOfflineOptimizer.exe` in a hidden smoke-test mode. That mode imports the complete GUI entry module without creating a window. The build fails if the frozen executable or bundled PySide6 runtime cannot load and exit successfully within 30 seconds.

## Packaged smoke testing

A Windows release candidate should additionally verify:

- clean-machine launch without Python installed;
- source and output folder browsing;
- cell/header tooltip display;
- every ticker's **Open report** button;
- the highlighted **One settings set to evaluate next** section and its single-row CSV;
- paired-evidence, execution-model, censoring, bootstrap, influence, and stable-region explanations/CSV exports;
- user cancellation;
- existing-lock blocking;
- concurrent BouncyBot launch blocking while analysis holds the lock;
- deterministic rerun reuse;
- local HTML opening;
- side-by-side placement beside BouncyBot;
- source file hashes unchanged after analysis.

## v1.4 Market Replay tests

Version 1.4 adds tests for both `.ibrec` containers and the separate workflow:

- format-2 ZIP schema, checksums, duplicate members/columns, line, row, and expansion limits;
- format-3 SQLite schema, `user_version`, integrity checks, foreign keys, per-record hashes, chain hashes, RTH digest, and checkpoints;
- the supplied Market Replay Lab 1.3.0 format-3 sample;
- source-copy verification, mutation detection, rollback-journal recovery/state checks, WAL/SHM rejection, final-component replacement/symlink rejection, and path-independent report identity;
- valid active-period recovery with no committed observed end, receipt-clock reversal handling, strict monotonic `elapsed_ns`, empty-recording preflight, and full-analysis empty-data rejection;
- raw-row integrity validation followed by semantics-preserving size/volume-event compression;
- UTC-aligned simple ATR reconstruction and exact Last-event trail triggering;
- full-cycle BUY/SELL simulation, stop normalization, minimum-profit protection, crossed-quote exclusion, feed selection, exact cancellation-boundary classification, right-censoring, and conservative open-position treatment;
- synthetic, delayed-only, mixed live/delayed, frozen-interruption, and receipt-clock evidence stability gates;
- bounded grid/refinement, stable-region selection, one-profile output, and deterministic reports;
- terminal success/failure behavior and independence from the trading-bot lock;
- optional native PySide6 preflight, result table, tooltips, and report button wiring.

The Windows quality gate remains authoritative for Ruff, Pyright, native PySide6, and frozen PyInstaller execution. The release builder's hidden smoke mode imports the full GUI plus Market Replay parser and report modules before accepting the executable.

The Windows build also collects the `tzdata` package required for portable format-2 timezone/liquid-hours reconstruction.


## v1.5 three-stage search, robustness, and release reproducibility tests

Version 1.5 adds regression coverage for:

- independent stage-1 bar-duration and stage-2 ATR-period selection;
- unconditional retention of the unchanged 14-period/60-second control;
- stage-3 candidate confinement to the narrowed ATR windows;
- same-session candidate/control pairing;
- deterministic 2,000-replicate trading-day or overnight-continuity-block bootstrap intervals;
- leave-one-day-out influence, sign-reversal, and single-day-dominance rejection;
- fallback to the unchanged control when a changed candidate is unstable;
- new report files and explanations;
- byte-identical deterministic ZIP output despite source modification-time changes;
- exact release-lock, license, `.gitignore`, and Windows build-script contracts;
- importability and fail-closed behavior of the release helper scripts.

A production Windows release additionally requires two independently built PyInstaller directory trees to compare byte-for-byte before packaging. The normalized ZIP is recreated and its hash must match the first archive.

## v1.5.3 Windows archive and source-audit regression tests

Version 1.5.3 adds cross-platform tests for deterministic executable metadata and source-tree isolation. A shebang script and native Windows executable must receive mode `0755` in the archive even when Windows reports no POSIX execute bit, while plain shell text and PowerShell scripts remain `0644`. The source manifest and source text audit share the same release-source filtering rules and prune `.venv`, `.venv-release`, custom `.venv-*`, build, release, cache, capture, and generated-report trees before traversal.

## v1.6 multi-recording and recommendation-soundness tests

Version 1.6 adds regression coverage for immutable multi-path configuration, duplicate path/content rejection, same-instrument validation, aggregate row/byte limits, deterministic input-order independence, multi-file report determinism, monotonic ATR timing, stale-gap warm-up reset, five-second phase stress, same-side touch fills, immediate spread drawdown, stale pre-entry bid rejection, unmarked open positions, right-censor authorization failure, refinement-window distribution, complete report inventories, and path-independent component fingerprints. Version 1.9.4 replaces complete-date overlap exclusion with coverage-first same-date fragment selection and adds tests for deterministic stitching, preserved gaps, sparse-complete versus dense-partial selection, schedule conflicts, complete multi-source provenance, fragment CSV/HTML/JSON evidence, filename-specific failures, and input-order independence.

The complete release gate still promotes `ResourceWarning` to an error, enforces branch-aware coverage, compiles all Python files, and requires Ruff and Pyright on Windows. The portable build remains a two-pass byte-comparison PyInstaller build using the exact release lock.

## v1.8 continuous replay and calibration tests

Version 1.8 adds regression coverage for:

- Friday-to-Monday overnight continuity and conservative weekday/holiday breaks;
- preservation of cost basis, quantity, realized equity, continuity-chain drawdown, submitted SELL-trail state, pending SELL fills, and detailed trade identity;
- ATR re-warm before an unsubmitted next-session SELL and locked handling of an already-submitted native SELL trail;
- refusal to carry an open long without a valid closing bid mark;
- disabling overnight replay and terminalizing unresolved state at each RTH boundary;
- whole-day bootstrap grouping by overnight-continuity block;
- SQLite-only calibration folders without `debug_captures`;
- normal bot-lock exclusion, private database snapshotting, exact conId preference, ticker-only legacy fallback, and path-independent calibration fingerprints;
- no-future same-side quote matching, quote-age limits, explicit same-price refreshes, crossed/stale quote rejection, and commission-currency rejection;
- broker-order grouping, row-level versus cycle-level commission evidence, 75th-percentile conservative cost reserve, minimum sample gates, and independent cost/notional replacement toggles;
- deterministic calibrated reports, new continuity/calibration CSV exports, GUI/CLI controls, and version/release metadata.

A production Windows release still requires Ruff, Pyright, native PySide6 interaction, and the two-pass reproducible PyInstaller build from the exact source archive. Linux validation does not substitute for those Windows-specific gates.

## v1.9 robust-selection regression coverage

The v1.9 tests additionally cover:

- one immutable balanced score definition shared by calculation and reports;
- drawdown-focused, return-focused, and cost-stressed policy comparisons;
- Pareto dominance and rejection of fully dominated candidates;
- exact raw-date leave-one-out chronology reconstruction;
- expanding walk-forward fold construction, training-only selection, and validation drawdown rebasing;
- deterministic moving-block samples and selection-aware out-of-bag bootstrap;
- overnight-dependency union across candidate and control profiles;
- separate BUY/SELL p50/p75/p90 execution evidence and date-cross-fitted assumptions;
- search-boundary detection, outward extension, and unresolved-boundary rejection;
- execution, quote-age, notional, and timing assumption stress;
- economic overnight-continuity metrics and named recommendation-gate completeness;
- deterministic reports containing every new evidence CSV.

The Linux release gate may execute the suite in disjoint processes when the host's third-party pytest instrumentation does not terminate cleanly after a large combined run. Every collected test file must still be included exactly once, `ResourceWarning` remains fatal, and coverage is accumulated across the disjoint processes before the 85% branch-aware gate is evaluated. The authoritative Windows gate remains `run_all_tests.bat` followed by the reproducible PyInstaller build.

## v2.0 protective SELL policy tests

Version 2.0 adds focused coverage for:

- protective profile validation, stable content keys, and JSON/report fields;
- manual and ATR-adaptive effective percentages and clamp accounting;
- BouncyBot-compatible initial protective-stop normalization and minimum-tick rounding;
- genuine-Last native trail movement and rejection of quote-only cached Last triggers;
- market-style protective exits waiting for a fresh bid;
- normal minimum-profit SELL replacement cancelling the protective order;
- active and already-triggered protective orders crossing a verified overnight boundary;
- ATR re-warm behavior and descriptive normal-activation thresholds after an overnight HOLD;
- stop-out recovery, later normal activation, avoided-loss, regret, and bounded observation evidence;
- adjacent protective-policy region construction and deterministic centre selection;
- rejection of isolated stop-policy winners, insufficient exits/days, tail deterioration, and clamp-bound ATR policies;
- strict separation of policy-specific Stage 1/2 ATR windows in Stage 3;
- separate refinement-seed allocation and stable-region adjacency across policies;
- conservative fallback to the disabled unchanged control;
- deterministic HTML, JSON, CSV, report-manifest, and analysis-identity output.

The final native Windows gate remains responsible for Ruff, Pyright, PySide6
thread/UI behavior, and the reproducible two-pass PyInstaller executable build.

Version 2.0.2 adds regression coverage proving that Market Replay accepts more
than the former 64-recording ceiling, accepts aggregate row totals above the
former 2,000,000-row ceiling, omits the retired terminal flags, exposes no
hidden count fields in `MarketReplayConfig`, and carries the unlimited policy
into analysis-contract evidence.

## v2.1 exact replay-performance tests

Version 2.1 adds regression coverage for the execution engine rather than a new
analytical method. The tests require:

- compact tick arrays to preserve every replay-relevant timestamp, price,
  quote-size, snapshot, and event flag;
- the original object evaluator, compact single-process evaluator, and spawned
  process evaluator to produce identical candidate and session dictionaries;
- one-worker and multi-worker complete analyses to have the same analysis ID,
  search contract, candidates, and recommendation;
- complete reports generated with different worker counts to be byte-identical;
- worker count to be validated but excluded from analytical identity;
- a worker exception to abort analysis rather than omit a batch;
- NumPy to be a declared runtime dependency and pinned in the reproducible
  Windows lock;
- `multiprocessing.freeze_support()` to run before the packaged application
  entry point;
- temporary memory maps to close before their directories are removed;
- deterministic profile-key ordering after out-of-order worker completion.

Version 2.3 extends those execution-only requirements to post-search
validation. Tests require one pool instance to survive profile evaluation and
high-level task submission; exact leave-one-day-out selector rows, ATR phase
evidence, assumption-stress evidence, walk-forward folds, and selection-aware
bootstrap results to match their serial paths; task results to be restored in
request order; worker failure to abort; and post-Stage-3 progress to name the
active operation with completed and pending counts.

A reproducible benchmark helper compares exact one-worker and multi-worker
runs. Performance measurements are informational and cannot replace the
result-equivalence assertions.
