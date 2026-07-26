# Architecture

## Pipeline

```text
User confirmation
  -> normalize source/output paths and analysis limits
  -> validate bot_state.sqlite, debug_captures, symlink boundaries, and absent bot lock
  -> atomically acquire ibkr_trading_bot.lock
  -> hash and record SQLite/WAL/shared-memory and capture-archive source state
  -> copy SQLite main/WAL files through ordinary read-only file access
  -> open only the private copy and create a standalone temporary snapshot
  -> schema-tolerant deterministic dataset load
  -> bounded capture ZIP manifest inventory without extraction
  -> derive a content-based analysis ID from input bytes and analysis limits
  -> reconstruct exact ATR profiles, chronological regimes, and per-cycle settings
  -> match fill-centred captures to BUY, normal SELL, and protective SELL fills
  -> run fixed counterfactual candidates per ticker
  -> estimate conservative fills from saved spread and empirical adverse slippage
  -> pair every candidate with the exact control on identical cycles
  -> classify trigger outcomes, including right-censored capture endings
  -> calculate trading-day bootstrap, leave-one-day-out, and stable-region evidence
  -> aggregate pooled and per-historical-profile metrics
  -> select one deterministic paper-evaluation settings set per ticker
  -> verify SQLite/WAL/shared-memory and every capture archive did not change
  -> release the bot lock
  -> assemble reports in a temporary sibling directory
  -> atomically publish or byte-verify the content-addressed HTML/JSON/CSV report
```

The core analysis uses only the Python standard library. PySide6 is isolated to `optimizer/gui.py`; terminal mode and the core automated suite can run without Qt.

## Modules

- `safety.py`: portable paths, source-boundary checks, shared lock lease, private SQLite snapshot, source-state verification, and SHA-256 hashing.
- `database.py`: schema inspection and deterministic grouped records from the private snapshot.
- `captures.py`: bounded ZIP validation, duplicate-member rejection, manifest parsing, JSONL/CSV fallback, freshness filtering, and semantic row de-duplication.
- `settings_history.py`: exact ATR snapshots, stable profile IDs, chronological regimes, field-wise historical medians, and current-settings applicability.
- `atr.py`: fixed-time OHLC bars, true range, simple-average ATR, candidate windows, clamps, and chronological historical-window fallback.
- `replay.py`: native trailing BUY and chronological normal-SELL activation/trail capture-window simulations; explicit trigger/censoring outcomes; quote context; pooled and historical-profile subgroup metrics.
- `evidence.py`: empirical execution adjustment, identical-cycle pairing, Kaplan-Meier trigger probabilities, deterministic trading-day bootstrap, leave-one-day-out influence, stability gates, and adjacent-region detection.
- `analysis.py`: ticker association, fill recovery, protective-exit separation, coverage grading, candidate generation, conservative primary-profile selection, and end-to-end source consistency checks.
- `determinism.py`: canonical serialization, input fingerprint, content-based run identity, and evidence-derived data-through timestamp.
- `presentation.py`: testable GUI column definitions, row-specific tooltips, and per-ticker report paths.
- `atomic_publish.py`: retry-safe atomic directory publication for transient Windows scanner/indexer locks; never copies a partial report into its final content-addressed path.
- `reports.py`: deterministic local HTML/JSON/CSV files, atomic publication, idempotent byte verification, manifest, and SHA-256 checksums.
- `gui.py` and `cli.py`: user interaction, explicit confirmation, progress, per-ticker report opening, and exit codes.

## Concurrency boundary

The optimizer never opens the production database with SQLite. It uses the same portable-folder lock as BouncyBot, copies the main database and WAL through read-only filesystem operations, and performs every SQL query against a private standalone snapshot. Capture ZIPs are read while the lock is held. Report generation starts only after all source reads and before/after source verification are complete.

The lock is the primary coordination mechanism. Byte-level before/after checks on SQLite, WAL, shared memory, and every capture ZIP are additional fail-closed controls for a writer that ignores the lock.

## Deterministic-output boundary

The analysis fingerprint includes:

- optimizer version and analysis-contract version;
- SQLite and WAL file names, sizes, and SHA-256 values;
- capture relative paths, sizes, and SHA-256 values;
- archive-size and row-count analysis limits.

It excludes wall-clock run time, file modification time, absolute source paths, absolute output paths, temporary directory names, and Python hash order. All report collections are ordered canonically. An existing directory with the same analysis ID is accepted only if its complete file tree is byte-identical to the newly generated tree.

## Evidence and primary evaluation-profile boundary

The local replay score remains available for continuity and descriptive sorting, but it cannot by itself authorize a changed highlighted profile. Version 1.3 builds a second evidence layer after replay:

1. Match a candidate and the exact evaluation control by cycle ID.
2. Reject duplicate rows and immutable-context mismatches rather than choosing one silently.
3. Compare execution-adjusted prices only where both settings triggered.
4. Compare trigger availability on the complete paired set with right-censoring-aware Kaplan-Meier estimates.
5. Resample whole UTC trading-day clusters and remove each day in turn.
6. Require an adjacent multi-point parameter plateau and select its deterministic center.

The highlighted set is selected only after that evidence layer; it never changes replay calculations or creates a new candidate. A changed profile must use the evaluation-control ATR window, change exactly one independently replayed BUY or normal-SELL leg, and pass every paired, execution, censoring, bootstrap, influence, timing, adverse-excursion, and stable-region gate. Combined BUY/SELL rows, alternate windows, ATR-enable transitions, and initial-drop experiments are not highlight-eligible.

This boundary prevents four architectural misconceptions: independently screened entry and exit rows are not a jointly simulated strategy; a candidate must be compared on the same historical opportunities as its control; capture ending does not prove that a candidate would never trigger; and an alternate ATR window cannot be fully scored from fill-centred captures because it also changes the unobserved initial-drop decision. If no changed one-leg region center is stable, the normalized evaluation control is retained as the one paper-test set. Actual saved source settings remain a separate provenance object whenever normalization was required.

## Empirical execution-model boundary

Trigger price is not assumed to equal fill price. The optimizer derives one sample per ticker, leg, and cycle from saved actual fill price plus the latest non-future quote context. Fresh executable bid/ask touch residuals are preferred; a Last-to-fill residual is the fallback. Crossed quotes and fill-touch or Last-reference evidence older than five seconds are excluded. Residuals are non-negative and capped for corruption resistance. Each replay observation excludes its own cycle from the residual model whenever another sample exists.

This remains a top-of-book estimate. It does not model order-book depth, queue position, partial fills, minimum-tick normalization, exchange-specific trigger behavior, gaps, commissions, or market impact.

## v1.4 Market Replay workflow

The Market Replay workflow is intentionally separate from the BouncyBot-data pipeline. `optimizer.ibrec` validates and normalizes one or more format-2 ZIP or format-3 SQLite recordings into one deterministic `IbrecRecording` analysis dataset. `optimizer.market_replay` performs ATR precomputation, complete standardized session replay, candidate aggregation, and stable-region selection. `optimizer.market_replay_reports` writes an independent content-addressed report.

The normal `.ibrec` path does not call the BouncyBot database or capture importer and does not acquire the trading-bot lock. Version 1.8 adds an explicitly optional SQLite-only calibration boundary. When selected, `optimizer.market_replay_calibration` validates the BouncyBot folder, acquires `BotFolderLease`, creates a private read-only SQLite snapshot, derives execution assumptions, verifies source stability, and releases the lock before the ATR search. It does not require or read `debug_captures`. The effective numeric assumptions and content fingerprints are then passed into the otherwise independent replay pipeline; the calibration source path is discarded.

Format 3 is opened only after the source and any rollback journal have been copied and verified in a private temporary directory. The parser verifies SQLite integrity and the Market Replay Lab hash/checkpoint contracts before producing domain objects. Analysis identity uses fixed component roles plus content hashes rather than an absolute path or original filename.

The format-3 lifecycle model deliberately accepts a committed RTH period left `active` by hard termination. A missing committed period end is derived conservatively from committed ticks and remains right-censored. Event order is based on sequence and monotonic `elapsed_ns`; recorder wall-clock reversals are retained as quality evidence and disable the stable-evidence label rather than reordering rows.

The importer validates all raw rows before applying a semantic replay projection. That projection can remove only redundant size/volume/high-low updates; it retains every Last event, full snapshot, price/feed change, first/final row, and at least one usable state per UTC second. This keeps candidate memory bounded without allowing a discarded row to alter ATR, trigger, stop-normalization, fill, or feed semantics.

The GUI owns one tab per workflow and one global worker slot. A run in either tab disables both tabs until the worker exits, preventing overlapping CPU- and disk-intensive analyses inside one process while preserving independent input, progress, result, and report controls.


## v1.5 Market Replay search and robustness boundary

The Market Replay search is hierarchical rather than one large coupled grid. Stage 1 owns only bar-duration selection with all other strategy values fixed. Stage 2 owns only ATR-period selection inside the advancing bar durations while preserving the unchanged 14×60 control. Stage 3 owns the entry/exit multiplier grid and local refinement inside the narrowed windows.

The session simulator remains the sole producer of per-profile `MarketReplaySessionResult` values. Robustness functions consume those immutable session results and pair candidate/control values by `(session_date, period_id)`. Whole-day bootstrap and leave-one-day-out therefore cannot alter event ordering or replay state.

Stable-region selection and day-level robustness are deliberately separate boundaries:

- region selection rejects isolated parameter peaks;
- whole-day resampling rejects dependence on the particular sample of recorded dates;
- source-quality gates reject synthetic, delayed, frozen-interrupted, or clock-anomalous evidence;
- recommendation assembly emits a changed profile only when all boundaries agree, otherwise it emits the unchanged control.

Release reproducibility is also separated from application analysis. The Windows builder creates a fresh pinned environment, produces two independent frozen directory trees, compares their content hashes, records deterministic provenance, and packages the accepted tree with a normalized ZIP writer.

## v1.6 multi-recording and recommendation-soundness boundary

The Market Replay input boundary accepts an immutable set of at most 64 `.ibrec` paths. Every file is copied and verified independently before combination. The combiner requires matching symbol, positive conId, currency, security type, exchange time zone, and minimum tick. Content-identical inputs are rejected. Exchange-routing metadata may differ only when the stronger instrument identity agrees, and that difference is reported.

One complete, unambiguous RTH period is required per trading date. A date represented by overlapping recordings or multiple interrupted periods is excluded in full; the optimizer never joins fragments because ATR bars, moving anchors, trailing-order state, and positions cannot be proven continuous across the gap. Combined identities use only component roles, sizes, hashes, formats, and canonical ordering, so input order, source path, and filename do not alter the result.

Recommendation authorization is separate from raw candidate ranking. A changed stable-region center must beat the unchanged control on identical complete sessions, have positive median and mean returns, improve at least 60% of paired dates, retain at least 80% of control trading-day participation, remain within tail-risk tolerances, survive 2,000 shared day-or-continuity-block bootstrap draws, stay positive after every complete three-stage leave-one-day-out rerun, and remain above control under five-second ATR phase stress. Any paired right-censored or unmarked open outcome blocks a change. Otherwise the single published profile is the unchanged control.

## v1.8 continuous replay and calibration boundary

`optimizer.market_replay` evaluates retained periods in chronological order. `_ReplayCarryState` is the only state object allowed to cross an RTH boundary. Carry is authorized only when both adjacent periods pass the primary quality gate and the next observed date is the conservatively expected weekday. The carried object can contain an open long, actual modeled BUY basis and quantity, cumulative portfolio equity, continuity-chain drawdown, an active or triggered SELL, and the detailed trade record. BUY trails are never carried. A missing day, ambiguous holiday, overlap, partial session, source-quality failure, or absent closing bid mark terminalizes the position instead of inventing state.

An open HOLD re-warms the new session's ATR before deriving an unsubmitted normal SELL. A SELL trail that was already submitted retains its locked trail percentage, stop, and running high. Sequence counters are reset at the recording boundary so the first genuine Last event in the new file can update or trigger that existing trail. A triggered SELL can remain pending until a valid bid appears.

The report's per-session returns are incremental portfolio returns relative to the prior continuity-chain close. The drawdown gate uses continuity-chain equity peaks. The bootstrap resamples individual trading days only when candidate and control finish flat. Days joined by an overnight position or active SELL order are one continuity-block unit, so the resample never separates an entry from its later exit. It remains an in-sample rejection test rather than an independent future-performance guarantee.

`optimizer.market_replay_calibration` is a narrow evidence adapter, not a second strategy engine. It matches actual executions to exact conId cycles where possible, groups rows by broker-order identity, matches only non-future same-side quotes, derives commission and adverse-slippage percentiles, and returns immutable effective assumptions. A supported 75th-percentile adverse cost can raise but never lower the configured reserve. A supported median actual BUY notional can replace the configured notional. Insufficient, conflicting, stale, crossed, future, duplicate, or currency-incompatible evidence is reported and excluded.

## v1.9 robust-selection boundary

Version 1.9 adds `market_replay_validation.py` as the single source of truth for score policies, Pareto comparison, continuity-block aggregation, moving-block resampling, chronological fold construction, and recommendation-gate normalization. Market Replay candidate selection remains in `market_replay.py`; the validation module receives immutable replay results and does not read files or mutate replay state.

A changed Market Replay recommendation is now a two-level process:

1. the bounded three-stage selector creates stable candidate regions; and
2. each region center is authorized or rejected by independent evidence gates.

The authorization layer includes exact raw-chronology leave-one-day-out reselection, chronological walk-forward testing, fixed-profile whole-day/continuity-block bootstrap, circular moving-block bootstrap, selection-aware out-of-bag bootstrap, multiple score policies, Pareto non-domination, deterministic search-boundary extension, assumption stress, ATR phase stress, and economic continuity-block comparison. Every gate is exported by name. Failure of any required gate returns the unchanged control rather than silently choosing the numerically strongest profile.

Exact leave-one-day-out removes the date before replay and reconstructs overnight continuity. Walk-forward training never sees validation dates, while validation replay may legitimately carry a position across the training/validation boundary. Calibration estimates are date-cross-fitted so same-day or future executions cannot calibrate an earlier decision.
