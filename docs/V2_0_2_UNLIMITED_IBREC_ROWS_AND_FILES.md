# BouncyBot Offline Optimizer v2.0.2

## Unlimited Market Replay row and recording counts

Version 2.0.2 removes the two arbitrary Market Replay capacity ceilings that
previously limited one analysis to 64 selected `.ibrec` files and 2,000,000
aggregate tick rows.

The desktop and terminal workflows may now accept any number of compatible
recordings and any aggregate row count. The importer continues to validate
every row and every selected recording, preserve deterministic input ordering,
reject duplicate paths and duplicate content, verify format-v2 checksums and
format-v3 integrity chains, and require one provable instrument identity.

The former terminal options `--max-ibrec-files` and `--max-ibrec-rows` have
been removed because they no longer correspond to application behavior. The
Market Replay analysis contract now records both limits as `null`, and its
contract version advances to 16 so reports generated under the unlimited-input
policy cannot collide with reports produced by an older release.

This change removes row-count and file-count ceilings only. The existing
aggregate input-byte limit, format-v2 uncompressed-archive limit, bounded CSV
field/line checks, ZIP-member validation, duplicate-member rejection, and
source-mutation checks remain in place to detect malformed or hostile input.
Available memory, storage bandwidth, and analysis runtime are therefore the
practical limits for very large recording sets.

The SQLite plus `debug_captures` workflow is unchanged and retains its own
capture-level safety limits.
