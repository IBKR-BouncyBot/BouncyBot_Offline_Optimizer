# BouncyBot Offline Optimizer v2.3.0

## Analysis-wide parallel robustness validation

Version 2.3.0 keeps one compact read-only Market Replay store and one spawned
process pool alive from Stage 3 through the expensive post-search authorization
work. Version 2.2.x already parallelized coarse Stage 3 profiles, multiplier
refinement, and outward boundary probes. The same workers now also evaluate
independent exact leave-one-day-out selector reruns, fixed-profile omission
replays, selection-aware bootstrap replicates, chronological walk-forward
folds, ATR bar-phase variants, and assumption-stress scenarios.

Each worker attaches once to the file-backed compact replay arrays. High-level
tasks contain only a deterministic identifier and small selector/profile
arguments. Worker tasks execute their own chronological replay serially; the
optimizer never splits one profile's time-ordered state machine across
processes and never creates nested process pools. The parent process still
creates bootstrap schedules, ranks candidates, constructs stable regions,
resolves boundaries, authorizes the recommendation, and writes reports.

Every task result is restored in caller-defined order rather than completion
order. Missing, duplicate, unexpected, or mismatched task identifiers abort the
analysis. Worker count and batching remain execution preferences excluded from
analytical identity. Serial and parallel runs must produce the same profile
results, recommendation, analysis identifier, and report bytes.

Progress reporting no longer leaves the interface at an outward-boundary
message after that batch has completed. Stage 3 emits an explicit completion
transition, and every expensive post-search operation reports its own name,
worker mode, completed count, total count, and pending count. The same pool is
then closed only after recommendation authorization is complete.

This release changes execution scheduling and progress visibility only. It does
not change ATR formulas, the three-stage search space, protective-policy search,
replay state transitions, score policies, bootstrap samples, leave-one-day-out
semantics, walk-forward folds, stable-region rules, boundary rules, or
recommendation authorization.
