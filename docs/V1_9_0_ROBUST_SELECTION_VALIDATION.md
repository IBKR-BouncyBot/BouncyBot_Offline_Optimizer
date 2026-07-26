# BouncyBot Offline Optimizer v1.9.0

## Robust selection and validation release

Version 1.9.0 strengthens the Market Replay recommendation process without changing BouncyBot or writing settings back to it. The optimizer still produces exactly one complete ATR profile per analysis. A changed profile is now authorized only after the complete search and the selected profile survive additional out-of-sample, resampling, policy, boundary, and assumption checks. When those checks are unavailable or fail, the unchanged BouncyBot control is shown as the reference profile.

## Exact leave-one-day-out selection

For each primary-eligible trading date, v1.9.0 removes that date from the raw chronological RTH input before rebuilding the analysis. This breaks and reconstructs overnight continuity correctly, recalculates ATR, reruns all three ATR-search stages, rebuilds the Stage 3 multiplier/clamp grid, performs omission-specific refinement, and selects a new stable region. The report records whether the full-data ATR window and profile remain selected.

The fixed candidate and unchanged control are also replayed on each reduced chronology. A changed recommendation requires positive candidate-versus-control evidence after every date omission and no sign reversal.

## Chronological walk-forward validation

When at least 20 primary-quality sessions are available, the optimizer creates expanding chronological folds. Each fold:

1. selects a profile using only earlier training sessions;
2. freezes that profile;
3. evaluates it on the next five unseen sessions;
4. evaluates the final proposed profile on the same unseen sessions;
5. compares both with the unchanged control.

Training returns are excluded from validation scoring. Drawdown is rebased at the validation boundary while a legitimately carried overnight position may cross that boundary. A changed profile requires positive validation evidence in every complete fold and recurrence of the final ATR window in the training-only selections.

## Selection-aware and moving-block bootstrap

The existing 2,000-replicate fixed-profile bootstrap remains. Version 1.9.0 adds:

- a circular moving-block bootstrap that samples adjacent trading-day or overnight-continuity units to test short-lived regime dependence; and
- a deterministic selection-aware bootstrap that reruns the three-stage selector inside each training bag and evaluates the selected profile on out-of-bag units.

Overnight-linked dates are kept in one economic unit. A candidate entry date cannot be sampled independently from its later exit date.

## Separate, date-cross-fitted execution calibration

SQLite calibration now maintains separate BUY and SELL cost distributions. It reports p50, p75, and p90 commission-plus-slippage evidence for each side. The primary replay uses conservative p75 reserves, while the assumption-stress layer uses p90 evidence.

For each replay date, calibration prefers strictly earlier executions. When there are too few earlier samples, it uses leave-date-out evidence and shrinks the estimate toward the configured default. Same-day or future fills cannot calibrate an earlier counterfactual decision.

## Single-source score policies and Pareto analysis

The active balanced score formula is represented by one immutable score-policy object used by calculation, JSON, HTML, CSV, documentation, and tests. No-trade dates remain in the denominator but do not receive a direct penalty. Execution costs, turnover, drawdown, censoring, and unresolved positions remain explicit.

The candidate is also evaluated under drawdown-focused, return-focused, and cost-stressed policies. A changed recommendation must remain preferable under every required policy. A profile dominated by another searched profile on return, tail result, participation, drawdown, censoring, turnover, and cost cannot be recommended.

## Search-boundary extension

A stable region touching the edge of the searched ATR period, bar duration, multiplier, or active minimum-clamp range is treated as truncated. The optimizer extends the affected dimension deterministically, evaluates outward profiles, and rebuilds the local region. Authorization remains blocked while the region still terminates at an arbitrary search boundary.

## Assumption-stress analysis

Candidate and control are replayed under deterministic variations of:

- execution reserve, including p90 evidence;
- quote-age limits;
- half and double trade notional;
- delayed entry start; and
- earlier or later entry cutoff.

A changed profile must remain better than the control and must not introduce an unmarked open position under every required scenario.

## Economic overnight-continuity evidence

The report aggregates linked sessions into economic continuity blocks. It records compounded block return, cross-session maximum drawdown, adverse and favorable excursion, underwater session endpoints, overnight gap contribution, linked-session count, completed trades, terminal censoring, and terminal position state. Candidate and control must be comparable on the same blocks.

## Recommendation-quality decomposition

Every changed-profile gate is exported as a named pass/fail row. The report separately identifies source quality, stable-region support, paired evidence, continuity blocks, fixed-profile bootstrap, moving-block bootstrap, leave-one-day-out selection, ATR phase, score-policy stability, Pareto status, search-boundary support, assumption stress, selection-aware bootstrap, and walk-forward validation.

## Important interpretation

The recommendation remains a bounded research result. Even when every v1.9.0 gate passes, it is one profile to evaluate prospectively in paper trading. It is not a mathematical global optimum, a guarantee of future performance, or a live-trading instruction.
