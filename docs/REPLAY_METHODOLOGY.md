# Replay methodology and optimizer algorithm

This document describes the implemented algorithm, not an idealized full-session backtest.

## 1. Source acquisition and identity

The optimizer requires the normal BouncyBot lock to be absent and explicit user confirmation. It then acquires the same lock, hashes the SQLite/WAL and capture ZIP inputs, copies SQLite/WAL through ordinary read-only file access, opens only the private copy, and reads ZIP members without extraction. After analysis it hashes the source again and aborts if anything changed.

The input fingerprint is a canonical SHA-256 over content hashes, capture relative paths, optimizer/analysis-contract version, and row/archive limits. It excludes wall-clock time and absolute paths. This fingerprint determines the analysis ID and output directory.

## 2. Database and capture association

For each ticker, the optimizer loads cycle rows and available order, execution, decision-event, and current settings records. Cycle fill fields are preferred; execution rows can recover a missing weighted-average fill and final fill timestamp.

Capture matching is leg-specific:

- `BUY_FILL` → BUY;
- normal `SELL_FILL` → normal profit-taking SELL;
- protective SELL fill → protective exit.

An explicit capture cycle ID is authoritative. A conflicting ID is never rescued by a matching ticker/cycle number. Legacy captures without an ID can use ticker plus cycle number. If several archives match, the event timestamp nearest the recorded fill wins.

## 3. Historical settings provenance

Every cycle's ATR fields are normalized without inventing missing values. The complete partial snapshot is canonicalized and hashed into a stable profile ID. Ordered cycles are grouped into consecutive regimes, so A → B → A remains three regimes.

Three settings concepts remain separate:

- **Exact historical profile**: settings stored together on a cycle.
- **Historical median baseline**: field-wise median/majority of actual cycle snapshots, with a default only when a field is absent from every cycle.
- **Current saved app settings**: applicable `app_settings.strategy`, never assumed to describe older cycles.

Candidate generation is centered on one **evaluation control**. The control hierarchy is: an applicable complete current saved profile; otherwise the latest complete historical cycle profile; otherwise the historical median/default summary. The report preserves the selected source values exactly and, when required, shows a separate normalized replay profile constrained to current BouncyBot-enterable limits. A repaired legacy value is never described as a value that actually ran.

When settings varied, replay observations are tagged with the exact profile stored on their cycle. A candidate itself remains fixed across all cycles; otherwise one candidate row would represent different calculations on different windows.

## 4. Price selection and row handling

Native BouncyBot trailing orders use the Last trigger method. Replay therefore prefers captured `fields.last` as `trigger_price` and falls back to the captured selected application price when Last is unavailable. ATR bars use the selected application price, matching BouncyBot's adaptive-percentage input.

When freshness flags are present, non-fresh cached reads are removed. Rows are ordered by timestamp while preserving capture order for equal timestamps. Only fully identical timestamp/price/Last/bid/ask/ATR/stage/freshness rows are de-duplicated; changed quotes at the same timestamp remain distinct.

JSONL is preferred. If it is present but has no usable rows and CSV is also present, CSV is used and the report records that fallback.

## 5. ATR reconstruction

For a candidate period `N` and bar duration `B`:

```text
bucket = floor(timestamp / B)
TR = max(high - low, abs(high - previous_close), abs(low - previous_close))
ATR = simple mean of the latest N true ranges
ATR% = ATR / latest_close * 100
```

The calculation needs `N + 1` bars. Candidate percentages are:

```text
effective % = clamp(ATR% × multiplier, minimum clamp %, maximum clamp %)
```

Every candidate uses the normalized evaluation control's clamps on every cycle. A zero BUY-rebound or SELL-trail multiplier is preserved as BouncyBot's immediate-market mode and bypasses the positive minimum clamp.

When reconstruction is too short, the bot-captured ATR can be used only if the candidate's period and bar duration match an exact period/duration pair stored on that cycle. For older rows that lack both fields, an exact pair in that capture's own event strategy snapshot may be used. If neither context contains both valid fields, the captured ATR remains unassigned; the optimizer never substitutes a ticker-wide median and calls it candidate-specific evidence. The fallback selects the nearest saved ATR at or before the decision timestamp; future rows are never read. Alternate windows must be reconstructed from captured prices.

## 6. Candidate grid

The grid is deliberately bounded and centered on the normalized evaluation control:

- evaluation-control ATR period/bar;
- a shorter/faster period and bar duration;
- a longer/smoother period and bar duration;
- BUY rebound multipliers around the evaluation control;
- SELL minimum-profit and trailing multipliers around the evaluation control;
- explicit zero-mode control plus small positive experiments when the evaluation-control multiplier is zero.

The historical median remains descriptive evidence. It is not substituted for a complete current or historical profile merely because it is convenient to aggregate.

Initial-drop multipliers are not replay-scored because the complete anchor-to-drop path is normally missing. Lower and higher initial-drop rows are emitted only as paper-trading sensitivity experiments.

## 7. BUY replay

Starting at the recorded BUY order-submission time:

1. Select saved points at or after the order time. Mark the observation left-censored if the order predates the capture.
2. Reconstruct/fallback ATR at the first available decision point.
3. Lock the candidate BUY rebound percentage.
4. Track the running low of Last/trigger prices.
5. At every point calculate:

```text
BUY stop = running low × (1 + locked BUY rebound %)
```

6. Trigger at the first point where Last/trigger price is at least that stop.
7. Compare the trigger price/time with the recorded average BUY fill.

If valid ATR and price context exist but no trigger occurs before the capture ends, the result is **right-censored**. It is not labelled a confirmed miss because later data is unavailable. This replays the local trailing rebound only; it does not invent a different initial-drop event.

## 8. Normal SELL replay

Replay uses the actual average BUY price. It starts at the BUY fill when available; a SELL capture that starts after the BUY/strategy context is marked left-censored and excluded from candidate ranking.

Before activation, at each saved point:

```text
minimum profit % = ATR% × minimum-profit multiplier
SELL trail % = ATR% × SELL-trail multiplier
activation price = average BUY × (1 + minimum profit %) / (1 - SELL trail %)
```

For a cycle where adaptive minimum profit was disabled, the stored manual `rise_trigger_pct` is retained while only the SELL trail is ATR-derived.

At the first point reaching activation, the effective minimum-profit and trail values are locked. Replay then tracks the running high:

```text
SELL stop = running high × (1 - locked SELL trail %)
```

The first point at or below the stop is the local counterfactual trigger. A valid candidate that has not triggered when the capture ends is right-censored. Protective exits are inventoried for coverage but excluded from normal-profit ranking.

## 9. Observation and censoring model

Every replay observation records explicit state:

- `triggered`: the local counterfactual trigger was observed;
- `right_censored`: valid replay context existed, but the capture ended first;
- `unavailable`: ATR or price/context evidence was insufficient;
- left-censored flag: the capture began after required strategy context.

The observation interval starts at the recorded strategy/order origin when visible, otherwise at the first capture row, and ends at the trigger or last capture row. Left-censored rows remain in coverage and raw evidence but cannot rank a setting.

For each candidate/control paired set, Kaplan-Meier trigger probabilities are calculated at 1, 5, and 15 minutes. The evidence gate uses the longest horizon supported for both settings. A changed primary suggestion requires at least five minutes of shared support and may not reduce the selected trigger probability by more than five percentage points.

Kaplan-Meier handling assumes that capture termination is not systematically related to the unseen future trigger after conditioning on the saved context. That assumption cannot be proved from the available data, so the estimates remain descriptive robustness evidence.

## 10. Empirical spread and slippage adjustment

Trigger prices are converted to conservative estimated fills from the data already saved for the ticker and leg.

For each historical cycle, the optimizer identifies the actual average fill and latest non-future quote context. A fill-touch quote is used only when it is no more than five seconds old, finite, positive, and not crossed. The model records:

```text
BUY adverse beyond-touch residual  = actual fill - ask touch
SELL adverse beyond-touch residual = bid touch - actual fill
```

When a usable touch is unavailable, it records an adverse Last-to-fill residual only when that Last reference is also no more than five seconds old. Negative (favorable) residuals are floored at zero and implausibly large residuals are rejected. The 75th percentile is used for conservative candidate adjustment.

For a candidate trigger:

```text
estimated BUY fill  = max(trigger Last, trigger ask) × (1 + adverse residual bps)
estimated SELL fill = min(trigger Last, trigger bid) × (1 - adverse residual bps)
```

If no usable trigger quote exists, Last plus/minus the Last-to-fill residual is used. A candidate cycle is excluded from its own empirical residual model whenever another cycle exists. If rows for one cycle disagree on the historical fill price, fill time, trading day, reference, bid, ask, spread, or quote age, that cycle is excluded from the execution model and the conflict blocks a changed primary suggestion. Missing legacy fields may be completed by a compatible row; incompatible non-missing values are never resolved by candidate ordering. Changed settings require at least five residual samples in the actual leave-one-cycle-out model used by paired observations. A real per-pair sample count of zero remains zero and cannot be replaced by a larger ticker-level aggregate count.

This is not an order-book simulation. It does not model depth, queue position, partial fills, gaps, commissions, broker minimum-tick rules, or exchange-specific stop behavior.

## 11. Identical-cycle paired comparison

Each candidate is compared only with the exact evaluation control on cycle IDs present and scoreable for both. The optimizer fails closed when:

- a candidate or control has duplicate observations for one cycle;
- ticker, leg, actual fill, trading day, historical settings profile, or fill execution context differs between the two rows.

Direct price evidence uses only paired cycles where both settings triggered and both have an execution-adjusted estimate:

```text
paired delta = candidate adjusted improvement - control adjusted improvement
```

Pairs without a valid UTC trading day are retained in raw/all-pairs diagnostics but cannot enter trading-day bootstrap or primary-selection statistics.

Candidate-only triggers, control-only triggers, and both-right-censored pairs are reported separately and contribute to the censoring-aware trigger comparison rather than being converted into an arbitrary price penalty.

## 12. Trading-day bootstrap and leave-one-day-out analysis

Paired adjusted deltas are grouped by UTC trading day. The deterministic cluster bootstrap:

1. sorts contributing days;
2. performs 2,000 deterministic hash-indexed resamples of whole days with replacement;
3. preserves all paired trades inside each sampled day;
4. calculates the median paired delta in every replicate;
5. reports 80% and 95% percentile intervals plus the percentage of replicates above zero.

A changed setting requires at least five contributing trading days, a strictly positive lower bound of the 80% interval, and at least 80% positive replicates.

Leave-one-day-out analysis removes each day in turn and recalculates the paired median. A changed setting is rejected when an available omission result is zero/negative or reverses the sign. The report includes the worst estimate and most influential day.

The bootstrap and influence checks reduce dependence on a small observed sample; they do not remove historical trade-selection bias or create independent market regimes.

## 13. Timing and adverse-excursion gates

For both-triggered pairs, the optimizer compares:

```text
absolute timing-error change = |candidate delay| - |control delay|
MAE change                   = candidate post-trigger MAE - control post-trigger MAE
```

A changed candidate is rejected when median absolute timing error increases by more than five minutes or median adverse excursion increases by more than 25 basis points. These are conservative screening limits, not fitted economic utility weights.

## 14. Stable parameter-region detection

A single best grid point can be noise. Version 1.3 therefore evaluates adjacency on the actual generated candidate grid.

For each local peak, it builds the connected component of independently stable adjacent points within the larger of:

```text
2 basis points
20% of the local peak's paired delta
```

One-point peaks are unsupported. Among multi-point components, the preferred region maximizes its worst paired delta, then median paired delta, best paired delta, size, and deterministic key. The proposed point is the deterministic geometric center of that preferred region, with paired evidence used as a tie-breaker.

Only the center of the preferred supported region is marked evidence-stable. Other rows remain in the report with explicit instability reasons.

## 15. Local descriptive screening score

The earlier local-window score remains in reports for continuity and descriptive ordering:

```text
screening score
  = median local execution-adjusted improvement bps
  - median absolute trigger-time error in minutes
  - 0.05 × median adverse excursion bps
```

A legacy explicit `confirmed_no_trigger` result can receive a fixed miss penalty, but capture-end v1.3 observations are right-censored and are not charged as misses. The local score is not the decision rule for the primary changed setting. Paired uncertainty and stable-region evidence govern that decision.

## 16. One settings set to evaluate next

Every ticker receives exactly one complete ATR profile.

A changed profile can be selected only when it:

1. changes exactly one independently replayed BUY or normal-SELL leg;
2. keeps the evaluation-control ATR period and bar duration;
3. has at least five identical-cycle pairs and three pairs where both trigger;
4. has at least five independent UTC trading days and five empirical execution samples;
5. has a positive paired execution-adjusted median;
6. has an 80% bootstrap interval fully above zero and at least 80% positive replicates;
7. remains positive under leave-one-day-out analysis without sign reversal;
8. has at least five minutes of mutually supported censoring follow-up and no material trigger-probability deficit;
9. does not materially worsen timing or adverse excursion;
10. is the center of the preferred multi-point stable parameter region.

If several changed region centers qualify, deterministic ranking first minimizes the number of changed fields, then prefers stronger bootstrap lower bound, paired median, probability-positive estimate, leave-one-day-out minimum, region size, existing priority, and stable content keys.

When no changed profile passes every gate, the optimizer emits the unchanged normalized evaluation control as the one set to continue evaluating. This fallback is not evidence that the control is optimal.

Combined BUY/SELL profiles, alternate ATR windows, ATR-enable transitions, and initial-drop experiments remain visible but cannot become the highlighted setting because the saved inputs do not jointly validate them.

## 17. Suggested settings and evidence files

The settings-to-evaluate table contains:

1. the normalized evaluation control used to construct the grid;
2. exact saved source/current profiles when distinct;
3. the historical median as descriptive actual-settings evidence;
4. replay-screened one-leg and comparison profiles;
5. unscored alternate-window experiments when reconstruction is insufficient;
6. unscored initial-drop sensitivity rows.

The single primary profile is exported separately. Per-ticker JSON and CSV files expose raw observations, candidate summaries, paired evidence, bootstrap intervals, influence results, execution model, stable regions, exact historical profiles, regimes, capture inventory, and every instability reason.

No row is called globally optimal. Every proposed setting requires forward paper-account evaluation.

# Part II — independent Market Replay recording method

## 18. Input and integrity

One or more format-2 ZIP and/or format-3 SQLite `.ibrec` recordings are copied independently into a private temporary directory. Every selected recording must describe the same provable instrument identity: symbol, positive conId, currency, security type, exchange time zone, and minimum tick. Duplicate paths and duplicate recording content are rejected. Format 2 validates the manifest, tick schema, checksums when supplied, and bounded archive limits. Format 3 validates schema and SQLite integrity, then verifies every tick/event/RTH record hash, tick and event chain, RTH digest, and latest committed checkpoint. Every original input component is re-hashed after analysis.

The combined dataset contains at most one unambiguous RTH period per calendar trading date. When overlapping files or interrupted fragments provide more than one period for a date, the complete date is excluded rather than spliced. The optimizer cannot prove that ATR bars, the moving anchor, native trailing-order state, or position state were continuous across separate fragments. Input order, original filenames, absolute paths, and Python hash seed do not affect the content-derived analysis identity.

All raw events are validated before a replay projection is built. The projection retains all Last events, full snapshots, selected-price/quote/mark/close changes, feed changes, first/final rows, and at least one usable state per UTC second. Only redundant size-, volume-, or high/low-only events can be omitted. Reports expose both row counts.

## 19. Session and feed selection

Explicit format-3 RTH periods are used directly. Format-2 periods are derived from contract liquid-hours metadata when available, otherwise from observed session bounds with a warning. Within a period, live rows are preferred. Delayed rows are used only when no live rows exist. Frozen and delayed-frozen rows are audit evidence but are excluded from simulation.

A format-3 period left `active` with no committed observed end is valid interrupted-recorder state. Its last committed tick receipt time becomes the conservative observed end and the session remains right-censored. Sequence and monotonic `elapsed_ns` preserve event order if `captured_at_utc` moves backwards. The clock anomaly is reported and blocks stable-evidence labelling.

## 20. Strategy-price and ATR reconstruction

The `.ibrec` format does not persist the derived `ib_async.Ticker.marketPrice()` value, so the optimizer applies the closest fail-closed Level 1 approximation:

```text
Last when Last lies inside a valid non-crossed spread,
otherwise bid/ask midpoint,
then mark,
then Last,
then close.
```

A lone bid or lone ask is not treated as a strategy price. A bid observed after a modeled BUY may still be used to conservatively mark the open long.

Rows are assigned to OHLC buckets on recorder `elapsed_ns`, the monotonic clock stored by Market Replay Lab. True range and simple ATR use the same formula documented for BouncyBot. A candidate needs `period + 1` bars, includes the current partial bar after warm-up, and discards bars outside the same bounded freshness horizon used by BouncyBot. Effective percentages are multiplied, clamped, and rounded to two percentage decimals; zero BUY/SELL trails retain immediate mode.

The absolute phase of BouncyBot's process monotonic clock is not available in the recording. The primary search uses the saved canonical elapsed phase. Before a changed profile can be authorized, candidate and control are stress-tested at deterministic five-second bar-phase offsets and under an adverse aggregation that may select a different tested phase for each session.

## 21. Standardized full-cycle replay

Every RTH period begins with no position and uses the same entry-delay/cutoff rules. The moving anchor follows new highs until the ATR-derived drop threshold is reached. A positive BUY trail follows Last downward and triggers only on a new Last event; zero triggers immediate modeled entry. After entry, the minimum-profit and SELL-trail percentages are derived chronologically. A positive SELL trail follows Last upward and triggers only on a new Last event; zero exits at the minimum-profit threshold.

Initial native stops use BouncyBot-compatible conservative references and minimum-tick rounding. A normalized SELL stop must remain at or above the rounded minimum-profit floor.

## 22. Modeled fills and returns

A modeled BUY fill requires a valid non-crossed ask and uses the worse of the trigger/reference and that ask. A modeled SELL fill requires a valid non-crossed bid and uses the worse of the trigger/reference and that bid. Last, mark, close, or the opposite quote cannot prove market-order execution. When a trail triggers without the same-side touch, the market order remains pending until a later event supplies executable evidence.

Session equity is marked conservatively. The bid/ask spread enters drawdown immediately on the modeled BUY-fill event. An open long can be marked only from a bid observed after that BUY; a stale pre-entry bid is not reused. If a position is open at recording end, an unrealized gain cannot improve the score and an unrealized loss remains a penalty. An unmarked open long is right-censored and blocks a changed recommendation.

## 23. Right-censoring

A period is right-censored when the observed recording ends with an open position, unresolved BUY trail, or future standardized entry opportunity. An unfilled BUY trail is instead classified as cancelled when the observed interval reaches the configured cancellation boundary, even if no tick exists at that exact instant. This prevents a short recording from being silently interpreted as a confirmed no-trade/full outcome. Censored periods remain in the candidate denominator and receive a separate penalty. A selected profile with more than 20% censored periods cannot be labelled stable.

## 24. Three-stage ATR-window and multiplier search

The Market Replay search separates ATR bar duration from ATR period before searching strategy multipliers:

1. **Bar duration:** hold period 14 and the unchanged BouncyBot multipliers fixed while comparing 15, 30, 60, and 120-second bars. The strongest bar durations advance.
2. **ATR period:** compare periods 5, 7, 10, 14, 21, and 28 inside the advancing bar durations. The unchanged 14-period/60-second control is always retained even when its bar duration does not advance from stage 1.
3. **Strategy multipliers:** evaluate the bounded initial-drop, BUY-rebound, minimum-profit, and SELL-trail grid only inside the narrowed windows, then refine the twelve strongest profiles by 0.25 around each multiplier.

The hierarchy makes period and bar duration separately observable and limits the much larger full cross-product. It also imposes an interaction limitation: stage 1 and stage 2 use the unchanged control multipliers, so a window that is weak under those multipliers does not reach stage 3 even if it might have performed well under another multiplier combination. The result is the strongest supported profile inside this staged search, not an exhaustive optimum over every possible period, bar, and multiplier combination.

Candidate score combines median, mean, and worst conservative session returns, maximum drawdown, open-position rate, no-trade rate, and right-censoring rate. Every candidate keeps every analyzable session in its denominator.

Profiles near the best score are linked by one-step multiplier adjacency within the same ATR period/bar window advances through stage 2. A stable-region center is preferred to an isolated numerical maximum.

## 25. Market Replay trading-day bootstrap and leave-one-day-out

Each stable-region center is compared with the unchanged 14-period/60-second BouncyBot control on exactly the same RTH session identities. Evidence is grouped by UTC trading date.

The deterministic bootstrap performs 2,000 replicates. Each replicate draws complete trading dates with replacement, includes every RTH period belonging to each selected date, and recomputes both complete candidate and control scores. All stable-region centers use the same resample schedule. It reports 80% and 95% intervals plus the percentage of replicates with a positive candidate-minus-control score.

Leave-one-day-out removes each date once and recomputes the same complete score difference. The report records every omission, the minimum/median/maximum result, sign reversals, and the most influential date.

A changed Market Replay recommendation requires:

- at least five paired trading days;
- a positive full-sample score difference;
- an 80% bootstrap interval entirely above zero;
- at least 80% positive bootstrap replicates;
- a positive leave-one-day-out score for every removable day;
- no leave-one-day-out sign reversal;
- the existing connected-region, trade-count, censoring, and source-quality gates.

Ticks are never sampled independently because that would destroy the strategy's chronological and path-dependent state. Bootstrap and leave-one-day-out are rejection tests, not new market observations. With only five days they can prevent a fragile change but cannot establish future optimality.

## 26. One Market Replay profile

When a changed stable-region center passes every source-quality and day-level robustness gate, the deterministic region center becomes the one profile to evaluate. Otherwise the unchanged control is emitted as the one profile, together with the rejected candidates' evidence.

The result remains a bounded, in-sample paper-testing candidate. It is not a proof of theoretical optimality.

## Version 1.6 Market Replay recording sets and final authorization gates

One Market Replay analysis may contain several v2/v3 recordings, but only for one provably identical instrument. Files are ordered by path-independent component fingerprints. Duplicate content is rejected. The analysis includes exactly one unambiguous RTH period per calendar trading date; overlapping files and interrupted same-day fragments are exported as exclusions rather than merged.

ATR OHLC buckets use recorder `elapsed_ns`, matching BouncyBot's monotonic-clock basis more closely than receipt-wall-clock UTC. The canonical search uses the stored elapsed phase. Because the absolute phase of BouncyBot's process monotonic clock is unavailable, every changed candidate is additionally evaluated over five-second phase offsets for candidate and control bar sizes. A deliberately adverse per-session phase aggregation must also remain above control.

The fill model is fail-closed. A BUY trigger becomes fillable only when a valid non-crossed ask is present; a SELL trigger becomes fillable only when a valid non-crossed bid is present. Last, mark, close, and the opposite touch do not prove market-order execution. An open long is marked only from bid observations recorded after its BUY fill. A stale pre-entry bid cannot be reused. Missing post-entry bid evidence leaves the session unmarked and blocks a changed recommendation.

The three-stage search is rerun after every omitted trading day: stage 1 bar-duration selection, stage 2 period selection, complete stage 3 coarse multipliers, omission-specific refinement, and stable-region selection. The bootstrap samples whole dates and applies the same deterministic draws to every center. These are conservative in-sample rejection tests; they do not replace later unseen-data validation.
