# BouncyBot Offline Optimizer v1.5.1 — Ruff SIM103 correction

Version 1.5.1 corrects the single Ruff 0.15.22 `SIM103` diagnostic reported by the Windows `run_all_tests.bat` quality gate for v1.5.0. The diagnostic was in the reproducible-release source-manifest helper.

## Correction

The final suffix-exclusion check in `scripts/create_source_manifest.py` now returns its Boolean membership test directly rather than expressing the same result through an `if` followed by `return True` and `return False`.

The change is behaviorally equivalent. Files with private or generated suffixes remain excluded, and ordinary source files remain included.

## Regression protection

Version 1.5.1 adds tests that verify:

- the helper ends with a direct Boolean return compatible with Ruff `SIM103`;
- sensitive database, recording, certificate, key, log, and bytecode suffixes remain excluded; and
- ordinary source and documentation suffixes remain eligible for the deterministic source manifest.

## Scope

This release does not change ATR reconstruction, the three-stage ATR-window search, bootstrap or leave-one-day-out evidence, Market Replay v2/v3 parsing, strategy replay, scoring, recommendation selection, report contents, source-manifest semantics, or reproducible-build policy.
