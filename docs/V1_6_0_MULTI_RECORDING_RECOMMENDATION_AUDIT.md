# BouncyBot Offline Optimizer v1.6.0 — multi-recording and recommendation audit

Version 1.6.0 extends the independent Market Replay workflow from one `.ibrec` file to a deterministic set of format-v2 and/or format-v3 recordings for the same instrument. It also completes a source-level audit of ATR reconstruction, three-stage window selection, multiplier search, bootstrap, leave-one-day-out analysis, replay state, scoring, stable regions, and final recommendation authorization.

## Multiple recording inputs

The desktop tab can add, remove, and clear several recordings. Terminal mode accepts several paths after one `--ibrec` option. The normalized input set is immutable, sorted deterministically, limited to 64 files by default, and subject to aggregate input-byte and row limits.

Every recording is copied and verified independently. A combined analysis requires matching:

- ticker symbol;
- positive IBKR conId;
- currency;
- security type;
- exchange time zone;
- minimum tick.

Duplicate paths and content-identical recording component sets are rejected. Exchange-routing metadata can differ only when the stronger instrument identity agrees, and the discrepancy is reported.

One unambiguous RTH period is required per calendar trading date. If overlapping recordings or interrupted fragments create more than one period for a date, the complete date is excluded. The optimizer never splices fragments whose ATR, anchor, trailing-order, or position state cannot be proven continuous. `input_recordings.csv` and `excluded_sessions.csv` document the decision.

The content identity uses canonical component roles, sizes, SHA-256 hashes, formats, and date ranges. Original filename, selection order, absolute input path, output path, and Python hash seed do not alter the result.

## ATR calculation audit

The Market Replay ATR follows BouncyBot's current simple-average implementation:

```text
TR = max(high - low, abs(high - previous close), abs(low - previous close))
ATR = mean(latest N true ranges)
ATR% = ATR / latest close * 100
```

It requires `N + 1` bars, includes the current partial bar, uses recorder `elapsed_ns` as the monotonic clock, and discards bars outside BouncyBot's bounded freshness horizon:

```text
max((period + 4) * bar_seconds, 300 seconds)
```

Adaptive percentages are clamped to the configured 0.10%–20.00% range and rounded to two decimals. Zero BUY- or SELL-trail multipliers remain immediate-market mode.

The absolute phase of BouncyBot's process monotonic clock is not recorded. The primary search uses the canonical saved elapsed phase; every changed recommendation is additionally stress-tested at five-second bar-phase offsets for candidate and control, plus an independently adverse per-session aggregation.

## Three-stage search and multiplier audit

Stage 1 compares 15-, 30-, 60-, and 120-second bars with period 14 and unchanged BouncyBot multipliers. The two strongest bars advance and the 60-second control remains available.

Stage 2 compares periods 5, 7, 10, 14, 21, and 28 inside the advancing bar durations. The two strongest non-control windows advance with the unchanged 14×60 control.

Stage 3 evaluates the complete coarse entry/exit multiplier grid only inside those windows. Refinement seeds are distributed across selected ATR windows and expanded in deterministic 0.25 steps. The search remains intentionally bounded: a window that is weak under control multipliers may be screened out before a different multiplier interaction is tested.

## Replay-state audit

The standardized state machine remains:

```text
ATR warm-up -> moving anchor -> initial drop -> BUY trail -> BUY
-> minimum-profit activation -> SELL trail -> SELL -> optional next cycle
```

The audit tightened execution and valuation rules:

- positive native trails trigger only on a genuine Last event or full snapshot;
- a BUY market fill requires a valid non-crossed ask;
- a SELL market fill requires a valid non-crossed bid;
- a triggered order remains pending until its same-side touch appears;
- initial trailing stops include BouncyBot's controller-side quote/Last/mark normalization and minimum-tick rounding;
- the immediate post-BUY bid/ask spread enters drawdown on the fill event;
- an open long is marked only from a bid observed after that BUY; a stale pre-entry bid is never reused;
- unresolved orders, open positions, and sessions ending before future entries are ruled out remain right-censored.

No changed recommendation is allowed when any paired candidate/control outcome is right-censored or when an open long cannot be marked from post-entry bid evidence.

## Score, bootstrap, leave-one-day-out, and stable regions

Every retained date remains in every candidate's denominator. The deterministic screen is unchanged:

```text
0.50 * median conservative return
+ 0.30 * mean conservative return
+ 0.20 * worst conservative return
- 0.35 * maximum drawdown
- 25 * open-position session fraction
- 10 * no-trade session fraction
- 15 * right-censored session fraction
```

Candidate and control are paired on identical RTH session identities. A changed profile must have positive median and mean returns, improve at least 60% of paired dates, retain at least 80% of control trading-day participation, and remain within drawdown and worst-session tolerances.

The default bootstrap performs 2,000 deterministic resamples of whole trading dates. All stable-region centers receive the same resample schedule. The 80% interval must remain above zero and at least 80% of replicates must favor the candidate.

Leave-one-day-out removes every date once and rebuilds all three search stages, including omission-specific coarse multiplier search and refinement. Every fixed candidate-minus-control omission result must remain positive, and the candidate ATR window must advance through stage 2 in at least 60% of reruns.

Near-best profiles form connected regions only within one ATR period/bar window and one 0.25 multiplier step. The deterministic region center is robustness-tested. Eligible centers are ordered by bootstrap lower bound, worst omission result, adverse phase result, observed paired improvement, raw score, region size, control distance, and profile key.

## Single profile output

The report still publishes exactly one complete ATR profile. A changed profile is emitted only when it passes every source-quality, connected-region, paired-day, complete-outcome, tail-risk, bootstrap, leave-one-day-out, window-selection, and ATR-phase gate. Otherwise the single profile is the unchanged BouncyBot control, with explicit failed-gate reasons.

The result remains an in-sample bounded-grid paper-testing candidate. It is not a mathematical optimum, expected-profit estimate, or instruction for live trading. Later unseen recordings and forward paper validation remain required.
