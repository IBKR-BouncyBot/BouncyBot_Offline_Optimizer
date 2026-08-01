# BouncyBot Offline Optimizer v2.0.1

A portable, read-only companion application for **BouncyBot - IBKR Portable Trading Bot** with two independent workflows:

- **BouncyBot SQLite & captures:** analyzes `bot_state.sqlite`, an accompanying WAL when present, and `debug_captures/` fill-centred market-data ZIP archives. It produces a per-ticker data-coverage report, settings-history audit, bounded counterfactual replay, and one ATR profile to evaluate in paper trading.
- **Market Replay (.ibrec v2/v3):** analyzes one or more Market Replay Lab recordings for the same instrument. It verifies every input, stitches compatible non-overlapping same-date fragments without hiding their outages, first compares protective SELL disabled with bounded manual and ATR-adaptive native trailing-stop policies, then performs the multi-session ATR search conditionally under the supported risk policy. It carries supported open positions and active normal or protective SELL trails across consecutive complete RTH recordings and generates exactly one complete ATR-plus-protective-policy profile. A stopped BouncyBot SQLite folder may optionally calibrate actual trade notional, commissions, and adverse execution cost.

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

The source database and captures are never modified. A pre-existing lock is never removed automatically. Database and capture symlinks that resolve outside the selected source folder are rejected.

The normal Market Replay path needs no BouncyBot files or lock. When optional execution calibration is selected, the optimizer requires BouncyBot to be closed, acquires the same lock only while it creates and reads a private SQLite snapshot, verifies the source state, and releases the lock before the ATR search. Calibration does not require `debug_captures` and does not write to the production database.

## Market Replay recording workflow

The second desktop tab accepts one or more recordings in either supported format:

- Market Replay format 2: a ZIP recording with manifest and tick CSV; or
- Market Replay format 3: the SQLite recording format from Market Replay Lab 1.3.0 with per-record hashes, chain hashes, explicit RTH periods, and committed checkpoints.

All selected recordings must describe the same ticker, positive conId, currency, security type, exchange time zone, and minimum tick. Duplicate content is rejected. Compatible same-date fragments are combined with a deterministic coverage-first maximal non-overlapping selector. Their real wall-clock outage remains visible to the ATR, quote-age, event-gap, and Last-density quality gates. Overlapping streams are never interleaved, and conflicting RTH schedules exclude the date. Input order, filenames, and absolute paths do not affect the analysis identity.

The optimizer copies and verifies every recording and never connects to IBKR. Before ATR optimization, it compares the disabled control with manual protective trails of 1–5% and ATR-adaptive protective multipliers from 1.5x to 4.5x at the unchanged `14 x 60-second` ATR control. An enabled policy advances only when it is the centre of a supported adjacent region and has enough exits/days, practical score improvement, acceptable tail risk, and identifiable ATR values. The full search then compares bar duration, ATR period, initial-drop, BUY-rebound, minimum-profit, SELL-trail, minimum-clamp, and the disabled versus supported protective policy without crossing one policy's selected ATR windows into another. In each leave-one-day-out run the policy and all three ATR stages are rebuilt. Positive native trails require genuine Last events; market-style fills require the recorded same-side touch; execution costs and turnover are included; touch-size evidence is reported; unresolved terminal outcomes are right-censored; and a changed profile is rejected if paired candidate/control evidence is incomplete. Candidate/control results are paired on identical sessions, resampled by whole trading day when flat or by complete overnight-continuity block when positions/orders link days, stress-tested after removing every day, and checked across ATR bar-phase offsets. Stable-region selection rejects an isolated score maximum.

By default, open long positions and already submitted normal or protective SELL trails can continue across adjacent primary-eligible RTH recordings. The BUY basis, quantity, cumulative equity, normal and protective running highs/stops, locked trail percentages, and pending bid-side fill state are preserved. A HOLD state re-warms ATR in the new session before creating a new normal SELL order; an already-submitted protective trail remains active on the first genuine Last update. Missing weekdays, ambiguous holidays, partial sessions, quality failures, schedule conflicts, or absent closing bid marks break continuity rather than inventing state. A stitched same-date period can participate only when its preserved gap and all other quality evidence pass the same primary gate. The bootstrap samples one trading day when both profiles finish flat. When a position or active SELL order links sessions, it samples the entire continuity block as one unit so an entry is never separated from its later exit. This remains an in-sample stability check and does not create independent future market regimes.

The protective order is submitted immediately after a modeled BUY. Its initial stop is calculated from the BUY basis, normalized with BouncyBot's conservative SELL reference and contract tick, and then moved upward only by genuine Last updates. If the Last triggers without a fresh bid, the market-style exit remains pending. When the normal minimum-profit SELL becomes eligible, the protective order is cancelled before replacement. The live bot waits for broker cancellation confirmation; `.ibrec` contains market events rather than order acknowledgements, so the replay models this cancel-and-replace atomically and states that limitation in the report. Policy reports include stop-outs, cancellations, later recovery to the original BUY/normal activation level, additional fall avoided, recovery regret, and overnight exits. Those hindsight diagnostics explain policy behavior but do not independently choose the winner.

Optional execution calibration matches actual BouncyBot executions to the exact conId where available, groups executions by broker order, derives commission and no-future same-side quote slippage evidence, and uses a supported 75th-percentile adverse-cost estimate as a floor-increasing reserve. It can also replace the assumed notional with the median actual BUY notional after the configured sample threshold. Absolute calibration paths are excluded from report identity and output. Calibration improves assumptions; it does not reproduce order-book depth, queue position, routing, market impact, or exact counterfactual fills.

For format 3, the importer verifies SQLite structure, foreign keys, every tick/event/RTH record hash, both integrity chains, the RTH digest, and the latest committed checkpoint. A period left `active` by hard termination is accepted as recovery evidence and remains right-censored. All raw rows are validated; redundant size/volume-only rows may then be removed from the in-memory strategy stream without removing Last events, full snapshots, price/feed changes, first/final rows, or the last usable state in a UTC second. Raw and retained row counts are reported separately.

Synthetic sample provenance, delayed-only sessions, mixed live/delayed sessions, frozen-feed interruptions, recorder clock reversals, inadequate RTH coverage, material connectivity gaps, sparse Last-event evidence, or an unfinalized source prevent a changed recommendation. Crossed two-sided quotes are retained as quality evidence but are not used as executable touches or stop-reference inputs.

The Market Replay result remains a bounded paper-testing candidate, not a mathematical optimum or live-trading instruction. See the [v2.0.1 Ruff F841 quality-gate correction](docs/V2_0_1_RUFF_F841_QUALITY_GATE_CORRECTION.md), the [v2.0.0 protective SELL policy optimization release note](docs/V2_0_0_PROTECTIVE_SELL_POLICY_OPTIMIZATION.md), the [v1.9.4 same-date fragment merging and diagnostics](docs/V1_9_4_SAME_DATE_FRAGMENT_MERGING_AND_DIAGNOSTICS.md), the [v1.9.3 calibration date-key correction and report integrity](docs/V1_9_3_CALIBRATION_DATE_KEY_AND_REPORT_INTEGRITY.md), the [v1.9.2 Market Replay preflight and Windows test clarification](docs/V1_9_2_GUI_PREFLIGHT_AND_WINDOWS_TEST_CLARIFICATION.md), the [v1.9.1 Ruff F841 quality-gate correction](docs/V1_9_1_RUFF_F841_QUALITY_GATE_CORRECTION.md), the [v1.9.0 robust selection and validation release note](docs/V1_9_0_ROBUST_SELECTION_VALIDATION.md), and the [v1.8.0 continuous replay and execution-calibration release note](docs/V1_8_0_CONTINUOUS_REPLAY_AND_EXECUTION_CALIBRATION.md).

## What the replay can and cannot do

BouncyBot saves up to 15 minutes before and 15 minutes after an actual fill. This supports local analysis of alternative BUY rebound and normal-SELL activation/trail behavior. It usually does **not** contain the full anchor-to-drop path, complete holding period, no-trade sessions, or trades that different settings would have created.

Consequently:

- BUY rebound and normal-SELL candidates receive local screening metrics.
- SELL activation is replayed chronologically; effective ATR-derived values are locked at activation.
- Manual minimum-profit cycles retain their stored manual threshold while SELL trail candidates are screened.
- Alternate ATR windows are ranked only when that exact period/bar can be reconstructed from enough saved rows.
- A saved bot ATR can be used only for a matching historical period/bar and only from a row at or before the replay decision.
- Protective SELL captures contribute to coverage but are excluded from normal profit-exit ranking in the SQLite/capture workflow. Protective policy optimization is available only in the independent full-session Market Replay workflow.
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
7. a deterministic trading-day or overnight-continuity-block bootstrap 80% interval that stays above zero and at least 80% positive replicates, with at least five independent resampling units;
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
