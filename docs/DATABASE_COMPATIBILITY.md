# Database compatibility

The optimizer discovers schema metadata at runtime instead of importing the trading bot's persistence code. `cycles` is the only required table. `orders`, `executions`, `decision_events`, and `app_settings` are optional and improve reconstruction when present. The high-volume general `events` table is inventoried in schema metadata but is not bulk-loaded.

Unknown columns and tables are ignored. Missing later-version fields remain explicitly missing in the exact historical profile. A documented BouncyBot default is used only in the field-wise historical median summary when no cycle contains any valid value for that field; the report identifies every such fallback.

Rows are selected with deterministic tie-break ordering based on the available ticker, cycle number, timestamps, and row IDs. This prevents database insertion-plan differences from changing report bytes.

## Fill reconstruction

Cycle fill fields are preferred. When an average fill or fill timestamp is missing or invalid, matching execution rows can provide a quantity-weighted average and final execution timestamp. Explicit order references are preferred. Side-only execution fallback is used only when BUY/SELL-leg ownership is unambiguous.

BouncyBot can mirror a protective exit into the normal final-SELL history fields so the cycle appears closed. The optimizer detects that representation, counts the protective exit for coverage, and does not double-count the mirrored values as a normal profit-taking SELL sample.

## Settings reconstruction

ATR-related values on each cycle row are treated as the settings snapshot for that cycle. Exact snapshots receive content-derived profile IDs and are grouped into chronological regimes. The latest applicable `app_settings.strategy` JSON value is loaded separately as **current saved app settings**; it is never retroactively assigned to older cycles.

## Source safety

The optimizer does not migrate, index, checkpoint, or write to the production database. It rejects a database or capture directory that resolves outside the selected source folder through a symlink. It copies the database and any WAL into a private temporary directory, opens only that copy, and creates a standalone SQLite snapshot. Temporary files are removed when analysis exits.
