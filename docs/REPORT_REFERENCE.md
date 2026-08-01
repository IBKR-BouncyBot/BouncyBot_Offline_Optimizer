# Report reference

## Content-addressed run directory

A run directory is named `optimizer_<fingerprint-prefix>`. Its identity is derived from SQLite/WAL and capture content, optimizer version, and analysis limits. Identical input produces the same directory and byte-identical files. Absolute paths and wall-clock run time are excluded.

The directory is assembled in a private same-filesystem staging folder and published atomically. Transient Windows access-denied, sharing-violation, and lock-violation errors are retried without falling back to a partial copy. If an identically named directory already exists, every generated file hash must match or the optimizer raises an error instead of overwriting it.

## Run-level files

- `index.html`: entry point, ticker summary, settings-change counts, analysis identity, and safety/interpretation boundary.
- `tickers_summary.csv`: one data-coverage row per ticker, including component breakdown.
- `data_quality_issues.csv`: run-level and per-ticker warnings/issues.
- `analysis_manifest.json`: content-derived identity, evidence-through timestamp, source file names, database fingerprint, schema, complete structured results, and relative generated-file list.
- `SHA256SUMS.txt`: SHA-256 for every generated report file except itself.
- `README_REPORT.txt`: portable methodology and interpretation summary.

## Per-ticker files

- `<TICKER>_coverage_and_replay.html`: fully explained human-readable report.
- `<TICKER>_coverage_and_replay.json`: complete structured ticker evidence.
- `<TICKER>_candidate_screening.csv`: pooled aggregate metrics for every candidate.
- `<TICKER>_paired_candidate_evidence.csv`: flattened identical-cycle pairing, censoring, execution-adjusted delta, bootstrap, influence, and stable-region evidence.
- `<TICKER>_execution_model.csv`: per-leg saved-spread and empirical adverse-slippage model, including excluded stale/crossed touch quotes.
- `<TICKER>_candidate_profile_breakdown.csv`: each candidate recomputed within each historical settings-profile subgroup.
- `<TICKER>_primary_settings_to_evaluate.csv`: exactly one highlighted paper-evaluation set plus its deterministic selection rationale and warning.
- `<TICKER>_atr_settings_to_evaluate.csv`: controls, screened candidates, and explicitly unscored experiments.
- `<TICKER>_historical_atr_profiles.csv`: distinct exact cycle-row ATR snapshots and occurrence ranges.
- `<TICKER>_atr_settings_regimes.csv`: consecutive chronological runs of exact profiles.
- `<TICKER>_atr_settings_by_cycle.csv`: cycle-by-cycle settings provenance and missing fields.
- `<TICKER>_capture_inventory.csv`: archive identity, integrity, event leg, rows, windows, issues, and relative path.
- `<TICKER>_replay_observations.csv`: one local counterfactual observation per cycle/candidate/leg.

The per-ticker JSON stores the same highlighted row under `primary_evaluation_setting`. The HTML places it between **Data coverage** and **Actual ATR settings and changes between cycles** so one complete paper-test profile appears before the detailed settings history.

A changed highlighted profile must use the evaluation-control ATR window and change exactly one independently replayed BUY or normal-SELL leg. It requires stable identical-cycle paired evidence after right-censoring, empirical execution adjustment, trading-day bootstrap, leave-one-day-out, trigger-probability, timing, adverse-excursion, and adjacent-region checks. Combined legs and alternate windows remain comparison experiments. If no one-leg change qualifies, the normalized evaluation control is highlighted unchanged.

## Important terms

**Historical median baseline**
A field-wise median/majority derived from actual settings saved on cycle rows. It is the actual-settings control summary, not a fitted recommendation. When settings changed it can combine values that never existed together.

**Current saved app settings**
The latest applicable `app_settings.strategy` snapshot. It is exact current state, not assumed historical state.

**Exact historical profile**
A stable content-derived ID for the ATR values stored together on a cycle row. Missing fields stay missing.

**Regime**
A consecutive run of cycles with one exact profile. A later return to the same profile begins a new regime.

**ATR coverage**
Percentage of candidate windows where its ATR could be reconstructed or, for a matching historical period/bar only, obtained from a saved ATR at or before the decision.

**Trigger rate**
Percentage of scoreable saved windows in which the candidate triggered before the capture ended. A capture-end non-trigger is right-censored, not a confirmed miss.

**Right-censored**
Valid candidate context existed, but the capture ended before a trigger was observed. Later behavior is unknown.

**Kaplan-Meier trigger probability**
Censoring-aware estimate of the probability of triggering by 1, 5, or 15 minutes. A changed setting uses the longest horizon supported for both candidate and control and requires at least five minutes of shared support.

**Local improvement**
For BUY, actual average fill minus counterfactual trigger, normalized in basis points. For SELL, counterfactual trigger minus actual average fill. Positive is locally favorable. The execution-adjusted form first converts the trigger to a conservative estimated fill from saved bid/ask and empirical adverse residuals. It still excludes commissions, depth, gaps, queue position, and market impact.

**Paired adjusted delta**
Candidate execution-adjusted improvement minus exact-control execution-adjusted improvement on an identical cycle where both triggered. The headline paired median uses only rows with a valid UTC trading day.

**Trading-day bootstrap**
Deterministic 2,000-replicate resampling of complete UTC trading-day clusters. Reports include 80% and 95% percentile intervals and the percentage of replicates with a positive median delta.

**Leave-one-day-out**
The paired median recalculated after omitting each contributing day. The minimum and sign-reversal count expose one-day dependence.

**Stable parameter region**
A connected multi-point group of adjacent candidates that independently pass every evidence gate and remain near a local peak. Only the deterministic center of the preferred robust region can be highlighted.

**MFE / MAE**
Maximum favorable/adverse movement after the simulated trigger and before the saved capture ends.

**Insufficient ATR coverage**
The exact alternate period/bar could not be reconstructed in at least three and at least 50% of candidate windows.

**Insufficient evidence**
ATR was available, but too few local windows/triggers existed for ranked presentation.

**Screening score**
A legacy local-window ranking heuristic combining execution-adjusted improvement, absolute delay, and adverse excursion. It is descriptive and is not the changed-setting decision rule, expected P/L, or a full backtest.

## Market Replay report

The separate Market Replay report is stored under `market_replay_<analysis-prefix>/` and contains:

| File | Purpose |
|---|---|
| `index.html` | Explained recording coverage, one complete profile, simulation method, candidate ranking, sessions, and limitations |
| `market_replay_analysis.json` | Complete machine-readable recording, contract, candidate, selected profile, session, and trade evidence |
| `atr_window_search.csv` | Stage-1 bar-duration, stage-2 period, and stage-3 narrowed-window decisions |
| `candidate_results.csv` | Every stage-3 coarse/refined profile and aggregate result |
| `robustness_evidence.csv` | Same-session candidate/control score deltas, bootstrap intervals, influence metrics, and rejection reasons for stable-region centers |
| `recommendation_leave_one_day_out.csv` | Per-trading-day omission results, stage-1/stage-2 window advancement, and stage-3 reranking evidence for the emitted profile |
| `recommended_atr_settings.csv` | Exactly one complete profile to evaluate |
| `recommended_session_results.csv` | Per-RTH-period result for the selected profile, including censoring state |
| `recommended_simulated_trades.csv` | Simulated BUY/SELL events for the selected profile |
| `control_session_results.csv` | Per-period result for the unchanged BouncyBot default control |
| `data_quality_issues.csv` | Recording and evidence limitations |
| `analysis_manifest.json` | Version, input hash, format, ticker, and search contract |
| `SHA256SUMS.txt` | SHA-256 digest of every report file except the manifest itself |

`evidence_stable=false` means the single profile is still emitted, but one or more region, source-quality, bootstrap, or leave-one-day-out rules failed. The reported same-window percentage means the recommendation's period/bar window advanced through stage 2; it is not a claim that the exact final multiplier profile was selected in every omission run. Check `instability_reasons`, the number of RTH sessions, completed trades, right-censored sessions, and stable-region size before interpreting it.

The Market Replay HTML and JSON also distinguish `raw_row_count` from `retained_row_count`. Every raw row passed container and integrity validation. The retained count is the semantic strategy stream after replay-irrelevant size/volume/high-low updates were removed. Synthetic provenance, delayed-only selection, mixed live/delayed data, frozen-feed interruption, or recorder receipt-clock reversal forces `evidence_stable=false` and appears in the issue/instability evidence.

## Market Replay v1.6 recording-set evidence

A Market Replay report now adds:

- `input_recordings.csv`: canonical physical-component inventory, hashes, format/container details, and data ranges for every selected recording;
- `recording_fragment_evidence.csv`: one row per same-date source period, including whether it was retained, stitched, dropped because of overlap/no rows, or excluded for schedule conflict;
- `excluded_sessions.csv`: complete trading dates excluded because no safe merged period could be constructed, including conflicting RTH schedules or missing retained evidence;
- paired-day, trade-participation, drawdown-tail, unmarked-position, and ATR-phase fields in recommendation and candidate tables.

`recording_count` is the number of selected main recording components. `raw_row_count` includes every validated row across inputs; `retained_row_count` is the strategy-relevant stream for included dates. A changed recommendation must have zero paired right-censored outcomes and zero unmarked open positions. The report's single profile is therefore either a changed profile that passed every authorization gate or the unchanged control accompanied by the failed-gate reasons.

## Market Replay v1.8 continuity and execution-calibration evidence

Version 1.8 adds two report files:

| File | Meaning |
|---|---|
| `continuity_evidence.csv` | Per-session continuity-chain identity, break reason, carried-position/trail flags, start/end equity, overnight gap return, terminal state, and overnight holding count. |
| `execution_calibration.csv` | Read-only BouncyBot SQLite calibration source identity, sample counts, matched/excluded evidence, commission and adverse-slippage percentiles, configured and effective cost/notional assumptions, and warnings. |

The Market Replay JSON exposes the same information under `continuity_evidence` and `execution_calibration`. Absolute calibration paths are excluded from deterministic report identity; source-content fingerprints are retained.

An overnight-linked set of RTH sessions is treated as one bootstrap dependence block. Session rows still remain visible individually, but recommendation authorization does not pretend that days connected by one open position are independent observations.

A calibration result can be **available but not applied**. This occurs when evidence exists but the relevant replacement toggle is disabled or the sample threshold is not met. The report distinguishes configured assumptions, evidence percentiles, effective assumptions, and whether cost or notional changed.

## Version 1.9 Market Replay evidence files

The Market Replay report adds these deterministic files:

| File | Meaning |
|---|---|
| `continuity_block_evidence.csv` | Candidate/control economic blocks spanning linked overnight sessions, including compounded return, cross-session drawdown, excursions, gap contribution, censoring, and terminal state |
| `score_policy_evidence.csv` | Candidate/control score and delta under balanced, drawdown-focused, return-focused, and cost-stressed policies |
| `moving_block_evidence.csv` | Circular moving-block bootstrap interval, positive probability, block length, and rejection reasons |
| `selection_bootstrap_evidence.csv` | Training-bag selected profiles and out-of-bag candidate/control deltas for selection-aware bootstrap |
| `walk_forward_evidence.csv` | Expanding chronological training/validation folds, training-only selection, final-candidate validation, and ATR-window recurrence |
| `pareto_frontier.csv` | Pareto status and profiles that dominate a candidate across return, risk, participation, turnover, censoring, and cost dimensions |
| `search_boundary_evidence.csv` | Search dimensions that touched a boundary, outward probes, and whether the region was resolved |
| `assumption_stress_evidence.csv` | Fixed candidate/control results under execution-cost, quote-age, notional, entry-delay, and entry-cutoff stress |
| `recommendation_quality_gates.csv` | Canonical required/optional pass/fail decomposition used to authorize or reject the changed profile |

The HTML report derives the displayed balanced score formula from the serialized score-policy contract. `evidence_stable=true` requires every required gate in `recommendation_quality_gates.csv` to pass. Missing walk-forward or selection-aware evidence is a failed authorization gate, not an implicit pass.

## Version 2.0 protective SELL policy evidence

The Market Replay report now describes one complete ATR and protective-SELL
policy profile. Two additional deterministic files are written:

| File | Meaning |
|---|---|
| `protective_sell_policy_comparison.csv` | Disabled, manual, and ATR-adaptive policy-screen candidates at the unchanged ATR control, including score delta, return, drawdown, completed/protective exits, cancellations, stable-region identity, selection/eligibility, clamp evidence, descriptive recovery metrics, and the decision reason. |
| `protective_sell_trade_diagnostics.csv` | Protective exits belonging to the final detailed recommended replay, including BUY/SELL timestamps and prices, effective policy, initial stop, Last trigger, normal-activation threshold, recovery/avoided-loss/regret evidence, overnight holding count, and observation boundary. |

`recommended_atr_settings.csv` remains a one-row file but now also contains
`protective_sell_mode`, `protective_sell_value`, the human-readable policy
label, enabled/adaptive flags, protective exit/cancellation counts, and the
protective-policy stability gate. Candidate, session, continuity, trade, HTML,
and JSON evidence contain the equivalent fields.

The policy comparison is performed before the ordinary ATR search. An enabled
policy may be marked `selected_policy` and `selected_for_atr_search` only when
it is the supported adjacent-region centre. This is not the final trading
recommendation: the later complete ATR-plus-policy profile must pass every
recommendation gate. If no changed profile passes, the recommendation row is
the unchanged `14 x 60-second` ATR control with `protective_sell_mode=disabled`.

Recovery and avoided-loss fields are descriptive hindsight diagnostics. They
are deliberately excluded from the candidate score and recommendation gates.
A blank value means the verified continuity chain did not provide the required
future executable bid evidence.
