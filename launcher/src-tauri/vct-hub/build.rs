//! Build script: bake a git build fingerprint into the vct-hub binary.
//!
//! v0.2.69 (hub-staleness home #3). The compile-time `[workspace.package]`
//! version (exposed on `/health` as `version`) is blind to
//! same-version-but-different-code builds — a hub built from v0.2.69 source
//! BEFORE a same-version code change still reports `0.2.69` while lacking
//! the new code. To make staleness detectable across same-version builds we
//! capture a short git SHA at compile time and expose it via the
//! `VCT_HUB_BUILD_FINGERPRINT` rustc-env, read with `option_env!` in
//! `crate::identity`.
//!
//! Soft-fail: when git is unavailable (released tarball, CI without a
//! checkout, no git on PATH) the fingerprint is left UNSET and
//! `option_env!` resolves to `None` — the identity check then treats the
//! fingerprint as "unknown" and falls back conservatively (never a false
//! kill). The Linux `/proc/<pid>/exe` inode comparison remains the robust
//! primary signal regardless of the fingerprint.

use std::process::Command;

fn main() {
    // Rebuild when HEAD moves so the baked SHA stays current. Best-effort:
    // if the .git layout differs (worktree, submodule) the worst case is a
    // slightly stale fingerprint, never a wrong PID decision.
    println!("cargo:rerun-if-changed=../../../.git/HEAD");
    println!("cargo:rerun-if-changed=../../../.git/refs");
    // Allow an explicit override (e.g. release CI that injects the tag SHA).
    println!("cargo:rerun-if-env-changed=VCT_HUB_BUILD_FINGERPRINT");

    if std::env::var_os("VCT_HUB_BUILD_FINGERPRINT").is_some() {
        // Honour an externally-provided fingerprint verbatim.
        return;
    }

    let sha = git_short_sha();
    if let Some(sha) = sha {
        println!("cargo:rustc-env=VCT_HUB_BUILD_FINGERPRINT={}", sha);
    }
    // else: leave unset — option_env! resolves to None at compile time.
}

/// Short git SHA + a `-dirty` suffix when the tree has uncommitted changes.
/// Returns None on any failure (no git, not a repo, command error).
fn git_short_sha() -> Option<String> {
    let head = Command::new("git")
        .args(["rev-parse", "--short=12", "HEAD"])
        .output()
        .ok()?;
    if !head.status.success() {
        return None;
    }
    let mut sha = String::from_utf8(head.stdout).ok()?.trim().to_string();
    if sha.is_empty() {
        return None;
    }

    // Dirty-tree marker: a build from a modified working tree is NOT the
    // same binary as the committed SHA, so flag it.
    if let Ok(status) = Command::new("git")
        .args(["status", "--porcelain", "--untracked-files=no"])
        .output()
    {
        if status.status.success() && !status.stdout.is_empty() {
            sha.push_str("-dirty");
        }
    }

    Some(sha)
}
