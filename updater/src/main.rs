#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

//! Generic in-place updater for folder-based Windows apps
//! (Ore Hold Watcher, Eve-Strait, ...).
//!
//! Dependency-free: std only, statically linked (see .cargo/config.toml), a
//! single ~300 KB `update.exe` with no DLLs. The parent app copies THIS exe to
//! a temp folder and runs it there, so it can overwrite the whole install -
//! including the installed `update.exe` - without the self-replace problem, and
//! without depending on PowerShell/cmd or any system script policy (the reason
//! the old PowerShell helper silently failed on locked-down machines).
//!
//! The binary is app-agnostic; everything comes from argv, so the same exe is
//! reused across apps:
//!
//!     update.exe <src_dir> <install_dir> <main_exe_name>
//!
//! where <src_dir> is the already-unpacked new program folder. It waits for the
//! app to exit (its exe becomes writable), mirrors the new folder over the
//! install dir (pruning files an old version dropped, but protecting the Inno
//! uninstaller and this log), then relaunches the app. It ALWAYS relaunches at
//! the end, even on failure, so the app never fails to come back up.

use std::collections::HashSet;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread::sleep;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

// Never overwritten or pruned: the Inno Setup uninstaller and our own log.
const PROTECTED: [&str; 3] = ["unins000.exe", "unins000.dat", "update-log.txt"];

fn unix_now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn log(logf: &Path, msg: &str) {
    if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(logf) {
        let _ = writeln!(f, "[{}] {}", unix_now(), msg);
    }
}

/// (absolute, path-relative-to-root) for every file under `root`.
fn files(root: &Path) -> Vec<(PathBuf, PathBuf)> {
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        if let Ok(rd) = fs::read_dir(&dir) {
            for entry in rd.flatten() {
                let p = entry.path();
                if p.is_dir() {
                    stack.push(p);
                } else if p.is_file() {
                    if let Ok(rel) = p.strip_prefix(root) {
                        out.push((p.clone(), rel.to_path_buf()));
                    }
                }
            }
        }
    }
    out
}

fn rel_key(rel: &Path) -> String {
    rel.to_string_lossy().replace('\\', "/").to_lowercase()
}

/// A running image is locked against write; once the app exits, opening it for
/// write succeeds. write(true) without truncate does not modify the file.
fn is_unlocked(exe: &Path) -> bool {
    fs::OpenOptions::new().write(true).open(exe).is_ok()
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 4 {
        return;
    }
    let src = PathBuf::from(&args[1]);
    let install = PathBuf::from(&args[2]);
    let exe_name = &args[3];
    let exe = install.join(exe_name);
    let logf = install.join("update-log.txt");

    log(
        &logf,
        &format!("updater started: {} -> {}", src.display(), install.display()),
    );

    // Wait for the app to exit (exe becomes writable), up to ~60s.
    let mut unlocked = false;
    for i in 0..120 {
        if is_unlocked(&exe) {
            log(&logf, &format!("exe unlocked after {} attempt(s)", i));
            unlocked = true;
            break;
        }
        sleep(Duration::from_millis(500));
    }
    if !unlocked {
        log(&logf, "exe never unlocked; relaunching existing build");
        let _ = Command::new(&exe).spawn();
        return;
    }

    // Mirror src -> install, retrying files still momentarily locked by a slow
    // exit. Even on a copy failure we press on and relaunch at the end.
    let mut copied = 0usize;
    for (abs, rel) in files(&src) {
        let dest = install.join(&rel);
        if let Some(parent) = dest.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let mut ok = false;
        for _ in 0..60 {
            if fs::copy(&abs, &dest).is_ok() {
                ok = true;
                break;
            }
            sleep(Duration::from_secs(1));
        }
        if ok {
            copied += 1;
        } else {
            log(&logf, &format!("WARN could not copy {}", rel.display()));
        }
    }
    log(&logf, &format!("copied/updated {} file(s)", copied));

    // Prune files the new build no longer ships (protecting installer + log,
    // and leaving any old 'update' staging leftovers alone).
    let fresh: HashSet<String> = files(&src).iter().map(|(_, r)| rel_key(r)).collect();
    let mut removed = 0usize;
    for (abs, rel) in files(&install) {
        let name = abs
            .file_name()
            .map(|n| n.to_string_lossy().to_lowercase())
            .unwrap_or_default();
        if PROTECTED.contains(&name.as_str()) {
            continue;
        }
        let top = rel
            .components()
            .next()
            .map(|c| c.as_os_str().to_string_lossy().to_lowercase())
            .unwrap_or_default();
        if top == "update" {
            continue;
        }
        if !fresh.contains(&rel_key(&rel)) && fs::remove_file(&abs).is_ok() {
            removed += 1;
        }
    }
    log(&logf, &format!("pruned {} stale file(s)", removed));

    log(&logf, &format!("starting {}", exe.display()));
    let _ = Command::new(&exe).spawn();

    // Best-effort cleanup of the unpacked folder from outside it.
    if let Some(parent) = src.parent() {
        let _ = fs::remove_dir_all(parent);
    }
    log(&logf, "done");
}
