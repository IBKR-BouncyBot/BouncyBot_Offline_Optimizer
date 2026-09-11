param(
    [switch]$RunTests,
    [switch]$SkipReproducibilityCheck
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = "2.3.1"
$appName = "BouncyBotOfflineOptimizer"
$runtimeDirectory = "BouncyBotOptimizerRuntime"
$requiredPythonVersion = "3.11.9"
$sourceDateEpoch = "1787788800"
$releaseName = "BouncyBot_Offline_Optimizer_${version}_Windows_x64"
$releaseDirectory = Join-Path $root "release"
$releaseRoot = Join-Path $releaseDirectory $releaseName
$releaseZip = Join-Path $releaseDirectory "$releaseName.zip"
$verificationZip = Join-Path $releaseDirectory "$releaseName.verify.zip"
$checksumsPath = Join-Path $releaseDirectory "SHA256SUMS.txt"
$releaseVenv = Join-Path $root ".venv-release"
$bootstrapLock = Join-Path $root "requirements-bootstrap.lock"
$releaseLock = Join-Path $root "requirements-release-win64.lock"
$versionInfoPath = [System.IO.Path]::GetFullPath((Join-Path $root "scripts\windows_version_info.txt"))

function Invoke-Checked {
    param([string]$Description, [scriptblock]$Command)
    Write-Host "==> $Description"
    # Keep native-tool stdout visible without returning it through the caller's
    # success pipeline.  Invoke-PyInstallerPass returns one path; allowing
    # PyInstaller output into that pipeline would turn the assigned path into an
    # array and make subsequent Join-Path calls environment-dependent.
    & $Command | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

function Resolve-PythonLauncher {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $candidates += [PSCustomObject]@{
            Command = "py"
            Arguments = @("-3.11")
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $candidates += [PSCustomObject]@{
            Command = "python"
            Arguments = @()
        }
    }
    foreach ($candidate in $candidates) {
        $candidateArguments = @($candidate.Arguments)
        $actual = @(& $candidate.Command @candidateArguments -c "import platform; print(platform.python_version()); print(platform.architecture()[0])" 2>$null)
        if (
            $LASTEXITCODE -eq 0 -and
            $actual.Count -eq 2 -and
            $actual[0].Trim() -eq $requiredPythonVersion -and
            $actual[1].Trim() -eq "64bit"
        ) {
            return $candidate
        }
    }
    throw "The reproducible release build requires exact Python $requiredPythonVersion x64."
}

function Invoke-PyInstallerPass {
    param([string]$PassName)
    $workPath = Join-Path $root "build\$PassName"
    $distPath = Join-Path $root "dist\$PassName"
    $specPath = Join-Path $root "build\spec-$PassName"
    foreach ($path in @($workPath, $distPath, $specPath)) {
        if (Test-Path $path) { Remove-Item -Recurse -Force $path }
    }
    New-Item -ItemType Directory -Path $specPath -Force | Out-Null
    $arguments = @(
        "--clean",
        "--noconfirm",
        "--noconsole",
        "--onedir",
        "--noupx",
        "--contents-directory", $runtimeDirectory,
        "--collect-data", "tzdata",
        "--name", $appName,
        # PyInstaller resolves relative version-file paths from the generated
        # spec directory.  Pass an absolute source-tree path because each
        # reproducibility pass writes its spec under build\spec-<pass>.
        "--version-file", $versionInfoPath,
        "--workpath", $workPath,
        "--distpath", $distPath,
        "--specpath", $specPath,
        "main.py"
    )
    Invoke-Checked "Build reproducibility pass $PassName" { & $python -m PyInstaller @arguments }
    $result = Join-Path $distPath $appName
    if (!(Test-Path (Join-Path $result "$appName.exe"))) {
        throw "Expected executable was not created in pass $PassName."
    }
    if (!(Test-Path (Join-Path $result $runtimeDirectory))) {
        throw "Expected unique runtime directory was not created in pass $PassName."
    }
    return $result
}

foreach ($required in @($bootstrapLock, $releaseLock, $versionInfoPath)) {
    if (!(Test-Path $required)) { throw "Required release input is missing: $required" }
}

$env:SOURCE_DATE_EPOCH = $sourceDateEpoch
$env:PYTHONHASHSEED = "0"
$env:TZ = "UTC"
$env:PYTHONDONTWRITEBYTECODE = "1"

if (Test-Path $releaseVenv) { Remove-Item -Recurse -Force $releaseVenv }
$launcher = Resolve-PythonLauncher
$launcherArguments = @($launcher.Arguments)
& $launcher.Command @launcherArguments -m venv $releaseVenv
if ($LASTEXITCODE -ne 0) { throw "Release virtual environment creation failed." }
$python = Join-Path $releaseVenv "Scripts\python.exe"

Invoke-Checked "Install pinned bootstrap tooling" {
    & $python -m pip install --require-virtualenv --disable-pip-version-check --no-deps --only-binary=:all: -r $bootstrapLock
}
Invoke-Checked "Install exact Windows release dependency lock" {
    & $python -m pip install --require-virtualenv --disable-pip-version-check --no-deps --only-binary=:all: -r $releaseLock
}
Invoke-Checked "Verify exact release environment" {
    & $python scripts\verify_release_environment.py --strict $bootstrapLock $releaseLock
}
Invoke-Checked "Verify pinned dependency consistency" {
    & $python -m pip check
}

if ($RunTests) {
    Invoke-Checked "Run complete quality gate in the pinned release environment" {
        & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -SkipInstall -PythonPath $python
    }
} else {
    Write-Host "Skipping tests. Use -RunTests for the production gate."
}

foreach ($path in @("build", "dist", $releaseRoot, $releaseZip, $verificationZip, $checksumsPath)) {
    if (Test-Path $path) { Remove-Item -Recurse -Force $path }
}
New-Item -ItemType Directory -Path $releaseDirectory -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $root "build") -Force | Out-Null

$sourceManifestBefore = Join-Path $root "build\SOURCE_MANIFEST.before.json"
$sourceManifestAfter = Join-Path $root "build\SOURCE_MANIFEST.after.json"
Invoke-Checked "Record source tree before compilation" {
    & $python scripts\create_source_manifest.py $root $sourceManifestBefore
}

$distRoot = Invoke-PyInstallerPass "pass1"
$exe = Join-Path $distRoot "$appName.exe"

Write-Host "==> Smoke-test packaged executable and PySide6 runtime"
$smokeProcess = Start-Process -FilePath $exe -ArgumentList "--packaged-smoke-test" -PassThru
if (!$smokeProcess.WaitForExit(30000)) {
    try { $smokeProcess.Kill() } catch { }
    throw "Packaged executable smoke test timed out after 30 seconds."
}
$smokeProcess.WaitForExit()
if ($smokeProcess.ExitCode -ne 0) {
    throw "Packaged executable smoke test failed with exit code $($smokeProcess.ExitCode)."
}

Write-Host "==> Smoke-test packaged spawned worker and NumPy runtime"
$spawnSmokeProcess = Start-Process -FilePath $exe -ArgumentList "--packaged-multiprocessing-smoke-test" -PassThru
if (!$spawnSmokeProcess.WaitForExit(60000)) {
    try { $spawnSmokeProcess.Kill() } catch { }
    throw "Packaged multiprocessing smoke test timed out after 60 seconds."
}
$spawnSmokeProcess.WaitForExit()
if ($spawnSmokeProcess.ExitCode -ne 0) {
    throw "Packaged multiprocessing smoke test failed with exit code $($spawnSmokeProcess.ExitCode)."
}

if (!$SkipReproducibilityCheck) {
    $secondDistRoot = Invoke-PyInstallerPass "pass2"
    Invoke-Checked "Verify two independent PyInstaller outputs are byte-identical" {
        & $python scripts\compare_trees.py $distRoot $secondDistRoot
    }
} else {
    Write-Warning "Skipping the second PyInstaller pass. This build is not independently reproducibility-verified."
}

Invoke-Checked "Record source tree after compilation" {
    & $python scripts\create_source_manifest.py $root $sourceManifestAfter
}
$sourceManifestBeforeHash = (Get-FileHash -Path $sourceManifestBefore -Algorithm SHA256).Hash
$sourceManifestAfterHash = (Get-FileHash -Path $sourceManifestAfter -Algorithm SHA256).Hash
if ($sourceManifestBeforeHash -ne $sourceManifestAfterHash) {
    throw "Source tree changed while the release was being compiled."
}

$appTarget = Join-Path $releaseRoot "APP"
New-Item -ItemType Directory -Path $appTarget -Force | Out-Null
Copy-Item -Path (Join-Path $distRoot "*") -Destination $appTarget -Recurse -Force
foreach ($name in @(
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "SECURITY.md",
    "requirements-bootstrap.lock",
    "requirements-release-win64.lock"
)) {
    Copy-Item -Path (Join-Path $root $name) -Destination $releaseRoot -Force
}
Copy-Item -Path (Join-Path $root "docs") -Destination $releaseRoot -Recurse -Force

$sourceManifest = Join-Path $releaseRoot "SOURCE_MANIFEST.json"
Copy-Item -Path $sourceManifestBefore -Destination $sourceManifest -Force

$provenance = Join-Path $releaseRoot "BUILD_PROVENANCE.json"
Invoke-Checked "Write deterministic build provenance" {
    & $python scripts\write_build_provenance.py $provenance `
        --application-version $version `
        --source-date-epoch $sourceDateEpoch `
        --lock $bootstrapLock `
        --lock $releaseLock `
        --script (Join-Path $root "scripts\build_windows.ps1") `
        --script (Join-Path $root "scripts\compare_trees.py") `
        --script (Join-Path $root "scripts\create_reproducible_zip.py") `
        --script (Join-Path $root "scripts\create_source_manifest.py") `
        --script (Join-Path $root "scripts\verify_release_environment.py") `
        --script (Join-Path $root "scripts\write_build_provenance.py") `
        --input (Join-Path $root "pyproject.toml") `
        --input (Join-Path $root "scripts\windows_version_info.txt") `
        --input $sourceManifest
}

$quickStart = @"
BouncyBot Offline Optimizer $version

Run as a separate portable app:
  APP\BouncyBotOfflineOptimizer.exe

Or place it beside BouncyBot:
  Copy APP\BouncyBotOfflineOptimizer.exe and the complete
  APP\BouncyBotOptimizerRuntime folder into the trading bot's GUI folder.

Do not copy only the executable.

The BouncyBot SQLite & captures tab requires BouncyBot to be closed. It refuses
to run while ibkr_trading_bot.lock exists and requests explicit confirmation
before acquiring that lock for its read-only source pass.

The Market Replay tab independently accepts one or more .ibrec format-v2 ZIP
or format-v3 SQLite recordings for the same instrument. It does not access
BouncyBot data, acquire the bot lock, or connect to IBKR.

The release environment is pinned in requirements-bootstrap.lock and
requirements-release-win64.lock. BUILD_PROVENANCE.json records the exact
interpreter, dependency set, lock hashes, and SOURCE_DATE_EPOCH used.

Open the generated optimizer_reports\...\index.html after completion.
"@
$quickStart = $quickStart.Replace("`r`n", "`n").Trim() + "`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
    (Join-Path $releaseRoot "QUICK_START.txt"),
    $quickStart,
    $utf8NoBom
)

Invoke-Checked "Create deterministic release ZIP" {
    & $python scripts\create_reproducible_zip.py $releaseRoot $releaseZip --root-name $releaseName --epoch $sourceDateEpoch
}
Invoke-Checked "Recreate deterministic release ZIP for byte comparison" {
    & $python scripts\create_reproducible_zip.py $releaseRoot $verificationZip --root-name $releaseName --epoch $sourceDateEpoch
}
$primaryZipHash = (Get-FileHash -Path $releaseZip -Algorithm SHA256).Hash
$verificationZipHash = (Get-FileHash -Path $verificationZip -Algorithm SHA256).Hash
if ($primaryZipHash -ne $verificationZipHash) {
    throw "Deterministic ZIP verification failed: repeated archives differ."
}
Remove-Item -Force $verificationZip

$releaseExe = Join-Path $appTarget "$appName.exe"
$hashLines = @()
foreach ($file in @($releaseExe, $releaseZip, $provenance)) {
    $hash = Get-FileHash -Path $file -Algorithm SHA256
    $relative = $file.Substring($root.Length + 1)
    $hashLines += "$($hash.Hash.ToLowerInvariant())  $relative"
}
Set-Content -Path $checksumsPath -Value $hashLines -Encoding ASCII

Write-Host "Build complete:"
Write-Host "  $releaseRoot"
Write-Host "  $releaseZip"
Write-Host "  $checksumsPath"
