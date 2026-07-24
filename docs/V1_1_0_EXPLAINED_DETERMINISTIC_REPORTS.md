# BouncyBot Offline Optimizer v1.1.0

Version 1.1.0 makes the optimizer's evidence, assumptions, settings history, and output identity explicit.

The GUI result table now explains every value through header and cell tooltips and provides a per-ticker report button. Ticker reports explain how coverage, ATR, BUY replay, SELL replay, candidate ranking, and limitations are derived.

The release distinguishes three different settings concepts:

1. **Exact historical profiles**: complete or partial ATR snapshots stored on individual cycle rows.
2. **Historical median baseline**: a field-wise control summary derived from those actual cycle snapshots. It is not a fitted recommendation and may not equal any one exact profile when settings changed.
3. **Current saved app settings**: the latest applicable `app_settings.strategy` record, shown separately because it cannot be assumed to have applied to older cycles.

When settings changed, every cycle is assigned a stable profile ID and a chronological regime. Counterfactual candidates remain fixed across cycles and are reported both as a pooled result and split by the historical profile originally stored on each cycle. These subgroup comparisons disclose settings-era sensitivity but do not establish causality because settings eras can coincide with different market conditions.

Report output is content-addressed. The analysis ID is derived from SQLite/WAL content, capture relative paths and content, optimizer version, and analysis limits. Wall-clock time and absolute paths do not affect the output. Repeated identical inputs therefore produce the same run ID and byte-identical reports; a conflicting existing directory is rejected rather than overwritten.

File modification times are used only to detect a source changing during one active analysis. They are deliberately excluded from report identity, so touching an otherwise byte-identical database or capture archive does not change the generated files.

The release also fixes future ATR fallback leakage, unknown-window ATR attribution, same-timestamp capture-row loss, unusable-JSONL fallback, symlink escape handling, reserved output-path handling, lock acquisition cleanup, deterministic database ordering, incompatible-schema CLI errors, misleading no-history text, and several numeric edge cases. Expanded regression tests cover these changes.
