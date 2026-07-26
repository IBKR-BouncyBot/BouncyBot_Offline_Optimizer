# BouncyBot Offline Optimizer v1.8.1

## Ruff quality-gate correction

Version 1.8.1 corrects the five Ruff diagnostics reported by the native Windows quality gate for v1.8.0.

- Normalized the `PySide6.QtWidgets` import ordering in `optimizer/gui.py`.
- Removed the unused `MarketReplaySessionQuality` import from `optimizer/market_replay.py`.
- Removed the unused `selected_window_set` assignment from the leave-one-day-out search path.
- Normalized the import-section spacing in `scripts/create_reproducible_zip.py`.
- Normalized the private-name import ordering in `tests/test_v180_continuous_replay_and_calibration.py`.

These corrections do not change ATR reconstruction, continuous overnight replay, SQLite execution calibration, staged search, scoring, bootstrap, leave-one-day-out analysis, stable-region selection, report contents other than release identity, or recommendation behavior.

The normal and reproducible Windows dependency sets now both pin Ruff 0.16.0, the version that reported these diagnostics, so future v1.8.1 validation cannot silently switch to a different Ruff ruleset.

A regression test protects the exact source conditions involved in the reported diagnostics and the Ruff pin.
