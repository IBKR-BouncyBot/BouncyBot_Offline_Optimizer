# BouncyBot Offline Optimizer v2.2.3

## Pyright compatibility and replay-resource hardening

Version 2.2.3 resolves the Pyright 1.1.411 failures reported for the v2.2.2
exact-refinement engine. The reference replay uses ordinary Python ATR lists,
while the compact engine uses NumPy arrays and read-only memory maps. Both now
implement one minimal shared protocol consisting only of `len()` and integer
indexing. The shared consumption boundary performs finite numeric narrowing,
so malformed, Boolean, NaN, or infinite values fail closed.

The platform memory probe no longer assumes that `os.sysconf` is present in
the active operating-system type surface. It discovers the capability at
runtime, validates exact integer results, and otherwise returns no memory
estimate so the existing conservative worker selection remains in force.

The deep audit also hardens the new performance infrastructure. Exact profile
collapsing now requires at least one observed session; an empty evidence set
cannot prove two profiles equivalent. Effective-array scalar memoization is
bounded, preventing a nearly unique multi-million-row ATR stream from creating
an unbounded dictionary for each behavior. Temporary compact-tick, ATR, and
effective-array files are written with explicit close-before-cleanup behavior,
failed partial files are removed, partial multi-map initialization is unwound,
and parent-process mappings are released even when process-pool shutdown
raises.

These corrections do not change valid-input ATR formulas, candidate grids,
replay state transitions, scoring, robustness tests, stable-region selection,
or recommendation authorization. The version bump gives reports a new
content-addressed identity and distinguishes this hardened implementation from
v2.2.2.
