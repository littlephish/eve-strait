# Build Eve-Strait for Windows with Nuitka.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -OneFile
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -Clean
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -Isolated
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
# -Clean wipes the persistent build workspace first and starts from nothing.
# Use it if a build is acting strange, or to reclaim the disk space.
#
# -Isolated builds in a one-off %TEMP% directory and deletes it afterwards,
# the way this script always used to. Slower on every single run, but two
# of these can run side by side without one clobbering the other's venv
# mid-compile -- the one real reason to still want it.
#
# Nuitka cannot resolve real paths under a OneDrive folder, so we build in a
# copy elsewhere with its own venv and copy the result back. No app data is
# bundled; the SDE is fetched at runtime into %LOCALAPPDATA%\eve-strait.
#
# WHERE THE TIME WAS GOING (fixed below): every build used to work in a
# %TEMP%\ejp_build_<PID> directory, unique per run, and delete the whole
# thing -- including its freshly created venv -- when it finished. That threw
# away two expensive things on every single build: the venv (a full
# reinstall of PySide6, Nuitka, anthropic, openai and everything under them,
# even though nothing had changed) and Nuitka's own incremental ".build"
# intermediate directory, which is what lets a second build touching only a
# few modules skip recompiling the rest. %TEMP% specifically compounds this:
# Windows Storage Sense and most disk-cleanup tools are expressly allowed to
# purge it, so even a build that *didn't* clean up after itself could still
# lose its cache to something else entirely.
#
# Nuitka's own module-compile cache (~/AppData/Local/Nuitka/Nuitka/Cache) was
# never affected by any of this -- it always lived outside %TEMP% and outside
# this script's reach.
#
# anthropic and openai (and the ~19 packages under them) were removed again
# after this: the in-app chat feature they existed for made direct API calls
# to Claude and OpenAI, which this app no longer does -- Claude and ChatGPT
# now only ever reach this app through the MCP server, which needs neither
# SDK. That dependency tree was also most of what made a cold build slow.

param(
    [switch]$OneFile,
    [switch]$NoZip,
    [switch]$Clean,
    [switch]$Isolated
)

$ErrorActionPreference = "Stop"
$proj = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if ($Isolated) {
    # Unique per run, and deleted at the end: the old default. Two builds
    # sharing one directory will delete each other's venv mid-compile, which
    # shows up as "cannot detect used DLLs" / missing site-packages files --
    # this is the only thing that combination of symptoms should mean.
    $build = Join-Path $env:TEMP "ejp_build_$PID"
    if (Test-Path $build) { Remove-Item -Recurse -Force $build }
} else {
    # Stable and outside %TEMP% on purpose: this is the whole fix. Reused
    # across runs so the venv and Nuitka's own intermediate build directory
    # both survive to the next build instead of being recreated from zero.
    $build = Join-Path $env:LOCALAPPDATA "eve-strait-build"
    if ($Clean -and (Test-Path $build)) { Remove-Item -Recurse -Force $build }
}

$venvExisted = Test-Path (Join-Path $build ".venv\Scripts\python.exe")
New-Item -ItemType Directory -Force $build | Out-Null

# Always refreshed from the real source rather than merged in: a file
# deleted from src/ must not linger here and get bundled into the exe by
# accident just because the workspace itself now persists between runs.
Remove-Item -Recurse -Force (Join-Path $build "src") -ErrorAction SilentlyContinue
Copy-Item (Join-Path $proj "src")            $build -Recurse
Copy-Item (Join-Path $proj "app.py")         $build -Force
Copy-Item (Join-Path $proj "pyproject.toml") $build -Force
Copy-Item (Join-Path $proj "README.md")      $build -Force

# Build a plain path string first: PowerShell splits `--opt=(expr)` into two
# tokens, which Nuitka rejects as extra positional arguments.
$out = Join-Path $build "out"
# The icon lives in the repo, not the build copy.
$icon = Join-Path $proj "dist_assets\win\eve-strait.ico"
# Single source of truth, so a local build never disagrees with
# what the app reports about itself.
$ver = (uv run python (Join-Path $proj "scripts\version.py")).Trim()
$mode = if ($OneFile) { "--onefile" } else { "--standalone" }

Push-Location $build
try {
    $py = Join-Path $build ".venv\Scripts\python.exe"
    if (-not $venvExisted) {
        # uv's venvs point at a minor-version junction (cpython-3.13-... ->
        # cpython-3.13.14-...). Nuitka's path resolver asserts on that
        # junction, so build the venv against the REAL versioned python.exe.
        $realPy = uv run python -c "import os,sys;print(os.path.join(os.path.realpath(sys.base_prefix),'python.exe'))"
        & $realPy -m venv .venv
        & $py -m pip install --quiet --upgrade pip
        Write-Host "New build venv created at $build\.venv" -ForegroundColor DarkGray
    } else {
        Write-Host "Reusing the build venv at $build\.venv (-Clean to start over)" -ForegroundColor DarkGray
    }
    # Cheap when already satisfied, and this is what picks up a version bump
    # or a newly added dependency without needing -Clean for every change.
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
    if ($Isolated) { Remove-Item -Recurse -Force $build -ErrorAction SilentlyContinue }
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

# Only in -Isolated mode: the default workspace is kept on disk deliberately,
# so the next build can reuse the venv and Nuitka's own intermediate files
# instead of starting from nothing. -Clean removes it explicitly when wanted.
if ($Isolated) { Remove-Item -Recurse -Force $build -ErrorAction SilentlyContinue }
$size = "{0:N0} MB" -f ((Get-ChildItem $target -Recurse | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "Built: dist\Eve-Strait\eve-strait.exe  ($size)" -ForegroundColor Green
