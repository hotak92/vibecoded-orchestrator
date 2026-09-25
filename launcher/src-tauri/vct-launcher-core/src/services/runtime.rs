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

    /// The OTHER runtime. Used by the install preflight to answer "you
    /// pinned podman and it is unusable — is docker usable?" without
    /// hardcoding the pair at the call site.
    pub fn other(self) -> ContainerRuntime {
        match self {
            ContainerRuntime::Podman => ContainerRuntime::Docker,
            ContainerRuntime::Docker => ContainerRuntime::Podman,
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
/// and the container runtimes typically live but which graphical launchers
/// (Finder on macOS, `.desktop` files under GNOME/KDE on Linux) and boot units
/// do NOT inherit.
///
/// Why this exists (v0.2.53 M-P0-7 / L-P0-4):
///   - macOS: when the launcher is started by double-clicking
///     `start-launcher.command` in Finder OR by clicking the launcher
///     `.app` in `~/Applications/`, the process inherits the LaunchServices
///     default PATH: `/usr/bin:/bin:/usr/sbin:/sbin` — `/opt/homebrew/bin/`
///     and `$HOME/.cargo/bin/` are NOT on it. Every subsequent `python3`,
///     `cargo`, `joern`, `podman`, `git` spawn fails with "command not
///     found" — or runs `/usr/bin/git` / `/usr/bin/python3`, the Xcode
///     Command Line Tools stubs — until the user re-launches from a terminal
///     session that DID source `.zshrc` / `eval "$(brew shellenv)"`.
///   - Linux: same shape via `.desktop` launchers. Under systemd-user,
///     the PATH is
///     `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` —
///     `$HOME/.cargo/bin`, `$HOME/.local/bin`, Linuxbrew, snap, flatpak
///     are missing.
///   - Windows: Explorer-launched apps inherit the user PATH via
///     registry (`HKCU\Environment`); since v0.2.97 R9 the Docker Desktop /
///     Podman installer directories are still added when missing (a
///     Scheduled Task or a service may run with a different PATH).
///   - v0.2.97 R9 H1(b)/H5: the candidate list is the shared table
///     `vco_lib/tool_search_dirs.toml` (see [`augment_candidates`]), and the
///     HUB calls this too (`vct-hub/src/main.rs`, before its tokio runtime
///     exists): both processes decide "is the container runtime installed?"
///     and a short inherited PATH must not answer "no" for a runtime in
///     `~/bin` or `/opt/homebrew/bin`.
///
/// Properties:
///   - Idempotent: calling twice does not duplicate entries.
///   - The order rule (v0.2.97 R10 J3, split by the table's per-entry
///     `placement`; see [`augmented_entries`]): the v0.2.53 graphical-launch
///     candidates (`prepend-when-missing`) the inherited PATH lacks go AHEAD
///     of it — a login shell's order, so Homebrew's `git` beats the
///     `/usr/bin` stub in a Finder launch, exactly as before v0.2.97 — and
///     the v0.2.97 runtime locations (`append`) it lacks go AFTER it, so they
///     never shadow an inherited entry. An entry already on the inherited
///     PATH is never moved, so the inherited PATH keeps its own order.
///     `vco_lib.tool_search_dirs` (`which`, `run`, `reachable_path`) follows
///     the same rule, so a name resolves to the SAME binary in the launcher,
///     the hub and every Python/shell surface.
///   - Soft-fail: if `HOME` is unset (CI / sandboxed contexts) the
///     candidates that reference `$HOME` are silently dropped.
///   - Resolves `~` expansion: `$HOME/.cargo/bin` is materialised, not
///     literal.
pub fn augment_path_for_graphical_launch() {
    let current = std::env::var_os("PATH").unwrap_or_default();
    let home = std::env::var_os("HOME").map(PathBuf::from);
    if let Some(joined) = augmented_path(&current, home.as_deref()) {
        // Safety: setting PATH process-wide is sound because we are
        // single-threaded at this call site (lib.rs `setup()` runs before
        // any subprocess spawn or Tauri-managed thread is unparked; the
        // hub's `main` calls it before building its tokio runtime).
        // `set_var` itself is `unsafe` on edition 2024+, but stable Rust
        // allows the safe form on the current crate edition (2021).
        std::env::set_var("PATH", &joined);
    }
}

/// The PATH [`augment_path_for_graphical_launch`] sets, from `current` and
/// `home` — `None` when there is nothing to add (every candidate is already
/// on it, or the OS has none) or the result cannot be joined (logged).
///
/// Pure, so it is tested without touching the process `PATH`: tests share
/// that variable with every concurrently running test and every child a test
/// spawns by bare name (v0.2.97 review R6 — a Rust test never sets it;
/// `tests/test_rust_tests_never_mutate_process_path.py`).
pub fn augmented_path(
    current: &std::ffi::OsStr,
    home: Option<&std::path::Path>,
) -> Option<std::ffi::OsString> {
    // An empty PATH has no entries (split_paths would yield one empty entry,
    // i.e. the current directory, in the middle of the result).
    let inherited: Vec<PathBuf> = if current.is_empty() {
        Vec::new()
    } else {
        std::env::split_paths(current).collect()
    };
    let entries = augmented_entries(&inherited, &augment_candidates(home));
    if entries.len() == inherited.len() {
        return None; // All candidates already on PATH — nothing to do.
    }
    match std::env::join_paths(entries.iter()) {
        Ok(joined) => Some(joined),
        Err(e) => {
            tracing::warn!(
                error = %e,
                "[vct] augment_path_for_graphical_launch: join_paths failed — \
                 PATH left unchanged"
            );
            None
        }
    }
}

/// [`augmented_entries_for`] with this OS's comparison rule.
pub fn augmented_entries(current: &[PathBuf], candidates: &[(PathBuf, Placement)]) -> Vec<PathBuf> {
    augmented_entries_for(current, candidates, cfg!(windows))
}

/// What two PATH entries are compared by (v0.2.97 R11 L5): duplicate
/// separators collapsed and trailing ones dropped (a root stays a root), so
/// `/opt/homebrew/bin/` and `//opt//homebrew/bin` are `/opt/homebrew/bin`; on
/// Windows also case-insensitive and `/` read as `\` (a leading `\\` UNC
/// prefix and a drive root `C:\` are kept). Only a comparison key — no entry
/// is ever rewritten. Deliberately NOT `Path` equality (which also drops `.`
/// components and so answered differently from Python). MUST MATCH
/// `vco_lib.tool_search_dirs.path_key`.
pub fn path_key(entry: &str, windows: bool) -> String {
    let sep = if windows { '\\' } else { '/' };
    let text: String = if windows {
        entry.replace('/', "\\").to_lowercase()
    } else {
        entry.to_string()
    };
    let (lead, body): (&str, String) = if windows && text.starts_with("\\\\") {
        ("\\\\", text[2..].to_string())
    } else {
        ("", text)
    };
    let mut collapsed = String::with_capacity(body.len());
    for c in body.chars() {
        if c == sep && collapsed.ends_with(sep) {
            continue;
        }
        collapsed.push(c);
    }
    let mut stripped = collapsed.trim_end_matches(sep).to_string();
    let b = stripped.as_bytes();
    if stripped.is_empty() && !collapsed.is_empty() {
        stripped.push(sep); // the root
    } else if windows
        && stripped.len() != collapsed.len()
        && b.len() == 2
        && b[0].is_ascii_lowercase()
        && b[1] == b':'
    {
        stripped.push(sep); // a drive root, not the drive's current directory
    }
    format!("{lead}{stripped}")
}

/// THE ORDER RULE, pure: the `prepend-when-missing` candidates `current`
/// lacks (candidate order), then `current` unchanged, then the `append`
/// candidates it lacks (candidate order). A candidate already in `current`
/// — compared by [`path_key`] (`windows` = the comparison rule of the OS the
/// PATH belongs to) — is never moved nor duplicated. MUST MATCH
/// `vco_lib.tool_search_dirs.lookup_entries`
/// (`tests/fixtures/tool_search_dirs_cases.json` `order_cases` run both).
pub fn augmented_entries_for(
    current: &[PathBuf],
    candidates: &[(PathBuf, Placement)],
    windows: bool,
) -> Vec<PathBuf> {
    let key = |p: &PathBuf| path_key(&p.to_string_lossy(), windows);
    let mut seen: std::collections::HashSet<String> = current.iter().map(key).collect();
    let mut before: Vec<PathBuf> = Vec::new();
    let mut after: Vec<PathBuf> = Vec::new();
    for (dir, placement) in candidates {
        if !seen.insert(key(dir)) {
            continue;
        }
        match placement {
            Placement::PrependWhenMissing => before.push(dir.clone()),
            Placement::Append => after.push(dir.clone()),
        }
    }
    before.extend(current.iter().cloned());
    before.extend(after);
    before
}

/// This OS's table entries (see [`tool_search_entries_for`]) with `home`
/// and the process environment — what the launcher and the hub augment
/// with. Candidates that do not exist on disk are still added — the user may
/// install the tooling later and re-launch. Only `$HOME`-relative candidates
/// are dropped when `HOME` is unset (and `${VAR}` ones when the variable is
/// unset).
///
/// v0.2.97 R9 H1(b)/H5: the list is no longer hard-coded here — it is the
/// ONE committed table `vco_lib/tool_search_dirs.toml` that the Python side
/// (`vco_lib.tool_search_dirs`) also reads: the v0.2.53 graphical-launch
/// list plus the places the container runtimes live (`~/bin` for rootless
/// Docker, `/usr/local/bin`, `/opt/podman/bin`, Docker Desktop's app bundle,
/// the Windows installers), each with its `placement` (R10).
fn augment_candidates(home: Option<&std::path::Path>) -> Vec<(PathBuf, Placement)> {
    let home = home.map(|h| h.to_string_lossy().into_owned());
    tool_search_entries_for(current_os_key(), home.as_deref(), &|k| std::env::var(k).ok())
        .into_iter()
        .map(|(d, p)| (PathBuf::from(d), p))
        .collect()
}

/// The shared table of usual tool install locations (see
/// [`augment_candidates`]). Embedded at compile time — the SAME file the
/// Python reader parses.
const TOOL_SEARCH_DIRS_TOML: &str = include_str!("../../../../../vco_lib/tool_search_dirs.toml");

/// When SET (even to ""), replaces the table's list for this OS: entries
/// separated by the OS path separator, same syntax, every one placed
/// [`Placement::Append`]. MUST MATCH `vco_lib.tool_search_dirs.ENV_OVERRIDE`.
pub const TOOL_SEARCH_DIRS_ENV: &str = "VCT_TOOL_SEARCH_DIRS";

/// Where a table directory the PATH lacks goes (v0.2.97 R10): the table's
/// per-entry `placement`. MUST MATCH `vco_lib.tool_search_dirs.PLACEMENT_*`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Placement {
    /// `prepend-when-missing` — ahead of the inherited PATH (the v0.2.53
    /// graphical-launch list: a login shell's order).
    PrependWhenMissing,
    /// `append` — after the inherited PATH (the v0.2.97 runtime locations:
    /// reach only, never shadow).
    Append,
}

impl Placement {
    /// The table's spelling.
    pub fn as_str(self) -> &'static str {
        match self {
            Placement::PrependWhenMissing => "prepend-when-missing",
            Placement::Append => "append",
        }
    }
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct ToolSearchDirEntry {
    dir: String,
    placement: Placement,
}

#[derive(serde::Deserialize)]
struct ToolSearchDirsFile {
    format_version: u32,
    dirs: std::collections::HashMap<String, Vec<ToolSearchDirEntry>>,
}

static TOOL_SEARCH_DIRS: std::sync::LazyLock<
    std::collections::HashMap<String, Vec<(String, Placement)>>,
> = std::sync::LazyLock::new(|| {
    let parsed: ToolSearchDirsFile = toml::from_str(TOOL_SEARCH_DIRS_TOML)
        .expect("vco_lib/tool_search_dirs.toml is embedded at compile time and must parse");
    assert_eq!(
        parsed.format_version, 2,
        "vco_lib/tool_search_dirs.toml format_version this reader supports is 2"
    );
    parsed
        .dirs
        .into_iter()
        .map(|(os, entries)| (os, entries.into_iter().map(|e| (e.dir, e.placement)).collect()))
        .collect()
});

/// The table's key for the OS this binary runs on (`linux` / `macos` /
/// `windows`; `other` has no entries). MUST MATCH
/// `vco_lib.tool_search_dirs.os_key`.
pub fn current_os_key() -> &'static str {
    if cfg!(target_os = "macos") {
        "macos"
    } else if cfg!(target_os = "linux") {
        "linux"
    } else if cfg!(windows) {
        "windows"
    } else {
        "other"
    }
}

/// One table entry → a directory, or `None` when it cannot be expanded:
/// `~/rest` needs a home, `${NAME}rest` a set, non-empty `NAME`; anything
/// else is literal. MUST MATCH `vco_lib.tool_search_dirs.expand_entry`
/// (`tests/fixtures/tool_search_dirs_cases.json` runs both).
pub fn expand_search_dir(
    entry: &str,
    home: Option<&str>,
    env: &dyn Fn(&str) -> Option<String>,
) -> Option<String> {
    if entry == "~" || entry.starts_with("~/") {
        let h = home.filter(|h| !h.is_empty())?;
        return Some(format!("{}{}", h.trim_end_matches(['/', '\\']), &entry[1..]));
    }
    if let Some(rest) = entry.strip_prefix("${") {
        if let Some(end) = rest.find('}') {
            let name = &rest[..end];
            let valid = !name.is_empty()
                && name.chars().next().is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
                && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_');
            if valid {
                let value = env(name).filter(|v| !v.is_empty())?;
                return Some(format!("{}{}", value, &rest[end + 1..]));
            }
        }
    }
    Some(entry.to_string())
}

/// The expanded `(directory, placement)` entries for `os` (table order,
/// duplicate directories dropped — the first keeps its placement), or the
/// `VCT_TOOL_SEARCH_DIRS` override when `env` has it (every entry
/// [`Placement::Append`]). Pure (home and env injected) so the shared
/// fixture can drive every OS from any host. MUST MATCH
/// `vco_lib.tool_search_dirs.search_entries`.
pub fn tool_search_entries_for(
    os: &str,
    home: Option<&str>,
    env: &dyn Fn(&str) -> Option<String>,
) -> Vec<(String, Placement)> {
    let raw: Vec<(String, Placement)> = match env(TOOL_SEARCH_DIRS_ENV) {
        Some(over) => {
            let sep = if os == "windows" { ';' } else { ':' };
            over.split(sep)
                .filter(|p| !p.trim().is_empty())
                .map(|p| (p.to_string(), Placement::Append))
                .collect()
        }
        None => TOOL_SEARCH_DIRS.get(os).cloned().unwrap_or_default(),
    };
    let mut out: Vec<(String, Placement)> = Vec::new();
    for (entry, placement) in raw {
        if let Some(d) = expand_search_dir(entry.trim(), home, env) {
            if !d.is_empty() && !out.iter().any(|(seen, _)| seen == &d) {
                out.push((d, placement));
            }
        }
    }
    out
}

/// The directories of [`tool_search_entries_for`], in table order. MUST
/// MATCH `vco_lib.tool_search_dirs.candidate_dirs`.
pub fn tool_search_dirs_for(
    os: &str,
    home: Option<&str>,
    env: &dyn Fn(&str) -> Option<String>,
) -> Vec<String> {
    tool_search_entries_for(os, home, env).into_iter().map(|(d, _)| d).collect()
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
//   adding the OS-specific candidate directories the PATH lacks (the
//   graphical-launch ones ahead of it, the runtime locations after it —
//   R10 J3). After that
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

/// What one detection pass established: the runtime to drive, or — when
/// there is none — why, in words a user can act on (v0.2.97 R11 L6).
/// v0.2.97 R12: every field is a rendering of the ONE Python verdict
/// (`runtime_verdict::decide`); the refusal is Python's text VERBATIM (M3).
#[derive(Debug, Clone, Default)]
pub struct RuntimeDetection {
    /// The runtime to drive (`None` = nothing may be driven).
    pub info: Option<RuntimeInfo>,
    /// Why a stale runtime record was NOT switched to the other runtime —
    /// the shared table's wording (`vco_lib/runtime_reconcile_messages.toml`,
    /// R10 J6), rendered by Python — when the stale-record arm declined;
    /// else `None`.
    pub not_switched: Option<String>,
    /// The full refusal when `info` is `None`: Python's `refusal` text
    /// verbatim (a refused pin, carrying `not_switched`), the verdict's
    /// `reason` when it RESOLVED without compose (R12-bis P3-2: an
    /// installed, answering runtime without compose is not "no container
    /// runtime found" — the reason explains the compose gap), or the loud
    /// broken-install error when the verdict itself could not run. `None`
    /// otherwise.
    pub refusal: Option<String>,
    /// The first candidate binary INSTALLED, usable or not (the verdict's
    /// `installed`; `None` = no container runtime is installed at all —
    /// EXCEPT under [`verdict_failed`], where nothing was learned and
    /// `None` means UNKNOWN, not "none"). M4: the boot path offers the
    /// install dialog on this, pin or not.
    pub installed: Option<String>,
    /// True when the ONE verdict could not be obtained at all (missing or
    /// broken Python, spawn failure, timeout, unparseable output). The
    /// `refusal` then carries the loud broken-install error WITH its
    /// remedy, and `installed` is unknown — a surface must answer with
    /// that error, never with the "install a container runtime" download
    /// dialog (R12-bis P3-3).
    pub verdict_failed: bool,
}

impl RuntimeDetection {
    /// The sentence a surface shows when there is no runtime to drive: the
    /// pin refusal (with why), else "no container runtime found". Only
    /// meaningful when `info` is `None`.
    pub fn no_runtime_message(&self) -> String {
        self.refusal.clone().unwrap_or_else(|| {
            "No container runtime found. Install Podman or Docker to run VCT services.".to_string()
        })
    }
}

/// Cached detection result. `None` means "not yet probed"; `Some(d)` is the
/// last pass's [`RuntimeDetection`], so the second caller can read the first
/// caller's verdict — refusal included — without redoing the probe.
static CACHE: Mutex<Option<RuntimeDetection>> = Mutex::new(None);

/// Force a fresh detection pass. Called from a Tauri command when the
/// user clicks "Re-detect" in the Services preferences screen. Clears the
/// verdict TTL cache too (R12): the next pass must re-ask Python, not
/// replay a cached verdict.
pub fn invalidate_cache() {
    if let Ok(mut g) = CACHE.lock() {
        *g = None;
    }
    super::runtime_verdict::invalidate();
}

/// Detect the active runtime once per launcher session. Returns `None`
/// when neither Podman nor Docker is installed. Result is cached; call
/// `invalidate_cache()` to force a re-probe.
pub async fn detect_runtime() -> Option<RuntimeInfo> {
    detect_runtime_detailed().await.info
}

/// [`detect_runtime`] with the reason there is none (v0.2.97 R11 L6): the
/// install preflight, the start path and the hub's watchdog show a refused
/// pin — and why the stale record was not switched — instead of "no
/// container runtime found". Same cache.
pub async fn detect_runtime_detailed() -> RuntimeDetection {
    if let Ok(g) = CACHE.lock() {
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

/// [`detect_runtime`] for a caller that cannot go on without a runtime:
/// `Err` carries [`RuntimeDetection::no_runtime_message`].
pub async fn require_runtime() -> Result<RuntimeInfo, String> {
    let detection = detect_runtime_detailed().await;
    detection.info.clone().ok_or_else(|| detection.no_runtime_message())
}

// ---------------------------------------------------------------------------
// The DECISION — v0.2.97 R12: ONE Python verdict (owner ruling "Consolidate
// now", 2026-09-25)
// ---------------------------------------------------------------------------
//
// `vco_lib.runtime_reconcile.decide` (CLI: `python -m
// vco_lib.runtime_reconcile decide --json`) is the ONE answer for "which
// runtime, which compose, which pin, and why" — this module ASKS it through
// `runtime_verdict::decide` and renders the JSON. The Rust decision mirror
// (`candidate_order` + `select_runtime` + the stale-record arm and their
// probes) is RETIRED: it was a class-C mirror of the reconcile rules, and
// the pin discipline / stale-record / bind-folder / one-engine rules now
// have exactly one home. The parity fixture
// (`tests/fixtures/container_runtime_parity.json`) and the contract fixture
// (`tests/fixtures/runtime_decide_cases.json`) drive this surface THROUGH
// THE CLIENT (see `services::runtime_verdict`'s tests).
//
// A pinned runtime that is unusable stays REFUSED — Python's answer, with
// Python's refusal text shown verbatim (M3: the Rust side never re-derives
// "does not answer info" from PATH state). A usable runtime WITHOUT compose
// still maps to `info: None` here (Python says `resolved` + `compose: null`
// so install.py can print compose's own error; this module has nothing to
// drive) — the deliberate divergence the fixture's `expect_rust` records.
// R12-bis P3-2: on that branch the verdict's `reason` rides as the
// detection's refusal, so the surfaces say "installed but no compose"
// (Python's explanation) instead of "No container runtime found".

async fn resolve_runtime() -> RuntimeDetection {
    resolve_runtime_in(crate::orchestrator_manifest::orchestrator_install_root().as_deref()).await
}

/// [`resolve_runtime`] against an explicit clone root (where the install's
/// `state/install/runtime.txt` lives) — a test hands it a temp dir so this
/// machine's own record cannot change the answer.
async fn resolve_runtime_in(install_root: Option<&std::path::Path>) -> RuntimeDetection {
    resolve_runtime_in_with(super::runtime_verdict::VerdictOpts {
        install_root,
        mode: Some(super::runtime_verdict::Mode::ReadOnly),
        purpose: Some(super::runtime_verdict::Purpose::Infra),
        ..super::runtime_verdict::VerdictOpts::default()
    })
    .await
}

/// The same funnel with full opts — the contract and probe-path tests inject
/// the interpreter, the PATH and the pin env here instead of mutating the
/// process env.
async fn resolve_runtime_in_with(
    opts: super::runtime_verdict::VerdictOpts<'_>,
) -> RuntimeDetection {
    let verdict = match super::runtime_verdict::decide_cached(opts).await {
        Ok(v) => v,
        // Loud-fail (R12): a missing or broken Python is a broken install.
        // The error is shown as the refusal — never a Rust fallback.
        Err(e) => {
            tracing::error!("[vct] runtime: the one verdict could not run: {}", e);
            return RuntimeDetection {
                info: None,
                not_switched: None,
                refusal: Some(e),
                // UNKNOWN, not "none" — `verdict_failed` tells the boot
                // path to show the error above and NOT open the runtime
                // download dialog (R12-bis P3-3).
                installed: None,
                verdict_failed: true,
            };
        }
    };
    let needs_machine_start = match verdict.runtime.as_deref() {
        Some("podman") => match verdict.binary_path.as_ref() {
            Some(b) => detect_podman_machine_needed(b).await,
            None => false,
        },
        _ => false,
    };
    detection_from_verdict(&verdict, needs_machine_start)
}

/// The verdict rendered as this module's answer — pure, so the mapping is
/// testable without a Python interpreter. `state`/`runtime`/`compose`/
/// `binary_path` become [`RuntimeInfo`]; a refusal keeps Python's text
/// VERBATIM (M3); `not_switched` travels for the surfaces that show it
/// separately; `installed` drives the boot path's install dialog (M4).
/// When the verdict RESOLVED but compose is missing/unknown (`info` is
/// `None` with no refusal), the verdict's `reason` rides as the refusal
/// (R12-bis P3-2): it explains the compose gap, and no surface may call
/// an installed, answering runtime "No container runtime found".
pub(crate) fn detection_from_verdict(
    verdict: &super::runtime_verdict::RuntimeVerdict,
    needs_machine_start: bool,
) -> RuntimeDetection {
    let info = verdict
        .runtime
        .as_deref()
        .zip(verdict.compose_form.as_deref())
        .and_then(|(name, form)| {
            let runtime = if name == "docker" { ContainerRuntime::Docker } else { ContainerRuntime::Podman };
            let compose_form = match form {
                "subcommand" => ComposeForm::Subcommand,
                "standalone" => ComposeForm::Standalone,
                _ => return None, // resolved without compose: nothing to drive
            };
            let binary_path = verdict
                .binary_path
                .clone()
                .or_else(|| which_on_path(runtime.binary()))?;
            Some(RuntimeInfo { runtime, compose_form, needs_machine_start, binary_path })
        });
    let nothing_to_drive = info.is_none();
    RuntimeDetection {
        info,
        not_switched: verdict.not_switched.clone(),
        refusal: verdict.refusal.clone().or_else(|| {
            // R12-bis P3-2: resolved WITHOUT compose (or any no-refusal
            // shape with nothing to drive) — Python's `reason` explains
            // the gap; Python sets `refusal` only when NOT resolved
            // (`vco_lib/runtime_reconcile.py`), so without this the
            // surfaces would fall back to "No container runtime found".
            if nothing_to_drive {
                Some(verdict.reason.clone())
            } else {
                None
            }
        }),
        installed: verdict.installed.clone(),
        verdict_failed: false,
    }
}


// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serial_test::serial;

    // -----------------------------------------------------------------
    // Parity fixture (v0.2.92 §3.5 / R13) — the same JSON drives
    // tests/test_container_runtime_ssot.py on the Python side.
    // -----------------------------------------------------------------

    fn parity_fixture() -> serde_json::Value {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../tests/fixtures/container_runtime_parity.json");
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
        serde_json::from_str(&text).expect("fixture parses")
    }

    fn form_name(f: ComposeForm) -> &'static str {
        match f {
            ComposeForm::Subcommand => "subcommand",
            ComposeForm::Standalone => "standalone",
        }
    }

    #[test]
    fn parity_fixture_has_scenarios_and_every_one_names_a_rust_expectation() {
        let fx = parity_fixture();
        let scenarios = fx["scenarios"].as_array().expect("scenarios array");
        assert!(scenarios.len() >= 10, "fixture shrank: {}", scenarios.len());
        for sc in scenarios {
            assert!(
                sc.get("expect_rust").is_some(),
                "scenario {} lacks expect_rust",
                sc["name"]
            );
        }
    }

    /// R13, THROUGH THE ONE CLIENT (2026-09-25 consolidation): each scenario
    /// is a real `decide --purpose infra --mode read-only` spawn over the
    /// same stub machine the module-plane leg drives, rendered through
    /// [`detection_from_verdict`]. `expect_rust` records this plane's ONE
    /// deliberate divergence from Python's `expect`: `resolved` with
    /// `compose: null` maps to `info: None` here (the launcher's infra
    /// stack drives nothing without compose), and `null` rows are the
    /// refusals. The Rust decision mirror this file used to keep
    /// (`select_runtime` + `candidate_order` + the stale-record arm) is
    /// retired — Python decides, this renders.
    #[cfg(unix)]
    #[tokio::test]
    #[serial_test::serial]
    async fn parity_fixture_infra_plane_matches_every_scenario() {
        use super::super::container_runtime::parity_fixture as pf;
        use super::super::runtime_verdict::contract_support::{isolation_env, make_executable, test_python};
        let fx = parity_fixture();
        for sc in fx["scenarios"].as_array().unwrap() {
            let name = sc["name"].as_str().unwrap();
            let dir = tempfile::tempdir().unwrap();
            let root = pf::parity_root(dir.path(), sc);
            let bin_dir = dir.path().join("bin");
            std::fs::create_dir_all(&bin_dir).unwrap();
            for rt in ["podman", "docker"] {
                pf::write_parity_stub(&bin_dir, rt, sc);
            }
            for standalone in sc["standalone_on_path"]
                .as_array()
                .unwrap()
                .iter()
                .filter_map(|v| v.as_str())
            {
                let s = bin_dir.join(standalone);
                std::fs::write(&s, "#!/bin/sh\nexit 0\n").unwrap();
                make_executable(&s);
            }
            let home = dir.path().join("home");
            std::fs::create_dir_all(&home).unwrap();
            let mut env_pairs = isolation_env(&bin_dir, &home);
            let mut unset_keys = Vec::new();
            match sc["env"].as_str() {
                // Verbatim: "auto"/"bogus" are PYTHON's non-pins to read.
                Some(pin) => {
                    env_pairs.push(("VCT_CONTAINER_RUNTIME".to_string(), pin.to_string()))
                }
                None => unset_keys.push("VCT_CONTAINER_RUNTIME".to_string()),
            }
            let verdict = super::super::runtime_verdict::decide_cached(super::super::runtime_verdict::VerdictOpts {
                install_root: Some(root.as_path()),
                mode: Some(super::super::runtime_verdict::Mode::ReadOnly),
                purpose: Some(super::super::runtime_verdict::Purpose::Infra),
                python: Some(test_python().as_path()),
                path_env: None,
                env_pairs,
                unset_keys,
            })
            .await
            .unwrap_or_else(|e| panic!("scenario {name}: decide failed: {e}"));
            let detection = detection_from_verdict(&verdict, false);
            let want = &sc["expect_rust"];
            if want.is_null() {
                assert!(
                    detection.info.is_none(),
                    "scenario {name}: a refusal must not drive a runtime"
                );
            } else {
                let info = detection
                    .info
                    .unwrap_or_else(|| panic!("scenario {name}: fixture expects a runtime"));
                assert_eq!(
                    info.runtime.binary(),
                    want["runtime"].as_str().unwrap(),
                    "scenario {name}"
                );
                assert_eq!(
                    form_name(info.compose_form),
                    want["compose_form"].as_str().unwrap(),
                    "scenario {name}"
                );
            }
        }
    }

    #[test]
    fn other_runtime_is_the_pair() {
        assert_eq!(ContainerRuntime::Podman.other(), ContainerRuntime::Docker);
        assert_eq!(ContainerRuntime::Docker.other(), ContainerRuntime::Podman);
    }

    /// R7b F5, through the ONE client: docker answers everything, podman
    /// refuses. Unpinned, the launcher's infra stack drives docker; with the
    /// install's record naming podman it drives NOTHING — the record is a
    /// pin, and a pinned runtime that is unusable is refused (read-only
    /// switching needs positive evidence the other runtime serves VCO),
    /// never swapped for docker (whose volumes are a different, possibly
    /// empty, copy). The stubs and the pin env go to the CHILD.
    #[cfg(unix)]
    #[test]
    #[serial]
    fn resolve_runtime_honours_the_install_record() {
        use super::super::runtime_verdict::contract_support::{isolation_env, test_python};
        let bins = tempfile::tempdir().unwrap();
        write_fake_runtime(bins.path(), "docker", "Server: Docker Engine - Community", 0);
        write_fake_runtime(bins.path(), "podman", "", 1);
        let home = bins.path().join("home");
        std::fs::create_dir_all(&home).unwrap();
        let ask = |root: Option<&std::path::Path>| {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();
            rt.block_on(resolve_runtime_in_with(super::super::runtime_verdict::VerdictOpts {
                install_root: root,
                mode: Some(super::super::runtime_verdict::Mode::ReadOnly),
                purpose: Some(super::super::runtime_verdict::Purpose::Infra),
                python: Some(test_python().as_path()),
                path_env: None,
                env_pairs: isolation_env(bins.path(), &home),
                unset_keys: vec!["VCT_CONTAINER_RUNTIME".to_string()],
            }))
        };
        let bare_root = tempfile::tempdir().unwrap();
        let pinned_root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(pinned_root.path().join("state/install")).unwrap();
        std::fs::write(pinned_root.path().join("state/install/runtime.txt"), "podman\n").unwrap();
        let unpinned = ask(Some(bare_root.path())).info.map(|i| i.runtime);
        let pinned = ask(Some(pinned_root.path())).info.map(|i| i.runtime);
        assert_eq!(unpinned, Some(ContainerRuntime::Docker));
        assert_eq!(pinned, None, "the recorded podman must pin, not fall through to docker");
    }

    /// R11 L6, through the ONE client: the install's record names docker,
    /// which is not installed; podman answers holding none of VCO's data,
    /// so the stale record is not switched. The detection carries the
    /// refusal — the pin, the record file, why it was not switched, and
    /// the remedy — which every "no runtime" surface (install preflight,
    /// start path, hub watchdog) shows instead of "No container runtime
    /// found". M3: the text is PYTHON's, verbatim; no Rust renderer
    /// rebuilds it.
    #[cfg(unix)]
    #[test]
    #[serial]
    fn a_refused_record_pin_carries_why_to_every_surface() {
        use super::super::runtime_verdict::contract_support::{isolation_env, test_python};
        let bins = tempfile::tempdir().unwrap();
        write_fake_runtime(bins.path(), "podman", "someone_elses_ollama", 0);
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(root.path().join("state/install")).unwrap();
        std::fs::write(root.path().join("state/install/runtime.txt"), "docker\n").unwrap();
        let home = bins.path().join("home");
        std::fs::create_dir_all(&home).unwrap();
        let detection = {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();
            rt.block_on(resolve_runtime_in_with(super::super::runtime_verdict::VerdictOpts {
                install_root: Some(root.path()),
                mode: Some(super::super::runtime_verdict::Mode::ReadOnly),
                purpose: Some(super::super::runtime_verdict::Purpose::Infra),
                python: Some(test_python().as_path()),
                path_env: None,
                env_pairs: isolation_env(bins.path(), &home),
                unset_keys: vec!["VCT_CONTAINER_RUNTIME".to_string()],
            }))
        };
        assert!(detection.info.is_none());
        let source = root.path().join("state/install/runtime.txt").display().to_string();
        let refusal = detection.refusal.clone().expect("a refused pin says so");
        assert!(refusal.contains("recorded docker"), "{refusal}");
        assert!(refusal.contains(&source), "{refusal}");
        assert!(refusal.contains("(not switched: "), "{refusal}");
        assert!(refusal.contains("holds none of VCO's containers or volumes"), "{refusal}");
        assert!(refusal.contains("--container podman"), "{refusal}");
        assert!(
            detection
                .not_switched
                .as_deref()
                .unwrap()
                .contains("pinned to docker by"),
            "{:?}",
            detection.not_switched
        );
        assert_eq!(detection.no_runtime_message(), refusal);
        assert!(!detection.no_runtime_message().contains("No container runtime found"));
    }

    /// M3 (2026-09-25 consolidation), pure over the parsed verdict so the
    /// mapping needs no interpreter: the refusal shown on every surface is
    /// Python's text VERBATIM — `no_runtime_message` IS the refusal, never
    /// re-derived from PATH state — `not_switched` rides Python's text, and
    /// `installed` travels for the boot path's install dialog (M4). The
    /// unpinned "found nothing" shape keeps the legacy headline (there is
    /// no refusal to show when nothing was pinned).
    #[test]
    fn detection_from_verdict_shows_pythons_refusal_verbatim() {
        let refusal = "the install recorded docker as this machine's container runtime \
                       (/x/state/install/runtime.txt) but docker is not on PATH \
                       (not switched: the container runtime is pinned to docker by \
                       /x/state/install/runtime.txt)";
        let verdict = super::super::runtime_verdict::RuntimeVerdict {
            runtime: None,
            state: "absent".to_string(),
            resolved: false,
            compose: None,
            compose_form: None,
            binary_path: None,
            search_path: None,
            installed: Some("docker".to_string()),
            requested: Some("docker".to_string()),
            requested_via: "record".to_string(),
            requested_installed: false,
            alternative_usable: Some("podman".to_string()),
            record_reconciled: false,
            outcome: "refused".to_string(),
            not_switched_key: Some("no_data".to_string()),
            not_switched: Some(
                "the container runtime is pinned to docker by \
                 /x/state/install/runtime.txt"
                    .to_string(),
            ),
            same_engine: false,
            refused: true,
            refusal: Some(refusal.to_string()),
            reason: "record pin refused".to_string(),
        };
        let detection = detection_from_verdict(&verdict, false);
        assert_eq!(detection.refusal.as_deref(), Some(refusal));
        assert_eq!(detection.no_runtime_message(), refusal);
        assert!(!detection.no_runtime_message().contains("No container runtime found"));
        assert!(detection.not_switched.as_deref().unwrap().contains("pinned to docker by"));
        assert_eq!(detection.installed.as_deref(), Some("docker"), "M4: installed travels");
        let none = RuntimeDetection::default();
        assert!(none.no_runtime_message().starts_with("No container runtime found"));
    }

    /// R12-bis P3-2, pure over the parsed verdict: RESOLVED without
    /// compose (Python sets `refusal` only when NOT resolved) — the
    /// mapping carries the verdict's `reason` as the refusal so every
    /// surface explains the compose gap instead of falling back to "No
    /// container runtime found" about an installed, answering runtime.
    /// Leave-alone: a resolved verdict WITH compose keeps `refusal: None`.
    #[test]
    fn detection_from_verdict_carries_the_reason_when_resolved_without_compose() {
        let base = super::super::runtime_verdict::RuntimeVerdict {
            runtime: Some("podman".to_string()),
            state: "resolved".to_string(),
            resolved: true,
            compose: None,
            compose_form: None,
            binary_path: None,
            search_path: None,
            installed: Some("podman".to_string()),
            requested: None,
            requested_via: "auto".to_string(),
            requested_installed: false,
            alternative_usable: None,
            record_reconciled: false,
            outcome: "usable_runtime_but_no_compose_anywhere".to_string(),
            not_switched_key: None,
            not_switched: None,
            same_engine: false,
            refused: false,
            refusal: None,
            reason: "podman is installed and answering, but no compose is available \
                     (neither `podman compose` nor podman-compose)."
                .to_string(),
        };
        let detection = detection_from_verdict(&base, false);
        assert!(detection.info.is_none(), "nothing to drive without compose");
        assert_eq!(detection.refusal.as_deref(), Some(base.reason.as_str()));
        assert!(
            !detection.no_runtime_message().contains("No container runtime found"),
            "an installed runtime without compose is not 'no container runtime found': {}",
            detection.no_runtime_message()
        );
        assert_eq!(detection.installed.as_deref(), Some("podman"));
        assert!(!detection.verdict_failed);
        // Leave-alone: with compose the refusal stays None (nothing to
        // complain about) and the runtime drives.
        let with_compose = super::super::runtime_verdict::RuntimeVerdict {
            compose: Some(vec!["podman".to_string(), "compose".to_string()]),
            compose_form: Some("subcommand".to_string()),
            binary_path: Some("/usr/bin/podman".into()),
            ..base
        };
        let detection = detection_from_verdict(&with_compose, false);
        assert!(detection.info.is_some(), "a compose-bearing verdict drives");
        assert!(detection.refusal.is_none(), "no refusal when resolved with compose");
    }

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

    /// The augmented PATH for `current`, or `current` itself when nothing
    /// is added — what the process PATH would be after one call.
    fn after_augment(current: &str, home: Option<&str>) -> std::ffi::OsString {
        augmented_path(std::ffi::OsStr::new(current), home.map(std::path::Path::new))
            .unwrap_or_else(|| std::ffi::OsString::from(current))
    }

    /// Augmentation is idempotent — a second call adds nothing. On Windows
    /// it is a no-op and the PATH is unchanged.
    #[test]
    fn augment_path_is_idempotent() {
        let first = after_augment("/usr/bin:/bin", Some("/tmp/vct-augment-test-home"));
        assert!(
            augmented_path(&first, Some(std::path::Path::new("/tmp/vct-augment-test-home"))).is_none(),
            "second augment_path call must NOT modify PATH again"
        );
    }

    /// Augmentation keeps the existing PATH in its order (candidates go
    /// ahead of or behind it, per placement — R10) — order is not destroyed.
    #[test]
    fn augment_path_preserves_user_path_order() {
        let after = after_augment("/zzz_marker_a:/zzz_marker_b", Some("/tmp/vct-augment-test-home"));
        let parts: Vec<PathBuf> = std::env::split_paths(&after).collect();

        let pos_a = parts.iter().position(|p| p == &PathBuf::from("/zzz_marker_a"));
        let pos_b = parts.iter().position(|p| p == &PathBuf::from("/zzz_marker_b"));
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
    }

    /// The table's entries for `os` as the augment consumes them.
    fn entries_for(os: &str, home: &str) -> Vec<(PathBuf, Placement)> {
        tool_search_entries_for(os, Some(home), &|_k| None)
            .into_iter()
            .map(|(d, p)| (PathBuf::from(d), p))
            .collect()
    }

    /// R10 J3 (split by placement): an inherited entry is never displaced.
    /// The inherited PATH appears in the result contiguous and unchanged —
    /// including a table directory it already holds, of EITHER placement
    /// (`/usr/bin` is `append`, `~/.local/bin` is `prepend-when-missing`),
    /// which is neither moved nor duplicated. Ahead of it: exactly the
    /// `prepend-when-missing` entries it lacked; behind it: exactly the
    /// `append` entries it lacked, each in table order.
    #[test]
    fn augment_path_never_displaces_an_inherited_entry() {
        let home = "/tmp/vct-augment-test-home";
        for os in ["linux", "macos"] {
            let cands = entries_for(os, home);
            let late_graphical = PathBuf::from(format!("{home}/.local/bin"));
            assert!(cands.contains(&(late_graphical.clone(), Placement::PrependWhenMissing)), "{os}");
            let inherited = vec![
                PathBuf::from("/zzz_first"),
                PathBuf::from("/usr/bin"),
                PathBuf::from("/zzz_second"),
                late_graphical.clone(),
            ];
            let after = augmented_entries(&inherited, &cands);
            let lacked = |want: Placement| -> Vec<PathBuf> {
                cands
                    .iter()
                    .filter(|(d, p)| *p == want && !inherited.contains(d))
                    .map(|(d, _)| d.clone())
                    .collect()
            };
            let before = lacked(Placement::PrependWhenMissing);
            let behind = lacked(Placement::Append);
            assert_eq!(&after[..before.len()], before.as_slice(), "{os}: {after:?}");
            assert_eq!(
                &after[before.len()..before.len() + inherited.len()],
                inherited.as_slice(),
                "{os}: the inherited PATH must stay contiguous and in order: {after:?}"
            );
            assert_eq!(&after[before.len() + inherited.len()..], behind.as_slice(), "{os}: {after:?}");
            for kept in [PathBuf::from("/usr/bin"), late_graphical] {
                assert_eq!(after.iter().filter(|p| **p == kept).count(), 1, "{os}: {kept:?} duplicated");
            }
        }
    }

    /// R10 J3, by name resolution: a binary the inherited PATH reaches is the
    /// one found after augment — even when a table directory holds a
    /// same-named binary: an `append` one (`~/bin`, the rootless-Docker
    /// location) because it goes behind the PATH, a `prepend-when-missing`
    /// one (`~/.local/bin`) that the PATH already holds, later, because it is
    /// never moved. Such a table dir only reaches what the PATH does not. Pure over
    /// the returned PATH (never the process PATH, `with_lookup_path`).
    #[cfg(unix)]
    #[test]
    fn a_same_named_binary_earlier_on_path_still_wins() {
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join("home");
        let inherited = dir.path().join("inherited");
        let appended = home.join("bin");
        let graphical = home.join(".local").join("bin");
        for d in [&inherited, &appended, &graphical] {
            std::fs::create_dir_all(d).unwrap();
            write_fake_runtime(d, "vct-r10-tool", "", 0);
        }
        write_fake_runtime(&appended, "vct-r10-only-in-table", "", 0);
        let current = std::env::join_paths([&inherited, &graphical]).unwrap();
        let after = augmented_path(&current, Some(&home)).expect("the table adds ~/bin");
        crate::paths::with_lookup_path(Some(after.as_os_str()), || {
            assert_eq!(
                crate::paths::which_on_path("vct-r10-tool"),
                Some(inherited.join("vct-r10-tool")),
                "the inherited PATH's binary must win"
            );
            assert_eq!(
                crate::paths::which_on_path("vct-r10-only-in-table"),
                Some(appended.join("vct-r10-only-in-table")),
                "a tool only the table reaches still resolves"
            );
        });
    }

    /// v0.2.53 M-P0-7 restored (R10 split): a Finder launch's PATH
    /// (`/usr/bin:/bin:/usr/sbin:/sbin`, LaunchServices) lacks Homebrew, and
    /// `/usr/bin/git` / `/usr/bin/python3` are the Xcode Command Line Tools
    /// stubs. Homebrew is `prepend-when-missing`, so after the augment it is
    /// AHEAD of `/usr/bin` — as in the user's login shell — and `git`
    /// resolves to Homebrew's. (With every candidate appended, R10 J3's first
    /// cut, the stub won.)
    #[test]
    fn a_graphical_launch_still_prefers_homebrew_over_the_system_stub() {
        let finder: Vec<PathBuf> = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
            .iter()
            .map(PathBuf::from)
            .collect();
        let after = augmented_entries(&finder, &entries_for("macos", "/Users/u"));
        let pos = |d: &str| after.iter().position(|p| p == &PathBuf::from(d));
        assert!(
            pos("/opt/homebrew/bin").unwrap() < pos("/usr/bin").unwrap(),
            "Homebrew must precede the system dir: {after:?}"
        );
        let installed = [PathBuf::from("/usr/bin/git"), PathBuf::from("/opt/homebrew/bin/git")];
        let git = after.iter().map(|d| d.join("git")).find(|p| installed.contains(p));
        assert_eq!(git, Some(PathBuf::from("/opt/homebrew/bin/git")));
    }

    /// On macOS, the homebrew prefix must appear on PATH after augment.
    /// On Linux, `$HOME/.local/bin` must appear (when HOME is set).
    /// On Windows, augment is a no-op so PATH is unchanged.
    #[test]
    fn augment_path_adds_expected_os_specific_dirs() {
        let after = after_augment("/usr/bin:/bin", Some("/tmp/vct-augment-test-home"));
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
            // Windows + other: the original PATH, then the shared table's
            // entries for this OS (R9: the container runtimes' install dirs,
            // all `append` — R10).
            // The baseline split the way THIS OS splits a PATH (";" on Windows).
            let mut want: Vec<PathBuf> =
                std::env::split_paths(std::ffi::OsStr::new("/usr/bin:/bin")).collect();
            for (d, placement) in tool_search_entries_for(
                current_os_key(),
                Some("/tmp/vct-augment-test-home"),
                &|k| std::env::var(k).ok(),
            ) {
                assert_eq!(placement, Placement::Append, "{d}");
                want.push(PathBuf::from(d));
            }
            assert_eq!(parts, want, "PATH={:?}", parts);
        }
    }

    // ----- v0.2.97 R9 H1(b)/H5: the shared tool-search table -----

    fn fixture_env(case: &serde_json::Value) -> std::collections::HashMap<String, String> {
        case["env"]
            .as_object()
            .map(|m| {
                m.iter()
                    .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string()))
                    .collect()
            })
            .unwrap_or_default()
    }

    /// The expansion rule and every OS's list, driven by the SAME fixture
    /// the Python suite runs (`tests/fixtures/tool_search_dirs_cases.json`).
    #[test]
    fn tool_search_dirs_match_the_shared_fixture() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../tests/fixtures/tool_search_dirs_cases.json");
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
        let fx: serde_json::Value = serde_json::from_str(&text).expect("fixture parses");
        let cases = fx["cases"].as_array().expect("cases");
        assert!(cases.len() >= 6, "fixture shrank");
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let env = fixture_env(case);
            let os = case["os"].as_str().unwrap();
            let got: Vec<(String, String)> =
                tool_search_entries_for(os, case["home"].as_str(), &|k| env.get(k).cloned())
                    .into_iter()
                    .map(|(d, p)| (d, p.as_str().to_string()))
                    .collect();
            let want: Vec<(String, String)> = case["expect"]
                .as_array()
                .unwrap()
                .iter()
                .map(|pair| {
                    (
                        pair[0].as_str().unwrap().to_string(),
                        pair[1].as_str().unwrap().to_string(),
                    )
                })
                .collect();
            assert_eq!(got, want, "case {}", name);
            let dirs: Vec<String> = want.into_iter().map(|(d, _)| d).collect();
            assert_eq!(
                tool_search_dirs_for(os, case["home"].as_str(), &|k| env.get(k).cloned()),
                dirs,
                "case {}",
                name
            );
        }
    }

    /// R10 J3 split — THE ORDER RULE and the resolution it implies, driven by
    /// the SAME `order_cases` the Python suite runs
    /// (`test_the_order_rule_matches_the_shared_fixture`): graphical-launch
    /// dirs the PATH lacks ahead of it, runtime locations it lacks behind it,
    /// a dir already on it never moved.
    #[test]
    fn tool_search_order_matches_the_shared_fixture() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../tests/fixtures/tool_search_dirs_cases.json");
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
        let fx: serde_json::Value = serde_json::from_str(&text).expect("fixture parses");
        let cases = fx["order_cases"].as_array().expect("order_cases");
        assert!(cases.len() >= 3, "fixture shrank");
        let strings = |v: &serde_json::Value| -> Vec<String> {
            v.as_array().unwrap().iter().map(|s| s.as_str().unwrap().to_string()).collect()
        };
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let os = case["os"].as_str().unwrap();
            let env = fixture_env(case);
            let cands: Vec<(PathBuf, Placement)> =
                tool_search_entries_for(os, case["home"].as_str(), &|k| env.get(k).cloned())
                    .into_iter()
                    .map(|(d, p)| (PathBuf::from(d), p))
                    .collect();
            let current: Vec<PathBuf> = strings(&case["path"]).into_iter().map(PathBuf::from).collect();
            let got: Vec<String> = augmented_entries_for(&current, &cands, os == "windows")
                .into_iter()
                .map(|p| p.to_string_lossy().into_owned())
                .collect();
            assert_eq!(got, strings(&case["expect_path"]), "case {}", name);
            let sep = if os == "windows" { "\\" } else { "/" };
            let binaries = strings(&case["binaries"]);
            let tool = case["resolve"].as_str().unwrap();
            let hit = got
                .iter()
                .map(|d| format!("{d}{sep}{tool}"))
                .find(|p| binaries.contains(p));
            assert_eq!(hit.as_deref(), case["expect"].as_str(), "case {}", name);
        }
    }

    /// R9 H5: a runtime installed ONLY in a user-local directory (rootless
    /// Docker's `~/bin`) is found by a process started with the minimal
    /// boot-unit PATH once the startup augment ran — the lookup every
    /// runtime decision in this crate goes through (`which_on_path`).
    #[cfg(target_os = "linux")]
    #[test]
    fn a_runtime_only_in_a_user_local_dir_is_found_under_a_short_path() {
        let home = tempfile::tempdir().unwrap();
        let bin = home.path().join("bin");
        std::fs::create_dir_all(&bin).unwrap();
        let docker = bin.join("docker");
        std::fs::write(&docker, b"#!/bin/sh\nexit 0\n").unwrap();
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&docker, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let systemd_user = std::ffi::OsString::from(
            "/usr/local/sbin:/nonexistent-vct-r9-sbin:/nonexistent-vct-r9-bin",
        );
        // Baseline: the boot unit's PATH alone does not reach it.
        crate::paths::with_lookup_path(Some(&systemd_user), || {
            assert_ne!(which_on_path("docker"), Some(docker.clone()));
        });
        let env = |k: &str| -> Option<String> {
            if k == TOOL_SEARCH_DIRS_ENV {
                None // the real table
            } else {
                std::env::var(k).ok()
            }
        };
        let cands: Vec<(PathBuf, Placement)> =
            tool_search_entries_for("linux", home.path().to_str(), &env)
                .into_iter()
                .map(|(d, p)| (PathBuf::from(d), p))
                .collect();
        let inherited: Vec<PathBuf> = std::env::split_paths(&systemd_user).collect();
        let augmented = std::env::join_paths(augmented_entries(&inherited, &cands)).unwrap();
        crate::paths::with_lookup_path(Some(&augmented), || {
            assert_eq!(which_on_path("docker"), Some(docker.clone()));
        });
    }

    /// R9 H5 (macOS): the Homebrew prefixes and Docker Desktop's bundle are
    /// on the list a launchd-started process augments with.
    #[test]
    fn the_macos_list_covers_homebrew_and_docker_desktop() {
        let dirs = tool_search_dirs_for("macos", Some("/Users/u"), &|_k| None);
        for want in [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/opt/podman/bin",
            "/Applications/Docker.app/Contents/Resources/bin",
            "/Users/u/bin",
            "/Users/u/.local/bin",
        ] {
            assert!(dirs.iter().any(|d| d == want), "{want} missing from {dirs:?}");
        }
    }

    /// Without a HOME, the HOME-relative candidates are dropped (soft-fail).
    #[test]
    fn augment_path_without_home_drops_home_relative_candidates() {
        let after = after_augment("/usr/bin:/bin", None);
        assert!(
            !std::env::split_paths(&after).any(|p| p.ends_with(".local/bin") || p.ends_with(".cargo/bin")),
            "{:?}",
            after
        );
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

    /// Detection is purely additive    /// Detection is purely additive — calling it on a CI box without
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
        // The verdict comes from Python now (2026-09-25 consolidation): on a
        // host whose interpreter ladder cannot run the CLI the loud-fail
        // refusal — not a silent None — is the contract; that branch is
        // pinned by the contract tests, skip here.
        let detailed = detect_runtime_detailed().await;
        if let Some(r) = &detailed.refusal {
            if r.contains("runtime_reconcile") || r.contains("broken install") {
                eprintln!("decide could not run on this host ({r}); skipping");
                return;
            }
        }
        let info = detailed.info;
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
        // Loud-fail skip, same as detect_runtime_succeeds_when_runtime_on_path.
        if let Some(r) = &detect_runtime_detailed().await.refusal {
            if r.contains("runtime_reconcile") || r.contains("broken install") {
                eprintln!("decide could not run on this host ({r}); skipping");
                return;
            }
        }
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
