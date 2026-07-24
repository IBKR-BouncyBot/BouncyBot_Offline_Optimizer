# BouncyBot Offline Optimizer v1.6.0

A portable, read-only companion application for **BouncyBot - IBKR Portable Trading Bot** with two independent workflows:

- **BouncyBot SQLite & captures:** analyzes `bot_state.sqlite`, an accompanying WAL when present, and `debug_captures/` fill-centred market-data ZIP archives. It produces a per-ticker data-coverage report, settings-history audit, bounded counterfactual replay, and one ATR profile to evaluate in paper trading.
- **Market Replay (.ibrec v2/v3):** analyzes one or more Market Replay Lab recordings for the same instrument independently of BouncyBot data. It verifies every input, excludes ambiguous overlapping dates instead of splicing fragments, performs a bounded multi-session ATR search, and generates one complete profile plus a separate deterministic report.

## Safety model

The optimizer is offline. It does not import an IBKR or network client, place orders, or write settings back to BouncyBot.

Before **BouncyBot SQLite & captures** analysis it requires `ibkr_trading_bot.lock` to be absent. After explicit confirmation it atomically creates that same lock for the duration of source reads. That workflow then:

1. hashes the SQLite/WAL and every capture archive;
2. copies SQLite/WAL through ordinary read-only file access into an operating-system temporary directory;
3. opens only that private copy with SQLite and creates a standalone snapshot;
4. reads capture ZIP members directly without extraction;
5. verifies that no database/WAL/shared-memory/capture input changed;
6. removes only the lock created by the optimizer process;
7. writes reports only under the selected output root.

The source database and captures are never modified. A pre-existing lock is never removed automatically. Database and capture symlinks that resolve outside the selected source folder are rejected. The separate Market Replay workflow has its own read-only copy-and-verify path and never uses the trading-bot lock.

## Market Replay recording workflow

The second desktop tab accepts one or more recordings in either supported format:

- Market Replay format 2: a ZIP recording with manifest and tick CSV; or
- Market Replay format 3: the SQLite recording format from Market Replay Lab 1.3.0 with per-record hashes, chain hashes, explicit RTH periods, and committed checkpoints.

All selected recordings must describe the same ticker, positive conId, currency, security type, exchange time zone, and minimum tick. Duplicate content is rejected. If more than one RTH period covers the same trading date, the complete date is excluded rather than splicing fragments whose ATR, anchor, order, or position state cannot be proven continuous. Input order, filenames, and absolute paths do not affect the analysis identity.

The optimizer copies and verifies every recording, never connects to IBKR, and does not acquire `ibkr_trading_bot.lock`. It first compares bar duration with period and multipliers fixed, then compares ATR period inside the strongest bar durations, and only then searches initial-drop, BUY-rebound, minimum-profit, and SELL-trail multipliers over each retained RTH period. In each leave-one-day-out run all three stages are rebuilt, including omission-specific multiplier refinement. Positive native trails require genuine Last events; market-style fills require the recorded same-side touch; unresolved outcomes are right-censored; and a changed profile is rejected if any paired candidate/control outcome is incomplete. Candidate/control results are paired on identical sessions, resampled by whole trading day, stress-tested after removing every day, and checked across five-second ATR bar-phase offsets. Stable-region selection rejects an isolated score maximum. The hierarchy remains bounded: a period/bar window that is weak under the unchanged control multipliers can be screened out before other multiplier interactions are tested.

For format 3, the importer verifies SQLite structure, foreign keys, every tick/event/RTH record hash, both integrity chains, the RTH digest, and the latest committed checkpoint. A period left `active` by hard termination is accepted as recovery evidence and remains right-censored. All raw rows are validated; redundant size/volume-only rows may then be removed from the in-memory strategy stream without removing Last events, full snapshots, price/feed changes, first/final rows, or the last usable state in a UTC second. Raw and retained row counts are reported separately.

Synthetic sample provenance, delayed-only sessions, mixed live/delayed sessions, frozen-feed interruptions, or recorder clock reversals prevent the result from being labelled stable evidence. Crossed two-sided quotes are retained as quality evidence but are not used as executable touches or stop-reference inputs.

The Market Replay result is separate from the SQLite/capture result. It does not use actual BouncyBot fills, commissions, account limits, or user operating times, and it must not be described as a mathematical optimum. See the [v1.6.0 multi-recording and recommendation-audit release note](docs/V1_6_0_MULTI_RECORDING_RECOMMENDATION_AUDIT.md), the [v1.4.0 Market Replay release note](docs/V1_4_0_MARKET_REPLAY_OPTIMIZATION.md), the [v1.4.1 Windows quality-fix release note](docs/V1_4_1_WINDOWS_QUALITY_FIXES.md), the [v1.4.2 Windows test-isolation release note](docs/V1_4_2_WINDOWS_TEST_ISOLATION_FIX.md), the [v1.4.3 Windows retry-test robustness release note](docs/V1_4_3_WINDOWS_RETRY_TEST_ROBUSTNESS.md), the [v1.4.4 Ruff quality-gate correction](docs/V1_4_4_RUFF_QUALITY_GATE_CORRECTION.md), the [v1.4.5 Pyright quality-gate correction](docs/V1_4_5_PYRIGHT_QUALITY_GATE_CORRECTION.md), the [v1.5.0 three-stage ATR robustness and reproducible-build release note](docs/V1_5_0_THREE_STAGE_ATR_ROBUSTNESS_AND_REPRODUCIBLE_BUILDS.md), the [v1.5.1 Ruff SIM103 quality-gate correction](docs/V1_5_1_RUFF_SIM103_QUALITY_GATE_CORRECTION.md), the [v1.5.2 Windows release-gate fixes](docs/V1_5_2_WINDOWS_RELEASE_GATE_FIXES.md), and the [v1.5.3 Windows portable-archive and source-audit fixes](docs/V1_5_3_WINDOWS_PORTABLE_ARCHIVE_AND_SOURCE_AUDIT_FIXES.md).

## What the replay can and cannot do

BouncyBot saves up to 15 minutes before and 15 minutes after an actual fill. This supports local analysis of alternative BUY rebound and normal-SELL activation/trail behavior. It usually does **not** contain the full anchor-to-drop path, complete holding period, no-trade sessions, or trades that different settings would have created.

Consequently:

- BUY rebound and normal-SELL candidates receive local screening metrics.
- SELL activation is replayed chronologically; effective ATR-derived values are locked at activation.
- Manual minimum-profit cycles retain their stored manual threshold while SELL trail candidates are screened.
- Alternate ATR windows are ranked only when that exact period/bar can be reconstructed from enough saved rows.
- A saved bot ATR can be used only for a matching historical period/bar and only from a row at or before the replay decision.
- Protective SELL captures contribute to coverage but are excluded from normal profit-exit ranking.
- Initial-drop alternatives are unscored because their full pre-entry path is normally absent.
- Suggestions are paper-trading experiments, not optimal or live-ready settings.

See [Data coverage and limitations](docs/DATA_COVERAGE_AND_LIMITATIONS.md) and [Replay methodology and algorithm](docs/REPLAY_METHODOLOGY.md).

## Actual settings and changed settings

The report separates:

- **Exact historical ATR profiles** saved on individual cycles.
- **Historical median baseline (derived from actual stored cycle ATR settings)**, a field-wise descriptive summary. It is not fitted. When settings changed, this summary may not equal any one exact configuration.
- **Current saved app settings**, shown separately and never assumed to have applied to older cycles.

Every counterfactual candidate is held fixed across cycles. Each observation is tagged with the exact historical profile originally saved on its cycle. Reports show pooled metrics and the same candidate split by historical profile, while warning that settings eras can be confounded with date and market regime.

## One settings set to evaluate next

Every ticker report now highlights exactly one complete settings set between **Data coverage** and **Actual ATR settings and changes between cycles**. This is a paper-evaluation suggestion, not a live recommendation.

A changed highlighted profile must be a **single-leg, baseline-window experiment**. Version 1.3 compares it with the exact control only on identical cycles and requires:

1. exactly one independently replayed leg: BUY or normal SELL;
2. the same ATR period and bar duration as the evaluation control;
3. at least five same-cycle candidate/control pairs and at least three pairs where both trigger;
4. at least five independent UTC trading days contributing execution-adjusted deltas;
5. at least five empirical execution samples after cycle-level leave-one-out exclusion;
6. a positive paired median after saved-spread and adverse-slippage adjustment;
7. a deterministic trading-day bootstrap 80% interval that stays above zero and at least 80% positive bootstrap replicates;
8. positive leave-one-day-out results with no sign reversal;
9. no material deterioration in censoring-aware trigger probability, trigger timing, or adverse excursion;
10. membership at the deterministic center of the preferred multi-point stable parameter region.

Capture-end non-triggers are right-censored rather than counted as confirmed misses. Candidate/control trigger availability is compared with Kaplan-Meier estimates at the longest mutually supported 15-, 5-, or 1-minute horizon; at least five minutes of shared support is required for a changed setting. Combined BUY/SELL profiles and alternate ATR windows remain visible comparison experiments but cannot be highlighted: the two legs were not jointly simulated, and changing the ATR window also changes the unobserved initial-drop decision. When no one-leg change qualifies, the report retains the normalized evaluation control rather than inventing a change. The actual saved source values remain visible separately if legacy or malformed values needed normalization for replay.

The selected row is also exported as `<TICKER>_primary_settings_to_evaluate.csv` and in the per-ticker JSON evidence.

## GUI results

The completed-results table provides:

- a derivation tooltip for every column header;
- a row-specific tooltip for every value;
- an **Open report** button beside every ticker.

The per-ticker HTML report explains coverage scoring, settings provenance, ATR reconstruction, BUY and SELL replay, candidate construction, ranking, capture matching, and limitations.

## Deterministic reports

Reports are content-addressed. Identical SQLite/WAL bytes, capture paths/bytes, optimizer version, and analysis limits produce:

- the same analysis ID;
- the same `optimizer_<fingerprint>` folder;
- byte-identical HTML, JSON, CSV, manifest, and checksum files;
- the same output even under a different source path, output path, wall-clock run time, or Python hash seed.

An existing same-ID directory is reused only when every generated byte matches. Divergent content is rejected instead of overwritten.

## Portable placement beside BouncyBot

The Windows build is PyInstaller `--onedir` with a unique runtime directory named `BouncyBotOptimizerRuntime`. Copy both items from the optimizer release's `APP` folder into BouncyBot's GUI folder:

```text
BouncyBotOfflineOptimizer.exe
BouncyBotOptimizerRuntime/
```

Do not copy only the executable. When placed beside BouncyBot, the optimizer finds `bot_state.sqlite`, `debug_captures`, and `ibkr_trading_bot.lock` in that directory.

## Running from source

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py
```

Terminal mode:

```powershell
.venv\Scripts\python main.py --no-gui --source-dir "C:\Path\To\BouncyBot\GUI"
```

Independent Market Replay mode:

```powershell
.venv\Scripts\python main.py --no-gui --ibrec "C:\Recordings\AAPL_Mon.ibrec" "C:\Recordings\AAPL_Tue.ibrec" "C:\Recordings\AAPL_Wed.ibrec" --output-dir "C:\Reports"
```

Use `--yes` only in controlled automation after independently confirming BouncyBot is closed. Capture hashing is mandatory; the legacy `--no-capture-hashes` switch is rejected.

## Output files

Each content-addressed run contains:

```text
optimizer_reports/
└── optimizer_<fingerprint-prefix>/
    ├── index.html
    ├── tickers_summary.csv
    ├── data_quality_issues.csv
    ├── analysis_manifest.json
    ├── SHA256SUMS.txt
    ├── README_REPORT.txt
    └── <TICKER>/
        ├── <TICKER>_coverage_and_replay.html
        ├── <TICKER>_coverage_and_replay.json
        ├── <TICKER>_candidate_screening.csv
        ├── <TICKER>_paired_candidate_evidence.csv
        ├── <TICKER>_execution_model.csv
        ├── <TICKER>_candidate_profile_breakdown.csv
        ├── <TICKER>_primary_settings_to_evaluate.csv
        ├── <TICKER>_atr_settings_to_evaluate.csv
        ├── <TICKER>_historical_atr_profiles.csv
        ├── <TICKER>_atr_settings_regimes.csv
        ├── <TICKER>_atr_settings_by_cycle.csv
        ├── <TICKER>_capture_inventory.csv
        └── <TICKER>_replay_observations.csv
```

No external web assets are used; HTML reports open locally.

The independent Market Replay workflow writes `market_replay_<fingerprint-prefix>/` with its own HTML, JSON, input-recording inventory, excluded-date evidence, ATR-window search, candidate/robustness tables, selected-settings CSV, session/trade evidence, manifest, and checksums.

## Build and test

```powershell
.\scripts\build_windows.ps1 -RunTests
.\run_all_tests.bat
```

The Windows gate runs all pytest tests with `ResourceWarning` promoted to an error, branch coverage, Python compilation, Ruff, and Pyright. The release builder then starts the frozen executable in a hidden smoke-test mode and fails unless the complete PySide6 GUI runtime imports and exits successfully within 30 seconds.

## Financial-risk notice

This application performs retrospective local-window screening only. It does not prove causality, future profitability, fill quality, or suitability. Evaluate every candidate in an IBKR paper account with forward data before considering live use.


## Reproducible Windows releases

Production Windows builds use exact lock files in a fresh Python 3.11.9 x64 environment. The builder fixes reproducibility-related environment values, verifies the installed distributions with an exact-set audit and `pip check`, performs two independent PyInstaller passes and compares their file hashes, writes deterministic source and build provenance, and recreates the release ZIP to require an identical SHA-256. Development installs may continue to use the broader ranges in `requirements.txt`; tagged release builds use `requirements-bootstrap.lock` and `requirements-release-win64.lock`.

Each release carries `SOURCE_MANIFEST.json` and `BUILD_PROVENANCE.json`. The source manifest records every release-relevant source file by relative path, size, portable executable classification, and SHA-256. Virtual environments and generated/private trees are pruned before traversal. Build provenance records the exact interpreter, installed distributions, lock files, scripts, source-manifest hash, and normalized build epoch.

## License

BouncyBot Offline Optimizer uses the same license as BouncyBot: **PolyForm Noncommercial License 1.0.0**. Commercial use is not permitted unless the licensor grants separate permission. See [`LICENSE`](LICENSE) for the complete terms.
