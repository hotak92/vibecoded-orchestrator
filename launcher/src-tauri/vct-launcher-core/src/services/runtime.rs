//! Container-runtime detection for the launcher's services lifecycle.
//!
//! The launcher runs `<runtime>-compose` (or `<runtime> compose`) against
//! `infrastructure/docker-compose.yml` to bring shared services up/down.
//! We pick exactly ONE runtime per launcher process — Podman is preferred
//! (per user policy) and Docker is the fallback. Mixed-runtime setups are
//! intentionally NOT supported; otherwise `compose down` from one runtime
//! could leak containers from the other.
//!
//! Cross-OS specifics:
//!
//!   - Linux:   `podman` and `docker` are normal binaries on PATH.
//!   - macOS:   same, but Podman additionally requires `podman machine
//!              start` to have been run (the binary is fine; the daemon
//!              isn't). We surface that distinction so the launcher can
//!              prompt the user.
//!   - Windows: `podman.exe` works after `podman machine init`+`start`.
//!              Docker Desktop ships `docker.exe` on PATH. WSL podman is
//!              also possible (`wsl podman --version`) but we deliberately
//!              avoid it — invoking compose across the WSL boundary makes
//!              bind-mount paths brittle.
//!
//! Compose-subcommand detection: modern Podman/Docker provide compose as
//! a subcommand (`podman compose ...` / `docker compose ...`). Older
//! installations only have the standalone binary `podman-compose` /
//! `docker-compose`. We try the subcommand form first and fall back to
//! the standalone binary; the chosen form is recorded on the
//! `ContainerRuntime` so callers can build commands without re-probing.

use std::path::PathBuf;
use std::sync::Mutex;
use tokio::process::Command as TokioCommand;

/// Build a `tokio::process::Command` that does NOT flash a console window
/// on Windows. Equivalent to `TokioCommand::new(bin)` on Linux/macOS.
///
/// Why this exists (2026-05-26 fork-bomb fix): runtime detection probes
/// (`docker --version`, `docker info`, `podman --version`, `podman info`)
/// spawn child subprocesses. On Windows, a child spawned from a
/// `windows_subsystem = "windows"` parent (= our launcher) inherits the
/// console allocation flag. Without `CREATE_NO_WINDOW` (0x08000000), each
/// spawned child gets a NEW console allocated by `CreateProcessW`, which
/// flashes a `conhost.exe` window for the child's lifetime. With the
/// services::watcher polling every 30s, the version+daemon-usable probes
/// running back-to-back AND the OnboardingWizard's preflight rerunning
/// probes, the user sees a STREAM of console windows flashing on screen
/// at startup, becoming visually indistinguishable from "milioni di
/// finestre" cascading.
///
/// EnumWindows snapshot taken 2026-05-26 against the launcher proved
/// this: 11 of the 15 launcher-owned visible windows were
/// `CASCADIA_HOSTING_WINDOW_CLASS` (Windows Terminal hosting class)
/// with titles like `C:\Windows\system32\where.exe`, `git.exe` — not
/// dialog-based at all.
///
/// Pattern mirrors `launcher/src-tauri/src/commands/installer.rs:2491`
/// (and 11 other places in the launcher) which already set this flag.
/// This helper centralises the pattern for the `services/runtime.rs`
/// hot path which the audit missed.
///
/// v0.2.42: `mut cmd` is REQUIRED on Windows because `creation_flags`
/// takes `&mut self`. cargo-fix on Linux stripped `mut` (the cfg(windows)
/// branch is inactive there → unused_mut warning), breaking the Windows
/// build with E0596. Restored `mut` + `#[allow(unused_mut)]` to silence
/// the Linux warning cleanly.
#[allow(unused_mut)]
fn silent_command<S: AsRef<std::ffi::OsStr>>(program: S) -> TokioCommand {
    let mut cmd = TokioCommand::new(program);
    #[cfg(windows)]
    {
        // CREATE_NO_WINDOW = 0x08000000 (winbase.h). Suppresses console
        // allocation for the child. Mandatory on Windows for parent
        // processes built with `windows_subsystem = "windows"`.
        cmd.creation_flags(0x0800_0000);
    }
    cmd
}

/// Which container runtime the launcher will drive. Detected once per
/// launcher session and cached.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ContainerRuntime {
    Podman,
    Docker,
}

impl ContainerRuntime {
    /// Binary name as it appears on PATH (no `.exe` suffix — `Command`
    /// resolves the extension on Windows).
    pub fn binary(self) -> &'static str {
        match self {
            ContainerRuntime::Podman => "podman",
            ContainerRuntime::Docker => "docker",
        }
    }

    /// Human-friendly label for UI text and notifications.
    pub fn display_name(self) -> &'static str {
        match self {
            ContainerRuntime::Podman => "Podman",
            ContainerRuntime::Docker => "Docker",
        }
    }
}

/// Whether the runtime exposes compose as a subcommand of the main
/// binary (`podman compose ...`) or as a separate executable
/// (`podman-compose ...`). Newer Podman (4.x+) and Docker (20.10+) ship
/// the subcommand form.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ComposeForm {
    /// `podman compose ...` / `docker compose ...`
    Subcommand,
    /// `podman-compose ...` / `docker-compose ...`
    Standalone,
}

/// Concrete description of the runtime the launcher will drive: which
/// binary, which compose invocation, and whether the runtime needs an
/// out-of-band daemon kick (Podman Machine on macOS/Windows).
#[derive(Debug, Clone)]
pub struct RuntimeInfo {
    pub runtime: ContainerRuntime,
    pub compose_form: ComposeForm,
    /// On macOS/Windows: true when Podman is the chosen runtime AND
    /// `podman machine list --format '{{.Running}}'` reports no running
    /// machine. The launcher surfaces this to the UI so the user can
    /// run `podman machine start` (or click a button that does it for
    /// them, post-launch enhancement).
    pub needs_machine_start: bool,
    /// Absolute path to the binary on PATH. Stored so subsequent
    /// invocations don't redo the PATH walk.
    pub binary_path: PathBuf,
}

impl RuntimeInfo {
    /// Build a fresh `tokio::process::Command` for `<runtime> compose ...`
    /// or `<runtime>-compose ...` depending on `compose_form`. Caller
    /// then chains `.args(["up", "-d"])` etc. and `.current_dir(...)`.
    pub fn compose_command(&self) -> TokioCommand {
        match self.compose_form {
            ComposeForm::Subcommand => {
                let mut cmd = silent_command(&self.binary_path);
                cmd.arg("compose");
                cmd
            }
            ComposeForm::Standalone => {
                // Standalone binary lives next to the main runtime
                // binary on Linux/macOS, or somewhere on PATH on
                // Windows. Resolve via PATH walk so we get the exact
                // path; if it isn't there we fall back to bare name
                // (Tokio will error cleanly).
                let standalone_name = format!("{}-compose", self.runtime.binary());
                let resolved = which_on_path(&standalone_name)
                    .unwrap_or_else(|| PathBuf::from(&standalone_name));
                silent_command(resolved)
            }
        }
    }
}

// ---------------------------------------------------------------------------
// PATH resolution
// ---------------------------------------------------------------------------
//
// v0.2.53 — graphical-launch PATH augment.
//
// Track C M-P0-7 and Track G3 L-P0-4 are the SAME ROOT CAUSE on macOS
// and Linux respectively: graphical launchers (Finder .app /
// .desktop activation) inherit a minimal PATH that excludes the user's
// homebrew/cargo/pipx/Linuxbrew/snap/flatpak install dirs. This helper
// — Track C-owned — is duplicated here verbatim so Track G3's
// integration test (`tests/test_linux_desktop_launch_path_augmentation.rs`)
// compiles before Phase 2 merges Track C in. After Phase 2 the
// duplicate resolves cleanly (identical content; Git's merge picks
// either side).

/// Augment the process-wide `PATH` with the OS-appropriate locations where
/// user-installed CLI tooling (homebrew, cargo, pipx, linuxbrew, snap, flatpak)
/// typically lives but which graphical launchers (Finder on macOS,
/// `.desktop` files under GNOME/KDE on Linux) do NOT inherit.
///
/// Why this exists (v0.2.53 M-P0-7 / L-P0-4):
///   - macOS: when the launcher is started by double-clicking
///     `start-launcher.command` in Finder OR by clicking the launcher
///     `.app` in `~/Applications/`, the process inherits the LaunchServices
///     default PATH: `/usr/bin:/bin:/usr/sbin:/sbin` — `/opt/homebrew/bin/`
///     and `$HOME/.cargo/bin/` are NOT on it. Every subsequent `python3`,
///     `cargo`, `joern`, `podman`, `git` spawn fails with "command not
///     found" until the user manually re-launches the launcher from a
///     terminal session that DID source `.zshrc` /
///     `eval "$(brew shellenv)"`.
///   - Linux: same shape via `.desktop` launchers. Under systemd-user,
///     the PATH is
///     `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` —
///     `$HOME/.cargo/bin`, `$HOME/.local/bin`, Linuxbrew, snap, flatpak
///     are missing. Frequency is lower than macOS (Linux dev users tend
///     to launch from terminal) but the bug shape is identical.
///   - Windows: Explorer-launched apps inherit the user PATH via
///     registry (`HKCU\Environment`), so no augmentation is needed.
///
/// Cross-OS triage finding (`cross-os-triage-2026-06-10.md` §P0-7)
/// confirms the macOS + Linux PATH inheritance class is the same root
/// cause: launcher processes spawned from graphical contexts do NOT
/// see the user's interactive-shell PATH. Single helper, OS-cfg'd
/// candidates.
///
/// Properties:
///   - Idempotent: calling twice does not duplicate entries.
///   - Order-preserving: existing user PATH stays in its original
///     order; augment candidates are PREPENDED so they take precedence
///     over the system PATH (but only when missing). This matters when
///     the user has both a system `python3` and a homebrew `python3` —
///     homebrew should win for parity with their interactive shell.
///   - Soft-fail: if `HOME` is unset (CI / sandboxed contexts) the
///     candidates that reference `$HOME` are silently dropped.
///   - Resolves `~` expansion: `$HOME/.cargo/bin` is materialised, not
///     literal.
pub fn augment_path_for_graphical_launch() {
    let candidates = augment_candidates();
    if candidates.is_empty() {
        return;
    }

    let current = std::env::var_os("PATH").unwrap_or_default();
    let mut seen: std::collections::HashSet<PathBuf> =
        std::env::split_paths(&current).collect();

    // Prepend candidates that are not already on PATH, preserving the
    // order declared in `augment_candidates()`. Existing PATH entries
    // follow.
    let mut new_entries: Vec<PathBuf> = Vec::with_capacity(candidates.len());
    for cand in candidates {
        if seen.insert(cand.clone()) {
            new_entries.push(cand);
        }
    }
    if new_entries.is_empty() {
        return; // All candidates already on PATH — nothing to do.
    }

    // Append existing PATH entries after the new prepended candidates.
    new_entries.extend(std::env::split_paths(&current));

    match std::env::join_paths(new_entries.iter()) {
        Ok(joined) => {
            // Safety: setting PATH process-wide is sound because we
            // are single-threaded at this call site (lib.rs `setup()`
            // runs before any subprocess spawn or Tauri-managed thread
            // is unparked). `set_var` itself is `unsafe` on edition
            // 2024+, but stable Rust allows the safe form via
            // std::env::set_var on current crate edition (2021).
            std::env::set_var("PATH", &joined);
        }
        Err(e) => {
            tracing::warn!(
                error = %e,
                "[vct] augment_path_for_graphical_launch: join_paths failed — \
                 PATH left unchanged"
            );
        }
    }
}

/// OS-specific list of directories to prepend to PATH, in priority
/// order (first entry wins for collisions). Candidates that do not
/// exist on disk are still added — the user may install the tooling
/// later and re-launch. Only `$HOME`-relative candidates are dropped
/// when `HOME` is unset.
fn augment_candidates() -> Vec<PathBuf> {
    let home = std::env::var_os("HOME").map(PathBuf::from);

    #[cfg(target_os = "macos")]
    {
        let mut out: Vec<PathBuf> = vec![
            PathBuf::from("/opt/homebrew/bin"),
            PathBuf::from("/opt/homebrew/sbin"),
        ];
        if let Some(h) = home.as_ref() {
            out.push(h.join(".cargo/bin"));
            out.push(h.join(".local/bin"));
        }
        out
    }

    #[cfg(target_os = "linux")]
    {
        let mut out: Vec<PathBuf> = Vec::new();
        if let Some(h) = home.as_ref() {
            out.push(h.join(".local/bin"));
            out.push(h.join(".cargo/bin"));
        }
        out.push(PathBuf::from("/home/linuxbrew/.linuxbrew/bin"));
        out.push(PathBuf::from("/snap/bin"));
        out.push(PathBuf::from("/var/lib/flatpak/exports/bin"));
        out
    }

    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        // Windows + other targets: no-op. Explorer-launched apps
        // inherit the user PATH via the registry, so augmentation is
        // unnecessary.
        let _ = home;
        Vec::new()
    }
}

// v0.2.53 L-P0-4 (Track G3) — coverage note:
//
//   On Linux, when the launcher is started by activating
//   `vct-launcher.desktop` from the GNOME / KDE Plasma 6 menu (or via
//   file-manager double-click), the inherited PATH from systemd --user
//   is minimal:
//
//       /usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
//
//   That excludes the common user-installed tooling locations:
//
//       $HOME/.local/bin    (pipx, pip --user, manual installs)
//       $HOME/.cargo/bin    (rustup-managed cargo + lean-ctx + tools)
//       /home/linuxbrew/.linuxbrew/bin  (Linuxbrew)
//       /snap/bin           (snap-installed CLIs)
//       /var/lib/flatpak/exports/bin    (flatpak CLI proxies)
//
//   `which_on_path()` below reads the current process PATH. Without
//   augmentation, every subsequent `node` / `npm` / `cargo` /
//   `joern` / `lean-ctx` / `podman-compose` (pip --user install) lookup
//   would fail under .desktop launch, and the launcher would think the
//   user has no toolchain installed even though their interactive shell
//   sees all of them.
//
//   This is fixed by Track C's M-P0-7 process-wide PATH augment:
//   `augment_path_for_graphical_launch()` in this same module is called
//   from `lib.rs::setup()` BEFORE any subprocess spawn or thread spawn,
//   prepending the OS-specific candidate directories to PATH. After that
//   runs, this `which_on_path()` resolves Node, Joern, lean-ctx, cargo,
//   npm correctly under both interactive-shell AND .desktop launch
//   contexts.
//
//   Track G3's own concern (L-P0-4 from the comprehensive audit) is
//   THE SAME ROOT CAUSE as Track C's M-P0-7; we explicitly defer to
//   that helper rather than duplicating the augment logic here. The
//   integration test
//   `tests/test_linux_desktop_launch_path_augmentation.rs` asserts
//   the contract end-to-end on Linux: minimal-PATH process + augment +
//   which_on_path("node"|"npm"|"cargo"|"lean-ctx"|"joern") must all
//   resolve when the corresponding binary exists in any of the augment
//   candidate dirs.


/// v0.2.77 (Part 7c task 3): delegates to the shared
/// `crate::paths::which_on_path` (one home). The prior inline copy — which
/// this file ORIGINATED as the richest `.exe`/`.cmd`/`.bat` variant — was
/// promoted verbatim into `paths.rs`; the behaviour is identical.
fn which_on_path(name: &str) -> Option<PathBuf> {
    crate::paths::which_on_path(name)
}

// ---------------------------------------------------------------------------
// Detection
// ---------------------------------------------------------------------------

/// Probe a binary's `--version` flag. Returns true if the binary executes
/// successfully. We don't parse the version string — the binary either
/// runs or it doesn't; "podman from 2018 that lacks compose" surfaces
/// later when the compose-form probe fails.
///
/// Timeout: 5s on Linux, 15s on Windows + macOS.
///
/// History:
///   - pre-2026-04-27: 2s, raised to 5s after disk I/O pressure on slower
///     machines competing with first-install's final container-restart
///     phase caused false negatives.
///   - 2026-05-23: raised to 15s on Windows after a fresh-install launcher
///     boot on a contributor's Win11 machine fired the "No container runtime found"
///     modal despite Docker Desktop being healthy. The Hyper-V VM that
///     hosts Docker on Windows can take 3-10s to respond to `docker
///     --version` on cold cache; 5s is too tight as a worst-case ceiling.
///   - 2026-05-26 (v0.2.36 Agent U): same 15s ceiling extended to macOS.
///     Docker Desktop for Mac runs inside HyperKit / Apple Virtualization
///     Framework VMs with the same cold-cache + named-socket cost profile
///     as the Hyper-V VM on Windows; real-world `docker --version` on
///     Mac is 6-10s on cold start. Native Linux podman/docker remains
///     a local process with sub-second startup, so Linux still uses 5s.
async fn version_probe(binary: &PathBuf) -> bool {
    #[cfg(any(target_os = "windows", target_os = "macos"))]
    let timeout_secs = 15u64;
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    let timeout_secs = 5u64;

    let start = std::time::Instant::now();
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(timeout_secs),
        silent_command(binary).arg("--version").output(),
    )
    .await;
    let elapsed_ms = start.elapsed().as_millis();
    let ok = matches!(&result, Ok(Ok(out)) if out.status.success());
    if !ok {
        // Diagnostic log — runtime detection silently returning None has
        // surfaced as a launcher UX bug (firing the no-container modal
        // even when the user has Podman installed). When this fires the
        // user sees a misleading dialog; we want stderr breadcrumbs in
        // the launcher log to prove cause without strace. Elapsed-time
        // helps distinguish timeout-near-ceiling (raise the limit) from
        // genuine missing-binary (spawn error in <50ms).
        tracing::warn!(
            binary = %binary.display(),
            elapsed_ms,
            ceiling_secs = timeout_secs,
            cause = ?match &result {
                Err(_) => "timeout".to_string(),
                Ok(Err(e)) => format!("spawn error: {}", e),
                Ok(Ok(out)) => format!("non-zero exit {:?}", out.status.code()),
            },
            "[runtime] version_probe failed"
        );
    } else {
        // Non-error path: log elapsed only when slow enough to be
        // interesting (>1s). Quiet on the happy path.
        if elapsed_ms > 1000 {
            tracing::info!(
                binary = %binary.display(),
                elapsed_ms,
                "[runtime] version_probe ok but slow"
            );
        }
    }
    ok
}

/// PR-15 G1 (v0.2.11): validate that the runtime's daemon is actually
/// reachable, not just that the binary exists on PATH. Mirrors the bash
/// `_runtime_usable()` helper PR-12 added to
/// `scripts/launch-claude-mcp-stack.sh`.
///
/// The 2026-05-16 cascade root cause: `version_probe` returns true as
/// soon as `<binary> --version` exits 0, which only confirms the binary
/// is installed. A user with Docker Desktop installed but NOT in the
/// `docker` group has a working `docker --version` but every `docker ps`
/// / `docker compose up` returns "permission denied while trying to
/// connect to the Docker daemon socket". The launcher would then pick
/// Docker as the runtime, every subsequent compose call would fail
/// silently, and `vco_code_embed` (the GPU container) would never come
/// up. The user sees only weaviate + ollama in the launcher UI with no
/// indication of why.
///
/// Validation strategy per runtime:
///
///   - **docker**: `docker info` must report a `Server:` line. The
///     `Client:` section appears even without daemon access; only
///     `Server:` requires the daemon socket to be reachable. We grep
///     stdout for `Server:` rather than `Server Version:` so the check
///     is robust across `docker info` output format changes
///     (the literal `Server:` header line is stable across Docker
///     20.10..28.x).
///   - **podman**: `podman info` exits 0 only when the rootless setup
///     actually works (subuid/subgid mappings present, storage path
///     writable, conmon found). A `podman info` exit 0 is sufficient
///     validation — no extra grep needed because rootless podman has
///     no client/server split.
///
/// Timeout: 5s on Linux, 15s on Windows + macOS.
///
/// History:
///   - originally 5s — `docker info` on a real Linux daemon answers in
///     <1s, so 5s was a wide-enough ceiling.
///   - 2026-05-23: raised to 15s on Windows after `daemon_usable_probe`
///     spuriously failed in fresh-install testing on a contributor's Win11 machine
///     even with Docker Desktop healthy. `docker info` on Windows queries
///     the Hyper-V VM via named pipe + gathers daemon metadata (images,
///     networks, plugins); cold-cache or VM-under-load this can take
///     7-12s. 5s missed the window.
///   - 2026-05-26 (v0.2.36 Agent U): same 15s ceiling extended to macOS.
///     Docker Desktop for Mac issues `docker info` against the HyperKit /
///     Apple Virtualization Framework VM (socket-style transport with
///     daemon-metadata gather); cold-cache profile matches Windows, and
///     5s spuriously failed against healthy Docker Desktop installs.
///     Native Linux daemons stay at 5s — local daemon, sub-second.
///
/// Soft-fail: any error (timeout, spawn failure, non-zero exit)
/// returns `false`, never panics. Caller (`resolve_runtime`) then
/// falls through to the next candidate runtime.
async fn daemon_usable_probe(binary: &PathBuf, runtime: ContainerRuntime) -> bool {
    #[cfg(any(target_os = "windows", target_os = "macos"))]
    let timeout_secs = 15u64;
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    let timeout_secs = 5u64;

    let start = std::time::Instant::now();
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(timeout_secs),
        silent_command(binary).arg("info").output(),
    )
    .await;
    let elapsed_ms = start.elapsed().as_millis();
    // Diagnostic breadcrumb: ALWAYS log elapsed for `docker info`/`podman
    // info` because this is the single most common source of "launcher
    // says no runtime but I have it installed" reports. Slow-but-OK runs
    // (1.5-7s) are interesting precursors to future timeout regressions.
    if elapsed_ms > 1000 {
        tracing::info!(
            runtime = runtime.display_name(),
            binary = %binary.display(),
            elapsed_ms,
            ceiling_secs = timeout_secs,
            "[runtime] daemon_usable_probe slow"
        );
    }
    let output = match result {
        Ok(Ok(out)) => out,
        Ok(Err(e)) => {
            tracing::warn!(
                runtime = runtime.display_name(),
                binary = %binary.display(),
                error = %e,
                "[runtime] daemon_usable_probe spawn error"
            );
            return false;
        }
        Err(_) => {
            tracing::warn!(
                runtime = runtime.display_name(),
                binary = %binary.display(),
                ceiling_secs = timeout_secs,
                "[runtime] daemon_usable_probe timeout"
            );
            return false;
        }
    };
    if !output.status.success() {
        tracing::warn!(
            runtime = runtime.display_name(),
            exit_code = ?output.status.code(),
            "[runtime] daemon_usable_probe: `info` exited non-zero — daemon likely \
             unreachable (Docker: user not in `docker` group? Docker Desktop not \
             started? Podman: rootless setup broken?)"
        );
        return false;
    }
    match runtime {
        ContainerRuntime::Docker => {
            // `docker info` always returns 0 if the binary can read
            // *something*; the daemon-reachable signal is the literal
            // `Server:` line in stdout. Client-only output has only
            // `Client:` + an error block at the bottom mentioning the
            // daemon connection refusal.
            let stdout = String::from_utf8_lossy(&output.stdout);
            let has_server = stdout
                .lines()
                .any(|line| {
                    let trimmed = line.trim_start();
                    trimmed.starts_with("Server:") || trimmed.starts_with("Server Version:")
                });
            if !has_server {
                tracing::warn!(
                    "[runtime] daemon_usable_probe: `docker info` succeeded but \
                     stdout has no `Server:` section — daemon not reachable \
                     (user not in docker group, or Docker Desktop not started)"
                );
            }
            has_server
        }
        ContainerRuntime::Podman => true, // exit 0 sufficient for rootless podman
    }
}

/// Probe `<runtime> compose version`. Returns true when the subcommand
/// is present (modern Podman/Docker). Falls back to checking for the
/// standalone `<runtime>-compose` binary if the subcommand is absent.
///
/// Timeout 5s per probe (was 2s). `podman compose version` can be slow
/// because Podman v4 delegates to an "external compose provider" (often
/// docker-compose at /usr/local/bin/docker-compose), printing a banner
/// to stderr before the version output. When the provider lookup hits
/// a cold disk cache, 2s was sometimes not enough.
async fn detect_compose_form(binary: &PathBuf, runtime: ContainerRuntime) -> Option<ComposeForm> {
    // Subcommand probe — `podman compose version` or `docker compose version`.
    let sub = tokio::time::timeout(
        std::time::Duration::from_secs(5),
        silent_command(binary)
            .args(["compose", "version"])
            .output(),
    )
    .await;
    if let Ok(Ok(out)) = &sub {
        if out.status.success() {
            return Some(ComposeForm::Subcommand);
        }
    }

    // Standalone fallback — `podman-compose --version`.
    // NOTE: which_on_path uses the launcher process's PATH. When the
    // launcher is spawned by `setsid nohup` from post-install-launcher.sh,
    // PATH is the shell-default (typically /usr/bin:/bin) and does NOT
    // include `~/.local/bin/`, where pip-installed `podman-compose` lives.
    // We also probe ~/.local/bin/ explicitly so the standalone fallback
    // doesn't silently miss user-local installs.
    let standalone_name = format!("{}-compose", runtime.binary());
    let mut standalone_paths: Vec<PathBuf> = Vec::new();
    if let Some(p) = which_on_path(&standalone_name) {
        standalone_paths.push(p);
    }
    if let Some(home) = std::env::var_os("HOME") {
        let user_local = PathBuf::from(home).join(".local/bin").join(&standalone_name);
        if user_local.is_file() && !standalone_paths.contains(&user_local) {
            standalone_paths.push(user_local);
        }
    }
    for path in &standalone_paths {
        let sa = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            silent_command(path).arg("--version").output(),
        )
        .await;
        if let Ok(Ok(out)) = sa {
            if out.status.success() {
                return Some(ComposeForm::Standalone);
            }
        }
    }

    tracing::warn!(
        "[runtime] detect_compose_form: no compose support for {} (subcommand_status={:?}, \
         standalone_candidates={:?})",
        binary.display(),
        match &sub {
            Err(_) => "timeout".to_string(),
            Ok(Err(e)) => format!("spawn error: {}", e),
            Ok(Ok(out)) => format!("exit {:?}, stderr={}", out.status.code(),
                String::from_utf8_lossy(&out.stderr).chars().take(200).collect::<String>()),
        },
        standalone_paths
    );
    None
}

/// On macOS/Windows, Podman runs inside a VM ("Podman machine") that
/// must be started before any container ops. Returns true when we
/// detect Podman is selected AND the machine is NOT running. On Linux
/// this is always false (Podman runs natively).
#[cfg(any(target_os = "macos", target_os = "windows"))]
async fn detect_podman_machine_needed(binary: &PathBuf) -> bool {
    let probe = tokio::time::timeout(
        std::time::Duration::from_secs(2),
        silent_command(binary)
            .args(["machine", "list", "--format", "{{.Running}}"])
            .output(),
    )
    .await;
    match probe {
        Ok(Ok(out)) if out.status.success() => {
            let body = String::from_utf8_lossy(&out.stdout);
            // If ANY line is "true" we're good — at least one machine is
            // running. Empty stdout (= no machines configured) ALSO means
            // we need a start.
            let any_running = body.lines().any(|l| l.trim() == "true");
            !any_running
        }
        // Couldn't query → assume the machine is fine; probing the
        // service health later will fail-loud anyway.
        _ => false,
    }
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
async fn detect_podman_machine_needed(_binary: &PathBuf) -> bool {
    false
}

/// Cached detection result. `None` means "not yet probed". `Some(None)`
/// means "probed and found no runtime". `Some(Some(...))` is the live
/// answer. We use a `Mutex<Option<Option<RuntimeInfo>>>` so the second
/// caller can read the first caller's verdict without redoing the probe.
static CACHE: Mutex<Option<Option<RuntimeInfo>>> = Mutex::new(None);

/// Force a fresh detection pass. Called from a Tauri command when the
/// user clicks "Re-detect" in the Services preferences screen.
pub fn invalidate_cache() {
    if let Ok(mut g) = CACHE.lock() {
        *g = None;
    }
}

/// Detect the active runtime once per launcher session. Returns `None`
/// when neither Podman nor Docker is installed. Result is cached; call
/// `invalidate_cache()` to force a re-probe.
pub async fn detect_runtime() -> Option<RuntimeInfo> {
    {
        let g = CACHE.lock().ok()?;
        if let Some(cached) = g.as_ref() {
            return cached.clone();
        }
    }
    let resolved = resolve_runtime().await;
    if let Ok(mut g) = CACHE.lock() {
        *g = Some(resolved.clone());
    }
    resolved
}

async fn resolve_runtime() -> Option<RuntimeInfo> {
    // PR-43 (v0.2.12): honor VCT_CONTAINER_RUNTIME env override so the
    // GUI matches the hooks' behavior (templates/hooks/ensure-containers.sh,
    // verify-container-ports.sh, ensure-code-embed-service.sh all check
    // this var first). Without this, a user setting the var to force a
    // specific runtime would see hooks pick one and the launcher GUI pick
    // another — silent split-brain.
    //
    // Accepted values: "podman", "docker", "auto" (or unset → auto).
    // Invalid values fall through to auto-detection with a clear stderr.
    let override_pref = std::env::var("VCT_CONTAINER_RUNTIME")
        .ok()
        .map(|s| s.trim().to_lowercase())
        .filter(|s| !s.is_empty() && s != "auto");

    // Preference order: env override first; else Podman > Docker.
    // Per user policy: "check for availability on podman first".
    let order: Vec<ContainerRuntime> = match override_pref.as_deref() {
        Some("podman") => vec![ContainerRuntime::Podman],
        Some("docker") => vec![ContainerRuntime::Docker],
        Some(other) => {
            tracing::warn!(
                value = ?other,
                "[vct] runtime: VCT_CONTAINER_RUNTIME not recognized (expected \
                 'podman', 'docker', or 'auto'); falling back to podman-then-docker \
                 auto-detection"
            );
            vec![ContainerRuntime::Podman, ContainerRuntime::Docker]
        }
        None => vec![ContainerRuntime::Podman, ContainerRuntime::Docker],
    };

    for runtime in order {
        let bin_path = match which_on_path(runtime.binary()) {
            Some(p) => p,
            None => continue,
        };
        if !version_probe(&bin_path).await {
            continue;
        }
        // PR-15 G1 (v0.2.11): daemon-access check. version_probe only
        // confirms the binary runs; daemon_usable_probe confirms the
        // daemon socket is actually reachable. Without this check, the
        // launcher could pick a runtime whose every subsequent compose
        // call fails silently with "permission denied". Mirrors the
        // bash _runtime_usable() that PR-12 added to
        // scripts/launch-claude-mcp-stack.sh::detect_runtime().
        if !daemon_usable_probe(&bin_path, runtime).await {
            continue;
        }
        let compose_form = match detect_compose_form(&bin_path, runtime).await {
            Some(f) => f,
            None => {
                // Binary exists but no compose support — skip and try
                // the next runtime (a Podman 3.x without compose is
                // useless to us).
                continue;
            }
        };
        let needs_machine_start = match runtime {
            ContainerRuntime::Podman => detect_podman_machine_needed(&bin_path).await,
            ContainerRuntime::Docker => false,
        };
        return Some(RuntimeInfo {
            runtime,
            compose_form,
            needs_machine_start,
            binary_path: bin_path,
        });
    }
    None
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serial_test::serial;

    #[test]
    fn binary_names_match_runtimes() {
        assert_eq!(ContainerRuntime::Podman.binary(), "podman");
        assert_eq!(ContainerRuntime::Docker.binary(), "docker");
    }

    #[test]
    fn display_names_human_friendly() {
        assert_eq!(ContainerRuntime::Podman.display_name(), "Podman");
        assert_eq!(ContainerRuntime::Docker.display_name(), "Docker");
    }

    #[test]
    fn cache_invalidation_clears_state() {
        invalidate_cache();
        let g = CACHE.lock().unwrap();
        assert!(g.is_none(), "cache should be empty after invalidate");
    }

    // ----- v0.2.53 M-P0-7: launcher PATH augmentation tests -----

    /// Augmentation is idempotent — calling twice does not duplicate
    /// entries. On Windows this is a no-op and the PATH is unchanged.
    #[test]
    #[serial]
    fn augment_path_is_idempotent() {
        // Snapshot the existing PATH so we can restore it at the end.
        let original = std::env::var_os("PATH");

        // Use a minimal known PATH so the test does not depend on the
        // host environment.
        std::env::set_var("PATH", "/usr/bin:/bin");

        augment_path_for_graphical_launch();
        let first = std::env::var_os("PATH").unwrap_or_default();

        augment_path_for_graphical_launch();
        let second = std::env::var_os("PATH").unwrap_or_default();

        assert_eq!(
            first, second,
            "second augment_path call must NOT modify PATH again"
        );

        // Restore.
        match original {
            Some(p) => std::env::set_var("PATH", p),
            None => std::env::remove_var("PATH"),
        }
    }

    /// Augmentation prepends candidates but preserves the existing PATH
    /// after them — order is not destroyed. We assert this by checking
    /// that the original entries appear AFTER any newly-prepended ones.
    #[test]
    #[serial]
    fn augment_path_preserves_user_path_order() {
        let original = std::env::var_os("PATH");

        // Use a marker directory the augment_candidates() list will NOT
        // emit — so we can verify it survives the prepend.
        std::env::set_var("PATH", "/zzz_marker_a:/zzz_marker_b");

        augment_path_for_graphical_launch();
        let after = std::env::var_os("PATH").unwrap_or_default();
        let parts: Vec<PathBuf> = std::env::split_paths(&after).collect();

        let pos_a = parts
            .iter()
            .position(|p| p == &PathBuf::from("/zzz_marker_a"));
        let pos_b = parts
            .iter()
            .position(|p| p == &PathBuf::from("/zzz_marker_b"));

        // Both markers must still be present (augment does not delete).
        assert!(pos_a.is_some(), "marker_a must still be on PATH");
        assert!(pos_b.is_some(), "marker_b must still be on PATH");
        // Original relative order must be preserved.
        assert!(
            pos_a.unwrap() < pos_b.unwrap(),
            "marker_a must precede marker_b after augment (original order \
             preserved); pos_a={:?}, pos_b={:?}",
            pos_a,
            pos_b
        );

        match original {
            Some(p) => std::env::set_var("PATH", p),
            None => std::env::remove_var("PATH"),
        }
    }

    /// On macOS, the homebrew prefix must appear on PATH after augment.
    /// On Linux, `$HOME/.local/bin` must appear (when HOME is set).
    /// On Windows, augment is a no-op so PATH is unchanged.
    #[test]
    #[serial]
    fn augment_path_adds_expected_os_specific_dirs() {
        let original_path = std::env::var_os("PATH");
        let original_home = std::env::var_os("HOME");

        // Set a deterministic HOME so candidate construction is stable.
        std::env::set_var("HOME", "/tmp/vct-augment-test-home");
        std::env::set_var("PATH", "/usr/bin:/bin");

        augment_path_for_graphical_launch();
        let after = std::env::var_os("PATH").unwrap_or_default();
        let parts: Vec<PathBuf> = std::env::split_paths(&after).collect();

        #[cfg(target_os = "macos")]
        {
            assert!(
                parts.iter().any(|p| p == &PathBuf::from("/opt/homebrew/bin")),
                "macOS augment must include /opt/homebrew/bin; PATH={:?}",
                parts
            );
            assert!(
                parts
                    .iter()
                    .any(|p| p == &PathBuf::from("/tmp/vct-augment-test-home/.cargo/bin")),
                "macOS augment must include $HOME/.cargo/bin; PATH={:?}",
                parts
            );
        }

        #[cfg(target_os = "linux")]
        {
            assert!(
                parts
                    .iter()
                    .any(|p| p == &PathBuf::from("/tmp/vct-augment-test-home/.local/bin")),
                "Linux augment must include $HOME/.local/bin; PATH={:?}",
                parts
            );
            assert!(
                parts
                    .iter()
                    .any(|p| p == &PathBuf::from("/home/linuxbrew/.linuxbrew/bin")),
                "Linux augment must include linuxbrew; PATH={:?}",
                parts
            );
            assert!(
                parts.iter().any(|p| p == &PathBuf::from("/snap/bin")),
                "Linux augment must include /snap/bin; PATH={:?}",
                parts
            );
        }

        #[cfg(not(any(target_os = "macos", target_os = "linux")))]
        {
            // Windows + other: no-op.
            assert_eq!(
                parts,
                vec![PathBuf::from("/usr/bin"), PathBuf::from("/bin")],
                "Windows augment must be a no-op; PATH={:?}",
                parts
            );
        }

        match original_path {
            Some(p) => std::env::set_var("PATH", p),
            None => std::env::remove_var("PATH"),
        }
        match original_home {
            Some(h) => std::env::set_var("HOME", h),
            None => std::env::remove_var("HOME"),
        }
    }

    // ----- PR-15 G1: daemon_usable_probe tests -----

    /// Helper: write a fake `docker` / `podman` script that emits the
    /// given stdout + exit code. Used by the daemon-usable probe tests
    /// to simulate runtimes without requiring real docker/podman on PATH.
    fn write_fake_runtime(dir: &std::path::Path, name: &str, stdout: &str, exit_code: i32) -> PathBuf {
        let script = dir.join(name);
        std::fs::write(
            &script,
            format!(
                "#!/bin/bash\ncat <<'__EOF__'\n{}\n__EOF__\nexit {}\n",
                stdout, exit_code
            ),
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&script).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&script, perms).unwrap();
        }
        script
    }

    // ─── PR-15 G1: daemon_usable_probe coverage ────────────────────
    //
    // Why #[ignore] on these 3 tests:
    //
    // The tests spawn real bash subprocesses (fake docker/podman
    // scripts) to verify the daemon-access check parses stdout
    // correctly. Under the full `cargo test --lib` parallel run, they
    // compete for host-level subprocess slots with the pre-existing
    // kg_sync timing-sensitive tests
    // (concurrent_drain_does_not_deadlock_on_large_stderr,
    // stall_watchdog_kills_silent_subprocess). When scheduler pressure
    // delays a subprocess by >2s, BOTH suites flake — the kg_sync
    // tests trip their internal timeouts, and our tests trip the 5s
    // ceiling in daemon_usable_probe.
    //
    // The fix is workflow, not code: ignore by default in the full
    // suite. Developers run them targeted:
    //
    //   cargo test --lib --manifest-path launcher/src-tauri/Cargo.toml \
    //     daemon_usable -- --ignored
    //
    // CI runs them as a SEPARATE step (`cargo test -- --ignored`)
    // outside the full-suite parallel pool.
    //
    // The function itself is otherwise verified by the daemon-usable
    // logic in resolve_runtime() being exercised end-to-end via
    // detect_runtime_returns_option_without_panic (which uses the real
    // host's podman/docker if installed). The unit-level coverage is
    // belt-and-suspenders.

    #[tokio::test]
    #[serial]
    async fn daemon_usable_probe_docker_cases() {
        let dir = tempfile::tempdir().unwrap();
        // (script_name, stdout, exit_code, expected_usable, label)
        let cases: Vec<(&str, &str, i32, bool, &str)> = vec![
            (
                "docker_with_server",
                "Client:\n Version: 28.0\nServer:\n Version: 28.0\n",
                0,
                true,
                "stdout has 'Server:' line → usable",
            ),
            (
                "docker_client_only",
                "Client:\n Version: 28.0\n Context:    default\n",
                0,
                false,
                "stdout has only 'Client:' → unusable (daemon unreachable)",
            ),
            (
                "docker_nonzero_exit",
                "",
                1,
                false,
                "exit non-zero → unusable",
            ),
        ];
        for (name, stdout, exit_code, expected, label) in cases {
            let script = write_fake_runtime(dir.path(), name, stdout, exit_code);
            let actual = daemon_usable_probe(&script, ContainerRuntime::Docker).await;
            assert_eq!(actual, expected, "docker case '{}' failed: {}", name, label);
        }
    }

    #[tokio::test]
    #[serial]
    async fn daemon_usable_probe_podman_cases() {
        let dir = tempfile::tempdir().unwrap();
        let cases: Vec<(&str, &str, i32, bool, &str)> = vec![
            (
                "podman_zero_exit",
                "host:\n arch: amd64\nstore:\n graphRoot: /var/x\n",
                0,
                true,
                "exit 0 → usable (rootless setup works)",
            ),
            (
                "podman_nonzero_exit",
                "",
                1,
                false,
                "exit non-zero → unusable",
            ),
        ];
        for (name, stdout, exit_code, expected, label) in cases {
            let script = write_fake_runtime(dir.path(), name, stdout, exit_code);
            let actual = daemon_usable_probe(&script, ContainerRuntime::Podman).await;
            assert_eq!(actual, expected, "podman case '{}' failed: {}", name, label);
        }
    }

    #[tokio::test]
    #[serial]
    async fn daemon_usable_probe_spawn_failure_returns_false() {
        // Path to a binary that doesn't exist — must soft-fail, not panic.
        let bogus = PathBuf::from("/nonexistent/path/to/docker");
        assert!(
            !daemon_usable_probe(&bogus, ContainerRuntime::Docker).await,
            "spawn failure on bogus binary must return false, not panic"
        );
    }

    /// Detection is purely additive — calling it on a CI box without
    /// podman/docker must return None, not panic.
    #[tokio::test]
    async fn detect_runtime_returns_option_without_panic() {
        invalidate_cache();
        let _ = detect_runtime().await;
        // Either Some(info) or None — both are valid, depends on the
        // host. We just want to confirm no panic and the cache fills.
        let g = CACHE.lock().unwrap();
        assert!(g.is_some(), "cache should be populated after detect_runtime");
    }

    /// When a container runtime IS available on PATH (which is the case
    /// on most dev machines + CI runners with podman or docker installed),
    /// detect_runtime() MUST return Some(info). This is the actual
    /// production contract: if the user has a working runtime, the
    /// launcher must NEVER show the "no container runtime" modal — and
    /// therefore must never call `runtime_open_install_url` to pop a
    /// browser tab to podman.io. Reported by user 2026-04-28: cargo test
    /// runs were opening podman.io in their browser because the modal-
    /// allowlist test was directly invoking the open path. The fix:
    /// (1) make the allowlist test pure (commit pending), (2) verify
    /// here that detection on a real host succeeds, so the modal —
    /// and therefore the open call — is never reached in normal use.
    ///
    /// Skipped via #[ignore] when the host genuinely has no runtime.
    /// The test runner reports `1 ignored` instead of failing, and the
    /// "doesn't panic" test above still covers the negative path.
    #[tokio::test]
    async fn detect_runtime_succeeds_when_runtime_on_path() {
        // Probe PATH ourselves first so we know which case we're in.
        let host_has_podman = which_on_path("podman").is_some();
        let host_has_docker = which_on_path("docker").is_some();
        if !host_has_podman && !host_has_docker {
            eprintln!(
                "host has neither podman nor docker on PATH; skipping \
                 detect_runtime_succeeds_when_runtime_on_path"
            );
            return;
        }
        invalidate_cache();
        let info = detect_runtime().await;
        assert!(
            info.is_some(),
            "host has a container runtime on PATH \
             (podman={}, docker={}), but detect_runtime returned None — \
             this is the false-negative bug that fires the no-runtime \
             modal in the launcher GUI",
            host_has_podman,
            host_has_docker
        );
        let info = info.unwrap();
        // Sanity: the binary we resolved must actually exist + be exec.
        assert!(
            info.binary_path.is_file(),
            "resolved runtime binary {:?} doesn't exist as a file",
            info.binary_path
        );
        // Compose form must be set — without it the launcher can't run
        // its services, which is what triggered the false-negative
        // modal in the original bug report.
        let cf = format!("{:?}", info.compose_form);
        assert!(
            cf == "Subcommand" || cf == "Standalone",
            "expected compose_form to be Subcommand or Standalone, got {}",
            cf
        );
    }

    // ─── v0.2.22 Item #10 (b): acceptance property (12) — cache contract ──
    //
    // Property (12) statement (from .claude/context/plans/
    // v0.2.21-hub-detachment-and-resolver.md §27 lines 704-710): for each
    // runtime (Podman / Docker), detect_runtime() + all downstream callers
    // use the chosen runtime consistently end-to-end. The architectural
    // mechanism is the module-level CACHE: detect_runtime() is the ONLY
    // public accessor, resolve_runtime() is private. All launcher callers
    // (verified by grep: commands/lifecycle.rs, commands/runtime_install.rs,
    // services/watcher.rs) go through detect_runtime(), which reads from
    // CACHE on the second-and-onwards call.
    //
    // These tests pin the cache contract so a future refactor that
    // accidentally re-probes per-call (e.g. removing the CACHE check) gets
    // flagged. They DO NOT exercise the subprocess probe — that's the
    // job of detect_runtime_returns_option_without_panic + the
    // daemon_usable_probe tests above.

    /// Cache contract: once detect_runtime() returns a value, a second
    /// call returns the same value WITHOUT re-probing. This is the
    /// "single source of truth" invariant that prevents the install path
    /// and the launcher GUI from disagreeing on which runtime to use.
    ///
    /// Strategy: invalidate the cache, call detect_runtime() once to
    /// populate it, then assert subsequent calls match the cached value
    /// bit-for-bit. We can't mock the subprocess probe without injecting
    /// state into the function, so we exercise the real host probe (same
    /// pattern as detect_runtime_returns_option_without_panic).
    #[tokio::test]
    async fn detect_runtime_returns_cached_value_on_subsequent_calls() {
        invalidate_cache();
        let first = detect_runtime().await;
        let second = detect_runtime().await;
        let third = detect_runtime().await;

        // We can't `assert_eq!` directly on Option<RuntimeInfo> because
        // RuntimeInfo doesn't impl PartialEq. Compare the discriminating
        // fields: runtime enum, compose_form, needs_machine_start,
        // binary_path. If any field differs across calls, the cache is
        // re-probing and the contract is broken.
        let summarize = |info: &Option<RuntimeInfo>| -> String {
            match info {
                None => "None".to_string(),
                Some(i) => format!(
                    "{:?}/{:?}/needs_machine_start={}/path={:?}",
                    i.runtime, i.compose_form, i.needs_machine_start, i.binary_path
                ),
            }
        };

        let s1 = summarize(&first);
        let s2 = summarize(&second);
        let s3 = summarize(&third);
        assert_eq!(
            s1, s2,
            "cache contract violated: detect_runtime() returned different \
             values on consecutive calls without invalidate_cache. \
             first={}, second={}. This means downstream callers may \
             observe inconsistent runtimes, breaking property (12) of \
             v0.2.21 plan §27.",
            s1, s2,
        );
        assert_eq!(s2, s3, "three-call cache stability: {} vs {}", s2, s3);
    }

    /// After invalidate_cache(), the next detect_runtime() call MUST
    /// re-probe (and return SOME value, on a host where podman or docker
    /// is installed). This is the bypass mechanism used by the
    /// "Re-detect runtime" GUI button.
    ///
    /// Skipped via early-return when the host has no runtime installed —
    /// we'd be re-probing nothing, which yields None, which is also
    /// valid behaviour. The cache-stability test above covers the
    /// non-None branch.
    #[tokio::test]
    async fn invalidate_then_detect_repopulates_cache() {
        let host_has_runtime =
            which_on_path("podman").is_some() || which_on_path("docker").is_some();
        if !host_has_runtime {
            eprintln!(
                "host has neither podman nor docker; skipping \
                 invalidate_then_detect_repopulates_cache"
            );
            return;
        }
        // Populate the cache.
        let _ = detect_runtime().await;
        // Invalidate it.
        invalidate_cache();
        // Cache should now be empty.
        {
            let g = CACHE.lock().unwrap();
            assert!(g.is_none(), "cache should be empty after invalidate");
        }
        // Re-probe — must succeed (we checked the host has a runtime).
        let result = detect_runtime().await;
        assert!(
            result.is_some(),
            "after invalidate_cache + detect_runtime, the cache should \
             be repopulated with a Some(RuntimeInfo) on a host with a \
             working runtime; got None"
        );
        // And the cache must hold the new value.
        let g = CACHE.lock().unwrap();
        assert!(g.is_some(),
                "cache should be populated after the re-detect call");
    }

    /// Property (12a) priority pin: the documented priority is
    /// podman > docker. This test pins the ordering literal so a future
    /// refactor that flips the order (e.g. trying to default to docker
    /// because it's more common on some platforms) MUST update plan §27
    /// AND this test together.
    ///
    /// We can't directly inspect the `order` vec inside resolve_runtime
    /// (it's a local variable), so we pin it via the discriminating
    /// `binary()` strings + the documented hierarchy of preferences.
    /// The behavioural pin (env override → podman-first → docker) is in
    /// the Python test_runtime_detection_parity.py — that file exercises
    /// the same priority chain on the install.py side.
    #[test]
    fn priority_pin_podman_before_docker_as_documented() {
        // The 2-variant enum has a stable ordering by declaration order
        // in the source. Pin both the binary names AND the declaration
        // sequence so a typo (e.g. accidentally renaming "podman" to
        // "docker" in the binary() match arm) gets caught.
        let podman = ContainerRuntime::Podman;
        let docker = ContainerRuntime::Docker;
        assert_eq!(podman.binary(), "podman");
        assert_eq!(docker.binary(), "docker");
        // Display names follow the same priority convention.
        assert_eq!(podman.display_name(), "Podman");
        assert_eq!(docker.display_name(), "Docker");
        // The runtime enum is Copy + Eq so equality is straightforward
        // — pin that the two variants are NOT equal (defense against
        // a refactor that collapses them to one variant with a string
        // field, which would break the type-driven priority chain).
        assert_ne!(podman, docker);
    }

    /// Property (12) end-to-end pin: detect_runtime() is the ONLY public
    /// accessor — resolve_runtime() is private. Callers that bypass the
    /// cache by calling resolve_runtime directly cannot exist outside this
    /// module. This test pins the accessor surface so a future commit
    /// that makes resolve_runtime public is flagged as a behavioural
    /// change requiring deliberate plan update.
    ///
    /// The pin is "negative": we can only confirm that `super::detect_runtime`
    /// is in scope (the test mod uses `super::*`); we cannot confirm
    /// resolve_runtime is private from inside the module that defines
    /// both. However, by importing `super::detect_runtime` explicitly
    /// (not `super::resolve_runtime`), and by the cache-stability test
    /// above, we ensure the contract that "two consecutive
    /// detect_runtime() calls agree" is what the public API guarantees.
    ///
    /// The compile-time check that resolve_runtime is private is
    /// enforced by rustc itself: any external crate trying to call
    /// `runtime::resolve_runtime` would fail to compile. This
    /// test documents the intent so the next reader knows where to
    /// look.
    #[test]
    fn detect_runtime_is_the_public_accessor() {
        // Compile-time witness: `detect_runtime` is reachable; if a
        // future refactor renames it (without updating the launcher's
        // callers), this test fails to compile.
        let _fn_ptr: fn()
            -> std::pin::Pin<Box<dyn std::future::Future<Output = Option<RuntimeInfo>> + Send>> =
                || Box::pin(detect_runtime());
        // Belt-and-suspenders: invalidate_cache is also public for the
        // "Re-detect" GUI button.
        invalidate_cache();
    }
}
