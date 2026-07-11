//! Version parsing + on-disk install-path status helpers.
//!
//! Verbatim extraction (v0.2.77 Part 7d) of the version readers
//! (`read_source_version`, `read_manifest_version`, `read_min_upgradable_from`,
//! `read_on_disk_binary_version`, `read_on_disk_hub_version`), the semver
//! comparators (`parse_version_tuple`, `version_is_below_floor`,
//! `update_requires_hard_cut`), and the launcher dist-slot resolvers
//! (`launcher_dist_subdir`, `launcher_binary_filename`) that previously lived
//! inline in `installer.rs`. Behaviour is unchanged; the facade re-exports
//! every symbol so call-sites + the `installer::tests` module resolve them via
//! `super::*`.
//!
//! CROSS-LANGUAGE PARITY: the dist-subdir literals + POSIX resolver here are
//! scanned by `tests/test_launcher_dist_subdir_parity.py`, which globs the
//! installer submodule set so the parity check follows the code.

use std::path::Path;

/// Read source version from `<install_path>/vct-module.json::version`.
/// Used by `check_for_updates` to compute `install_stale`. Returns
/// `None` if the file is missing or doesn't have a usable version field.
pub(crate) fn read_source_version(install_path: &Path) -> Option<String> {
    let vct_module = install_path.join("vct-module.json");
    if let Ok(txt) = std::fs::read_to_string(&vct_module) {
        if let Ok(val) = serde_json::from_str::<serde_json::Value>(&txt) {
            if let Some(s) = val.get("version").and_then(|v| v.as_str()) {
                if !s.is_empty() {
                    return Some(s.to_string());
                }
            }
        }
    }
    None
}

/// Read `state/install-manifest.json::version`. Returns `None` if the
/// manifest doesn't exist (fresh install never completed) or doesn't
/// contain a non-empty version string.
pub(crate) fn read_manifest_version(install_path: &Path) -> Option<String> {
    let manifest = install_path
        .join("state")
        .join("install-manifest.json");
    if let Ok(txt) = std::fs::read_to_string(&manifest) {
        if let Ok(val) = serde_json::from_str::<serde_json::Value>(&txt) {
            if let Some(s) = val.get("version").and_then(|v| v.as_str()) {
                if !s.is_empty() {
                    return Some(s.to_string());
                }
            }
        }
    }
    None
}

/// v0.2.60 (Piece 5): read `vct-module.json::min_upgradable_from` — the
/// oldest installed version this release can update IN-PLACE from. Below
/// this floor, the update routes to the guided hard-cut instead of an
/// in-place pull. Returns `None` when the field is absent (older manifests)
/// or empty — callers treat `None` as "no floor declared → never hard-cut".
pub(crate) fn read_min_upgradable_from(install_path: &Path) -> Option<String> {
    let vct_module = install_path.join("vct-module.json");
    let txt = std::fs::read_to_string(&vct_module).ok()?;
    let val = serde_json::from_str::<serde_json::Value>(&txt).ok()?;
    let s = val.get("min_upgradable_from").and_then(|v| v.as_str())?;
    if s.is_empty() {
        None
    } else {
        Some(s.to_string())
    }
}

/// Parse a `major.minor.patch` version string into a comparable tuple.
/// The orchestrator uses plain numeric `0.2.x` versions (no pre-release /
/// build metadata — see `bump-version.sh`), so a 3-int tuple is sufficient.
/// Missing components default to 0; non-numeric components make the parse
/// fail (returns `None`) so a malformed version can never be silently
/// treated as `0.0.0` and wrongly trip the floor.
pub(crate) fn parse_version_tuple(v: &str) -> Option<(u64, u64, u64)> {
    let v = v.trim().trim_start_matches('v');
    let mut parts = v.split('.');
    let major = parts.next()?.parse::<u64>().ok()?;
    // minor/patch default to 0 when absent (e.g. "1" → (1,0,0)), but a
    // PRESENT-but-non-numeric component is a hard failure.
    let minor = match parts.next() {
        Some(s) => s.parse::<u64>().ok()?,
        None => 0,
    };
    let patch = match parts.next() {
        Some(s) => s.parse::<u64>().ok()?,
        None => 0,
    };
    Some((major, minor, patch))
}

/// True iff `installed` is strictly below the `floor` version (= an in-place
/// update is NOT supported and the hard-cut path applies). Fail-SAFE: if
/// either version can't be parsed, returns `false` (never force a
/// destructive hard-cut on a parse ambiguity — prefer the in-place attempt,
/// which surfaces its own errors).
pub(crate) fn version_is_below_floor(installed: &str, floor: &str) -> bool {
    match (parse_version_tuple(installed), parse_version_tuple(floor)) {
        (Some(i), Some(f)) => i < f,
        _ => false,
    }
}

/// v0.2.60 (Piece 5): decide whether an update from the installed version
/// must take the guided hard-cut path (installed < the source manifest's
/// `min_upgradable_from`) rather than an in-place pull.
///
/// INERT in v0.2.60: the shipped floor is `"0.0.0"` (vct-module.json), and
/// no real install is below `0.0.0`, so this ALWAYS returns false today.
/// v0.3.0 raises the floor to declare the first real hard-cut boundary;
/// only then does this gate ever open. Returns false when no floor is
/// declared or the installed version is unknown (fresh install → there's
/// nothing to upgrade-from, the normal install path runs).
pub(crate) fn update_requires_hard_cut(install_path: &Path) -> bool {
    let Some(floor) = read_min_upgradable_from(install_path) else {
        return false; // no floor declared → never hard-cut
    };
    let Some(installed) = read_manifest_version(install_path) else {
        return false; // never completed an install here → normal install path
    };
    version_is_below_floor(&installed, &floor)
}

/// Read the on-disk binary version from
/// `launcher/dist/<arch>/<launcher-binary>.metadata.json::launcher_version`.
/// The arch subdir is selected via `launcher_dist_subdir()` and the
/// binary filename via `launcher_binary_filename()` — both mirror the
/// `commands::restart::launcher_binary_relative_path` pattern. On
/// Windows the sidecar is `vct-launcher.exe.metadata.json` (with `.exe.`
/// infix) because `scripts/build-bundled-launcher.sh` stages it as
/// `${DEST}.metadata.json` where `$DEST` already carries the `.exe`
/// extension. v0.2.45 V45-H fixed a hardcoded path that only resolved
/// on Linux/macOS — on Windows the lookup returned `None`, which made
/// V45-B's `wait_for_binary_refresh` always time out after 5 minutes
/// (FINDING C1 of the v0.2.45 pre-tag review).
pub(crate) fn read_on_disk_binary_version(install_path: &Path) -> Option<String> {
    let subdir = launcher_dist_subdir();
    let binary = launcher_binary_filename();
    let meta_path = install_path
        .join("launcher")
        .join("dist")
        .join(subdir)
        .join(format!("{}.metadata.json", binary));
    if let Ok(txt) = std::fs::read_to_string(&meta_path) {
        if let Ok(val) = serde_json::from_str::<serde_json::Value>(&txt) {
            if let Some(s) = val.get("launcher_version").and_then(|v| v.as_str()) {
                if !s.is_empty() {
                    return Some(s.to_string());
                }
            }
        }
    }
    None
}

/// v0.2.55 (hub-freshness gap): read the on-disk vct-hub binary version
/// from its dist sidecar `launcher/dist/<subdir>/vct-hub[.exe].metadata.json`.
/// The hub metadata uses the SAME `launcher_version` field as the launcher
/// sidecar (verified: scripts/build-bundled-launcher.sh writes one schema
/// for all three binaries). Returns None when the sidecar is absent (older
/// installs that predate hub metadata) — the WaitForBinaryRefresh gate
/// treats absent-metadata as "don't block on hub" so it never deadlocks.
pub(crate) fn read_on_disk_hub_version(install_path: &Path) -> Option<String> {
    let subdir = launcher_dist_subdir();
    // Hub dist filename mirrors the launcher's `.exe` suffix rule on
    // Windows (build-bundled-launcher.sh's `${DEST}.metadata.json` includes
    // the extension). vct-hub on POSIX, vct-hub.exe on Windows.
    #[cfg(target_os = "windows")]
    let hub_name = "vct-hub.exe";
    #[cfg(not(target_os = "windows"))]
    let hub_name = "vct-hub";
    let meta_path = install_path
        .join("launcher")
        .join("dist")
        .join(subdir)
        .join(format!("{}.metadata.json", hub_name));
    if let Ok(txt) = std::fs::read_to_string(&meta_path) {
        if let Ok(val) = serde_json::from_str::<serde_json::Value>(&txt) {
            if let Some(s) = val.get("launcher_version").and_then(|v| v.as_str()) {
                if !s.is_empty() {
                    return Some(s.to_string());
                }
            }
        }
    }
    None
}

/// Compile-time per-OS launcher dist subdirectory. Mirror of
/// `install.py::_launcher_binary_relative_path` and the analogous
/// helper in `commands::restart` — kept here to avoid a cross-module
/// dependency from installer.rs into restart.rs.
pub(crate) fn launcher_dist_subdir() -> &'static str {
    #[cfg(target_os = "windows")]
    {
        "windows-x64"
    }
    // v0.2.54 Track C (Intel-Mac fix): Apple Silicon and Intel Macs use
    // different dist slots. Releases only ship `macos-arm64` (release.yml
    // builds arm64 only), but a LOCAL cargo build on an Intel Mac lands
    // in `macos-x64/` — hardcoding arm64 made the launcher read/write
    // the wrong slot on x86_64 hosts. Compile-time arch is correct here:
    // the binary executes on the arch it was built for (Rosetta-translated
    // x86_64 builds correctly resolve macos-x64, matching where their own
    // build artifacts land).
    #[cfg(all(target_os = "macos", target_arch = "x86_64"))]
    {
        "macos-x64"
    }
    #[cfg(all(target_os = "macos", not(target_arch = "x86_64")))]
    {
        "macos-arm64"
    }
    #[cfg(all(not(target_os = "windows"), not(target_os = "macos")))]
    {
        "linux-x64"
    }
}

/// Compile-time per-OS launcher binary filename. Mirror of
/// `commands::restart::launcher_binary_relative_path` (which returns the
/// `(subdir, filename)` pair as a tuple); duplicated here so paths in
/// installer.rs don't have to hardcode `vct-launcher.metadata.json` and
/// silently break on Windows where the actual on-disk sidecar is
/// `vct-launcher.exe.metadata.json` (because `${DEST}.metadata.json` in
/// `scripts/build-bundled-launcher.sh` includes the `.exe` extension).
/// Added in v0.2.45 V45-H — keep in lock-step with the restart helper.
pub(crate) fn launcher_binary_filename() -> &'static str {
    #[cfg(target_os = "windows")]
    {
        "vct-launcher.exe"
    }
    #[cfg(not(target_os = "windows"))]
    {
        "vct-launcher"
    }
}

