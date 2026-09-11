# BouncyBot Offline Optimizer v2.2.2

## Ruff quality-gate correction

Version 2.2.2 corrects the final Ruff 0.16.0 findings in the v2.2 performance release:

- removes an unused `dataclasses.replace` import from `optimizer/market_replay_optimization.py`;
- uses Ruff's canonical import order in `tests/test_v220_exact_refinement_acceleration.py`; and
- removes an unused `_atr_cache_key` import from `tests/test_v221_deep_bug_audit.py`.

The release contains no intended analytical-methodology change relative to the corrected v2.2 engine.
