# BouncyBot Offline Optimizer v1.9.4

## Same-date fragment merging and diagnostic evidence

Version 1.9.4 changes multi-recording Market Replay handling so verified,
non-overlapping fragments for one trading date can contribute to one replay
period. Earlier releases excluded the complete date whenever more than one RTH
period was present, which discarded useful morning/afternoon captures and
recorder-restart continuations.

The merger remains fail-closed. Selected recordings must still describe the
same symbol, positive conId, currency, security type, exchange time zone, and
minimum tick. Same-date fragments must also agree on the scheduled RTH open and
close. A deterministic interval selector maximizes verified wall-clock coverage
first, then live-only coverage, normal-close evidence, and retained strategy
rows. It prefers fewer stitches on an exact tie and uses only content-derived
values for final tie-breaking. A sparse complete live recording therefore
cannot be displaced merely by denser but shorter or delayed partial captures.

Selected fragments are stitched chronologically into one period. Each fragment
keeps its recorder-monotonic spacing; later fragments are rebased using the
wall-clock offset of their first retained event. The merged monotonic clock is
strictly increasing, while the real outage between fragments remains visible to
ATR freshness, quote age, event-gap, and Last-event-density checks. Overlapping
tick streams are never interleaved because two recorders can disagree about the
same market instant. Conflicting RTH schedules exclude the date instead of
inventing a session boundary.

The final review hardened several boundary cases. Schedule compatibility is
evaluated only across fragments that retain strategy-relevant rows, so an empty
recovery fragment with stale metadata cannot invalidate a valid date. Fragments
that touch at one timestamp are joined only when their complete replay-relevant
boundary state agrees; the duplicate row is then removed. A conflicting
same-instant boundary remains overlapping. A fragment whose receipt UTC clock
reverses can remain descriptive single-recording evidence but is never stitched
to another recorder. The interval dynamic program also retains the best chain
ending at each concrete fragment so an incompatible same-end prefix cannot be
combined with a later boundary by mistake.

A merged period retains every contributing recording fingerprint. Session
quality resolves format, manifest status, and connectivity events across all
contributors. Missing component provenance or mixed format ownership fails
closed. Quality-gate exclusions now list the individual contributing hashes
rather than a synthetic joined value.

Reports add `recording_fragment_evidence.csv` and an equivalent HTML/JSON
section. Each source period is labelled as retained, stitched, dropped because
of overlap, dropped because no strategy-relevant rows remained, or excluded for
schedule conflict. The selection reason, source and merged period IDs, observed
range, recording fingerprint, and retained-row count are preserved.

Per-recording load and preflight failures name the offending file without
duplicating an existing filename in the underlying diagnostic. Preflight also
rejects a recording whose manifest declares zero market-data rows, rather than
showing Ready and failing only after analysis starts.

The Market Replay analysis contract advances to version 14 so content-addressed
reports cannot collide with results produced under the earlier complete-date
exclusion policy. ATR formulas, the three-stage search, multiplier and clamp
search, replay state machine, execution calibration, scoring, bootstrap,
leave-one-day-out, walk-forward, stable-region selection, and recommendation
authorization are otherwise unchanged.
