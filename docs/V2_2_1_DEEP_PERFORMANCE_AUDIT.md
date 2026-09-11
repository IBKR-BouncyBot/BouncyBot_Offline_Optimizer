# BouncyBot Offline Optimizer v2.2.1

## Deep performance-engine audit

Version 2.2.1 adds fail-closed integrity checks around the v2.2 acceleration path. Evaluation must return exactly one result for each requested nominal profile, in the same deterministic order. Missing, duplicate, reordered, or mismatched results abort analysis rather than allowing an incomplete search to continue.

The release also closes parent-process NumPy memory maps explicitly before deleting the temporary compact replay store. This prevents Windows sharing violations from stale mapped-file handles after serial evaluation or catalog inspection.
