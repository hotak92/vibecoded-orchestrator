// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Preflight checks that gate the install pipeline at the GUI level.
//!
//! v0.2.35 (Agent M, 2026-05-26):
//!
//! Why this exists alongside `installer_engine::detect_container_runtime`:
//! the engine function is called DEEP inside `run_install` — by the time
//! it errors with "no container runtime found", the user has already
//! clicked Install, watched the spinner spin for a few seconds, and now
//! sees a one-line error string in a toast. That error doesn't tell them
//! HOW to fix it.
//!
//! This module runs ABOVE the engine, at the GUI click handler boundary
//! in `ModuleCatalog.svelte::handleInstall`. It produces a structured
//! `RuntimeAvailability` shape that the frontend turns into a modal with
//! an OS-aware "Install Podman" link + a "Detect again" affordance.
//!
//! Why NOT reuse the boot-time `NoContainerRuntimeDialog` flow:
//!   - The boot dialog listens for an event emitted by
//!     `commands::lifecycle::auto_start_on_boot` that fires exactly once
//!     per launcher boot. A user who installed the launcher with a
//!     working runtime, then uninstalled the runtime later, would NEVER
//!     see the boot dialog re-fire — the install click would just fail
//!     deep in the pipeline.
//!   - This preflight runs on EVERY install click, so the gate is
//!     transactional: runtime present right now → proceed; runtime
//!     missing right now → block + explain.
//!
//! The new Tauri command is `check_container_runtime_available`. It uses
//! the SAME `services::runtime::detect_runtime` helper that
//! `runtime_install::runtime_recheck` uses, so the "Detect again" button
//! on this modal and the one on the boot-time modal converge to the same
//! truth source.

use serde::{Deserialize, Serialize};

use vct_launcher_core::services::runtime_verdict::{self, RuntimeVerdict};

/// Result of the install-pipeline preflight check.
///
/// `detected` is `Some("podman" | "docker")` when at least one runtime
/// is on PATH (and its `--version` probe succeeded inside `detect_runtime`).
/// `install_url` is OS-specific and points at a user-friendly install
/// page; the frontend opens it via `runtime_open_install_url` (the
/// existing allowlist-guarded opener command), NOT via direct `<a href>`,
/// to keep the URL gating server-side.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RuntimeAvailability {
    /// True iff `detect_runtime` returned a usable runtime.
    pub available: bool,
    /// `"podman"` | `"docker"` when present, `None` otherwise. The string
    /// matches `ContainerRuntime::binary()` so the frontend can render it
    /// verbatim ("Detected runtime: podman").
    pub detected: Option<String>,
    /// `"linux"` | `"macos"` | `"windows"` | `"unknown"`. Drives which
    /// install URL the modal links to.
    pub platform: String,
    /// Canonical install-instructions URL for the current platform. The
    /// frontend passes this to `runtime_open_install_url`, which enforces
    /// an allowlist (see `commands::runtime_install`). `None` for
    /// unknown platforms or when a runtime IS already available (no link
    /// needed in the success case).
    pub install_url: Option<String>,
    /// The pinned runtime — `VCT_CONTAINER_RUNTIME` when it names one, else
    /// the install's `state/install/runtime.txt` record (R7b F5). v0.2.92
    /// BLOCKER-4: a pin is honoured or REFUSED, never swapped for the other
    /// runtime — podman and docker have per-runtime named volumes, so
    /// driving the other one would bring the stack up on an empty data plane.
    pub pinned: Option<String>,
    /// WHERE the pin came from, so the modal names the knob to turn:
    /// `"VCT_CONTAINER_RUNTIME"`, or the ABSOLUTE path of the runtime.txt
    /// record. `None` when nothing is pinned.
    pub pinned_via: Option<String>,
    /// True when a pin is set and nothing resolved — i.e. the runtime the
    /// user pinned is the one that is unusable. Lets the modal say "podman is
    /// pinned but unusable" instead of the false "no container runtime is
    /// installed" it used to show a user whose podman was merely stopped.
    pub pinned_unusable: bool,
    /// Whether the pinned runtime's binary is on PATH at all. Splits the two
    /// remedies: `true` → start it; `false` → install it, or repin.
    pub pinned_installed: bool,
    /// The OTHER runtime, named only when it is usable and the pinned one is
    /// not — the user's repin target. The launcher does NOT switch to it on
    /// its own, for the same volume reason.
    pub alternative_usable: Option<String>,
    /// Why the install's STALE runtime record was not switched to the other
    /// runtime (v0.2.97 R11 L6) — the shared table's wording
    /// (`vco_lib/runtime_reconcile_messages.toml`), the same reason every
    /// other surface gives. `None` unless the pin is refused AND the
    /// stale-record reconcile declined.
    #[serde(default)]
    pub not_switched: Option<String>,
}

/// Resolve the canonical install URL for the current OS. Mirrors the
/// URLs the boot-time `NoContainerRuntimeDialog` offers — same allowlist
/// applies on the opener side, so all three URLs are accepted.
///
/// - Linux: podman.io's canonical install page. Linux distros vary
///   wildly (apt/dnf/pacman/zypper); the page has per-distro tabs. The
///   boot-time dialog can elevate to install via pkexec; this preflight
///   just links to the docs because auto-install would re-implement that
///   whole flow.
/// - macOS: Podman Desktop's macOS download page — most user-friendly
///   path on Mac (the .dmg wraps `podman machine init`).
/// - Windows: Podman Desktop's Windows download page — handles the
///   WSL2 prerequisite-checking inside its installer.
fn install_url_for(platform: &str) -> Option<String> {
    match platform {
        "linux" => Some("https://podman.io/docs/installation".to_string()),
        "macos" => Some("https://podman-desktop.io/downloads/macos".to_string()),
        "windows" => Some("https://podman-desktop.io/downloads/windows".to_string()),
        _ => None,
    }
}

/// Normalize `std::env::consts::OS` to the three platforms the frontend
/// renders branches for. Unknown OS values surface as `"unknown"` so the
/// frontend can show a generic "install a container runtime" message
/// without crashing on an unmapped string.
fn current_platform() -> String {
    match std::env::consts::OS {
        "linux" => "linux".into(),
        "macos" => "macos".into(),
        "windows" => "windows".into(),
        other => {
            // freebsd, dragonfly, netbsd, openbsd, etc. — VCT services
            // theoretically work via Podman on these, but we don't ship
            // tested install URLs for them. Falling back to "unknown"
            // lets the modal render a generic message.
            let _ = other;
            "unknown".into()
        }
    }
}

/// GUI-level preflight: check whether a container runtime is currently
/// available so the install pipeline can proceed.
///
/// Cache discipline: invalidates the `services::runtime` cache before
/// probing, so a "Detect again" click from the modal reflects the
/// current state of PATH rather than a stale cached `None` from boot.
/// The boot-time runtime probe uses the same cache, so a successful
/// detection here also unblocks the cached value for the rest of the
/// session.
///
/// Returns `Err` in exactly ONE case (v0.2.97 R12 loud-fail): the ONE
/// verdict itself could not run — a missing or broken Python is a broken
/// install and is surfaced, never papered over with `available: false`.
/// Every runtime-state answer (refused pin, nothing installed, no
/// compose) is the structured `RuntimeAvailability` shape that drives
/// the modal's branches.
#[tauri::command]
pub async fn check_container_runtime_available() -> Result<RuntimeAvailability, String> {
    // Always re-probe — the user may have installed/uninstalled a
    // runtime since the launcher booted. Both the session detection
    // cache and the ONE verdict cache are dropped, and the surfaces that
    // share the verdict cache (boot, hub watchdog) re-probe with the
    // fresh answer too.
    crate::services::runtime::invalidate_cache();
    runtime_verdict::invalidate();

    // v0.2.97 R12: the modal's every field is a rendering of the ONE
    // Python verdict (`vco_lib.runtime_reconcile decide --json`) —
    // pinned / pinned_installed / alternative_usable / not_switched
    // come from the verdict instead of re-derived by Rust probes.
    let root = vct_launcher_core::orchestrator_manifest::orchestrator_install_root();
    let verdict = runtime_verdict::decide(
        root.as_deref(),
        runtime_verdict::Mode::ReadOnly,
        runtime_verdict::Purpose::Infra,
    )
    .await?;

    Ok(availability(&verdict, current_platform()))
}

/// The modal's shape from the ONE verdict — pure, so every branch is
/// testable without a runtime (or a Python) on the machine. A resolved
/// verdict with compose is "available"; everything else renders the pin
/// fields from the verdict (`requested`, `requested_via`,
/// `requested_installed`, `alternative_usable`) and the stale-record
/// reason (`not_switched`) — Python's wording, verbatim (M3).
fn availability(verdict: &RuntimeVerdict, platform: String) -> RuntimeAvailability {
    let available = verdict.resolved
        && verdict.runtime.is_some()
        && verdict.compose_form.is_some();
    let detected = if available { verdict.runtime.clone() } else { None };
    let install_url = if available { None } else { install_url_for(&platform) };
    let pinned = if available { None } else { verdict.requested.clone() };
    let pinned_via = pinned.as_ref().map(|_| pin_source_label(&verdict.requested_via));
    RuntimeAvailability {
        available,
        detected,
        platform,
        install_url,
        pinned_unusable: pinned.is_some(),
        pinned,
        pinned_via,
        pinned_installed: verdict.requested_installed,
        alternative_usable: verdict.alternative_usable.clone(),
        not_switched: if available { None } else { verdict.not_switched.clone() },
    }
}

/// What the modal shows as the pin's origin: the env var's name, or the
/// ABSOLUTE path of the runtime.txt record (the file the user would
/// edit) — mapped from the verdict's `requested_via` (`env` /
/// `record` / `confirmed`).
fn pin_source_label(requested_via: &str) -> String {
    match requested_via {
        "env" => "VCT_CONTAINER_RUNTIME".to_string(),
        _ => vct_launcher_core::orchestrator_manifest::orchestrator_install_root()
            .map(|root| {
                root.join("state").join("install").join("runtime.txt").display().to_string()
            })
            .unwrap_or_else(|| "state/install/runtime.txt".to_string()),
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;

    /// R11 L6 / R12 M3: the stale-record reason reaches the modal on the
    /// refused-pin path — and only there — rendered from the VERDICT's
    /// `not_switched` (Python's text; nothing is re-derived from PATH).
    #[test]
    fn the_modal_carries_why_the_stale_record_was_not_switched() {
        let why = "docker is not installed; podman holds none of VCO's data";
        let refused = refused_verdict(why);
        let av = availability(&refused, "linux".into());
        assert!(!av.available && av.pinned_unusable);
        assert_eq!(av.pinned.as_deref(), Some("docker"));
        assert_eq!(av.pinned_installed, false);
        assert_eq!(av.alternative_usable.as_deref(), Some("podman"));
        assert_eq!(av.not_switched.as_deref(), Some(why));
        // The field serialises under the name the frontend reads.
        let json = serde_json::to_value(&av).unwrap();
        assert_eq!(json["not_switched"], serde_json::json!(why));
        // No pin → nothing to explain; a resolved verdict → nothing either.
        let mut unpinned = refused.clone();
        unpinned.requested = None;
        unpinned.requested_via = "auto".into();
        unpinned.not_switched = None;
        assert_eq!(availability(&unpinned, "linux".into()).not_switched, None);
        let mut quiet = refused_verdict(why);
        quiet.resolved = true;
        quiet.refused = false;
        quiet.refusal = None;
        quiet.runtime = Some("podman".into());
        quiet.compose_form = Some("subcommand".into());
        quiet.requested = None;
        quiet.requested_via = "auto".into();
        let ok = availability(&quiet, "linux".into());
        assert!(ok.available && ok.not_switched.is_none());
    }

    /// A refused-pin verdict shape for [`availability`] tests.
    fn refused_verdict(why: &str) -> RuntimeVerdict {
        RuntimeVerdict {
            runtime: None,
            state: "absent".into(),
            resolved: false,
            compose: None,
            compose_form: None,
            binary_path: None,
            search_path: None,
            installed: Some("podman".into()),
            requested: Some("docker".into()),
            requested_via: "record".into(),
            requested_installed: false,
            alternative_usable: Some("podman".into()),
            record_reconciled: false,
            outcome: "unusable".into(),
            not_switched_key: Some("no_data".into()),
            not_switched: Some(why.into()),
            same_engine: false,
            refused: true,
            refusal: Some("refused (not switched)".into()),
            reason: "why".into(),
        }
    }

    #[test]
    fn the_pin_source_names_the_env_var_or_the_record_file() {
        assert_eq!(pin_source_label("env"), "VCT_CONTAINER_RUNTIME");
        let record = pin_source_label("record");
        assert!(
            record.ends_with("runtime.txt"),
            "the record label must name the file: {record}"
        );
    }

    #[test]
    fn install_url_known_platforms_resolve() {
        // Each of the three first-tier platforms must map to a non-empty
        // canonical URL that the runtime_install opener's allowlist accepts.
        let linux = install_url_for("linux").expect("linux url present");
        let macos = install_url_for("macos").expect("macos url present");
        let windows = install_url_for("windows").expect("windows url present");

        // Each URL must start with one of the allowlisted prefixes from
        // `runtime_install::ALLOWED_INSTALL_URL_PREFIXES`. We can't
        // import the const (it's private to the module) but we can
        // assert the prefix shape — if the constants drift, this test
        // catches the drift before the runtime_open_install_url call
        // rejects the URL at click time.
        assert!(
            linux.starts_with("https://podman.io/"),
            "linux URL must use podman.io prefix"
        );
        assert!(
            macos.starts_with("https://podman-desktop.io/"),
            "macos URL must use podman-desktop.io prefix"
        );
        assert!(
            windows.starts_with("https://podman-desktop.io/"),
            "windows URL must use podman-desktop.io prefix"
        );
    }

    #[test]
    fn install_url_unknown_platform_returns_none() {
        // freebsd / unknown / empty / random — all None so the frontend
        // shows the generic message rather than linking to an irrelevant
        // OS-specific page.
        assert!(install_url_for("freebsd").is_none());
        assert!(install_url_for("unknown").is_none());
        assert!(install_url_for("").is_none());
        assert!(install_url_for("plan9").is_none());
    }

    #[test]
    fn current_platform_returns_lowercase_known_or_unknown() {
        // Whichever platform the test runs on, the returned string must
        // be one of the four allowed values. This protects future
        // refactors from accidentally returning the raw arch suffix or
        // a capitalized variant.
        let p = current_platform();
        assert!(
            matches!(p.as_str(), "linux" | "macos" | "windows" | "unknown"),
            "current_platform returned unexpected value: {}",
            p
        );
    }

    // ───────────────────────────────────────────────────────────────
    // The command's REAL contract, pinned hermetically (R12-bis): Ok —
    // with the shape the modal renders — on a healthy install, Err (the
    // loud broken-install fail) when vco_lib cannot import. The test
    // this replaces asserted "never Err", which stopped being the
    // contract in v0.2.97 R12.
    //
    // Hermeticity: the command takes no install root — it walks
    // `orchestrator_install_root()`, which under `cargo test` resolves
    // to THIS checkout, whose `vco_lib` is what the decide child
    // imports (child cwd == root; `python -m` puts cwd first on
    // sys.path). So `storage_ux::fake_runtime_support::
    // use_checkout_vco_lib` has nothing to add here — what the HOST
    // could leak in is pinned instead: `$VCT_VENV` names the
    // interpreter (the ladder's first tier, so the install-root tiers
    // never run), the lookup PATH is a stub dir, the pin is cleared and
    // the tool-search table emptied (the child can never probe the
    // host's real podman/docker). The runtime caches are invalidated by
    // the command itself.
    // ───────────────────────────────────────────────────────────────
    #[cfg(test)]
    mod hermetic {
        use super::*;
        use std::path::{Path, PathBuf};

        /// A fake runtime binary on the stub lookup PATH. POSIX reuses the
        /// shared `fake_runtime_support::fake_runtime` (shell grammar);
        /// Windows gets a local `.cmd` twin answering the same probe
        /// grammar the Python resolver drives (`version` / `--version` /
        /// `info` / `compose version` → exit 0, everything else → 1).
        /// These stubs were the only thing keeping this module unix-only
        /// (R12-bis N4(b)): the Err/Ok contract itself is OS-neutral.
        #[cfg(unix)]
        fn fake_runtime(dir: &Path, name: &str) {
            crate::commands::storage_ux::fake_runtime_support::fake_runtime(dir, name, &[]);
        }

        #[cfg(windows)]
        fn fake_runtime(dir: &Path, name: &str) {
            // Found by the Python child's `shutil.which` via PATHEXT
            // (.CMD is on the default PATHEXT); CreateProcess runs .cmd
            // through cmd.exe, so `exit /b` codes propagate.
            //
            // Quoting (v0.2.97 follow-up): the stub's path may live
            // under a %TEMP% containing spaces. Every spawner on the
            // chain passes an ARG VECTOR, never a pre-built string —
            // Rust std (>=1.77.2) applies the BatBadBut cmd.exe
            // quoting rules when the program is a .bat/.cmd, and
            // Python's list2cmdline quotes spaced paths — so the path
            // itself arrives correctly quoted either way. The residual
            // risk was INSIDE the script: `%1` KEEPS the caller's
            // surrounding quotes, which would make `if "%1"=="info"`
            // compare `"info"` against `info` and miss (stub answers 1
            // → the Ok legs fail spuriously). `%~1`/`%~2` strip the
            // surrounding quotes, so the probe grammar matches under
            // either caller quoting. The script references no paths of
            // its own — only these literals — so nothing else in it
            // needs quoting.
            let script = concat!(
                "@echo off\r\n",
                "if \"%~1 %~2\"==\"compose version\" exit /b 0\r\n",
                "if \"%~1\"==\"info\" exit /b 0\r\n",
                "if \"%~1\"==\"--version\" exit /b 0\r\n",
                "if \"%~1\"==\"version\" exit /b 0\r\n",
                "exit /b 1\r\n",
            );
            std::fs::write(dir.join(format!("{name}.cmd")), script).unwrap();
        }

        /// A vco_lib-capable interpreter for the Ok legs: the resolver's
        /// own ladder answer, else the OS's system interpreter. `None`
        /// means this host cannot run the Ok contract at all — skip
        /// rather than fail (the same discipline as `services::runtime`'s
        /// on-host probes).
        fn vco_lib_python() -> Option<PathBuf> {
            if let Some(p) = vct_launcher_core::python_resolve::resolve_python_for_vco_lib() {
                return Some(p);
            }
            #[cfg(unix)]
            {
                let system = PathBuf::from("/usr/bin/python3");
                if system.is_file() {
                    return Some(system);
                }
                vct_launcher_core::paths::which_on_path("python3")
            }
            #[cfg(windows)]
            {
                // `python` (PATH, e.g. the runner's toolchain) else the
                // `py` launcher — both accept `-m` / `-c` unchanged.
                vct_launcher_core::paths::which_on_path("python")
                    .or_else(|| vct_launcher_core::paths::which_on_path("py"))
            }
        }

        /// True when `py` can import `vco_lib.runtime_reconcile` with the
        /// CHECKOUT as cwd — exactly what the decide child does.
        fn imports_vco_lib(py: &Path) -> bool {
            let checkout = Path::new(env!("CARGO_MANIFEST_DIR"))
                .ancestors()
                .nth(2)
                .expect("CARGO_MANIFEST_DIR reaches the checkout root");
            std::process::Command::new(py)
                .arg("-c")
                .arg("import vco_lib.runtime_reconcile")
                .current_dir(checkout)
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .status()
                .map(|s| s.success())
                .unwrap_or(false)
        }

        /// A `$VCT_VENV` that IS a broken interpreter: every invocation
        /// prints vco_lib's can't-import error and exits 1 — the shape of
        /// an install whose venv lost its vco_lib (loud-fail territory).
        /// POSIX: a `#!/bin/sh` script; Windows: a `.cmd` twin (the
        /// resolver accepts any existing file as the interpreter-binary
        /// shape, and CreateProcess runs `.cmd` through cmd.exe, so the
        /// stderr text and the exit code propagate to the decide child's
        /// error surface). Quoting (v0.2.97 follow-up): the stub path
        /// may live under a `%TEMP%` containing spaces — the decide
        /// child spawns it via Rust std's arg-vector `Command::new(path)`
        /// (tokio delegates to std), and std >=1.77.2 applies the
        /// BatBadBut cmd.exe quoting rules for `.bat`/`.cmd` programs,
        /// so a spaced path is quoted correctly and the script RUNS
        /// rather than failing to spawn. The script body references no
        /// paths — only literals — so it holds no further quoting
        /// surface. $VCT_VENV carries the path as a plain env var (never
        /// a command line), which needs no quoting.
        #[cfg(unix)]
        fn broken_python(dir: &Path) -> PathBuf {
            use std::os::unix::fs::PermissionsExt;
            let script = dir.join("python");
            std::fs::write(
                &script,
                "#!/bin/sh\necho \"ModuleNotFoundError: No module named 'vco_lib'\" >&2\nexit 1\n",
            )
            .unwrap();
            std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
            script
        }

        #[cfg(windows)]
        fn broken_python(dir: &Path) -> PathBuf {
            let script = dir.join("python.cmd");
            std::fs::write(
                &script,
                "@echo off\r\necho ModuleNotFoundError: No module named 'vco_lib' 1>&2\r\nexit /b 1\r\n",
            )
            .unwrap();
            script
        }

        /// Run the command under the pinned environment: `venv` as
        /// `$VCT_VENV` (absolute interpreter path — the ladder's
        /// interpreter-binary shape), `bin` as the only lookup PATH, pin
        /// cleared, tool-search table emptied — on a current-thread
        /// runtime, under the workspace env lock.
        fn with_pinned_env<T>(
            venv: &Path,
            bin: &Path,
            fut: impl std::future::Future<Output = T>,
        ) -> T {
            let venv = venv.display().to_string();
            let mut out = None;
            vct_launcher_core::test_env::with_env_vars(
                &[
                    ("VCT_VENV", Some(venv.as_str())),
                    // The ladder must stop at $VCT_VENV — the host's
                    // install-root vars never reach it.
                    ("VCT_INSTALL_ROOT", None),
                    ("VCT_ORCHESTRATOR_ROOT", None),
                    ("VCT_CONTAINER_RUNTIME", None),
                    // An empty value REPLACES the table.
                    ("VCT_TOOL_SEARCH_DIRS", Some("")),
                ],
                || {
                    let rt = tokio::runtime::Builder::new_current_thread()
                        .enable_all()
                        .build()
                        .unwrap();
                    out = Some(vct_launcher_core::paths::with_lookup_path(
                        Some(bin.as_os_str()),
                        || rt.block_on(fut),
                    ));
                },
            );
            out.unwrap()
        }

        #[test]
        fn check_is_ok_and_available_on_a_healthy_install() {
            let Some(py) = vco_lib_python() else {
                eprintln!("skip: no vco_lib-capable python on this host");
                return;
            };
            if !imports_vco_lib(&py) {
                eprintln!(
                    "skip: {} cannot import vco_lib.runtime_reconcile from the checkout",
                    py.display()
                );
                return;
            }
            // A fake podman answering the resolver's probes (version /
            // info / compose version) on the stub PATH — nothing real.
            let dir = tempfile::tempdir().unwrap();
            fake_runtime(dir.path(), "podman");

            let r = with_pinned_env(&py, dir.path(), async {
                check_container_runtime_available().await
            })
            .expect("healthy install: the verdict runs, so Ok");

            // Deterministic branch: podman + compose answered on the
            // stub PATH, so the modal gets "available" with the detected
            // name — never an install URL.
            assert!(r.available);
            assert_eq!(r.detected.as_deref(), Some("podman"));
            assert!(r.install_url.is_none());
            assert!(
                matches!(r.platform.as_str(), "linux" | "macos" | "windows" | "unknown"),
                "platform must be a known value, got: {}",
                r.platform
            );
            // v0.2.92 BLOCKER-4 invariants: an available runtime is
            // never also a refused pin.
            assert!(r.pinned.is_none() && !r.pinned_unusable && !r.pinned_installed);
            assert!(r.alternative_usable.is_none());
        }

        #[test]
        fn check_is_ok_but_not_available_when_no_runtime_exists() {
            let Some(py) = vco_lib_python() else {
                eprintln!("skip: no vco_lib-capable python on this host");
                return;
            };
            if !imports_vco_lib(&py) {
                eprintln!(
                    "skip: {} cannot import vco_lib.runtime_reconcile from the checkout",
                    py.display()
                );
                return;
            }
            // A stub PATH with NO runtime binaries at all: the verdict
            // resolves "nothing installed" — a runtime STATE, so still
            // Ok, rendered by the modal's not-installed branch.
            let dir = tempfile::tempdir().unwrap();

            let r = with_pinned_env(&py, dir.path(), async {
                check_container_runtime_available().await
            })
            .expect("no runtime installed is a state, not a failure: Ok");

            assert!(!r.available);
            assert!(r.detected.is_none());
            if r.platform == "unknown" {
                assert!(r.install_url.is_none());
            } else {
                assert!(
                    r.install_url.is_some(),
                    "not-available on a known platform offers the install URL"
                );
            }
            // No pin was set, so no pin is rendered.
            assert!(r.pinned.is_none() && !r.pinned_unusable && !r.pinned_installed);
            assert!(r.alternative_usable.is_none());
        }

        #[test]
        fn check_errs_loudly_when_vco_lib_cannot_import() {
            let stub_dir = tempfile::tempdir().unwrap();
            let py = broken_python(stub_dir.path());
            let empty_bin = tempfile::tempdir().unwrap();

            let err = with_pinned_env(&py, empty_bin.path(), async {
                check_container_runtime_available().await
            })
            .expect_err(
                "a venv that cannot import vco_lib is a broken install: Err, \
                 never a silent available:false",
            );

            // The loud-fail message names the failed verdict child and
            // carries Python's own diagnosis — it must NOT be the
            // not-installed shape the runtime download dialog renders.
            // (The child died before printing JSON, so the arm that
            // fires is the unreadable-output one; either way the name
            // and the stderr diagnosis are the contract.)
            assert!(
                err.contains("runtime_reconcile decide"),
                "Err must name the failed decide child, got: {err}"
            );
            assert!(
                err.contains("No module named 'vco_lib'"),
                "Err must carry Python's can't-import diagnosis, got: {err}"
            );
        }
    }
}
