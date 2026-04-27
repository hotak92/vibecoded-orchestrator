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
                let mut cmd = TokioCommand::new(&self.binary_path);
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
                TokioCommand::new(resolved)
            }
        }
    }
}

// ---------------------------------------------------------------------------
// PATH resolution
// ---------------------------------------------------------------------------

fn which_on_path(name: &str) -> Option<PathBuf> {
    // On Windows, also try with .exe / .cmd / .bat appended. PATHEXT may
    // contain other extensions but those three cover Podman/Docker.
    #[cfg(windows)]
    let candidates: Vec<String> = vec![
        name.to_string(),
        format!("{}.exe", name),
        format!("{}.cmd", name),
        format!("{}.bat", name),
    ];
    #[cfg(not(windows))]
    let candidates: Vec<String> = vec![name.to_string()];

    let paths = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&paths) {
        for cand in &candidates {
            let p = dir.join(cand);
            if p.is_file() {
                return Some(p);
            }
        }
    }
    None
}

// ---------------------------------------------------------------------------
// Detection
// ---------------------------------------------------------------------------

/// Probe a binary's `--version` flag. Returns true if the binary executes
/// successfully. We don't parse the version string — the binary either
/// runs or it doesn't; "podman from 2018 that lacks compose" surfaces
/// later when the compose-form probe fails.
///
/// Timeout 5s (was 2s pre-2026-04-27): `podman --version` itself is fast,
/// but a freshly-spawned launcher process competing with `first-install`'s
/// final container-restart phase has been observed to time out at 2s
/// under disk I/O pressure on slower machines. A 5s ceiling is still well
/// under any user-perceivable lag if the runtime is genuinely missing.
async fn version_probe(binary: &PathBuf) -> bool {
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(5),
        TokioCommand::new(binary).arg("--version").output(),
    )
    .await;
    let ok = matches!(&result, Ok(Ok(out)) if out.status.success());
    if !ok {
        // Diagnostic log — runtime detection silently returning None has
        // surfaced as a launcher UX bug (firing the no-container modal
        // even when the user has Podman installed). When this fires the
        // user sees a misleading dialog; we want stderr breadcrumbs in
        // the launcher log to prove cause without strace.
        eprintln!(
            "[runtime] version_probe failed for {}: {:?}",
            binary.display(),
            match &result {
                Err(_) => "timeout".to_string(),
                Ok(Err(e)) => format!("spawn error: {}", e),
                Ok(Ok(out)) => format!("non-zero exit {:?}", out.status.code()),
            }
        );
    }
    ok
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
        TokioCommand::new(binary)
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
            TokioCommand::new(path).arg("--version").output(),
        )
        .await;
        if let Ok(Ok(out)) = sa {
            if out.status.success() {
                return Some(ComposeForm::Standalone);
            }
        }
    }

    eprintln!(
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
        TokioCommand::new(binary)
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
    // Preference order: Podman > Docker. Per user policy: "check for
    // availability on podman first".
    for runtime in [ContainerRuntime::Podman, ContainerRuntime::Docker] {
        let bin_path = match which_on_path(runtime.binary()) {
            Some(p) => p,
            None => continue,
        };
        if !version_probe(&bin_path).await {
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
}
