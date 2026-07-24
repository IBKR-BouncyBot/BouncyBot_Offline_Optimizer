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

Open **Market Replay (.ibrec v2/v3)**, add one or more recordings for the same instrument, and select a report output root. **Check recordings** performs bounded structural preflight and aggregate-limit checks. **Analyze recordings** performs the full private-copy and integrity verification, excludes ambiguous overlapping dates, runs the bounded three-stage search plus robustness analysis, and writes the deterministic report.

The preflight shows lifecycle status for every selected recording and flags synthetic provenance. Full hash-chain/checkpoint verification occurs during analysis. Prefer closed, committed recordings. A format-3 file with an active period is accepted as interrupted recovery evidence, but the affected session remains right-censored. Do not analyze a file while Market Replay Lab is actively writing it; source or rollback-journal mutation causes the run to abort.

The portable build bundles `tzdata` so format-2 liquid-hours reconstruction is available on Windows systems without a separate Python installation. Format 3 uses its explicit UTC RTH periods.

This workflow does not require BouncyBot to be closed and does not inspect or create `ibkr_trading_bot.lock`. It does not use `bot_state.sqlite` or `debug_captures` and does not connect to IBKR.

Terminal example:

```powershell
BouncyBotOfflineOptimizer.exe --no-gui --ibrec `
    "D:\Recordings\AAPL_2026-07-13.ibrec" `
    "D:\Recordings\AAPL_2026-07-14.ibrec" `
    "D:\Recordings\AAPL_2026-07-15.ibrec" `
    --output-dir "D:\Optimizer Reports"
```

Keep every recording immutable while analysis is running. The run aborts when any source component or active rollback journal changes. Version-3 recordings may be accompanied by `-journal`; the journal is treated as part of the corresponding input identity. Symlink inputs are rejected.

Each recording should begin early enough for ATR warm-up and continue through the standardized entry cutoff or through all open simulated positions. Short, interrupted, or manually stopped recordings remain reportable but produce right-censored outcomes and unstable evidence. Do not select two files for the same trading date unless the complete-date exclusion is intentional; the optimizer will not join same-day fragments.


## Reproducible Windows release build

Use the exact source tree and Python 3.11.9 x64:

```powershell
.\scripts\build_windows.ps1 -RunTests
```

The release command creates a fresh `.venv-release`, installs `requirements-bootstrap.lock` and `requirements-release-win64.lock` without dependency resolution, verifies the exact installed distribution set, runs `pip check`, fixes reproducibility-related environment values, and performs two independent PyInstaller builds. The build fails when any corresponding output file differs.

It then writes `SOURCE_MANIFEST.json` and `BUILD_PROVENANCE.json`, creates a deterministic release ZIP with normalized path order, timestamps, and host-independent executable metadata, recreates that archive, and requires both ZIP hashes to match. Source-manifest traversal prunes virtual environments and generated/private trees before descent. `-SkipReproducibilityCheck` exists for diagnostics only and must not be used for a published release.

Development commands may use `requirements.txt`; a tagged binary release must use the exact release locks through the build script. Update those locks only as a deliberate release change followed by the complete Windows test, frozen-app smoke, and two-pass reproducibility gate.
