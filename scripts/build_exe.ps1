# Build a portable single-file Windows EXE with Nuitka.
#   Usage:  powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
# Output:  dist\eve-jump-planner.exe  (one file; first run downloads map data)
#
# Nuitka cannot resolve real paths under a OneDrive folder, so we build in a
# plain %TEMP% copy with its own venv and copy the EXE back. The first build
# downloads a C compiler (MinGW) and takes several minutes. No app data is
# bundled; the SDE is fetched at runtime into %LOCALAPPDATA%\eve-jump-planner.

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

Push-Location $build
try {
    # uv's venvs point at a minor-version junction (cpython-3.13-... ->
    # cpython-3.13.14-...). Nuitka's path resolver asserts on that junction, so
    # build the venv against the REAL versioned python.exe instead.
    $realPy = uv run python -c "import os,sys;print(os.path.join(os.path.realpath(sys.base_prefix),'python.exe'))"
    Remove-Item -Recurse -Force (Join-Path $build ".venv") -ErrorAction SilentlyContinue
    & $realPy -m venv .venv
    & (Join-Path $build ".venv\Scripts\python.exe") -m pip install --quiet --upgrade pip
    & (Join-Path $build ".venv\Scripts\python.exe") -m pip install --quiet `
        PySide6 requests nuitka zstandard ordered-set
    # Install the project itself so Nuitka can locate the package to include.
    & (Join-Path $build ".venv\Scripts\python.exe") -m pip install --quiet .
    $env:VIRTUAL_ENV = Join-Path $build ".venv"
    & (Join-Path $build ".venv\Scripts\python.exe") -m nuitka `
        --onefile `
        --enable-plugin=pyside6 `
        --include-package=eve_jump_planner `
        --windows-console-mode=disable `
        --assume-yes-for-downloads `
        --company-name="eve-jump-planner" `
        --product-name="EVE Jump Planner" `
        --file-version=0.1.0 `
        --output-dir=$out `
        --output-filename=eve-jump-planner.exe `
        app.py
} finally {
    Pop-Location
}

$exe = Join-Path $out "eve-jump-planner.exe"
if (-not (Test-Path $exe)) {
    Write-Host "`nBUILD FAILED: Nuitka produced no executable." -ForegroundColor Red
    Write-Host "The existing dist\eve-jump-planner.exe (if any) was left untouched."
    exit 1
}
New-Item -ItemType Directory -Force (Join-Path $proj "dist") | Out-Null
# A previously-launched copy would lock the destination file.
Get-Process eve-jump-planner -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1
Copy-Item $exe (Join-Path $proj "dist") -Force
Remove-Item -Recurse -Force $build -ErrorAction SilentlyContinue
Write-Host "`nBuilt: dist\eve-jump-planner.exe" -ForegroundColor Green
