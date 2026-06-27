//! Build identity for the vct-hub binary.
//!
//! v0.2.69 (hub-staleness home #3). Two distinct identity signals let a
//! start path decide whether an ALREADY-RUNNING hub is the current binary
//! or a stale/foreign one:
//!
//!   * **Linux executable inode** — the authoritative signal. `/proc/<pid>/
//!     exe` stat'd yields the inode the process is ACTUALLY running (valid
//!     even after the on-disk file was replaced in place), so comparing it
//!     to OUR own `current_exe()` inode catches BOTH a different-path hub
//!     (borrowed dev build) AND an older build at the same path (in-place
//!     update). This is the same mechanism the launcher's
//!     `hub_launcher::running_hub_is_stale` uses; it is ported here so the
//!     hub's OWN start path (`--start-if-not-running`) is identity-aware,
//!     not only the launcher GUI boot path.
//!
//!   * **Build fingerprint** — a cross-OS fallback for when the inode check
//!     is unavailable (non-Linux, restricted `/proc`). A short git SHA baked
//!     at compile time by `build.rs` into `VCT_HUB_BUILD_FINGERPRINT`,
//!     combined with the compile-time `CARGO_PKG_VERSION`. Recorded on the
//!     lockfile's second line so a different running hub can be detected by
//!     comparing recorded-vs-ours. `None` (released tarball without git, or
//!     a pre-v0.2.69 lockfile) is treated as "unknown" → conservative
//!     fallback, never a false kill.

/// The compile-time build fingerprint, or `None` when `build.rs` could not
/// resolve a git SHA. Format: `"<version>+<short-sha>[-dirty]"`.
pub fn build_fingerprint() -> Option<String> {
    option_env!("VCT_HUB_BUILD_FINGERPRINT")
        .filter(|s| !s.is_empty())
        .map(|sha| format!("{}+{}", env!("CARGO_PKG_VERSION"), sha))
}

/// The fingerprint string to record on the lockfile / expose on `/health`.
/// Falls back to the bare workspace version when no git SHA was baked, so
/// the field is always present and human-meaningful — the same-version-
/// stale blind spot is documented and handled by the inode check on Linux.
pub fn fingerprint_or_version() -> String {
    build_fingerprint().unwrap_or_else(|| env!("CARGO_PKG_VERSION").to_string())
}

/// True only when `build.rs` baked a real git SHA (i.e. the fingerprint is
/// strictly stronger than the bare version). Used to decide whether a
/// fingerprint comparison is trustworthy enough to act on.
pub fn has_git_fingerprint() -> bool {
    build_fingerprint().is_some()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fingerprint_or_version_always_starts_with_version() {
        // Whether or not a git SHA was baked, the string must begin with the
        // compile-time crate version so it is human-meaningful and sorts by
        // version first.
        let s = fingerprint_or_version();
        assert!(
            s.starts_with(env!("CARGO_PKG_VERSION")),
            "fingerprint_or_version {:?} must start with version {}",
            s,
            env!("CARGO_PKG_VERSION")
        );
    }

    #[test]
    fn build_fingerprint_shape_when_present() {
        // build.rs either bakes a `<version>+<sha>` (in a git checkout) or
        // leaves it unset (released tarball). Both are valid; if present it
        // must carry the `+` separator and a non-empty SHA segment.
        if let Some(fp) = build_fingerprint() {
            assert!(has_git_fingerprint());
            let (ver, sha) = fp.split_once('+').expect("fingerprint must contain '+'");
            assert_eq!(ver, env!("CARGO_PKG_VERSION"));
            assert!(!sha.is_empty(), "sha segment must be non-empty");
        } else {
            assert!(!has_git_fingerprint());
            // The fallback is exactly the bare version.
            assert_eq!(fingerprint_or_version(), env!("CARGO_PKG_VERSION"));
        }
    }
}
