# BouncyBot Offline Optimizer v2.0.0

## Protective SELL policy optimization

Version 2.0.0 extends the independent Market Replay workflow from ATR-only
profile selection to one complete ATR **and protective-SELL policy** profile.
The SQLite plus `debug_captures` workflow remains unchanged: it inventories
historical protective exits for coverage but does not optimize them because its
fill-centred windows normally omit the complete losing-position path.

Before the three-stage ATR search, Market Replay compares the unchanged control
with protective SELL disabled against a bounded policy grid at the unchanged
`14 x 60-second` ATR control:

```text
Disabled
Manual native trailing SELL: 1%, 2%, 3%, 4%, 5%
ATR-adaptive trailing SELL: 1.5x, 2.0x, 2.5x, 3.0x, 3.5x, 4.0x, 4.5x
```

The policy stage is intentionally separate from ATR period, bar-duration, and
entry/profit-exit multiplier search. It requires an adjacent near-best region
of at least three policy values; an isolated winner cannot advance. A region
centre must also have at least three modeled protective exits across three
trading dates, a practical score improvement over disabled, no material
maximum-drawdown or worst-session deterioration, and identifiable ATR-adaptive
values rather than at least 90% clamp saturation. The disabled control and at
most one supported enabled policy proceed to the full ATR search. Each policy
selects its own Stage 1 and Stage 2 ATR windows before Stage 3 searches normal
strategy multipliers and the minimum ATR clamp.

The final profile is not authorized merely because its stop policy passed the
first screen. The complete ATR-plus-policy candidate must still pass every
existing recommendation gate against the disabled unchanged control: paired
same-session evidence, whole-day/continuity-block bootstrap, exact
leave-one-day-out reselection, ATR bar-phase stress, score-policy stability,
Pareto non-domination, resolved search boundaries, economic continuity blocks,
moving-block bootstrap, assumption stress, selection-aware out-of-bag
bootstrap, and chronological walk-forward validation. When no complete changed
profile passes, the report displays the unchanged `14 x 60-second` control with
protective SELL disabled as a reference; this is not a claim that disabled is
optimal.

The replay mirrors BouncyBot's protective order semantics as far as `.ibrec`
Level 1 data permits. Immediately after a modeled BUY, a manual or ATR-adaptive
percentage produces an initial protective stop below the average BUY. The stop
is normalized with the conservative SELL reference and contract minimum tick,
then follows favorable genuine Last-price updates upward. Quote-only events
carrying a cached Last cannot move or trigger the order. After a Last trigger,
the market-style SELL remains pending until a fresh valid bid supplies
executable evidence. Active or already-triggered protective orders can cross a
provably continuous overnight boundary. When the normal minimum-profit SELL
becomes eligible, the protective order is cancelled before the normal SELL
replaces it.

The live bot waits for broker cancellation confirmation before submitting the
normal SELL. A market-data recording contains no cancellation acknowledgement,
so Market Replay models this cancel-and-replace atomically and states the
limitation in every report. The simulator also remains top-of-book rather than
a depth, queue-position, partial-fill, routing, or market-impact model.

Reports add:

```text
protective_sell_policy_comparison.csv
protective_sell_trade_diagnostics.csv
```

The policy comparison includes return, drawdown, turnover/cost-aware score,
protective exits, cancellations for normal SELL replacement, stable-region and
selection status, clamp evidence, and the decision reason. Detailed protective
trades include the effective percentage, initial stop, Last trigger, modeled
bid fill, overnight holding count, and descriptive post-exit evidence. The
latter measures whether the future executable bid recovered to the original
BUY or the contemporaneous normal-SELL activation level, the additional fall
avoided after the stop, and the recovery regret before the next modeled BUY or
the end of the verified continuity chain. These diagnostics explain the
trade-off; they do not independently drive candidate ranking.

The Market Replay analysis contract advances to version 15. Existing v1.9.4
reports remain immutable and receive different content-derived report IDs when
reanalyzed under v2.0.0.
