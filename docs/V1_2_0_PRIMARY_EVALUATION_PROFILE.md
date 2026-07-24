# BouncyBot Offline Optimizer v1.2.0

Version 1.2.0 adds one clearly identified settings set to evaluate next for every ticker while preserving the complete control, candidate, settings-history, and replay evidence below it. It also contains a full architecture and defensive-input audit of the offline analysis pipeline.

The new section appears directly between **Data coverage** and **Actual ATR settings and changes between cycles**. It contains the complete ATR configuration, the evidence source, the one replay leg that justified any change, candidate ATR coverage, the score comparison with the control, the deterministic selection rule, and an explicit paper-trading warning.

## Conservative selection boundary

The selector never combines independently selected BUY and SELL rows into a supposed optimized strategy. A changed highlighted profile must:

1. use the evaluation control's ATR period and bar duration;
2. change exactly one independently replayed leg: BUY or normal SELL;
3. have at least five complete-context windows for that leg;
4. have at least three triggers;
5. improve the exact control by at least five screening-score points when a control score exists, or have a positive score when no control score can be calculated.

Eligible rows are ordered by the smallest changed-field set, strongest control-relative comparison, screening score, candidate ATR coverage, and stable content-based tie-breakers. Combined BUY/SELL rows, alternate ATR windows, ATR-enable transitions, and initial-drop experiments remain visible but are never eligible for the one highlighted set.

When no changed profile qualifies, the report highlights the unchanged normalized evaluation control. This is the least assumptive paper-test set, not evidence that the control is optimal.

## Actual settings versus replay control

Actual settings are evidence and remain unchanged in the settings-history exports. Counterfactual candidates must be enterable in the current trading application, so malformed, legacy, or manually edited periods, bar durations, multipliers, and clamps are normalized only in a separate **counterfactual replay control**. The report shows source and replay values side by side and lists every adjustment. It never relabels a normalized value as one that actually ran.

## Reliability changes

- Fixed the reported Ruff `F541` literal-only f-string.
- Streamed large CSV capture members rather than reading a complete CSV into memory.
- Counted a capture as usable only when it contains at least one usable timestamped positive-price row.
- Loaded optional audit events deterministically.
- Preserved zero BUY/SELL multipliers as immediate-market mode while constraining suggestions to current GUI limits.
- Rejected fractional and boolean values where integer metadata is required.
- Deduplicated archive diagnostics across retries.
- Retained content-addressed, byte-repeatable report generation.
- Recomputed the selected profile's control-relative score from the canonical candidate and control scores; an exact zero score remains a valid number and is never converted to a missing value during ranking.
- Used the same canonical selected-leg score and delta in the HTML evidence table.
- Rejected WAL/SHM links that resolve outside the selected portable folder before fingerprinting or snapshot staging.
- Made JSON report serialization fail closed on unsupported objects rather than converting them through a potentially nondeterministic string representation.
- Corrected the methodology documentation so the evaluation-control hierarchy, control clamps, candidate centering, and median absolute timing-error penalty match the executable code.
- Isolated subprocess determinism checks from inherited coverage/tracing variables so the child process cannot replace the parent test run's coverage database in environments that auto-start coverage.

The selected set is exported in the ticker HTML report, under `primary_evaluation_setting` in JSON evidence, and in `<TICKER>_primary_settings_to_evaluate.csv`. It remains an in-sample local fill-window screening result. It must be evaluated prospectively in paper trading before any live consideration.
