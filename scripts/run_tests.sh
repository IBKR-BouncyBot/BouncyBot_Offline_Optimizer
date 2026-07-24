#!/usr/bin/env sh
set -eu
PYTHON_BIN="${PYTHON_BIN:-python3}"
export PYTHONWARNINGS="error::ResourceWarning"

# Fail quickly on source-quality defects before running the longer coverage suite.
"$PYTHON_BIN" -m compileall -q optimizer scripts tests main.py
"$PYTHON_BIN" -m ruff check optimizer scripts tests main.py
"$PYTHON_BIN" -m pyright --pythonpath "$PYTHON_BIN"

"$PYTHON_BIN" -m coverage erase
"$PYTHON_BIN" -m coverage run --branch -m pytest
"$PYTHON_BIN" -m coverage report
"$PYTHON_BIN" -m coverage json -o coverage.json
"$PYTHON_BIN" -m coverage xml -o coverage.xml
