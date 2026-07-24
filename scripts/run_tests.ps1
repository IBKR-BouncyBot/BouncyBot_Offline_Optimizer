param(
    [switch]$SkipInstall,
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if ($PythonPath) {
    $python = [System.IO.Path]::GetFullPath($PythonPath)
    if (!(Test-Path $python)) { throw "Requested Python interpreter does not exist: $python" }
} else {
    if (!(Test-Path ".venv\Scripts\python.exe")) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3.11 -m venv .venv
        } else {
            & python -m venv .venv
        }
        if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }
    }
    $python = Join-Path $root ".venv\Scripts\python.exe"
}
if (!$SkipInstall) {
    & $python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
}

$env:PYTHONWARNINGS = "error::ResourceWarning"

# Fail quickly on source-quality defects before running the longer coverage suite.
& $python -m compileall -q optimizer scripts tests main.py
if ($LASTEXITCODE -ne 0) { throw "Python compilation failed." }
& $python -m ruff check optimizer scripts tests main.py
if ($LASTEXITCODE -ne 0) { throw "Ruff failed." }
& $python -m pyright --pythonpath $python
if ($LASTEXITCODE -ne 0) { throw "Pyright failed." }

& $python -m coverage erase
& $python -m coverage run --branch -m pytest
if ($LASTEXITCODE -ne 0) { throw "pytest failed." }
& $python -m coverage report
if ($LASTEXITCODE -ne 0) { throw "coverage gate failed." }
& $python -m coverage json -o coverage.json
if ($LASTEXITCODE -ne 0) { throw "coverage JSON generation failed." }
& $python -m coverage xml -o coverage.xml
if ($LASTEXITCODE -ne 0) { throw "coverage XML generation failed." }

Write-Host "All tests and quality checks passed."
