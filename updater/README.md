# app-updater (`update.exe`)

A tiny, generic, dependency-free Windows updater for **folder-based** apps
(Nuitka `--standalone`, PyInstaller onedir, etc.). Statically linked std-only
Rust, so it builds to one ~300 KB `update.exe` with **no DLLs** and **no
interpreter**. That matters: it works on locked-down machines where a `.ps1` is
blocked by execution policy or Constrained Language Mode, which is exactly how
the previous PowerShell helper failed silently.

**This folder is shared with
[ore-hold-watcher](https://github.com/littlephish/ore-hold-watcher/tree/main/updater)
and is kept byte-identical on purpose.** Nothing in it is app-specific: the
install path, source folder and exe name all come from argv. Fix a bug here and
port the same change back, rather than letting the two copies drift.

## What it does

```
update.exe <src_dir> <install_dir> <main_exe_name>
```

1. Waits for `<install_dir>\<main_exe_name>` to become writable, which is the
   reliable signal that the app has exited. Polls the lock rather than waiting
   on a PID.
2. Mirrors `<src_dir>` (the already-unpacked new build) over `<install_dir>`.
3. Prunes files the new build no longer ships, but never `unins000.exe`,
   `unins000.dat`, `update-log.txt`, or the `update\` staging folder.
4. Relaunches the app. It **always** relaunches at the end, even on failure, so
   a bad update never leaves the user with nothing.

Progress goes to `<install_dir>\update-log.txt`.

## How Eve-Strait drives it

A program cannot overwrite its own running exe, so the app copies **just this
one file** to a temp folder and runs it from there. That lets it replace the
entire install, including the installed `update.exe`, with no self-replace
problem. See `apply_and_restart()` in `src/eve_strait/update.py`.

## Build

```
cargo build --release --manifest-path updater/Cargo.toml
```

produces `updater/target/release/update.exe`.

`scripts/build_exe.ps1` does this automatically and drops the result into the
program folder; it warns and carries on if Rust is not installed locally, since
the app falls back to the PowerShell path. The release workflow treats a failed
updater build as fatal, because a published build must ship it.
