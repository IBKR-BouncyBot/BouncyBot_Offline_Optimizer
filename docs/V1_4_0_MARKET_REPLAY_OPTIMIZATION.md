# BouncyBot Offline Optimizer v1.4.0 — Market Replay optimization

Version 1.4.0 adds a second, independent analysis workflow for one Market Replay Lab `.ibrec` recording. The existing BouncyBot SQLite and `debug_captures` workflow remains unchanged in purpose and continues to produce one conservative per-ticker setting for forward paper evaluation.

## Separate desktop tab

The desktop application now has two tabs:

1. **BouncyBot SQLite & captures** — the established lock-coordinated, read-only analysis of BouncyBot data.
2. **Market Replay (.ibrec v2/v3)** — a standalone full-recording ATR grid search that does not read BouncyBot data, acquire `ibkr_trading_bot.lock`, or connect to IBKR.

The Market Replay tab preflights the selected recording, displays its ticker, format, container, lifecycle status, row count, and synthetic-sample warning when applicable, asks for confirmation, runs the bounded search in a worker thread, and presents exactly one complete ATR profile with a button to open its dedicated report.

## Supported recording formats

The importer accepts:

- **Format 2:** legacy ZIP container with `manifest.json`, `ticks.csv`, optional `events.jsonl`, and optional SHA-256 member checksums.
- **Format 3:** SQLite container introduced by Market Replay Lab 1.3.0, including metadata, ticks, events, explicit RTH periods, record hashes, tick/event hash chains, and committed checkpoints.

Format 1 is rejected because it cannot distinguish a new Last event from a cached Last retained on a quote-only update. Positive native trailing orders require this distinction.

Format-3 validation includes SQLite structural checks, foreign-key checks, manifest/schema agreement, contiguous event sequencing, per-record hashes, chain hashes, RTH-period digest verification, and latest-checkpoint verification. The attached Market Replay Lab 1.3.0 sample is accepted as format 3 and is identified from its manifest as synthetic validation data.

An RTH period may legitimately remain `active` after hard termination. If it has no committed `observed_end_utc`, the last committed tick receipt time is used as a conservative observed end and the period remains right-censored. The format guarantees monotonic sequence and `elapsed_ns`, not a monotonic operating-system wall clock. Receipt-clock reversals therefore do not reorder events; they are reported and prevent stable-evidence labelling.

## Read-only input handling

The selected `.ibrec` and an active rollback journal, when present, are copied to a private temporary directory with ordinary file reads. Only the private copy is opened. SQLite may recover a copied hot rollback journal before the private database is reopened read-only. The optimizer hashes the original before copying, verifies the private copy, and re-hashes/revalidates every source component after analysis. Symlink inputs, a component replaced by a symlink, source mutation, journal-state changes, and unexpected WAL/SHM sidecars are rejected.

The report identity uses path-independent component roles and content hashes. Renaming or moving a byte-identical recording does not change the analysis ID or generated report bytes.

## Full-recording ATR replay

The separate workflow jointly evaluates:

- ATR period;
- ATR bar duration;
- initial-drop ATR multiplier;
- BUY-rebound ATR multiplier;
- minimum-profit ATR multiplier;
- SELL-trail ATR multiplier.

ATR uses UTC epoch-aligned OHLC bars and BouncyBot's simple mean of true ranges. The bounded coarse grid covers four ATR windows and fixed multiplier sets, followed by deterministic local refinement around the twelve strongest coarse profiles.

Each RTH period is replayed independently through:

```text
ATR warm-up
→ moving anchor
→ initial drop
→ BUY trailing rebound
→ modeled BUY
→ minimum-profit activation
→ SELL trail
→ modeled SELL
→ optional next cycle
```

The simulation preserves BouncyBot's standard entry delay, entry cutoff, BUY-trail cancellation time, minimum-tick stop rounding, minimum-profit protection check, and Last-event semantics. Version-2 and version-3 `changed_fields` are authoritative: a quote-only event carrying a cached Last cannot trigger a positive native trail.

Every raw row is integrity-checked before optimization. The in-memory strategy stream can then discard only redundant size-, volume-, or high/low-only updates that cannot change strategy price, Last-trigger identity, stop normalization, modeled execution, feed choice, or UTC-second state. Raw and retained counts are both present in the report.

## Fill and price model

ATR uses the selected strategy-price approximation in this order:

```text
valid bid/ask midpoint
mark price
Last
close
bid
ask
```

For modeled execution:

```text
BUY fill  = worse of trigger/reference and valid ask
SELL fill = worse of trigger/reference and valid bid
```

The model does not reproduce depth, queue position, partial fills, commissions, routing, market impact, hidden liquidity, or IBKR `marketPrice`, which the recording does not store. It is therefore a deterministic screening simulator rather than an execution backtester.

## Right-censored recording outcomes

A recording can end while:

- a position remains open;
- a BUY trailing setup remains active; or
- the standardized entry window is still open and a later setup remains possible.

Those sessions are explicitly right-censored. If the observed interval reaches the configured five-minute-before-close BUY-trail cancellation boundary, an unfilled BUY trail is classified as cancelled rather than right-censored even when no event occurred at the exact boundary. A profitable unrealized mark cannot improve the conservative return, an unrealized loss remains a penalty, and the candidate score includes a right-censoring penalty. More than 20% right-censored sessions prevents the selected profile from being labelled stable evidence.

## Candidate ranking and stable-region selection

Every candidate keeps every analyzable session in its denominator. The ranking score is:

```text
0.50 × median conservative session return
+ 0.30 × mean conservative session return
+ 0.20 × worst conservative session return
− 0.35 × maximum drawdown
− 25 × open-position session fraction
− 10 × no-trade session fraction
− 15 × right-censored session fraction
```

Near-best adjacent profiles are grouped into connected parameter regions. The optimizer prefers a supported region and selects its deterministic geometric center rather than blindly choosing an isolated numerical maximum. Stable evidence requires at least five RTH sessions, at least three completed simulated trades in at least three sessions, no more than 20% right-censored sessions, and an adjacent near-best region. Synthetic provenance, delayed-only evidence, mixed live/delayed evidence, frozen-feed interruption, or a recorder clock reversal independently prevents the stable label.

The report always emits exactly one complete profile. When no candidate trades, the unchanged BouncyBot default profile is emitted as an explicit fallback rather than being described as optimized.

## Deterministic report

The Market Replay workflow writes a separate content-addressed report containing:

```text
index.html
market_replay_analysis.json
candidate_results.csv
recommended_atr_settings.csv
recommended_session_results.csv
recommended_simulated_trades.csv
control_session_results.csv
data_quality_issues.csv
README_REPORT.txt
analysis_manifest.json
SHA256SUMS.txt
```

Identical input bytes and analysis contract produce the same analysis ID and byte-identical report files across different source names, source paths, output paths, wall-clock times, and Python hash seeds.

## Interpretation

The selected profile is the best-supported profile inside the documented bounded grid for the supplied recording. It is not a mathematical optimum, future-market forecast, or live-trading instruction. A short or single-session recording is insufficient for a robust ticker-wide conclusion. Prefer many unbiased full-session recordings and validate the one selected profile in forward paper trading.
