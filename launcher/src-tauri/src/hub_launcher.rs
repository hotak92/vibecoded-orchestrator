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
//! Discovery chain — **owned by `vco_lib/hub_ensure.py`**, not by this file.
//! v0.2.92 (ruling R20) merged the three copies of it (this module, the
//! `.sh` SessionStart hook and its `.ps1` sibling) into that ONE Python home;
//! [`find_hub_binary`] now asks it via
//! `python -m vco_lib.hub_ensure resolve --json`. The chain is unchanged:
//!   1. `$VCT_HUB_BIN` env override (highest priority — dev builds).
//!   2. `<dir of this launcher binary>/vct-hub` (the INSTALL-FOLDER copy
//!      — the hub that shipped WITH this exact launcher; populated by
//!      `build-bundled-launcher.sh`), then the arch-less fallback one dir
//!      up. This is the one step Python cannot derive on its own, so the
//!      launcher passes it down via [`launcher_install_dirs`] as
//!      `--extra-dir`. **Preferred** over PATH/`~/.vct/bin` (v0.2.63): the
//!      sibling copy is guaranteed to match this launcher's version, whereas
//!      PATH or `~/.vct/bin` can point at a stale dev build or an older
//!      install. user request 2026-06-19: "we should always use the installed
//!      copy from the launcher's install folder."
//!   3. First `vct-hub` on PATH.
//!   4. `$HOME/.vct/bin/vct-hub` (install.py default install location).
//!
//! What stays HERE is what is launcher-only and therefore NOT duplicated
//! anywhere: the update gate, the stale/foreign-binary identity swap, and
//! the `CREATE_NO_WINDOW` spawn.
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

/// Find the vct-hub binary on disk, delegating to the ONE home.
///
/// v0.2.92, ruling R20: the four-step discovery chain used to be mirrored
/// here, in `templates/hooks/session-start-ensure-hub.sh` and in its `.ps1`
/// sibling — three hand-maintained copies of one question, which had already
/// drifted on arch-slot naming. The chain now lives in `vco_lib/hub_ensure.py`
/// and this function asks it via
/// `python -m vco_lib.hub_ensure resolve --json`. Class A of the repo's A>B>C
/// cross-language rule: every caller of this function is a user action or a
/// once-per-boot step (launcher setup, `hub_status::stop`, the boot-autostart
/// toggles, the install/update flow), never a poll loop — so a ~100 ms
/// subprocess is affordable and a mirror is not justified.
///
/// The launcher contributes the one input Python cannot derive: the
/// install-folder anchors relative to its OWN running binary
/// ([`launcher_install_dirs`]). Everything else — `$VCT_HUB_BIN`, the
/// `launcher/dist/<arch>/` slots, `$PATH`, `~/.vct/bin` — is resolved
/// by the module, from the environment this process passes down.
///
/// Returns `None` in two distinguishable-in-the-log situations:
///   * the module answered `binary_not_found` — a true fact about this
///     machine; callers degrade to hub-unavailable mode as they always did;
///   * the module could not be RUN at all — a BROKEN install. That is logged
///     at ERROR with the reason and still returns `None`. It is deliberately
///     NOT patched over with an inline re-derivation of the chain: a silent
///     fallback copy is exactly the drift this consolidation removes, and it
///     would mask an install that needs repairing.
pub fn find_hub_binary() -> Option<PathBuf> {
    let Some(python) = vct_launcher_core::python_resolve::resolve_python_for_vco_lib() else {
        tracing::error!(
            "[vct] no Python interpreter could be resolved for \
             `vco_lib.hub_ensure` — this is a BROKEN VCO install, not a \
             fallback case. The hub cannot be located; run install.py."
        );
        return None;
    };

    let mut cmd = Command::new(&python);
    // `--no-repo-dist`: the launcher anchors step 2 on its OWN binary's
    // directory, NOT on the checkout's `launcher/dist/`. In a shipped install
    // those are the same directory; in a dev tree they are not, and letting a
    // `cargo run` launcher fall back to the checkout's dist/ would hand it a
    // hub it never used to find. Consolidating the chain must not quietly add
    // a discovery source to one of its callers.
    cmd.args([
        "-m",
        "vco_lib.hub_ensure",
        "resolve",
        "--json",
        "--no-repo-dist",
    ]);
    for dir in launcher_install_dirs() {
        cmd.arg("--extra-dir").arg(dir);
    }
    // Run from the orchestrator checkout so `-m vco_lib...` imports even when
    // the resolved interpreter is a bare PATH python rather than the install's
    // venv. This sets only the CWD — it is deliberately NOT passed as
    // `--repo-root` (see `--no-repo-dist` above). Best-effort: an unresolvable
    // root just means we rely on the venv having `vco_lib` installed.
    if let Ok(root) = crate::commands::installer::find_local_repo_root() {
        cmd.current_dir(&root);
    }
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }

    let out = match cmd.output() {
        Ok(out) => out,
        Err(e) => {
            tracing::error!(
                "[vct] could not run `{} -m vco_lib.hub_ensure resolve`: {} \
                 — BROKEN VCO install; the hub cannot be located.",
                python.display(),
                e
            );
            return None;
        }
    };

    let stdout = String::from_utf8_lossy(&out.stdout);
    let parsed: serde_json::Value = match serde_json::from_str(stdout.trim()) {
        Ok(v) => v,
        Err(e) => {
            tracing::error!(
                "[vct] `vco_lib.hub_ensure resolve` produced unparseable output \
                 (exit {:?}): {} — stderr: {}",
                out.status.code(),
                e,
                String::from_utf8_lossy(&out.stderr).trim()
            );
            return None;
        }
    };

    match parsed.get("state").and_then(|s| s.as_str()) {
        Some("resolved") => parsed
            .get("binary")
            .and_then(|b| b.as_str())
            .map(PathBuf::from),
        Some("binary_not_found") => None,
        other => {
            tracing::error!(
                "[vct] `vco_lib.hub_ensure resolve` reported unexpected state \
                 {:?}: {}",
                other,
                parsed
                    .get("reason")
                    .and_then(|r| r.as_str())
                    .unwrap_or("(no reason)")
            );
            None
        }
    }
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

/// The launcher's own install-folder anchors, handed to the SSOT resolver as
/// `--extra-dir` (highest-priority install-folder candidates).
///
/// These are the ONE input `vco_lib.hub_ensure` cannot derive for itself: the
/// directory of the RUNNING launcher binary, and its parent. That is the
/// layout `build-bundled-launcher.sh` produces, and it is preferred over
/// `$PATH` / `~/.vct/bin` (v0.2.63) because the sibling copy is guaranteed to
/// be the hub that shipped with this exact launcher build — a stale `vct-hub`
/// on PATH (a leftover dev build, an old global install) must not win over
/// the copy install.py just deployed next to the launcher.
///
/// Returns an EMPTY list under a cargo test harness (or when
/// `VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY` is set), so tests never resolve
/// `target/debug/vct-hub`.
///
/// v0.2.53's test-isolation env var is honoured here; v0.2.92 made the gate
/// DEFAULT-ON under a test harness rather than opt-in. The env var was the
/// only protection, and only the tests in THIS module ever set it. Every
/// other test in the workspace that reached `find_hub_binary()` resolved
/// `target/debug/vct-hub`, and `ensure_hub_running()` then SPAWNED it —
/// against the developer's real `~/.vct`, where `Db::open` ran migrations on
/// their production `launcher.db`. Measured: a `cargo test --workspace` run
/// produced a complete live state dir (`launcher.db`, `hub.pid`, `hub.token`,
/// `logs/hub.<date>.log`) whose `hub.pid` identity line carried the working
/// tree's `-dirty` suffix — i.e. it was this `target/debug` build, not the
/// installed one.
///
/// "Remember to set the env var" is not a mechanism; [`exe_is_test_harness`]
/// is. See its docs for why the check cannot produce a false positive in
/// production.
fn launcher_install_dirs() -> Vec<PathBuf> {
    if std::env::var_os("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY").is_some() {
        return Vec::new();
    }
    let Ok(exe) = std::env::current_exe() else {
        return Vec::new();
    };
    if exe_is_test_harness(&exe) {
        return Vec::new();
    }
    let Some(parent) = exe.parent() else {
        return Vec::new();
    };
    let mut dirs = vec![parent.to_path_buf()];
    // Arch-less fallback one dir up (some packaging layouts).
    if let Some(grandparent) = parent.parent() {
        dirs.push(grandparent.to_path_buf());
    }
    dirs
}


/// True when `exe` is a cargo TEST binary rather than a shipped launcher.
///
/// Cargo puts every test / bench / example binary in `<target>/<profile>/deps/`
/// — a hash-suffixed file whose PARENT DIRECTORY is named `deps`. That holds
/// for unit tests (`--lib`), integration tests (`tests/*.rs`), doc-tests and
/// `cargo nextest`, on all three OSes, and it does not depend on anyone
/// remembering to set anything.
///
/// It cannot fire in production. Shipped layouts are
/// `<install>/launcher/dist/<os>-<arch>/vct-launcher` (parent
/// `linux-x64` / `darwin-arm64` / `windows-x64`) and the packaged app bundles;
/// none is named `deps`. A dev `cargo run` is `<target>/<profile>/vct-launcher`
/// — parent `debug`, not `deps` — so the dev workflow of "my `cargo run`
/// launcher uses my `cargo build` hub" is preserved deliberately.
///
/// Tri-OS: `Path::parent` / `Path::file_name` are platform-native, so the same
/// comparison reads `...\target\debug\deps\foo-abc.exe` on Windows. Nothing
/// here is case-sensitive beyond the directory name cargo itself creates.
///
/// Worst case if a user really does install into a directory named `deps`:
/// discovery falls through to `$VCT_HUB_BIN`, `$PATH` and `~/.vct/bin`, which
/// is a degraded lookup — never a wrong action.
fn exe_is_test_harness(exe: &std::path::Path) -> bool {
    exe.parent()
        .and_then(|p| p.file_name())
        .map(|n| n == std::ffi::OsStr::new("deps"))
        .unwrap_or(false)
}

/// [`exe_is_test_harness`] applied to THIS process.
///
/// The ONE home for "am I a cargo test binary?" — used by the hub-discovery
/// gate above and by
/// [`crate::commands::update_gate::pre_update_hub_kill_sweep`], which reaps
/// hub processes by exe basename and is therefore blind to `VCT_STATE_DIR`
/// isolation. Do not add a second copy of this predicate.
///
/// Conservative on error: an unresolvable `current_exe()` reads as "not a
/// test", i.e. production behaviour, because the production consequence of a
/// wrong `false` (a hub spawn) is milder than the consequence of a wrong
/// `true` (an update that silently skips stopping the hub).
pub(crate) fn running_under_test_harness() -> bool {
    std::env::current_exe()
        .map(|exe| exe_is_test_harness(&exe))
        .unwrap_or(false)
}

// v0.2.92 (R20): `hub_binary_name()`, `is_executable()` and `find_on_path()`
// used to live here. They were the Rust third of a three-way mirror of hub
// binary discovery; all three now have ONE home in `vco_lib/hub_ensure.py`
// (`hub_binary_names()`, `_is_executable()`, and the `$PATH` step of
// `find_hub_binary()`). Do not re-add a Rust copy — call the module.


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
        tracing::warn!(
            "[vct] ensure_hub_running: orchestrator update in progress \
             (.update-in-progress.json is fresh) — skipping hub respawn; \
             the post-update launcher boot will start the new hub."
        );
        return SpawnOutcome::SkippedUpdateInProgress;
    }

    let Some(bin) = find_hub_binary() else {
        tracing::warn!(
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
            tracing::warn!(
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
                other => tracing::warn!(
                    "[vct] could not stop the stale hub ({:?}); the install-folder \
                     copy may not take over until the stale hub exits.",
                    other
                ),
            }
        }
    }

    tracing::info!("[vct] auto-starting vct-hub from {}", bin.display());

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
            tracing::warn!(
                "[vct] vct-hub --start-if-not-running exited {}; degraded mode",
                code
            );
            SpawnOutcome::HubReportedError(code)
        }
        Err(e) => {
            let msg = format!("{}", e);
            tracing::warn!(
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

    /// An ABSOLUTE python interpreter path, resolved from the REAL `$PATH`
    /// before a test constrains it.
    ///
    /// Needed because v0.2.92 (R20) made `find_hub_binary` delegate to
    /// `vco_lib.hub_ensure`, which means `$PATH` now feeds TWO things: step 3
    /// of the hub-discovery chain (what these tests want to control) and the
    /// last-resort tier of interpreter discovery (what they must not break).
    /// Pinning the interpreter through `$VCT_VENV` — tier 1 of the RT-4
    /// ladder, ahead of `$PATH` — decouples them, so a test can still nuke
    /// `$PATH` to isolate the chain.
    ///
    /// Panics if no interpreter exists: a machine that cannot run Python
    /// cannot run VCO at all (`install.py` IS the installer), so that is a
    /// broken environment to report loudly, not to skip over.
    fn absolute_python_for_tests() -> String {
        let names: &[&str] = if cfg!(windows) {
            &["python.exe", "py.exe", "python3.exe"]
        } else {
            &["python3", "python"]
        };
        let path_env = std::env::var_os("PATH").expect("PATH must be set");
        for dir in std::env::split_paths(&path_env) {
            for name in names {
                let candidate = dir.join(name);
                if candidate.is_file() {
                    return candidate.to_string_lossy().to_string();
                }
            }
        }
        panic!(
            "no python interpreter on PATH; hub discovery is delegated to \
             vco_lib.hub_ensure and cannot be exercised without one"
        );
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

        let py = absolute_python_for_tests();
        with_env(
            &[
                ("VCT_HUB_BIN", Some(exe.to_str().unwrap())),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some("/nonexistent-home")),
                ("VCT_VENV", Some(&py)),
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
        let py = absolute_python_for_tests();
        with_env(
            &[
                ("VCT_HUB_BIN", Some(nonexec.to_str().unwrap())),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some("/nonexistent-home")),
                // v0.2.53: disable current_exe()-based discovery so the
                // `target/debug/vct-hub` binary other cargo runs leave behind
                // doesn't poison this test. Production never sets this var.
                ("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1")),
                ("VCT_VENV", Some(&py)),
            ],
            || {
                // No legitimate hub anywhere → None.
                assert_eq!(find_hub_binary(), None);
            },
        );
    }

    #[test]
    fn find_hub_binary_returns_none_when_nothing_resolves() {
        let py = absolute_python_for_tests();
        with_env(
            &[
                ("VCT_HUB_BIN", None),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some("/nonexistent-home")),
                ("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1")),
                ("VCT_VENV", Some(&py)),
            ],
            || {
                assert_eq!(find_hub_binary(), None);
            },
        );
    }

    // ─── v0.2.92: the default-on test-harness gate ───────────────────────

    #[test]
    fn exe_in_a_cargo_deps_dir_is_recognised_as_a_test_harness() {
        // Exactly the shape `cargo test` produces, for the three build
        // layouts a harness binary can appear in.
        for p in [
            "/repo/launcher/src-tauri/target/debug/deps/vct_launcher_temp-1a2b3c",
            "/repo/target/release/deps/integration_test-9f8e7d",
            "/custom/CARGO_TARGET_DIR/debug/deps/doctest-0000",
        ] {
            assert!(
                exe_is_test_harness(std::path::Path::new(p)),
                "must be recognised as a harness: {p}"
            );
        }
    }

    #[test]
    fn shipped_and_dev_launcher_layouts_are_not_test_harnesses() {
        // If ANY of these read as a harness, a real user's launcher would
        // stop finding its install-folder hub — the guard's only failure
        // mode that matters.
        for p in [
            // Shipped: `build-bundled-launcher.sh` layout.
            "/opt/vct/launcher/dist/linux-x64/vct-launcher",
            "/Applications/VCT.app/Contents/MacOS/vct-launcher",
            "/home/u/.local/share/vct/vct-launcher",
            // Dev: `cargo run` / `cargo build` output (NOT under deps/).
            "/repo/launcher/src-tauri/target/debug/vct-launcher",
            "/repo/launcher/src-tauri/target/release/vct-launcher",
            // A path with no parent at all.
            "vct-launcher",
        ] {
            assert!(
                !exe_is_test_harness(std::path::Path::new(p)),
                "must NOT be treated as a harness: {p}"
            );
        }
    }

    /// The gate is only worth anything if it fires for the process actually
    /// running these assertions. If cargo ever changes its output layout,
    /// this fails and says so — rather than the protection quietly lapsing.
    #[test]
    fn this_very_test_binary_is_detected_as_a_test_harness() {
        let exe = std::env::current_exe().expect("current_exe");
        assert!(
            exe_is_test_harness(&exe),
            "the running test binary must be detected as a harness; \
             current_exe() = {}",
            exe.display()
        );
        assert!(running_under_test_harness());
    }

    /// Default-on: with the opt-in env var ABSENT, the launcher must still
    /// hand the resolver NO install-folder anchors under the harness. This is
    /// the assertion that would have caught the original incident — the
    /// pre-v0.2.92 code returned `Some(target/debug/vct-hub)` here.
    #[test]
    fn launcher_install_dirs_are_empty_under_a_test_harness_without_the_env_gate() {
        with_env(&[("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", None)], || {
            assert!(
                launcher_install_dirs().is_empty(),
                "install-folder anchors must be OFF by default under cargo \
                 test, not merely off when a test remembers to set the env var"
            );
        });
    }

    #[test]
    fn launcher_install_dirs_respect_test_isolation_gate() {
        // The helper must honour the test-isolation gate the same way the
        // inlined steps 4+5 did (else cargo-test cross-talk poisons the
        // "nothing resolves" tests).
        with_env(
            &[("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1"))],
            || {
                assert!(launcher_install_dirs().is_empty());
            },
        );
    }

    #[test]
    fn ensure_hub_running_reports_binary_not_found_in_clean_env() {
        let py = absolute_python_for_tests();
        with_env(
            &[
                ("VCT_HUB_BIN", None),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some("/nonexistent-home")),
                ("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1")),
                ("VCT_VENV", Some(&py)),
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

    /// Filename a test FIXTURE must use on this platform. Not a resolution
    /// primitive — the real per-platform name list is
    /// `vco_lib.hub_ensure.hub_binary_names()` (pinned by
    /// `tests/test_v0292_hub_ensure.py`), which this only has to agree with
    /// well enough to create a file the resolver will find.
    fn test_hub_filename() -> &'static str {
        if cfg!(windows) {
            "vct-hub.exe"
        } else {
            "vct-hub"
        }
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
        let exe = dir.join(test_hub_filename());
        std::fs::write(&exe, "#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let py = absolute_python_for_tests();
        with_env(
            &[
                ("VCT_HUB_BIN", None),
                ("PATH", Some(dir.to_str().unwrap())),
                ("HOME", Some("/nonexistent-home")),
                ("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1")),
                ("VCT_VENV", Some(&py)),
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
        let exe = bindir.join(test_hub_filename());
        std::fs::write(&exe, "#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let py = absolute_python_for_tests();
        with_env(
            &[
                ("VCT_HUB_BIN", None),
                ("PATH", Some("/nonexistent-dir")),
                ("HOME", Some(tmp.path().to_str().unwrap())),
                ("VCT_HUB_DISABLE_CURRENT_EXE_DISCOVERY", Some("1")),
                ("VCT_VENV", Some(&py)),
            ],
            || {
                assert_eq!(find_hub_binary(), Some(exe.clone()));
            },
        );
    }
}
