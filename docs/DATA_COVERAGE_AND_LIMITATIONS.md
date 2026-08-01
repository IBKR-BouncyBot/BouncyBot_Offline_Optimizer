# Data coverage and limitations

## Available evidence

The optimizer assumes only the files BouncyBot currently writes:

- SQLite cycle, order, execution, order-relevant decision-event, and current app-settings records.
- Completed market-data capture ZIPs centred on actual BUY, normal SELL, or protective SELL fills.

A capture normally contains up to 900 seconds before and 900 seconds after its fill. The pre-window can be shorter after startup, connection loss, sparse updates, or an incomplete rolling buffer. A capture can be absent when the app closes before the complete post-window is saved. The general `events` table is not required for trigger reconstruction and is not bulk-loaded.

Legacy or incomplete cycle rows can use execution rows as a bounded fill fallback. BUY, normal SELL, and protective SELL references remain separate. Protective exits contribute to exit coverage but are excluded from normal-profit SELL ranking.

## Coverage score and grade

The 0–100 coverage score is an evidence-completeness score. It combines capped components for:

- any cycle evidence;
- completed-cycle depth;
- BUY capture matching;
- all exit capture matching, including protective exits;
- usable archive ratio;
- replayable BUY-window ratio;
- replayable normal-SELL-window ratio;
- presence of ATR evidence;
- calendar-span depth.

The report lists every component and its points. Grades add sample-depth requirements:

- A: score at least 80 and at least 20 completed cycles;
- B: score at least 65 and at least 10 completed cycles;
- C: score at least 45 and at least 3 completed cycles;
- D: everything else.

A grade is not a profitability, confidence, or strategy-quality score.

## Settings changed between cycles

Changing settings creates a confounding problem: later settings may coincide with a different volatility regime, date range, or market condition. The optimizer therefore does not pretend that one setting applied throughout history.

For every ticker it exports:

- the exact ATR snapshot stored on each cycle;
- stable exact-profile IDs;
- profile occurrence counts;
- chronological consecutive regimes, preserving A → B → A as three regimes;
- a separate current `app_settings.strategy` snapshot when applicable;
- pooled candidate metrics;
- the same fixed candidate split by the historical profile attached to each cycle.

A subgroup difference is descriptive, not causal. A small profile subgroup is especially unstable. The fixed candidate does not adopt each cycle's original multipliers or clamps; doing so would test a different candidate on every row. Historical settings remain provenance labels for subgroup analysis.

## Historical median baseline

The row labelled **Historical median baseline (derived from actual stored cycle ATR settings)** is a descriptive summary constructed from the cycle table:

- numeric fields use the field-wise median;
- boolean fields use a deterministic majority;
- a documented default is used only when no cycle stores that field.

The underlying values are actual saved historical settings, but when settings varied the combined median row may never have existed as one complete configuration. Exact profile and cycle-history tables are therefore authoritative for individual cycles. Candidate generation instead uses the applicable complete current profile, otherwise the latest complete historical profile, and only then this median/default summary. The median row is not a fitted recommendation.

## One highlighted settings set

The ticker report highlights one complete settings set for the next controlled paper evaluation. A changed set may be justified by only one independently replayed leg at a time. It must keep the control ATR window and pass all identical-cycle pairing, execution-adjustment, censoring, bootstrap, leave-one-day-out, trigger-probability, timing, adverse-excursion, and stable-region requirements. This avoids presenting independently selected BUY and SELL rows as one jointly tested strategy or choosing an isolated best grid point.

Alternate ATR windows cannot be highlighted because changing the window also changes the initial-drop decision, which is normally outside the saved fill-centred captures. Combined BUY/SELL rows remain visible comparison profiles but are not jointly simulated. If no one-leg change qualifies, the report retains the normalized evaluation control. A control-only result means the available evidence does not support a changed profile; it does not mean the control is optimal.

## Right-censoring and trigger availability

A candidate that has not triggered when its capture ends is not known to have failed. Version 1.3 records it as right-censored and estimates trigger probability with Kaplan-Meier calculations at 1, 5, and 15 minutes. Candidate and control are compared at the longest horizon supported for both; a changed suggestion requires at least five minutes of mutual follow-up.

This handling assumes censoring is not systematically related to the unseen future trigger after conditioning on the recorded context. Capture files are generated around actual historical fills, so that assumption cannot be guaranteed. The estimates are robustness checks, not proof of eventual fill probability.

## Execution adjustment limitations

Saved actual fills, Last, bid, and ask are used to estimate adverse execution residuals by ticker and leg. Fill-touch quotes and Last references older than five seconds, non-positive values, and crossed quotes are excluded from execution-residual estimation. A candidate cycle is omitted from its own empirical model when another sample exists. The 75th-percentile non-negative residual produces a conservative local estimated fill.

The estimate still lacks order-book depth, queue priority, hidden liquidity, exchange routing, partial-fill sequence, gap behavior, minimum-tick rounding, commissions, and market impact. Sparse samples force the changed recommendation to remain the control.

## Uncertainty and multiple comparisons

Trading-day bootstrap and leave-one-day-out checks measure sensitivity to the observed days. Stable-region detection reduces the chance that one isolated grid point is highlighted. These safeguards do not eliminate selection bias, regime confounding, or the fact that several candidate settings were examined. The selected set remains in-sample and must be frozen and evaluated prospectively in paper trading.

## Unobservable counterfactuals

The available files usually cannot answer:

- whether another initial-drop value would have produced a trade earlier in the session;
- whether another configuration would have traded on a day where the recorded configuration did not;
- the complete path between BUY and SELL when the holding period exceeds the saved windows;
- queue position, hidden liquidity, exact partial fills, gaps, market impact, or broker/exchange trigger semantics for a hypothetical order;
- how the strategy behaves in market regimes absent from the stored sample.

Initial-drop alternatives are therefore emitted only as unscored sensitivity experiments. Alternate ATR windows are ranked only when that exact window can be reconstructed in enough captures. Full-session optimization requires future continuous recording or a separately licensed historical data source.

## Selection bias

Only fills created by historical settings have captures. This is selection bias. The optimizer can compare local trigger behavior around recorded fills; it cannot observe all trades a different configuration would have created, skipped, or held longer. Every suggestion must be treated as a bounded paper-trading experiment.

## Market Replay recording coverage

Format 2 and format 3 provide full-session market-path evidence when the recording itself spans the full session. Format 3 can also contain a valid active period after abrupt recorder termination. Such a period remains right-censored; the optimizer does not infer that a still-open setup or position resolved after the final committed event.

The report distinguishes raw validated rows from the smaller retained strategy stream. Compression removes only events that cannot change strategy price, Last-trigger identity, stop normalization, modeled fill, feed selection, or the final usable state in a UTC second. This is an analysis-performance optimization, not a reduction in integrity verification.

Synthetic samples are suitable for software validation, not ticker-setting evidence. Delayed-only sessions, live/delayed transitions, frozen-feed interruptions, crossed quotes, and receipt-clock reversals are reported. Synthetic, delayed, mixed-feed, frozen-interruption, or clock-reversal evidence cannot be labelled stable even when its numerical grid result is internally consistent.

The `.ibrec` workflow improves path coverage because it can observe the anchor, initial drop, BUY rebound, holding period, profit activation, and SELL trail in one continuous RTH period. It can therefore screen all ATR multipliers and ATR windows jointly inside the standardized simulator.

The recording itself does not contain BouncyBot's actual settings, user start/stop decisions, account risk limits, order acknowledgements, partial fills, commissions, or real execution outcomes. Optional v1.8 SQLite calibration can add actual ticker-specific execution quantity, commission, and quote-relative adverse-cost evidence, but it does not reconstruct historical app state or prove a hypothetical fill. Market Replay remains Level 1 evidence without market depth or queue position. The independently recorded feed may differ slightly from the exact callback stream seen by BouncyBot.

A session is right-censored when the recording ends with a position open, an entry trail active, or enough standardized entry time remaining that another setup could still occur. Those sessions stay in every candidate's denominator and receive an explicit score penalty. More than 20% right-censored outcomes prevents a stable-evidence label.

Format 2 and format 3 both preserve `changed_fields`. Positive native trails are evaluated only on a new Last event or a full-snapshot row. Format 1 is not accepted for primary optimization because it cannot provide this event distinction.

## Multiple Market Replay recordings

Several recordings improve independent-day coverage only when they represent the same instrument. Verified same-date fragments with compatible scheduled RTH boundaries can now be stitched into one period. The deterministic selector maximizes wall-clock coverage, then prefers live-only and normal-close evidence before callback density, preserves the actual outage between fragments in the merged monotonic clock, and never interleaves overlapping streams. This does not make a gap disappear: excessive event gaps, sparse Last evidence, incomplete boundaries, mixed feeds, connectivity loss, or other quality failures still prevent the date from authorizing a changed recommendation. Conflicting schedules exclude the date.

Millions of events across five dates still provide roughly five independent day-level observations. Whole-day bootstrap, leave-one-day-out, stable regions, tail checks, and ATR phase stress are used to reject fragile changes; they cannot create new regimes or make an in-sample result out-of-sample. Any incomplete paired outcome blocks a changed recommendation in v1.6. Later unseen recordings and forward paper trading remain required.

## Version 1.8 continuity and calibration limitations

An open long or submitted SELL trail can cross an RTH boundary only when the adjacent periods are complete, primary-eligible, and conservatively consecutive. Friday-to-Monday is recognized. A missing weekday or exchange holiday is ambiguous without a complete exchange calendar; the optimizer therefore breaks continuity instead of assuming the market was closed. Premarket/after-hours movement and overnight executions are not present in RTH-only recordings. The next session begins from the prior close mark, and the first fresh in-session bid determines the observed opening gap for an existing long.

A carried HOLD re-warms ATR before deriving an unsubmitted normal SELL. An already submitted native SELL trail remains locked. This distinction mirrors the strategy lifecycle but still assumes that no broker-side cancellation, corporate action, manual trade, split, dividend, currency movement, or account event altered the position between recordings.

Whole-day bootstrap values use the observed incremental daily outcomes of the continuous replay. Those outcomes can be serially dependent when one position spans several dates. Resampling days does not recreate the underlying path and does not produce independent market regimes. Bootstrap and leave-one-day-out remain fail-safe rejection evidence, not future-performance confidence guarantees.

Optional SQLite calibration is accepted only from a stopped BouncyBot folder. Exact positive conId evidence is preferred; legacy ticker-only evidence is used only when no exact cycle exists. Quote matches are no-future and age-bounded, but the `.ibrec` feed and BouncyBot execution feed are independent subscriptions and may not align event-for-event. The 75th-percentile adverse reserve is deliberately conservative and is applied symmetrically per side even when only one side supplies the strongest estimate. Commission values in an incompatible currency are excluded because no FX series is available.

Calibration cannot model market depth, queue priority, hidden liquidity, routing, market impact, exact partial-fill ordering, or whether a counterfactual order would have reached the same venue at the same time. It can raise the assumed cost reserve and replace the assumed quantity scale; it cannot turn the replay into an execution simulator.

## Version 1.9 validation limits

Walk-forward, moving-block, and selection-aware bootstrap reuse the selected historical recording set. Walk-forward creates chronologically unseen validation blocks inside that set, but it is not a substitute for later recordings collected after the recommendation was frozen. Selection-aware bootstrap estimates the stability of the search procedure and its out-of-bag result; it does not create new market regimes.

A changed profile normally requires at least 20 primary-quality sessions because the default expanding walk-forward policy uses 15 training sessions and a five-session validation block. Exact leave-one-day-out and the fixed-profile bootstrap remain available from five primary days, but they are not sufficient by themselves to authorize a v1.9 change.

Cross-fitted SQLite calibration prevents same-day and future execution evidence from calibrating an earlier replay date. Small per-side samples are shrunk toward the configured default, so reported p75/p90 values can remain assumption-sensitive. Market depth, routing, queue position, and counterfactual partial-fill ordering are still unobservable.
