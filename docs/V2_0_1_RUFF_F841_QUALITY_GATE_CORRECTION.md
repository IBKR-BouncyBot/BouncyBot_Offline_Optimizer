# BouncyBot Offline Optimizer v2.0.1

## Ruff F841 quality-gate correction

Version 2.0.1 removes an unused `selected_windows` local reported by Ruff 0.16.0 in the top-level Market Replay analysis. The policy-specific ATR windows are already retained in `policy_window_sets`, which is the structure consumed by the Stage 3 profile generator. The removed aggregate list did not affect any search, replay, scoring, validation, or report result.

The now-redundant `selected_window_set` collection in that same function was removed as well. The equivalent aggregation inside `_run_bounded_selector` remains because its sorted result is part of the selector evidence returned to leave-one-day-out, bootstrap, and walk-forward validation.

An AST regression test verifies that the top-level analysis does not reintroduce the unused assignments while the bounded selector continues to retain and return its selected-window evidence. The source archive is also rebuilt from release-relevant files only, excluding pytest caches and compiled bytecode.
