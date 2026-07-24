# BouncyBot Offline Optimizer v1.5.0 — three-stage ATR search, default day-level robustness, and reproducible releases

Version 1.5.0 strengthens the independent Market Replay `.ibrec` v2/v3 workflow while preserving the existing SQLite and `debug_captures` workflow.

## Three-stage ATR-window search

The earlier Market Replay search coupled ATR period and bar duration at four fixed points. Version 1.5.0 separates those dimensions:

1. **Bar-duration screen:** compare 15, 30, 60, and 120-second bars while holding period 14 and all strategy multipliers at the unchanged BouncyBot control values.
2. **ATR-period screen:** compare periods 5, 7, 10, 14, 21, and 28 within the strongest bar durations. The unchanged 14-period/60-second control is always retained.
3. **Strategy-parameter search:** run the bounded initial-drop, BUY-rebound, minimum-profit, and SELL-trail multiplier grid only inside the narrowed ATR windows, then refine the strongest profiles in deterministic 0.25 steps.

This hierarchy makes bar duration and period separately observable before the larger multiplier search. It remains a bounded in-sample screen; it is not an exhaustive mathematical optimizer.

Because stage 1 and stage 2 hold the strategy multipliers at the unchanged control values, the hierarchy can discard a period/bar window that would interact favorably only with a different multiplier combination. The report states this limitation explicitly rather than presenting the staged result as the optimum of the full cross-product.

## Default trading-day robustness

A changed Market Replay recommendation must now pass both checks against the unchanged BouncyBot control on exactly the same RTH session identities:

- **Trading-day bootstrap:** 2,000 deterministic resamples of complete UTC trading days with replacement. All stable-region centers use the same resample schedule. The 80% interval for the candidate-minus-control score must remain above zero and at least 80% of replicates must favor the candidate.
- **Leave-one-day-out:** remove each available trading day once and recompute the complete score difference. Every omission must remain positive and no omission may reverse the result's sign. The two ATR-window stages are recalculated from the remaining days; the already simulated stage-3 pool is reranked without manufacturing a new omitted-day-only refinement grid.

Ticks are never independently shuffled. Resampling whole days preserves each session's chronological, path-dependent BUY and SELL sequence.

The report exports the ATR-window screen, candidate robustness evidence, and recommendation-specific leave-one-day-out results. A changed profile also requires its ATR period/bar window to advance through stage 2 in at least 60% of the omission reruns. When no changed stable-region center passes every source-quality and day-level robustness gate, the one profile shown for evaluation remains the unchanged control.

## License and repository hygiene

- The project continues under the same byte-identical PolyForm Noncommercial 1.0.0 license used by BouncyBot.
- Package metadata explicitly references the repository `LICENSE` file.
- `.gitignore` now excludes local virtual environments, caches, build output, SQLite databases and sidecars, `.ibrec` recordings and sidecars, captures, optimizer reports, logs, credentials, signing material, and editor/operating-system files.
- `.gitattributes` normalizes source text across operating systems while treating recordings, databases, archives, and binaries as binary data.

## Reproducible Windows release pipeline

The Windows release builder now:

- requires exact CPython 3.11.9 x64;
- builds in a fresh dedicated `.venv-release` environment;
- installs exact bootstrap and Windows dependency locks without dependency resolution;
- verifies the installed environment contains exactly the pinned distributions;
- runs `pip check` after the exact environment has been assembled;
- fixes `SOURCE_DATE_EPOCH`, `PYTHONHASHSEED`, timezone, and bytecode settings;
- disables UPX;
- performs two independent PyInstaller `--onedir` builds by default and compares every generated file by SHA-256;
- writes deterministic build provenance;
- writes a path- and modification-time-independent SHA-256 manifest of the release-relevant source tree;
- creates path-ordered ZIP archives with normalized timestamps and permissions;
- recreates the archive and requires an identical SHA-256 before accepting the release.

The exact Windows dependency lock reflects the validated Windows toolchain and must be updated intentionally through a new tested release rather than by broad version resolution during a tagged build.

## Compatibility

- BouncyBot SQLite/capture analysis contracts are unchanged.
- Market Replay format 2 and format 3 remain supported.
- The Market Replay analysis contract is incremented so a v1.5.0 report cannot reuse a pre-v1.5.0 content-addressed directory.
- Report recommendations remain paper-evaluation candidates, not guarantees of future performance or mathematically proven optima.
