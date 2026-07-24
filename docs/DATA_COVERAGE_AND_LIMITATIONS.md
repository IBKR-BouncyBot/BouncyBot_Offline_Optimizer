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

It still does not contain BouncyBot's actual settings, user start/stop decisions, account risk limits, order acknowledgements, partial fills, commissions, or real execution outcomes. It also contains only Level 1 state, not market depth or queue position. The independently recorded feed may differ slightly from the exact callback stream seen by BouncyBot.

A session is right-censored when the recording ends with a position open, an entry trail active, or enough standardized entry time remaining that another setup could still occur. Those sessions stay in every candidate's denominator and receive an explicit score penalty. More than 20% right-censored outcomes prevents a stable-evidence label.

Format 2 and format 3 both preserve `changed_fields`. Positive native trails are evaluated only on a new Last event or a full-snapshot row. Format 1 is not accepted for primary optimization because it cannot provide this event distinction.

## Multiple Market Replay recordings

Several recordings improve independent-day coverage only when they represent the same instrument and non-overlapping complete dates. The optimizer does not combine partial periods into one synthetic day. A date appearing more than once is excluded in full, including when the fragments might appear complementary, because continuity of ATR, anchor, native trail, and position state cannot be established from separate files.

Millions of events across five dates still provide roughly five independent day-level observations. Whole-day bootstrap, leave-one-day-out, stable regions, tail checks, and ATR phase stress are used to reject fragile changes; they cannot create new regimes or make an in-sample result out-of-sample. Any incomplete paired outcome blocks a changed recommendation in v1.6. Later unseen recordings and forward paper trading remain required.
