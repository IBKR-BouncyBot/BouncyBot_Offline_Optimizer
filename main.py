"""BouncyBot Offline Optimizer executable entry point."""

from __future__ import annotations

import multiprocessing

from optimizer.cli import main

if __name__ == "__main__":
    # Required by spawned process pools in frozen Windows executables.  It is
    # harmless for source execution and preserves PyInstaller's child-process
    # bootstrap contract.
    multiprocessing.freeze_support()
    raise SystemExit(main())
