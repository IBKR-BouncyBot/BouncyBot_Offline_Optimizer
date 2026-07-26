# BouncyBot Offline Optimizer v1.8.0 — continuous replay and execution calibration

Version 1.8.0 extends the independent Market Replay workflow with two optional evidence improvements:

1. continuous open-position and active-SELL-trail replay across provably consecutive complete RTH recordings; and
2. read-only calibration of modeled trade size and execution costs from actual BouncyBot SQLite executions.

It also incorporates the unreleased real-recording audit work performed after v1.6.0: primary session-quality gates, connectivity-gap evidence, top-of-book liquidity checks, transaction-cost and turnover penalties, clamp-saturation diagnostics, minimum-clamp exploration, representative multiplier mini-grids during ATR-window screening, and a faster exploratory path when too few primary days exist for recommendation authorization.

The report still publishes exactly one complete ATR profile. A changed profile remains impossible unless it passes every source-quality, complete-outcome, paired-day, tail-risk, bootstrap, leave-one-day-out, window-selection, bar-phase, liquidity, clamp-identifiability, and stable-region gate.

## Continuous overnight-position replay

When enabled, the Market Replay engine orders all retained RTH periods chronologically. State may cross a session boundary only when both adjacent sessions pass the primary data-quality gate and the next recording is the conservatively expected weekday session. Friday-to-Monday continuity is accepted. A missing weekday or exchange holiday cannot be distinguished safely from absent data without a complete exchange calendar, so such a gap breaks continuity and terminalizes any unresolved position.

The carry state can preserve:

- an open long position;
- actual modeled BUY price and cost basis;
- modeled quantity;
- cumulative equity and continuity-chain drawdown;
- minimum-profit state;
- a submitted native SELL trail, including its locked trail percentage, running high, and current stop;
- a triggered SELL waiting for a valid bid-side market fill;
- detailed trade identity for a later-session exit.

An unsubmitted SELL is not frozen overnight. The next session must re-warm the configured ATR before deriving new minimum-profit and SELL-trail percentages. A SELL trail that was already submitted remains locked and can trigger on the first genuine Last event in the next recording. A BUY trail is never carried: BouncyBot's standardized five-minute-before-close cancellation remains in force.

The engine requires a valid bid-side end mark before an open long may cross into the next session. Otherwise the position is terminal and right-censored. Missing, overlapping, partial, delayed, interrupted, or otherwise non-primary periods break continuity rather than inventing market or order state.

Reports add `continuity_evidence.csv` and per-session fields for chain identity, carried position/trail state, continuity breaks, session-start and session-end equity, overnight gap return, terminal position state, and overnight holding count.

Robustness analysis does not split an economically linked entry and exit. Flat sessions remain individual bootstrap units; sessions connected by an overnight position or active SELL order form one continuity block. The exact candidate and control are replayed after each omitted day. Because the full three-stage omission search still ranks from full-run session summaries when overnight state crosses the omitted boundary, that condition is disclosed and blocks a changed recommendation rather than overstating selection stability.

## Optional execution calibration from BouncyBot SQLite

The Market Replay tab and CLI can optionally select a stopped BouncyBot portable-data folder containing `bot_state.sqlite`. This is a separate read-only prepass:

1. require the normal BouncyBot lock to be absent;
2. atomically acquire that lock;
3. fingerprint the SQLite main file and sidecars;
4. copy them through ordinary read-only file access;
5. open only a private temporary snapshot;
6. derive calibration evidence;
7. verify the source state did not change;
8. release the lock before the expensive ATR search begins.

`debug_captures` is not required for this calibration path. No SQLite statement is executed against the production database and no setting is written back.

Instrument matching prefers exact positive conId plus compatible ticker and currency. Legacy ticker-only cycles are used only when no exact-contract cycle exists. Conflicting contracts and incompatible commission currencies are excluded.

Actual execution rows are deduplicated and grouped by broker-order identity. The calibration derives:

- median actual completed BUY notional;
- per-order commission basis points;
- adverse BUY slippage beyond the latest eligible ask;
- adverse SELL slippage below the latest eligible bid;
- combined commission-plus-slippage evidence;
- sample counts and rejection reasons for each side.

A quote match is no-future and bounded by the configured maximum age. Explicit same-price quote updates refresh quote age. Crossed, non-positive, non-finite, future, or stale quotes are excluded. Positive completed-cycle commission totals can supply side-level commission evidence when row-level execution commissions are missing or less authoritative. Protective and normal SELL totals are not added together when one economic exit is mirrored into both fields.

The 75th percentile of supported adverse cost evidence is used as a conservative per-side reserve. The effective reserve can increase above the configured floor but never reduce it. The actual-notional median replaces the configured notional only after the minimum sample count is met. Calibration can be enabled while independently disabling cost or notional replacement.

Reports add `execution_calibration.csv`, the complete machine-readable calibration record, source-content fingerprints without absolute paths, effective assumptions, sample counts, percentile evidence, currency/identity warnings, and whether either assumption changed.

## Recommendation boundary

Continuous replay improves the handling of positions that genuinely survive an RTH boundary, but it does not create data for an unrecorded holiday, missing session, premarket move, overnight execution, or exchange closure. The bootstrap resamples ordinary flat sessions by trading day and overnight-linked sessions as complete continuity blocks. This prevents an entry from being sampled without its later exit. A changed recommendation requires at least five independent units. The result remains an in-sample stability test, not a future-performance confidence guarantee.

Execution calibration improves the assumed cost and size model; it does not reproduce order-book depth, queue position, hidden liquidity, routing, market impact, every partial fill, FX conversion, or exact counterfactual broker execution. Actual fills calibrate assumptions but do not prove that a hypothetical order would have received the same fill.

The selected profile remains the best-supported bounded-grid paper-testing candidate under the recorded data and configured rules. It is not a mathematical optimum or live-trading instruction.
