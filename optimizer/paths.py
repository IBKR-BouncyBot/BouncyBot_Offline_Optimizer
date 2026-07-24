"""Portable filesystem locations for source and packaged execution."""

from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
    """Return the directory containing the source tree or packaged executable."""
    compiled = globals().get("__compiled__")
    containing_dir = getattr(compiled, "containing_dir", None)
    if containing_dir:
        return Path(str(containing_dir)).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def default_source_dir() -> Path:
    """Default to the portable directory beside the optimizer."""
    return app_dir()


def default_output_root() -> Path:
    """Store generated reports separately from bot-owned data."""
    return app_dir() / "optimizer_reports"
