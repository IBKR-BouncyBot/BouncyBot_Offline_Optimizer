# Operations and portable placement

## Normal GUI workflow

1. Close BouncyBot completely.
2. Confirm `ibkr_trading_bot.lock` is absent.
3. Start `BouncyBotOfflineOptimizer.exe`.
4. Verify the selected folder contains `bot_state.sqlite` and, when available, `debug_captures`.
5. Click **Analyze read-only data**.
6. Read the confirmation and select **OK** only when the trading bot is closed.
7. Wait for completion.
8. Hover over result headers or cells for derivation explanations.
9. Use the ticker row's **Open report** button, or open the run-level `index.html`.

BouncyBot cannot normally start from the same portable folder while the optimizer owns its lock. A pre-existing lock is never removed by the optimizer. Do not manually delete a lock while either program is running.

## Side-by-side installation

Copy both items from the optimizer release's `APP` folder into the BouncyBot GUI directory:

```text
BouncyBotOfflineOptimizer.exe
BouncyBotOptimizerRuntime/
```

The unique runtime directory avoids collision with BouncyBot's PyInstaller runtime. Do not copy only the executable.

## Terminal workflow from source

```powershell
.venv\Scripts\python main.py --no-gui --source-dir .
```

The packaged Windows executable is intentionally built with PyInstaller's windowed/no-console mode so normal GUI launches do not open a console window. Use terminal mode from the source checkout when automation or machine-readable output is required; the packaged executable is the portable GUI application.

Without `--yes`, terminal mode requests the same confirmation. Exit codes are:

- `0`: completed successfully;
- `1`: user cancelled;
- `2`: source or lock safety check failed;
- `3`: analysis or report generation failed;
- `4`: GUI dependency unavailable.

Capture hashing is mandatory. The legacy `--no-capture-hashes` switch is rejected because deterministic identity and mutation detection require content hashes.

The report root must not be the SQLite main file, its `-wal` or `-shm` sidecars, the `ibkr_trading_bot.lock` path, `debug_captures`, or a path underneath any of those reserved locations. The optimizer rejects these choices before acquiring the lock or creating output.

## Content-addressed reports

Each report directory is named:

```text
optimizer_<first 16 hexadecimal characters of the input fingerprint>
```

Running the same optimizer version with byte-identical SQLite/WAL/capture input and the same analysis limits generates the same directory name and byte-identical report tree. If that directory already exists and matches, it is reused. If any byte differs, the optimizer refuses to overwrite it and reports possible corruption.

A database checkpoint, changed WAL, changed capture ZIP bytes, different capture relative path, changed analysis limit, or newer optimizer analysis contract produces a different ID. File modification times and absolute folder locations do not. Modification times are still checked before and after a single active run as a conservative source-mutation signal.

## Report retention

Reports can contain exact timestamps, prices, quantities, order references, P/L, and inferred strategy settings. Protect, archive, or delete them according to the policy used for the source database. `SHA256SUMS.txt` can be used to verify report files after copying.

## Market Replay operation

Open **Market Replay (.ibrec v2/v3)**, add one or more recordings for the same instrument, and select a report output root. **Check recordings** performs bounded structural preflight and aggregate-limit checks. **Analyze recordings** performs the full private-copy and integrity verification, stitches compatible non-overlapping same-date fragments while preserving their outages, compares protective SELL disabled with bounded manual and ATR-adaptive policies, runs the three-stage ATR search separately for the supported policies, applies the complete robustness authorization stack, and writes the deterministic report.

**Profile-evaluation workers** controls the exact Stage 3/outward-probe
execution engine:

```text
Automatic  keep inputs below 50,000 retained rows on the reference engine;
           otherwise leave one logical processor free and use at most eight workers
1          compact exact single-process execution
2–64       explicit spawned worker-process count
```

The worker setting changes runtime only. It is not written into the analytical
search contract or report identity. Independent profiles are returned to the
parent and sorted by stable profile key before ranking. A worker error aborts
the complete analysis; do not lower the worker count to work around a failing
profile.

The preflight shows lifecycle status for every selected recording and flags synthetic provenance. Full hash-chain/checkpoint verification occurs during analysis. Prefer closed, committed recordings. A format-3 file with an active period is accepted as interrupted recovery evidence, but the affected session remains right-censored. Do not analyze a file while Market Replay Lab is actively writing it; source or rollback-journal mutation causes the run to abort.

The portable build bundles `tzdata` so format-2 liquid-hours reconstruction is available on Windows systems without a separate Python installation. Format 3 uses its explicit UTC RTH periods.

Without optional calibration, this workflow does not require BouncyBot to be closed and does not inspect or create `ibkr_trading_bot.lock`. It does not use `bot_state.sqlite` or `debug_captures` and does not connect to IBKR.

When **Optional execution calibration** is selected, close BouncyBot first. The optimizer validates the selected folder, requires its normal lock to be absent, and asks for explicit confirmation. It then acquires the same lock only while it fingerprints and privately snapshots `bot_state.sqlite` and any SQLite sidecars. `debug_captures` is not required or read. The lock is released before the Market Replay search. Absolute calibration paths are excluded from the report and deterministic analysis identity.

The **Overnight replay** option is enabled by default. It can carry an open long, active normal or protective SELL trail, or a triggered SELL waiting for a bid only across consecutive primary-eligible recordings. Missing weekdays, ambiguous holidays, schedule conflicts, partial/failed-quality sessions, excessive stitched gaps, or the absence of a closing bid mark break continuity and terminalize the unresolved position. A HOLD state re-warms ATR in the next session; a normal or protective SELL trail that was already submitted retains its locked trail state. The Market Replay report models protective cancellation and normal-SELL replacement atomically because `.ibrec` does not record broker cancellation acknowledgements; forward paper testing remains required.

Terminal example:

```powershell
BouncyBotOfflineOptimizer.exe --no-gui --ibrec `
    "D:\Recordings\AAPL_2026-07-13.ibrec" `
    "D:\Recordings\AAPL_2026-07-14.ibrec" `
    "D:\Recordings\AAPL_2026-07-15.ibrec" `
    --ibrec-workers 6 `
    --calibration-source-dir "D:\BouncyBot\GUI" `
    --output-dir "D:\Optimizer Reports"
```

Keep every recording immutable while analysis is running. The run aborts when any source component or active rollback journal changes. Version-3 recordings may be accompanied by `-journal`; the journal is treated as part of the corresponding input identity. Symlink inputs are rejected.

Each recording should begin early enough for ATR warm-up and continue through the standardized entry cutoff or through all open simulated positions. Short, interrupted, or manually stopped recordings remain reportable but produce right-censored outcomes and unstable evidence. Selecting complementary files for one date is supported when their RTH schedules agree and their retained tick ranges do not overlap. Review `recording_fragment_evidence.csv`: real outages are preserved and may still disqualify the date; overlapping streams are never interleaved.


## Reproducible Windows release build

Use the exact source tree and Python 3.11.9 x64:

```powershell
.\scripts\build_windows.ps1 -RunTests
```

The release command creates a fresh `.venv-release`, installs `requirements-bootstrap.lock` and `requirements-release-win64.lock` without dependency resolution, verifies the exact installed distribution set, runs `pip check`, fixes reproducibility-related environment values, and performs two independent PyInstaller builds. The build fails when any corresponding output file differs.

It then writes `SOURCE_MANIFEST.json` and `BUILD_PROVENANCE.json`, creates a deterministic release ZIP with normalized path order, timestamps, and host-independent executable metadata, recreates that archive, and requires both ZIP hashes to match. Source-manifest traversal prunes virtual environments and generated/private trees before descent. `-SkipReproducibilityCheck` exists for diagnostics only and must not be used for a published release.

Development commands may use `requirements.txt`; a tagged binary release must use the exact release locks through the build script. Update those locks only as a deliberate release change followed by the complete Windows test, frozen-app smoke, and two-pass reproducibility gate.

## v1.9 validation runtime and interpretation

Exact leave-one-day-out reruns the complete staged selector once per primary trading date. With at least 20 primary-quality sessions, the default analysis also performs expanding walk-forward selection and selection-aware out-of-bag bootstrap. These checks are intentionally more expensive than fixed-profile replay. They are required only for authorizing a changed profile; insufficient evidence produces the unchanged control with explicit failed-gate rows.

Do not interrupt an analysis by deleting partial report directories. The report is published atomically only after all validation files and checksums are complete. Review `recommendation_quality_gates.csv` before transferring any setting to paper trading. A control result means no changed profile passed every gate; it is not proof that the control is optimal.

## Large Market Replay input sets

The Market Replay workflow does not impose a maximum recording count or a
maximum tick-row count. Very large selections are accepted as long as every
recording passes structural and instrument-identity validation. Runtime and
memory use increase with retained strategy events, sessions, candidate search,
and robustness reruns.

The aggregate input-byte and format-v2 uncompressed-archive safeguards remain
enabled to reject malformed or unexpectedly expansive input. They are
configurable in terminal mode and are distinct from the removed row/file-count
ceilings.

For very large retained datasets, start with `Automatic`. If the machine
becomes memory-bandwidth constrained, compare an explicit value of four or six
with the automatic result. A higher process count is not always faster. Small
recordings are normally faster in single-process mode because worker startup
cost exceeds the replay work. All supported worker settings must produce the
same analysis ID and report bytes.

The worker pool remains active after Stage 3 for exact leave-one-day-out
selector reruns, selection-aware bootstrap replicates, chronological
walk-forward folds, ATR phase variants, and assumption-stress scenarios.
Progress text identifies the active operation, execution mode, completed
tasks, and pending tasks. A completed outward-boundary batch at 100% is
followed by a new named post-search operation rather than remaining displayed
for the rest of analysis.
