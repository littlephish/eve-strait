"""Self-update from a GitHub release zip.

Only meaningful for the packaged standalone build: the app lives in a program
folder, so an update is "download the new folder, swap it in, relaunch". That
is impossible with a onefile build (the exe is locked and self-extracting),
which is one more reason the shipped artifact is a folder.

Windows locks the running executable, so the swap is handed to a small
throwaway .cmd that waits for us to exit, mirrors the new folder over the old
one, relaunches, and deletes itself.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import __version__, config

GITHUB_REPO = "littlephish/eve-strait"
_API = "https://api.github.com/repos/{repo}/releases/latest"
_EXE_NAME = "eve-strait.exe"


def is_frozen() -> bool:
    """True when running as the compiled standalone build."""
    return bool(getattr(sys, "frozen", False) or globals().get("__compiled__"))


def install_dir() -> Path:
    """Folder holding the running executable (the thing we replace)."""
    return Path(sys.executable).resolve().parent


def _version_tuple(text: str):
    parts = []
    for chunk in str(text).lstrip("vV").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts or [0])


def check(repo: str = GITHUB_REPO, current: str | None = None,
          progress=None) -> dict | None:
    """Return release info when a newer version is published, else None.

    Follows pyfa's model: check and notify, never silently replace anything.
    """
    current = current or __version__
    if progress:
        progress("Checking for updates...")
    try:
        req = urllib.request.Request(
            _API.format(repo=repo),
            headers={"User-Agent": f"eve-strait/{current}",
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None

    tag = data.get("tag_name") or ""
    if not tag or _version_tuple(tag) <= _version_tuple(current):
        return None

    asset = next((a for a in data.get("assets", [])
                  if a.get("name", "").lower().endswith(".zip")
                  and "win" in a.get("name", "").lower()), None)
    return {
        "version": tag.lstrip("vV"),
        "tag": tag,
        "notes": (data.get("body") or "").strip(),
        "page": data.get("html_url", ""),
        "zip_url": asset.get("browser_download_url") if asset else None,
        "zip_size": asset.get("size", 0) if asset else 0,
    }


def staging_dir() -> Path:
    """Where a pending update is unpacked: a subfolder of the install dir.

    Keeping it beside the app (rather than %TEMP%) means the swap is a local
    move on the same volume, and a half-finished download is obvious.
    """
    return install_dir() / "update"


def can_write_install_dir() -> bool:
    """An install under Program Files needs elevation we do not ask for."""
    try:
        probe = install_dir() / ".write-test"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def download(url: str, progress=None, dest_dir: Path | None = None) -> Path:
    """Fetch the release zip, reporting percent complete."""
    base = dest_dir or Path(tempfile.mkdtemp(prefix="eve-strait-update-"))
    base.mkdir(parents=True, exist_ok=True)
    dest = base / "update.zip"
    req = urllib.request.Request(url, headers={"User-Agent": "eve-strait"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while chunk := resp.read(1 << 16):
            fh.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(f"Downloading update... {done * 100 // total}%")
    return dest


def _extract(zip_path: Path, progress=None) -> Path:
    """Unpack the zip and return the folder that holds the executable."""
    if progress:
        progress("Extracting...")
    out = zip_path.parent / "unpacked"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out)
    if (out / _EXE_NAME).exists():
        return out
    for child in out.rglob(_EXE_NAME):      # zip usually nests one folder deep
        return child.parent
    raise RuntimeError(f"{_EXE_NAME} not found in the downloaded archive")


_SWAP_PS1 = r"""# Written by Eve-Strait to finish an update. Do not run by hand.
param([string]$Src, [string]$Dst, [string]$ExeName)

$exe = Join-Path $Dst $ExeName
$log = Join-Path $Dst 'update-log.txt'
function Log($m) {
    "{0} {1}" -f (Get-Date -Format s), $m | Out-File $log -Append -Encoding utf8
}
Log "updater started: '$Src' -> '$Dst'"

# Do NOT wait on a PID. The reliable signal that the app has exited is the
# executable becoming writable again, so poll the lock itself.
$unlocked = $false
for ($i = 0; $i -lt 90; $i++) {
    try {
        $fs = [System.IO.File]::Open($exe, 'Open', 'Write', 'None')
        $fs.Close()
        $unlocked = $true
        Log "exe unlocked after $i attempt(s)"
        break
    } catch {
        Start-Sleep -Milliseconds 1000
    }
}

if (-not $unlocked) {
    Log "gave up waiting for the exe to unlock; install left untouched"
    Start-Process -FilePath $exe
    exit 1
}

# Copy natively rather than shelling out to robocopy: no dependency on an
# external tool and no argument-quoting surprises.
$srcRoot = (Resolve-Path -LiteralPath $Src).Path.TrimEnd('\')
$dstRoot = (Resolve-Path -LiteralPath $Dst).Path.TrimEnd('\')
$copied = 0
try {
    Get-ChildItem -LiteralPath $srcRoot -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($srcRoot.Length).TrimStart('\')
        $target = Join-Path $dstRoot $rel
        $dir = Split-Path $target -Parent
        if (-not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force -ErrorAction Stop
        $copied++
    }
} catch {
    Log "copy failed after $copied file(s): $_"
    Log "relaunching the existing build; the download is kept for a retry"
    Start-Process -FilePath $exe
    exit 1
}
Log "copied $copied file(s)"

# Prune files an older version left behind, but never the staging folder we
# are running from, the Inno Setup uninstaller, or this log.
$fresh = @{}
Get-ChildItem -LiteralPath $srcRoot -Recurse -File | ForEach-Object {
    $fresh[$_.FullName.Substring($srcRoot.Length).TrimStart('\').ToLower()] = $true
}
$protected = @('unins000.exe', 'unins000.dat', 'update-log.txt')
$removed = 0
Get-ChildItem -LiteralPath $dstRoot -Recurse -File |
    Where-Object { -not $_.FullName.StartsWith($srcRoot, 'OrdinalIgnoreCase') } |
    ForEach-Object {
        $rel = $_.FullName.Substring($dstRoot.Length).TrimStart('\')
        if (-not $fresh.ContainsKey($rel.ToLower()) -and
                $protected -notcontains $_.Name) {
            Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
            $removed++
        }
    }
Log "pruned $removed stale file(s)"

Log "starting '$exe'"
Start-Process -FilePath $exe

# Clean up the staging folder from outside it.
Start-Sleep -Milliseconds 500
try {
    Remove-Item -LiteralPath $Src -Recurse -Force -ErrorAction Stop
    Log "staging folder removed"
} catch {
    Log "could not remove staging folder: $_"
}
"""


def apply_and_restart(zip_path: Path, progress=None) -> None:
    """Swap in the downloaded build and relaunch. Does not return on success."""
    if not is_frozen():
        raise RuntimeError("Updating in place only works for the packaged build")
    new_dir = _extract(zip_path, progress)
    target = install_dir()

    script = staging_dir() / "apply_update.ps1"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(_SWAP_PS1, encoding="utf-8")
    if progress:
        progress("Restarting to finish the update...")

    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-File", str(script),
            "-Src", str(new_dir), "-Dst", str(target), "-ExeName", _EXE_NAME]

    DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x8)
    NEWGROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x1000000)
    # Break out of any job object the app was launched in, or the helper dies
    # the moment we exit. Fall back when the job forbids breakaway.
    for flags in (DETACHED | NEWGROUP | BREAKAWAY, DETACHED | NEWGROUP):
        try:
            subprocess.Popen(args, creationflags=flags, close_fds=True)
            break
        except OSError:
            continue
    else:
        raise RuntimeError("Could not launch the update helper")

    os._exit(0)      # skip Qt teardown; the helper is waiting on our exit


# -- preferences ------------------------------------------------------------
def auto_check_enabled() -> bool:
    return bool(config.load_config().get("check_updates", True))


def set_auto_check(enabled: bool) -> None:
    cfg = config.load_config()
    cfg["check_updates"] = bool(enabled)
    config.save_config(cfg)
