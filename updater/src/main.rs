#![cfg_attr(all(target_os = "windows", not(test)), windows_subsystem = "windows")]

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

// Per-file copy attempts before falling back to rename-and-replace. Short on
// purpose: this only has to cover a main app that is a beat slow to exit, and
// a genuinely locked file is never going to come free by waiting -- something
// else is running it.
const COPY_RETRIES: u32 = 5;

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

fn lower_name(p: &Path) -> String {
    p.file_name()
        .map(|n| n.to_string_lossy().to_lowercase())
        .unwrap_or_default()
}

fn top_component(rel: &Path) -> String {
    rel.components()
        .next()
        .map(|c| c.as_os_str().to_string_lossy().to_lowercase())
        .unwrap_or_default()
}

/// Files we parked out of the way on a previous run, waiting for whatever was
/// executing them to exit.
fn is_backup(name: &str) -> bool {
    name.ends_with(".old") || name.contains(".old-")
}

fn backup_path(dest: &Path, stamp: Option<u64>) -> PathBuf {
    let mut name = dest.as_os_str().to_os_string();
    match stamp {
        Some(t) => name.push(format!(".old-{}", t)),
        None => name.push(".old"),
    }
    PathBuf::from(name)
}

enum Outcome {
    Copied,
    Renamed,
    Failed,
}

/// Copy `src` over `dest`, shoving a locked `dest` out of the way if needed.
///
/// Windows will not let you overwrite a file that is mapped as a running
/// image, but it will let you *rename* one on the same volume: the running
/// process keeps executing happily from the renamed file. That is the only
/// way to update an install whose exe is still in use, and it is the normal
/// case here -- the app's own MCP server runs from the installed exe and is
/// started by a different program (Claude Desktop) that this updater neither
/// owns nor should be killing.
///
/// The parked file is deleted by the sweep on a later run, once nothing is
/// executing it. A process still running from a renamed file keeps the code
/// it already mapped; it does not see the new build until it restarts, which
/// is the honest tradeoff for not having to kill it.
fn replace_file(src: &Path, dest: &Path) -> Outcome {
    // Fast path, and a few retries for a main app that is still winding down.
    for i in 0..COPY_RETRIES {
        if fs::copy(src, dest).is_ok() {
            return Outcome::Copied;
        }
        if i + 1 < COPY_RETRIES {
            sleep(Duration::from_millis(500));
        }
    }
    if !dest.exists() {
        return Outcome::Failed; // not a lock: bad path, full disk, read-only
    }

    // Locked. Park it and take its place.
    //
    // The plain .old name is reused only when nothing is executing whatever is
    // already parked there. Checking first is not paranoia: a file opened with
    // FILE_SHARE_DELETE (which is how Windows holds a running image) can be
    // deleted successfully, and the name is unlinked immediately, so a blind
    // remove_file would silently recycle the name out from under a process
    // still running that build. The writability probe is the same one used to
    // tell whether the app has exited.
    let plain = backup_path(dest, None);
    if plain.exists() && is_unlocked(&plain) {
        let _ = fs::remove_file(&plain);
    }
    let parked = (!plain.exists() && fs::rename(dest, &plain).is_ok())
        || fs::rename(dest, backup_path(dest, Some(unix_now()))).is_ok();
    if parked && fs::copy(src, dest).is_ok() {
        Outcome::Renamed
    } else {
        Outcome::Failed
    }
}

/// Delete parked files left by earlier runs. Ones still in use simply stay
/// until the run after that; nothing here is load-bearing.
fn sweep_backups(install: &Path) -> usize {
    let mut removed = 0usize;
    for (abs, rel) in files(install) {
        if top_component(&rel) == "update" {
            continue;
        }
        if is_backup(&lower_name(&abs)) && fs::remove_file(&abs).is_ok() {
            removed += 1;
        }
    }
    removed
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

    let swept = sweep_backups(&install);
    if swept > 0 {
        log(&logf, &format!("swept {} parked file(s) from a previous run", swept));
    }

    // Wait for the app to exit (exe becomes writable), up to ~60s. Not fatal
    // any more: it used to give up here and relaunch the OLD build, which is
    // silent and looks exactly like a broken update. The exe stays locked for
    // as long as ANY process is running it -- including the --mcp server
    // started by Claude Desktop, which never exits just because the GUI did --
    // so waiting longer would not have helped. Park-and-replace does.
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
        log(
            &logf,
            "exe still locked after 60s (another process is running it);              replacing by rename instead",
        );
    }

    // Mirror src -> install, retrying files still momentarily locked by a slow
    // exit. Even on a copy failure we press on and relaunch at the end.
    let mut copied = 0usize;
    let mut renamed = 0usize;
    let mut failed = 0usize;
    for (abs, rel) in files(&src) {
        let dest = install.join(&rel);
        if let Some(parent) = dest.parent() {
            let _ = fs::create_dir_all(parent);
        }
        match replace_file(&abs, &dest) {
            Outcome::Copied => copied += 1,
            Outcome::Renamed => {
                copied += 1;
                renamed += 1;
            }
            Outcome::Failed => {
                failed += 1;
                log(&logf, &format!("WARN could not copy {}", rel.display()));
            }
        }
    }
    log(&logf, &format!("copied/updated {} file(s)", copied));
    if renamed > 0 {
        log(
            &logf,
            &format!("{} file(s) were in use and were replaced by rename;                       the parked copies are deleted on the next update", renamed),
        );
    }
    if failed > 0 {
        log(&logf, &format!("WARN {} file(s) could not be replaced at all", failed));
    }

    // Prune files the new build no longer ships (protecting installer + log,
    // and leaving any old 'update' staging leftovers alone).
    let fresh: HashSet<String> = files(&src).iter().map(|(_, r)| rel_key(r)).collect();
    let mut removed = 0usize;
    for (abs, rel) in files(&install) {
        let name = lower_name(&abs);
        if PROTECTED.contains(&name.as_str()) {
            continue;
        }
        // Parked files are not stale files the new build dropped; the sweep at
        // the top of the next run owns them.
        if is_backup(&name) {
            continue;
        }
        if top_component(&rel) == "update" {
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::windows::fs::OpenOptionsExt;

    // Share flags describing exactly how Windows holds a *running* executable:
    // other processes may read it, and may rename or delete it, but may NOT
    // write to it. Emulating the real thing matters here -- a lock that also
    // denied rename (FILE_SHARE_READ alone) would make these tests pass for
    // the wrong reason, or fail for one.
    const FILE_SHARE_READ: u32 = 0x1;
    const FILE_SHARE_DELETE: u32 = 0x4;

    fn scratch(tag: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let d = std::env::temp_dir().join(format!("updater-test-{}-{}", tag, nanos));
        fs::create_dir_all(&d).unwrap();
        d
    }

    fn write(path: &Path, body: &str) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, body).unwrap();
    }

    /// Hold `path` open the way a running process holds its own image.
    fn lock_as_running_image(path: &Path) -> fs::File {
        fs::OpenOptions::new()
            .read(true)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_DELETE)
            .open(path)
            .expect("could not open the file to lock it")
    }

    #[test]
    fn replaces_a_file_locked_like_a_running_exe() {
        let dir = scratch("locked");
        let src = dir.join("new.exe");
        let dest = dir.join("app.exe");
        write(&src, "new build");
        write(&dest, "old build");

        let _held = lock_as_running_image(&dest);

        // Sanity: the lock must be the real thing, or this test proves nothing.
        assert!(
            fs::copy(&src, &dest).is_err(),
            "the emulated lock did not block a plain overwrite"
        );

        match replace_file(&src, &dest) {
            Outcome::Renamed => {}
            Outcome::Copied => panic!("expected the rename path, not a plain copy"),
            Outcome::Failed => panic!("a locked file must still be replaced"),
        }

        assert_eq!(fs::read_to_string(&dest).unwrap(), "new build");
        assert_eq!(
            fs::read_to_string(dir.join("app.exe.old")).unwrap(),
            "old build",
            "the in-use build must be parked, not destroyed"
        );

        drop(_held);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn plain_copy_when_nothing_holds_the_file() {
        let dir = scratch("free");
        let src = dir.join("new.exe");
        let dest = dir.join("app.exe");
        write(&src, "new build");
        write(&dest, "old build");

        match replace_file(&src, &dest) {
            Outcome::Copied => {}
            _ => panic!("an unlocked file should never need parking"),
        }
        assert_eq!(fs::read_to_string(&dest).unwrap(), "new build");
        assert!(
            !dir.join("app.exe.old").exists(),
            "no parked file should be left behind when a copy works"
        );

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_second_update_parks_again_while_the_first_park_is_still_in_use() {
        let dir = scratch("twice");
        let src = dir.join("new.exe");
        let dest = dir.join("app.exe");
        write(&src, "build 2");
        write(&dest, "build 1");

        let _first = lock_as_running_image(&dest);
        assert!(matches!(replace_file(&src, &dest), Outcome::Renamed));

        // The parked copy is still being executed, so it cannot be deleted;
        // the next update must not fail or clobber it.
        let _still_running = lock_as_running_image(&dir.join("app.exe.old"));
        write(&src, "build 3");
        let _second = lock_as_running_image(&dest);
        assert!(matches!(replace_file(&src, &dest), Outcome::Renamed));

        assert_eq!(fs::read_to_string(&dest).unwrap(), "build 3");
        assert_eq!(
            fs::read_to_string(dir.join("app.exe.old")).unwrap(),
            "build 1",
            "the still-running park must survive untouched"
        );
        let stamped: Vec<_> = files(&dir)
            .into_iter()
            .filter(|(abs, _)| lower_name(abs).contains(".old-"))
            .collect();
        assert_eq!(stamped.len(), 1, "build 2 should be parked under a stamped name");

        drop(_first);
        drop(_still_running);
        drop(_second);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn sweep_clears_parked_files_and_leaves_everything_else() {
        let dir = scratch("sweep");
        write(&dir.join("app.exe"), "current");
        write(&dir.join("app.exe.old"), "parked");
        write(&dir.join("qt.dll.old-1787363793"), "parked, stamped");
        write(&dir.join("update/unpacked/thing.old"), "staging, not ours");

        assert_eq!(sweep_backups(&dir), 2);
        assert!(dir.join("app.exe").exists(), "the live build must survive");
        assert!(!dir.join("app.exe.old").exists());
        assert!(!dir.join("qt.dll.old-1787363793").exists());
        assert!(
            dir.join("update/unpacked/thing.old").exists(),
            "the staging folder is not the sweep's business"
        );

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn sweep_cannot_delete_a_park_that_is_still_running() {
        let dir = scratch("busy");
        write(&dir.join("app.exe.old"), "still executing");
        let _held = lock_as_running_image(&dir.join("app.exe.old"));

        // Deleting is allowed by FILE_SHARE_DELETE, but Windows only unlinks
        // it once the last handle closes, so it stays visible meanwhile. All
        // the sweep owes us is that it does not fall over.
        let _ = sweep_backups(&dir);

        drop(_held);
        let _ = fs::remove_dir_all(&dir);
    }
}
