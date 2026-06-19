//! Launcher-side helper to bring up the detached `vct-hub` binary.
//!
//! v0.2.21 Step 6. Replaces the in-process `hub::server::start_hub_
//! server` call that Step 4 stubbed out. The launcher remains
//! responsible for ENSURING the hub is running (so users opening the
//! GUI see the hub immediately even on a clean machine where install.
//! py hasn't yet registered boot-time auto-start), but it does NOT
//! own the hub's lifecycle past spawning — `vct-hub --stop` from
//! launcher quit would defeat the "hub outlives launcher GUI" goal.
//!
//! Discovery chain (must match the SessionStart hook in
//! `templates/hooks/session-start-ensure-hub.sh`):
//!   1. `$VCT_HUB_BIN` env override (highest priority — dev builds).
//!   2. `<dir of this launcher binary>/vct-hub` (the INSTALL-FOLDER copy
//!      — the hub that shipped WITH this exact launcher; populated by
//!      `build-bundled-launcher.sh`), then the arch-less fallback one dir
//!      up. **Preferred** over PATH/`~/.vct/bin` (v0.2.63): the sibling
//!      copy is guaranteed to match this launcher's version, whereas PATH
//!      or `~/.vct/bin` can point at a stale dev build or an older
//!      install. user request 2026-06-19: "we should always use the installed
//!      copy from the launcher's install folder."
//!   3. First `vct-hub` on PATH.
//!   4. `$HOME/.vct/bin/vct-hub` (install.py default install location).
//!
//! Invocation: `vct-hub --start-if-not-running`. The CLI returns 0
//! whether the hub started fresh OR was already running; both are
//! success states for us — BUT "already running" is not enough on its
//! own: see `ensure_hub_running`'s v0.2.63 identity-aware swap, which
//! replaces a hub running from a DIFFERENT binary than the install-folder
//! copy (the lockfile + `--start-if-not-running` only check liveness,
//! never binary identity).
//!
//! Soft-fail throughout: a missing binary, a failed spawn, or a non-
//! zero exit are all just `eprintln!` warnings — never block the
//! launcher GUI from coming up. The hub being unavailable degrades
//! the launcher to "hub-unavailable mode" (resolver falls back to
//! env vars; supervisor doesn't run) but the GUI still works.

use std::path::PathBuf;
use std::process::{Command, Stdio};

/// Find the vct-hub binary on disk, returning the first hit from the
/// documented discovery chain. Returns `None` if no candidate exists.
pub fn find_hub_binary() -> Option<PathBuf> {
    // 1. Explicit override.
    if let Ok(p) = std::env::var("VCT_HUB_BIN") {
        let path = PathBuf::from(&p);
        if is_executable(&path) {
            return Some(path);
        }
        eprintln!(
            "[vct] VCT_HUB_BIN set to {} but not executable; falling through",
            p
        );
    }

    // NOTE on hub freshness during an update (v0.2.55): the hub is kept
    // fresh by TWO mechanisms —
    //   (1) install.py Step 8c starts the freshly-deployed dist hub by its
    //       ABSOLUTE path (`[<dist>/vct-hub, --start-if-not-running]`,
    //       install.py ~21468) before control returns to the launcher; and
    //   (2) `WaitForBinaryRefresh` (installer.rs) now gates the post-update
    //       restart on the on-disk hub METADATA version (read from the dist
    //       sidecar, not from PATH), so the restart can't proceed into a
    //       stale-hub state.
    // An earlier v0.2.55 draft added an in-process dist-preference branch
    // here gated on `VCT_AUTO_RESTART_LAUNCHER=1`; that env var is set only
    // on the install.py CHILD (installer.rs cmd.env), never on the launcher
    // process that runs find_hub_binary(), so the branch was dead in
    // production. Dropped. v0.2.63 makes the dist-sibling preference the
    // steady-state default below (NOT env-gated) — see step 2.

    // 2. INSTALL-FOLDER copy (v0.2.63): the `vct-hub` sibling of THIS
    // launcher binary, then the arch-less fallback one dir up. Preferred
    // over PATH/`~/.vct/bin` because it is guaranteed to be the hub that
    // shipped with this exact launcher build. A stale `vct-hub` on PATH
    // (a leftover dev build, an old global install) must NOT win over the
    // copy install.py just deployed next to the launcher. Honours the
    // `VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY` test-isolation gate.
    if let Some(sibling) = find_hub_dist_sibling() {
        return Some(sibling);
    }

    // 3. PATH lookup.
    if let Some(on_path) = find_on_path("vct-hub") {
        return Some(on_path);
    }

    // 4. User-install location.
    if let Some(home) = std::env::var_os("HOME").map(PathBuf::from) {
        let candidate = home.join(".vct").join("bin").join(hub_binary_name());
        if is_executable(&candidate) {
            return Some(candidate);
        }
    }
    // Windows: USERPROFILE rather than HOME.
    #[cfg(windows)]
    if let Some(profile) = std::env::var_os("USERPROFILE").map(PathBuf::from) {
        let candidate = profile.join(".vct").join("bin").join(hub_binary_name());
        if is_executable(&candidate) {
            return Some(candidate);
        }
    }

    None
}

/// True if `a` and `b` resolve to the same on-disk executable. Canonicalizes
/// both (resolving symlinks); on unix also treats an equal `(dev, inode)` pair
/// as identical (covers hardlinks and canonicalize-failed paths). Falls back to
/// raw path equality. Conservative: when in doubt it returns `false`, so the
/// only consequence of a mis-compare is leaving a hub alone (never a false
/// kill — see `ensure_hub_running`).
fn same_binary(a: &std::path::Path, b: &std::path::Path) -> bool {
    if let (Ok(ca), Ok(cb)) = (std::fs::canonicalize(a), std::fs::canonicalize(b)) {
        if ca == cb {
            return true;
        }
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if let (Ok(ma), Ok(mb)) = (std::fs::metadata(a), std::fs::metadata(b)) {
            if ma.dev() == mb.dev() && ma.ino() == mb.ino() {
                return true;
            }
        }
    }
    a == b
}

/// Decide whether the hub running as `pid` is STALE relative to the
/// install-folder copy at `install_copy` — i.e. the launcher should stop it and
/// start `install_copy` instead. A running hub is stale two ways:
///   * **different binary by location** — a borrowed dev build, an old global
///     install, a hub from another checkout. Caught cross-OS by path identity.
///   * **older build at the SAME path** — a POSIX in-place update rewrote
///     `<dist>/vct-hub` to a NEW inode, but the old process is still executing
///     the previous (now-unlinked) inode. This is the "restart on the new
///     binary if an older binary is running" case (user request 2026-06-19). Caught
///     on Linux by comparing the inode `/proc/<pid>/exe` actually resolves to
///     (valid even after the on-disk file was replaced) against the on-disk
///     install copy's inode.
///
/// Strictly conservative: returns `false` whenever staleness cannot be
/// POSITIVELY confirmed, so a hub we cannot identify is never killed.
fn running_hub_is_stale(
    pid: u32,
    running_exe: Option<&std::path::Path>,
    install_copy: &std::path::Path,
) -> bool {
    // Linux: the authoritative signal. `/proc/<pid>/exe`, when stat'd, yields
    // the inode the process is ACTUALLY running (the kernel keeps it valid even
    // if the file was replaced). Comparing it to the on-disk install copy's
    // inode catches BOTH staleness modes at once — different inode => stale;
    // identical inode => definitively the same running file => fresh.
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::fs::MetadataExt;
        let proc_exe = format!("/proc/{}/exe", pid);
        if let (Ok(run), Ok(disk)) =
            (std::fs::metadata(&proc_exe), std::fs::metadata(install_copy))
        {
            return run.dev() != disk.dev() || run.ino() != disk.ino();
        }
        // metadata failed (pid gone, restricted /proc) — fall through to the
        // cross-OS path check below.
    }

    // Cross-OS fallback: a running exe whose PATH is not the install copy is
    // stale. On Windows a running `.exe` cannot be replaced in place (the
    // updater renames-aside + MoveFileEx swaps only after the process exits),
    // so a path check is sufficient there. macOS same-path-replaced is a known
    // gap — the GUI update flow stops the hub before swapping, so it rarely
    // arises. `None` (exe unresolved) => not provably stale => leave it.
    match running_exe {
        Some(p) => !same_binary(p, install_copy),
        None => false,
    }
}

/// v0.2.55: resolve the in-tree dist `vct-hub` sibling relative to the
/// running launcher binary (the layout `build-bundled-launcher.sh`
/// produces). Returns the first executable of:
///   4. `<dir of current_exe>/vct-hub` (sibling)
///   5. `<dir of current_exe>/../vct-hub` (arch-less fallback)
/// or `None`.
///
/// v0.2.53 test-isolation gate is honoured here: when running under
/// `cargo test`, `current_exe()` points into `target/debug/deps/` whose
/// grandparent is `target/debug/` — and sibling cargo invocations leave a
/// real `vct-hub` binary there, making deterministic "no hub anywhere"
/// tests impossible. Setting `VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY=1`
/// makes this return `None`; production never sets it so it's a no-op
/// there.
fn find_hub_dist_sibling() -> Option<PathBuf> {
    if std::env::var_os("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY").is_some() {
        return None;
    }
    let exe = std::env::current_exe().ok()?;
    let parent = exe.parent()?;
    // Sibling layout: same dir as the launcher binary contains vct-hub too.
    let sibling = parent.join(hub_binary_name());
    if is_executable(&sibling) {
        return Some(sibling);
    }
    // Arch-less fallback one dir up (some packaging layouts).
    if let Some(grandparent) = parent.parent() {
        let fallback = grandparent.join(hub_binary_name());
        if is_executable(&fallback) {
            return Some(fallback);
        }
    }
    None
}

/// `vct-hub` on POSIX, `vct-hub.exe` on Windows.
fn hub_binary_name() -> &'static str {
    if cfg!(windows) {
        "vct-hub.exe"
    } else {
        "vct-hub"
    }
}

/// Is `path` an executable regular file? On Unix we also check the
/// owner-execute bit; on Windows we only check existence + is_file
/// (file association determines runnability).
fn is_executable(path: &std::path::Path) -> bool {
    let Ok(meta) = std::fs::metadata(path) else {
        return false;
    };
    if !meta.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        meta.permissions().mode() & 0o111 != 0
    }
    #[cfg(not(unix))]
    {
        true
    }
}

/// Walk `$PATH` looking for `name` (with `.exe` suffix on Windows).
fn find_on_path(name: &str) -> Option<PathBuf> {
    let Some(path_env) = std::env::var_os("PATH") else {
        return None;
    };
    let needle = if cfg!(windows) && !name.ends_with(".exe") {
        format!("{}.exe", name)
    } else {
        name.to_string()
    };
    for dir in std::env::split_paths(&path_env) {
        let candidate = dir.join(&needle);
        if is_executable(&candidate) {
            return Some(candidate);
        }
    }
    None
}

/// Outcome of an attempted start.
#[derive(Debug, PartialEq, Eq)]
pub enum SpawnOutcome {
    /// `vct-hub --start-if-not-running` returned 0 (started fresh or
    /// was already running — both success).
    Started,
    /// Binary not found on this machine; degraded mode.
    BinaryNotFound,
    /// Binary found but exec failed (permissions, missing libraries).
    SpawnFailed(String),
    /// Spawn succeeded but `--start-if-not-running` exited non-zero.
    HubReportedError(i32),
    /// v0.2.54 Track C (C-7): an orchestrator update is in progress
    /// (`<vct_root>/.update-in-progress.json` is fresh) — respawning
    /// the hub now would re-lock `vct-hub.exe` on Windows mid-swap and
    /// recreate the exact sharing-violation the update flow's hub-stop
    /// was designed to prevent. Caller should treat this as a benign
    /// skip; the post-update launcher boot starts the hub.
    SkippedUpdateInProgress,
}

/// Attempt to bring up the detached vct-hub. Best-effort; never
/// returns Err — the launcher's setup must continue even if the hub
/// can't start (see module docs for the "degraded mode" contract).
pub fn ensure_hub_running() -> SpawnOutcome {
    // v0.2.54 Track C (C-7): honour the V52-AI update gate the same way
    // MCP servers do. During the update window the launcher explicitly
    // stops the hub (`ensure_hub_stopped_for_update`) so the binary can
    // be swapped; an ungated boot-time respawn here (e.g. a second
    // launcher start, or any future caller) would resurrect the OLD hub
    // and relock `vct-hub.exe` between stop and swap. A stale lockfile
    // (deadline passed) does NOT block — `is_update_in_progress` treats
    // it as absent, and boot-time `cleanup_if_stale` removes it.
    //
    // Note: the update flow's own intentional mid-flow hub starts go
    // through `installer::ensure_hub_started_after_update`, which is
    // NOT gated — those call sites run either after the gate has been
    // disarmed (success tail) or on error paths where the gate is about
    // to be dropped, and they must succeed regardless.
    if crate::commands::update_gate::is_update_in_progress() {
        eprintln!(
            "[vct] ensure_hub_running: orchestrator update in progress \
             (.update-in-progress.json is fresh) — skipping hub respawn; \
             the post-update launcher boot will start the new hub."
        );
        return SpawnOutcome::SkippedUpdateInProgress;
    }

    let Some(bin) = find_hub_binary() else {
        eprintln!(
            "[vct] vct-hub binary not found on this machine; \
             launcher will run in hub-unavailable degraded mode. \
             Set VCT_HUB_BIN or run install.py to deploy it."
        );
        return SpawnOutcome::BinaryNotFound;
    };

    // v0.2.63 — identity-aware swap. The single-instance lockfile and
    // `--start-if-not-running` only answer "is A hub alive?", never "is the
    // alive hub the CURRENT install-folder copy?". Two ways a live hub is
    // wrong, both of which `--start-if-not-running` would silently no-op past:
    //   * a DIFFERENT binary — a dev `cargo run`/`--foreground` from another
    //     checkout, an old global install, a hub a MANUAL `install.py --update`
    //     left running (the GUI update flow stops the hub before the swap; a
    //     bare `install.py --update` does not); and
    //   * an OLDER BUILD of the same path — a POSIX in-place update rewrote
    //     `<dist>/vct-hub` but the old process is still running the previous
    //     inode ("restart on the new binary if an older binary is running").
    // `running_hub_is_stale` detects both; if stale we stop the old hub here so
    // the spawn below brings up the install-folder copy.
    //
    // This does NOT violate the "hub outlives the launcher GUI" contract: that
    // forbids stopping the hub at launcher QUIT, not swapping a foreign/stale
    // hub at BOOT. The update-gate short-circuit above already prevents this
    // from firing mid-update.
    //
    // Strictly conservative: stop ONLY on a POSITIVE mismatch (running exe
    // resolved AND != our copy). An unresolved running exe (sysinfo returned
    // None) is left untouched — we never kill a hub we can't identify.
    if let crate::hub_status::HubStatus::Running { pid } = crate::hub_status::probe() {
        let running_exe = crate::commands::update_gate::process_exe_by_pid(pid);
        if running_hub_is_stale(pid, running_exe.as_deref(), &bin) {
            eprintln!(
                "[vct] vct-hub pid {} is running a stale or foreign binary ({}); \
                 stopping it so the install-folder copy {} can take over.",
                pid,
                running_exe
                    .as_deref()
                    .map(|p| p.display().to_string())
                    .unwrap_or_else(|| "path unresolved".to_string()),
                bin.display()
            );
            // Soft-fail. `--stop` is lockfile-driven (it signals the pid in
            // hub.pid regardless of which binary issues it) and blocks up to
            // ~10s on graceful shutdown, so the lockfile is released by the
            // time it returns and the spawn below starts fresh. A failed stop
            // just leaves the spawn to no-op on the old hub — no worse than
            // pre-v0.2.63.
            match crate::hub_status::stop() {
                crate::hub_status::StopOutcome::Stopped
                | crate::hub_status::StopOutcome::AlreadyStopped => {}
                other => eprintln!(
                    "[vct] could not stop the stale hub ({:?}); the install-folder \
                     copy may not take over until the stale hub exits.",
                    other
                ),
            }
        }
    }

    eprintln!("[vct] auto-starting vct-hub from {}", bin.display());

    // Invoke synchronously so we know whether the spawn succeeded.
    // `--start-if-not-running` itself spawns a detached child and
    // returns quickly (within ~100ms on the smoke test in Step 5);
    // it does NOT block waiting for the hub to bind a port.
    //
    // We deliberately drop stdio so any noise from the child doesn't
    // pollute the launcher's logs. The hub writes its own log.
    //
    // CREATE_NO_WINDOW (0x08000000) on Windows: without it the vct-hub
    // child spawned from a `windows_subsystem = "windows"` parent
    // allocates a fresh conhost.exe console that flashes on screen for
    // the hub's ~100ms startup window. ensure_hub_running is called once
    // at every launcher boot, so this is one of the visible-flash sources
    // we audited 2026-05-26.
    let mut cmd = Command::new(&bin);
    cmd.arg("--start-if-not-running")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000);
    }
    let result = cmd.status();

    match result {
        Ok(status) if status.success() => SpawnOutcome::Started,
        Ok(status) => {
            let code = status.code().unwrap_or(-1);
            eprintln!(
                "[vct] vct-hub --start-if-not-running exited {}; degraded mode",
                code
            );
            SpawnOutcome::HubReportedError(code)
        }
        Err(e) => {
            let msg = format!("{}", e);
            eprintln!(
                "[vct] failed to spawn vct-hub from {}: {}; degraded mode",
                bin.display(),
                msg
            );
            SpawnOutcome::SpawnFailed(msg)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    static SERIALIZE: Mutex<()> = Mutex::new(());

    fn with_env<F: FnOnce()>(vars: &[(&str, Option<&str>)], f: F) {
        let _g = SERIALIZE.lock().unwrap_or_else(|p| p.into_inner());
        let saved: Vec<(String, Option<std::ffi::OsString>)> = vars
            .iter()
            .map(|(k, _)| (k.to_string(), std::env::var_os(k)))
            .collect();
        for (k, v) in vars {
            unsafe {
                match v {
                    Some(val) => std::env::set_var(k, val),
                    None => std::env::remove_var(k),
                }
            }
        }
        f();
        for (k, v) in saved {
            unsafe {
                match v {
                    Some(val) => std::env::set_var(&k, val),
                    None => std::env::remove_var(&k),
                }
            }
        }
    }

    #[test]
    fn is_executable_returns_false_for_missing_path() {
        assert!(!is_executable(std::path::Path::new(
            "/definitely/not/a/real/path/vct-hub"
        )));
    }

    #[test]
    fn is_executable_returns_false_for_directory() {
        let tmp = tempfile::tempdir().unwrap();
        assert!(!is_executable(tmp.path()));
    }

    #[test]
    fn find_hub_binary_returns_explicit_override_when_executable() {
        let tmp = tempfile::tempdir().unwrap();
        let exe = tmp.path().join("vct-hub-fake");
        std::fs::write(&exe, "#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o755)).unwrap();
        }

        with_env(
            &[
                ("VCT_HUB_BIN", Some(exe.to_str().unwrap())),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some("/nonexistent-home")),
            ],
            || {
                let found = find_hub_binary().expect("override resolves");
                assert_eq!(found, exe);
            },
        );
    }

    #[test]
    fn find_hub_binary_falls_through_when_override_is_not_executable() {
        let tmp = tempfile::tempdir().unwrap();
        let nonexec = tmp.path().join("does-not-exist");
        with_env(
            &[
                ("VCT_HUB_BIN", Some(nonexec.to_str().unwrap())),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some("/nonexistent-home")),
                // v0.2.53: disable current_exe()-based discovery so the
                // `target/debug/vct-hub` binary other cargo runs leave behind
                // doesn't poison this test. Production never sets this var.
                ("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1")),
            ],
            || {
                // No legitimate hub anywhere → None.
                assert_eq!(find_hub_binary(), None);
            },
        );
    }

    #[test]
    fn find_hub_binary_returns_none_when_nothing_resolves() {
        with_env(
            &[
                ("VCT_HUB_BIN", None),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some("/nonexistent-home")),
                ("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1")),
            ],
            || {
                assert_eq!(find_hub_binary(), None);
            },
        );
    }

    #[test]
    fn find_hub_dist_sibling_respects_test_isolation_gate() {
        // The extracted helper must honour the test-isolation gate the
        // same way the inlined steps 4+5 did (else cargo-test cross-talk
        // poisons the "nothing resolves" tests).
        with_env(
            &[("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1"))],
            || {
                assert_eq!(find_hub_dist_sibling(), None);
            },
        );
    }

    #[test]
    fn ensure_hub_running_reports_binary_not_found_in_clean_env() {
        with_env(
            &[
                ("VCT_HUB_BIN", None),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some("/nonexistent-home")),
                ("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1")),
            ],
            || {
                assert_eq!(ensure_hub_running(), SpawnOutcome::BinaryNotFound);
            },
        );
    }

    // v0.2.54 Track C (C-7): hub respawn is gated on the V52-AI update
    // lockfile. A fresh `.update-in-progress.json` must short-circuit
    // ensure_hub_running BEFORE any binary discovery happens.
    #[test]
    fn ensure_hub_running_skips_when_update_in_progress() {
        let tmp = tempfile::tempdir().unwrap();
        // Write a fresh lockfile into the isolated state dir.
        let lock = tmp
            .path()
            .join(crate::commands::update_gate::LOCKFILE_BASENAME);
        crate::commands::update_gate::write_lockfile_at(
            &lock,
            crate::commands::update_gate::Phase::InstallPy,
            15,
        )
        .expect("lockfile write");

        with_env(
            &[
                ("VCT_STATE_DIR", Some(tmp.path().to_str().unwrap())),
                ("VCT_HUB_BIN", None),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some("/nonexistent-home")),
                ("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1")),
            ],
            || {
                assert_eq!(
                    ensure_hub_running(),
                    SpawnOutcome::SkippedUpdateInProgress
                );
            },
        );
    }

    // Stale lockfile (deadline in the past) must NOT block the respawn —
    // it falls through to normal discovery (BinaryNotFound in this env).
    #[test]
    fn ensure_hub_running_ignores_stale_update_lockfile() {
        let tmp = tempfile::tempdir().unwrap();
        let lock = tmp
            .path()
            .join(crate::commands::update_gate::LOCKFILE_BASENAME);
        let past = (chrono::Utc::now() - chrono::Duration::minutes(30))
            .format("%Y-%m-%dT%H:%M:%SZ")
            .to_string();
        let payload = crate::commands::update_gate::LockfilePayload {
            started_at: past.clone(),
            started_by_pid: 1,
            phase: crate::commands::update_gate::Phase::BinaryRefresh,
            expected_completion_by: past,
        };
        std::fs::write(&lock, serde_json::to_string(&payload).unwrap()).unwrap();

        with_env(
            &[
                ("VCT_STATE_DIR", Some(tmp.path().to_str().unwrap())),
                ("VCT_HUB_BIN", None),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some("/nonexistent-home")),
                ("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1")),
            ],
            || {
                assert_eq!(ensure_hub_running(), SpawnOutcome::BinaryNotFound);
            },
        );
    }

    #[test]
    fn hub_binary_name_picks_per_platform_extension() {
        let n = hub_binary_name();
        #[cfg(windows)]
        assert_eq!(n, "vct-hub.exe");
        #[cfg(not(windows))]
        assert_eq!(n, "vct-hub");
    }

    // ── v0.2.63: same_binary identity check (drives the boot-time swap) ──

    #[test]
    fn same_binary_true_for_identical_path() {
        let tmp = tempfile::tempdir().unwrap();
        let f = tmp.path().join("vct-hub");
        std::fs::write(&f, b"x").unwrap();
        assert!(same_binary(&f, &f));
    }

    #[test]
    fn same_binary_false_for_distinct_files() {
        let tmp = tempfile::tempdir().unwrap();
        let a = tmp.path().join("a-hub");
        let b = tmp.path().join("b-hub");
        std::fs::write(&a, b"x").unwrap();
        std::fs::write(&b, b"x").unwrap();
        // Distinct files (different inode) with identical content must NOT
        // compare equal — this is the dev-build-vs-install-folder case.
        assert!(!same_binary(&a, &b));
    }

    #[cfg(unix)]
    #[test]
    fn same_binary_true_through_symlink() {
        let tmp = tempfile::tempdir().unwrap();
        let real = tmp.path().join("real-hub");
        std::fs::write(&real, b"x").unwrap();
        let link = tmp.path().join("link-hub");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        // canonicalize() resolves the symlink → same file.
        assert!(same_binary(&real, &link));
    }

    #[test]
    fn same_binary_false_for_distinct_missing_paths() {
        // Unresolvable + unequal → false. Guarantees we never report a false
        // "same" that would suppress a legitimate swap.
        assert!(!same_binary(
            std::path::Path::new("/no/such/a-hub"),
            std::path::Path::new("/no/such/b-hub")
        ));
    }

    // ── v0.2.63: reorder keeps the PATH + user-install fallbacks reachable
    // after the install-folder copy was promoted to step 2. ──────────────

    #[test]
    fn find_hub_binary_falls_to_path_when_sibling_disabled() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("pathdir");
        std::fs::create_dir(&dir).unwrap();
        let exe = dir.join(hub_binary_name());
        std::fs::write(&exe, "#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        with_env(
            &[
                ("VCT_HUB_BIN", None),
                ("PATH", Some(dir.to_str().unwrap())),
                ("HOME", Some("/nonexistent-home")),
                ("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1")),
            ],
            || {
                assert_eq!(find_hub_binary(), Some(exe.clone()));
            },
        );
    }

    // ── v0.2.63: running_hub_is_stale (drives the boot-time auto-restart) ─

    #[cfg(target_os = "linux")]
    #[test]
    fn running_hub_is_stale_false_for_own_running_exe() {
        // /proc/self/exe and the on-disk current_exe are the same inode → the
        // running process IS executing the install copy → not stale.
        let myexe = std::env::current_exe().unwrap();
        assert!(!running_hub_is_stale(std::process::id(), Some(&myexe), &myexe));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn running_hub_is_stale_true_when_install_copy_is_a_different_file() {
        // The test process's /proc/self/exe inode differs from an unrelated
        // file → stale. Passing running_exe=None proves the Linux /proc inode
        // branch works even when sysinfo could not resolve the exe path — this
        // is the "older / foreign binary running" detection.
        let tmp = tempfile::tempdir().unwrap();
        let other = tmp.path().join("vct-hub");
        std::fs::write(&other, b"x").unwrap();
        assert!(running_hub_is_stale(std::process::id(), None, &other));
    }

    #[cfg(not(target_os = "linux"))]
    #[test]
    fn running_hub_is_stale_uses_path_check_off_linux() {
        let tmp = tempfile::tempdir().unwrap();
        let a = tmp.path().join("a-hub");
        std::fs::write(&a, b"x").unwrap();
        let b = tmp.path().join("b-hub");
        std::fs::write(&b, b"x").unwrap();
        // Different path → stale; same path → fresh; unresolved → not stale.
        assert!(running_hub_is_stale(424242, Some(&a), &b));
        assert!(!running_hub_is_stale(424242, Some(&a), &a));
        assert!(!running_hub_is_stale(424242, None, &b));
    }

    #[test]
    fn find_hub_binary_falls_to_user_install_when_path_empty() {
        let tmp = tempfile::tempdir().unwrap();
        let bindir = tmp.path().join(".vct").join("bin");
        std::fs::create_dir_all(&bindir).unwrap();
        let exe = bindir.join(hub_binary_name());
        std::fs::write(&exe, "#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        with_env(
            &[
                ("VCT_HUB_BIN", None),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some(tmp.path().to_str().unwrap())),
                ("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1")),
            ],
            || {
                assert_eq!(find_hub_binary(), Some(exe.clone()));
            },
        );
    }
}
