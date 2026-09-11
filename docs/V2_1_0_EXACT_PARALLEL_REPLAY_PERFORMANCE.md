# BouncyBot Offline Optimizer v2.1.0

## Exact compact and process-parallel replay performance

Version 2.1.0 reduces the dominant Market Replay runtime without changing the
analytical search or recommendation. The performance work targets Stage 3
coarse profiles, local multiplier refinements, and outward search-boundary
probes. Candidate generation, sorting, stable-region construction, bootstrap
schedules, leave-one-day-out logic, walk-forward logic, recommendation gates,
and report serialization remain deterministic in the parent process.

## Compact replay representation

Each primary replay session is transformed once into a read-only NumPy
structured array containing only state used by the replay engine:

```text
sequence
receipt timestamp and exact receipt epoch-millisecond
monotonic elapsed_ns
selected strategy price
validated bid and ask
Last and mark
bid and ask sizes
full-snapshot / Last-event / bid-event / ask-event flags
```

Strategy price, crossed-quote rejection, and event semantics are calculated
from the validated `IbrecTick` objects before the compact files are published.
ATR values for each selected period/bar window are also stored in read-only
file-backed arrays. Spawned Windows workers attach to those files rather than
receiving millions of pickled Python objects.

The temporary files contain derived copies only. They are deleted when the
profile scheduler closes and are never included in a report or source archive.
A worker or mapping failure aborts the analysis; it cannot remove profiles from
the search silently.

## Exact inner-loop reductions

The state machine retains the same chronological decision order while reducing
allocation and repeated work:

- maximum drawdown is updated online whenever equity changes rather than
  retaining a complete equity list and scanning it afterward;
- one-observation-per-component-per-second clamp evidence uses the last sampled
  second instead of allocating `(component, second)` tuples in a set;
- validated configuration is normalized once for compact profile evaluation;
- session assumptions and event flags are read from compact arrays;
- broad profile evaluation retains aggregate/session evidence, while detailed
  trade evidence is still produced by the existing selected-profile replay.

The floating-point formulas, ATR construction, stop rounding, quote-age rules,
execution assumptions, and update ordering are unchanged.

## Process-parallel profile evaluation

Independent profiles are evaluated in one persistent spawned process pool.
The parent sends only small profile batches and optional day weights. Workers
attach to the same read-only tick and ATR memory maps, evaluate complete
chronological profile paths, and return compact candidate/session summaries.
Results are sorted by stable profile key after completion, so operating-system
completion order cannot affect downstream ranking or report bytes.

The GUI setting **Profile-evaluation workers** and terminal option
`--ibrec-workers` accept:

```text
0   Automatic
1   Exact compact single-process mode
2–64 Explicit worker count
```

Automatic mode leaves one logical processor free and caps the pool at eight
workers. For fewer than 50,000 retained rows it keeps the existing object-based
reference evaluator, avoiding both compact-array preparation and process
startup where those costs would dominate. At 50,000 retained rows or more it
uses the compact engine and, when more than one logical processor is available,
spawned workers. An explicit value of 1 opts into the compact single-process
engine at any size; 2-64 opts into the compact spawned-process engine. Worker
count is an execution preference only and is excluded from the analysis
contract, content-addressed input identity, candidate keys, and report content.

`main.py` calls `multiprocessing.freeze_support()` before application startup,
and the packaged smoke test imports the compact replay module so the pinned
Windows PyInstaller build verifies NumPy and spawn-path collection.

## Equivalence requirements

The release tests compare the previous Python-object reference engine with:

1. compact single-process evaluation; and
2. compact spawned-process evaluation.

The tests require equality for candidate summaries and every session result.
They also require one-worker and multi-worker complete analyses to produce the
same analysis ID, search contract, candidate dictionaries, recommendation, and
byte-identical report files. Different process completion order, input path,
input order, and Python hash seed must not alter output.

## Performance interpretation

A local deterministic 5,000-row end-to-end search probe required 17.798684
seconds under the published v2.0.2 object engine. The v2.1.0 compact engine
completed the same analysis in 6.469525 seconds with one worker and 4.029773
seconds with four workers in the Linux release environment. The analysis ID,
candidate evidence, and recommendation were identical. This corresponds to
about 2.75x and 4.42x speedups on that fixture. Small recordings can still be
slower when compact-array preparation or process startup dominates; Automatic
therefore keeps inputs below 50,000 retained rows on the reference evaluator.

These numbers are engineering measurements for one machine and fixture, not a
runtime guarantee. Large recordings with many Stage 3 and boundary profiles
are expected to benefit more. File validation, Stage 1/2 selection, robust
validation, report writing, disk speed, memory bandwidth, and the number of
retained strategy events remain part of total runtime.

## Analytical scope

Version 2.1.0 does **not**:

- reduce retained market rows;
- shrink the candidate grid;
- skip outward probes;
- reduce bootstrap replicates;
- use approximate early stopping;
- change ATR formulas or score policies;
- change recommendation authorization.

It is an execution-engine release. An identical validated input and analytical
configuration must produce the same recommendation whether the worker setting
is automatic, one, or any supported explicit process count.
