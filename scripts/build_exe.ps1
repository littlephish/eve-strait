# Build Eve-Strait for Windows with Nuitka.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -OneFile
#
# DEFAULT (standalone): dist\Eve-Strait\eve-strait.exe plus its DLLs, and a
# zip beside it. This is the form to ship. A onefile build is a self-extracting
# stub that unpacks to %TEMP% and runs itself, which Microsoft Defender and
# CrowdStrike routinely flag as dropper behaviour; a plain program folder does
# not trip those heuristics.
#
# -OneFile rebuilds the old single .exe. Convenient for personal use, expect
# AV complaints when distributing it.
#
# Nuitka cannot resolve real paths under a OneDrive folder, so we build in a
# plain %TEMP% copy with its own venv and copy the result back. The first build
# downloads a C compiler and takes several minutes. No app data is bundled; the
# SDE is fetched at runtime into %LOCALAPPDATA%\eve-strait.

param(
    [switch]$OneFile,
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"
$proj = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
# Unique per run: two builds sharing one directory will delete each other's
# venv mid-compile, which shows up as "cannot detect used DLLs" / missing
# site-packages files.
$build = Join-Path $env:TEMP "ejp_build_$PID"

if (Test-Path $build) { Remove-Item -Recurse -Force $build }
New-Item -ItemType Directory -Force $build | Out-Null
Copy-Item (Join-Path $proj "src")            $build -Recurse
Copy-Item (Join-Path $proj "app.py")         $build
Copy-Item (Join-Path $proj "pyproject.toml") $build
Copy-Item (Join-Path $proj "README.md")      $build

# Build a plain path string first: PowerShell splits `--opt=(expr)` into two
# tokens, which Nuitka rejects as extra positional arguments.
$out = Join-Path $build "out"
# The icon lives in the repo, not the temp build copy.
$icon = Join-Path $proj "dist_assets\win\eve-strait.ico"
# Single source of truth, so a local build never disagrees with
# what the app reports about itself.
$ver = (uv run python (Join-Path $proj "scripts\version.py")).Trim()
$mode = if ($OneFile) { "--onefile" } else { "--standalone" }

Push-Location $build
try {
    # uv's venvs point at a minor-version junction (cpython-3.13-... ->
    # cpython-3.13.14-...). Nuitka's path resolver asserts on that junction, so
    # build the venv against the REAL versioned python.exe instead.
    $realPy = uv run python -c "import os,sys;print(os.path.join(os.path.realpath(sys.base_prefix),'python.exe'))"
    Remove-Item -Recurse -Force (Join-Path $build ".venv") -ErrorAction SilentlyContinue
    & $realPy -m venv .venv
    $py = Join-Path $build ".venv\Scripts\python.exe"
    & $py -m pip install --quiet --upgrade pip
    & $py -m pip install --quiet PySide6 requests nuitka zstandard ordered-set
    # Install the project itself so Nuitka can locate the package to include.
    & $py -m pip install --quiet .
    $env:VIRTUAL_ENV = Join-Path $build ".venv"
    & $py -m nuitka `
        $mode `
        --enable-plugin=pyside6 `
        --include-package=eve_strait `
        --windows-console-mode=disable `
        --assume-yes-for-downloads `
        --company-name="Eve-Strait" `
        --product-name="Eve-Strait" `
        --file-version=$ver `
        --product-version=$ver `
        --file-description="EVE Online capital jump route planner" `
        --copyright="Eve-Strait" `
        --windows-icon-from-ico=$icon `
        --include-package-data=eve_strait `
        --output-dir=$out `
        --output-filename=eve-strait.exe `
        app.py
} finally {
    Pop-Location
}

New-Item -ItemType Directory -Force (Join-Path $proj "dist") | Out-Null
# A previously-launched copy would lock the destination.
Get-Process eve-strait -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

if ($OneFile) {
    $exe = Join-Path $out "eve-strait.exe"
    if (-not (Test-Path $exe)) {
        Write-Host "`nBUILD FAILED: Nuitka produced no executable." -ForegroundColor Red
        Write-Host "Anything already in dist\ was left untouched."
        exit 1
    }
    Copy-Item $exe (Join-Path $proj "dist") -Force
    Remove-Item -Recurse -Force $build -ErrorAction SilentlyContinue
    Write-Host "`nBuilt: dist\eve-strait.exe (onefile; expect AV false positives)" -ForegroundColor Yellow
    exit 0
}

# Standalone: Nuitka writes <script>.dist next to the output dir.
$distSrc = Get-ChildItem -Path $out -Directory -Filter "*.dist" | Select-Object -First 1
if (-not $distSrc -or -not (Test-Path (Join-Path $distSrc.FullName "eve-strait.exe"))) {
    Write-Host "`nBUILD FAILED: Nuitka produced no program folder." -ForegroundColor Red
    Write-Host "Anything already in dist\ was left untouched."
    exit 1
}

$target = Join-Path $proj "dist\Eve-Strait"
if (Test-Path $target) { Remove-Item -Recurse -Force $target }
Copy-Item $distSrc.FullName $target -Recurse

# update.exe: the helper that swaps the program folder on the next launch.
# Built here rather than committed, so the binary in the release is always
# built from the source in updater/. Not fatal locally: the app falls back to
# the PowerShell path, and a dev build is rarely the one that self-updates.
$cargo = (Get-Command cargo -ErrorAction SilentlyContinue).Source
if (-not $cargo -and (Test-Path "$env:USERPROFILE\.cargo\bin\cargo.exe")) {
    $cargo = "$env:USERPROFILE\.cargo\bin\cargo.exe"   # rustup installs here
}
if ($cargo) {
    & $cargo build --release --manifest-path (Join-Path $proj "updater\Cargo.toml")
    $upd = Join-Path $proj "updater\target\release\update.exe"
    if ($LASTEXITCODE -eq 0 -and (Test-Path $upd)) {
        Copy-Item $upd $target -Force
        Write-Host "Bundled: update.exe" -ForegroundColor Green
    } else {
        Write-Host "WARNING: updater build failed; shipping without update.exe" -ForegroundColor Yellow
    }
} else {
    Write-Host "WARNING: cargo not found; shipping without update.exe." -ForegroundColor Yellow
    Write-Host "         The app will fall back to the PowerShell updater."
}

if (-not $NoZip) {
    $zip = Join-Path $proj "dist\Eve-Strait-$ver-win64.zip"
    if (Test-Path $zip) { Remove-Item -Force $zip }
    Compress-Archive -Path $target -DestinationPath $zip
    Write-Host "`nZipped: dist\Eve-Strait-$ver-win64.zip" -ForegroundColor Green
}

Remove-Item -Recurse -Force $build -ErrorAction SilentlyContinue
$size = "{0:N0} MB" -f ((Get-ChildItem $target -Recurse | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "Built: dist\Eve-Strait\eve-strait.exe  ($size)" -ForegroundColor Green
