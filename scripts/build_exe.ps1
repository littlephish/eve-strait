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
$build = Join-Path $env:TEMP "ejp_build"

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
    uv sync
    uv run --with nuitka --with zstandard --with ordered-set `
      python -m nuitka `
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
New-Item -ItemType Directory -Force (Join-Path $proj "dist") | Out-Null
Copy-Item $exe (Join-Path $proj "dist") -Force
Write-Host "`nBuilt: dist\eve-jump-planner.exe" -ForegroundColor Green
