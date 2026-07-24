# Security and data handling

## Offline operation

BouncyBot Offline Optimizer has no network client dependency and does not connect to IBKR, TWS, IB Gateway, telemetry, update, analytics, or cloud services.

## Source-data protection

- The source SQLite and WAL files are copied with ordinary read-only file access; SQLite never opens the source database.
- SQLite operates only on a private temporary copy and produces a standalone temporary snapshot; the source schema is never migrated.
- SQLite, WAL, and shared-memory file metadata are checked before and after source reads; an unexpected change aborts the run.
- Capture ZIP files are read without extraction.
- ZIP member-count, duplicate-name, expansion, row-count, and per-row limits reduce malformed/archive-bomb and ambiguous-member risk.
- A pre-existing trading-bot lock always blocks analysis.
- The optimizer creates the trading-bot lock only after user confirmation and removes only a lock containing its own PID.
- Generated reports are assembled in a temporary sibling folder and atomically published only after all files and checksums succeed.
- Text exported to CSV is neutralized when it begins with a spreadsheet-formula prefix.
- Reports exclude the account-routing field by design, although order references and cycle identifiers can still be operationally sensitive.

## Market Replay input protection

- The independent `.ibrec` workflow does not acquire the trading-bot lock or access BouncyBot files.
- Version-2 ZIP and version-3 SQLite recordings are copied to a private temporary directory before parsing. A copied hot rollback journal may be recovered only beside that private copy; unexpected WAL/SHM sidecars are rejected.
- Version-3 record hashes, chains, RTH digest, and checkpoint are verified; version-2 member checksums are verified when present.
- The original recording and rollback journal are re-hashed and type-checked after analysis; mutation, journal-state change, disappearance, or replacement by a symlink aborts the run.
- Recording and journal symlinks are rejected before and after analysis.
- Report identity uses content hashes and fixed component roles, not absolute paths or user-controlled filenames.

## Recommended handling

Treat the database, capture files, and generated reports as confidential financial records. Store and transmit them only through trusted channels. Make a backup before moving or deleting any portable trading-bot folder.

Report vulnerabilities privately to the project owner. Do not include live account credentials or unrestricted database copies in a public issue.
