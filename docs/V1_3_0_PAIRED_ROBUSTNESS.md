# BouncyBot Offline Optimizer v1.3.0 — paired robustness release

Version 1.3.0 strengthens the single per-ticker ATR paper-evaluation suggestion while continuing to use only `bot_state.sqlite` and `debug_captures/`.

## Implemented evidence model

- Every changed candidate is compared with the exact evaluation control on the intersection of identical cycle IDs.
- Duplicate candidate/control rows and immutable-context disagreements are excluded fail-closed and reported.
- A valid non-trigger at capture end is right-censored, not automatically treated as a failed trigger.
- Kaplan-Meier trigger probabilities are calculated at 1, 5, and 15 minutes. The longest horizon supported for both candidate and control is used; a changed primary suggestion requires at least five minutes of shared support.
- Candidate trigger prices are converted to conservative estimated fills using saved executable bid/ask touches and empirical adverse fill residuals. Fill-touch quotes and Last references older than five seconds, plus crossed quotes, are excluded from execution-residual estimation. A candidate cycle is removed from its own residual sample whenever another cycle is available.
- If counterfactual rows for the same cycle disagree on the historical fill or quote context, that cycle is excluded from the empirical execution model and no changed primary setting can pass the stability gate. A real per-pair sample count of zero is retained as zero rather than replaced by an aggregate count.
- Paired execution-adjusted deltas are resampled by whole UTC trading days in a deterministic 2,000-replicate bootstrap.
- Leave-one-day-out estimates expose whether a single day controls the conclusion.
- Stable-parameter-region detection rejects isolated grid peaks. The preferred region maximizes its worst supported paired delta, then median and best-point evidence; only its deterministic center can be highlighted.

## Changed-setting stability gates

A changed single-leg setting is not highlighted unless it has at least five paired windows, three both-triggered pairs, five contributing UTC trading days, five empirical execution samples, a positive paired median, a positive 80% bootstrap interval, at least 80% positive bootstrap replicates, positive leave-one-day-out results, no sign reversal, no material trigger-probability deficit, no more than five minutes of median absolute timing deterioration, no more than 25 basis points of median adverse-excursion deterioration, and adjacent stable-region support.

When those requirements are not met, the report still emits exactly one complete set: the unchanged normalized evaluation control. This fallback is the least-assumptive next paper test, not a statement that the control is optimal.

## Output additions

Per-ticker JSON and CSV evidence now include paired-cycle counts, excluded duplicate/context-mismatch cycles, right-censoring outcomes, selected Kaplan-Meier horizon, bootstrap intervals, leave-one-day-out influence, empirical execution-model details, stable-region membership, and every reason a candidate was considered unstable.
