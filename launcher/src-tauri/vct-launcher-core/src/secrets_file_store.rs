// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! Tier-2 **file store** location + presence probes.
//!
//! The sanctioned secret RESOLVERS are a three-tier chain — see
//! `vco_lib/agent_secrets.py::get`, `templates/scripts/vct_secrets_resolve.sh`
//! and its `.ps1` sibling:
//!
//! ```text
//!   tier 1  vct-hub  → the OS keychain (the launcher's system of record)
//!   tier 2  file store  $VCT_SECRETS_DIR (default ~/.vct-secrets):
//!                       projects/<NAME>/<key>  →  shared/<key>
//!   tier 3  the project's own .env  (read-only, lowest priority)
//! ```
//!
//! The launcher, however, only ever probed tier 1. A key that lives ONLY
//! in the file store therefore rendered in the SecretsPanel as "not set"
//! while every consumer resolved it perfectly well — and the documented
//! consequence of that lie is severe: `CLAUDE.md` warns that a
//! launcher-GUI save and a `vct set` are DIFFERENT stores, so a user who
//! believes a working key is unset re-enters it in the GUI and forks the
//! value into two stores that then drift. The display was steering users
//! into the exact failure mode the docs warn about.
//!
//! This module is the ONE home for tier-2 filesystem knowledge on the
//! Rust side (path resolution, filename filtering, presence probing,
//! value reads). Three copies of the path resolution existed before it
//! (`installer.rs`, `secrets_import.rs`, `secrets_cmd.rs`) and two of
//! them silently ignored `$VCT_SECRETS_DIR`, so they addressed a
//! different directory than the CLI and the resolvers whenever that
//! variable was set. Do not add a fourth — call in here.
//!
//! # Value discipline
//!
//! [`read_key_value`] is the only function that touches a secret's
//! bytes. Its result must be compared or handed to a consumer and then
//! dropped; it must never be logged, formatted into an error, returned
//! to the frontend, or written anywhere. Every OTHER function in this
//! module deals in KEY NAMES and PATHS only.

use std::path::{Path, PathBuf};

use serde::Serialize;

/// Tri-state presence of a secret in ONE store.
///
/// The third state is load-bearing: an unreadable store (locked keychain,
/// `EACCES` on the secrets directory) must NOT be reported as `Absent`.
/// "We could not look" and "it is not there" drive opposite user actions —
/// the first means wait/unlock, the second means type the value in. See
/// `knowledge/concepts/a-check-that-could-not-run-reads-as-absence-2026-09-03.md`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Presence {
    Present,
    Absent,
    Unknown,
}

impl Presence {
    /// `true` only for [`Presence::Present`]. Named so call sites read as
    /// a question rather than a comparison.
    pub fn is_present(self) -> bool {
        matches!(self, Presence::Present)
    }
}

/// Root of the file store: `$VCT_SECRETS_DIR` when set and non-empty,
/// else `<home>/.vct-secrets`.
///
/// Honouring `$VCT_SECRETS_DIR` is not optional politeness — it is what
/// `tools/vct-secrets/vct`, `vct_secrets_resolve.sh|.ps1` and
/// `agent_secrets.py` all do. A probe that ignored it would report on a
/// directory no consumer reads.
///
/// `None` when the home directory cannot be resolved at all (no
/// `$HOME` / `%USERPROFILE%`); callers treat that as
/// [`Presence::Unknown`], never as absence.
pub fn secrets_root() -> Option<PathBuf> {
    if let Some(v) = std::env::var_os("VCT_SECRETS_DIR") {
        let p = PathBuf::from(v);
        if !p.as_os_str().is_empty() {
            return Some(p);
        }
    }
    directories::UserDirs::new().map(|u| u.home_dir().join(".vct-secrets"))
}

/// `<root>/shared` — the cross-project namespace (`vct set --shared`).
pub fn shared_dir() -> Option<PathBuf> {
    secrets_root().map(|d| d.join("shared"))
}

/// `<root>/projects/<name>` — the per-project namespace
/// (`vct set --project NAME`).
///
/// `None` when `name` is not a safe single path component (see
/// [`is_safe_component`]) — a project name carrying `..` or a separator
/// must never be joined onto the store root.
pub fn project_dir(name: &str) -> Option<PathBuf> {
    if !is_safe_component(name) {
        return None;
    }
    secrets_root().map(|d| d.join("projects").join(name))
}

/// Filename of the per-project opt-out marker for the tier-2 SHARED tier.
///
/// A project holding `projects/<NAME>/.no-shared-fallback` resolves ONLY
/// its own `projects/<NAME>/<key>` files from the file store —
/// `shared/<key>` is not consulted for it.
///
/// This spelling is MIRRORED, byte-for-byte, in the four resolvers that
/// cannot call into this crate:
///   * `vco_lib/agent_secrets.py`          (`NO_SHARED_FALLBACK_MARKER`)
///   * `templates/scripts/vct_secrets_resolve.sh`  (same name)
///   * `templates/scripts/vct_secrets_resolve.ps1` (`$VctNoSharedFallbackMarker`)
///   * `tools/vct-secrets/vct`             (same name)
/// and it is WRITTEN by the launcher's "Disable shared secrets for this
/// project" toggle (`commands/secrets_cmd.rs::set_shared_secrets_read_disabled`),
/// which now joins this constant rather than repeating the literal.
/// `tests/test_no_shared_fallback_marker_parity.py` locks the spellings
/// together; each resolver's own suite pins that its gate actually fires.
pub const NO_SHARED_FALLBACK_MARKER: &str = ".no-shared-fallback";

/// Whether `project_name` has opted out of the tier-2 SHARED tier.
///
/// Must match `agent_secrets._shared_fallback_disabled`,
/// `vct_secrets_resolve.sh::shared_fallback_disabled`,
/// `vct_secrets_resolve.ps1::Test-SharedFallbackDisabled` and
/// `vct::shared_fallback_disabled` — including their two degenerate cases:
/// an EMPTY name has nothing to opt out of, and the pseudo-name `shared`
/// must not be able to opt the shared tier out of itself (a stray
/// `projects/shared/` orphan would otherwise silently disable every
/// shared read).
///
/// `false` on an unreadable marker path, because `Path::exists()` reports
/// a permission error as "not there" — precisely what
/// `Path.exists()` / `[ -f ]` do in the four mirrors. Mirroring their
/// behaviour is the point: a probe that gated differently from the
/// resolvers would describe a resolution that never happens.
pub fn shared_fallback_disabled(project_name: &str) -> bool {
    if project_name.is_empty() || project_name == "shared" {
        return false;
    }
    match project_dir(project_name) {
        Some(d) => d.join(NO_SHARED_FALLBACK_MARKER).exists(),
        // An unsafe component never names a real project directory; the
        // caller ([`probe_shared_fallback`]) refuses it outright.
        None => false,
    }
}

/// Would tier-2 `shared/<key>` satisfy `key` FOR `project_name`?
///
/// This is the fall-through leg the per-project namespace does not cover.
/// [`probe_key`] deliberately answers about ONE directory (so a file is
/// never attributed to two rows); this answers the different question the
/// per-project surfaces actually need — "if my own namespace misses, does
/// the shared one serve me?" — and it is the ONLY place the marker gate is
/// applied on the Rust side.
///
/// * [`Presence::Absent`] when the project holds the opt-out marker: the
///   file may well exist, but it does NOT resolve for this project, and a
///   status surface that said otherwise would be describing someone else's
///   resolution.
/// * [`Presence::Unknown`] when the project name is not a safe path
///   component or the store root cannot be resolved — the gate could not
///   be evaluated, which is not evidence of absence.
pub fn probe_shared_fallback(project_name: &str, key: &str) -> Presence {
    if !is_safe_component(project_name) {
        return Presence::Unknown;
    }
    if shared_fallback_disabled(project_name) {
        return Presence::Absent;
    }
    match shared_dir() {
        Some(d) => probe_key(&d, key),
        None => Presence::Unknown,
    }
}

/// A single, non-traversing path component: non-empty, not `.` / `..`,
/// no `/`, `\` or NUL.
///
/// Both project names and secret keys reach this module from the
/// launcher DB, i.e. ultimately from user input. Joining an unchecked
/// component onto the store root would let a crafted key probe (and, via
/// [`read_key_value`], READ) an arbitrary file.
pub fn is_safe_component(s: &str) -> bool {
    !s.is_empty()
        && s != "."
        && s != ".."
        && !s.contains('/')
        && !s.contains('\\')
        && !s.contains('\0')
}

/// Whether a filename in a file-store directory names a real secret.
///
/// Excluded:
/// * dotfiles — `.no-shared-fallback` and friends are markers, not secrets;
/// * `*.broken-*` / `*.recovered-*` — historical recovery copies kept
///   beside a key after an incident (see the `github_pat` recovery note);
/// * `_README.md` — the explanatory file `install` materialises into the
///   shared store from `templates/vct-secrets-shared-readme.template`.
pub fn is_secret_filename(name: &str) -> bool {
    if name.is_empty() || name.starts_with('.') {
        return false;
    }
    if name.contains(".broken-") || name.contains(".recovered-") {
        return false;
    }
    if name == "_README.md" {
        return false;
    }
    true
}

/// Presence of `key` inside `dir` — the ONE probe.
///
/// Deliberately probes exactly ONE directory even though the resolvers
/// fall through `projects/<NAME>/` → `shared/`: attributing one file to
/// two rows would make "where does this value live?" unanswerable in the
/// panel. The shared copy is surfaced by its own shared-scope row, and
/// cross-scope precedence is what the shadow badge is for.
///
/// The fall-through is NOT thereby ignored — it is a different question,
/// asked by [`probe_shared_fallback`] ("would `shared/` serve THIS
/// project?"), which is also the only function that applies the
/// [`NO_SHARED_FALLBACK_MARKER`] gate. Surfaces that must answer "does
/// this key resolve for me?" (rather than "which directory holds it?")
/// read both.
///
/// Distinguishes "not there" from "could not look".
pub fn probe_key(dir: &Path, key: &str) -> Presence {
    if !is_safe_component(key) {
        return Presence::Absent;
    }
    match std::fs::metadata(dir.join(key)) {
        Ok(m) if m.is_file() => Presence::Present,
        // A directory (or symlink-to-directory) at the key's name is not
        // a value the resolvers would read.
        Ok(_) => Presence::Absent,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Presence::Absent,
        // EACCES / EIO / anything else: the store exists but we cannot
        // see into it. Soft-fail to Unknown — never a wrong "absent".
        Err(_) => Presence::Unknown,
    }
}

/// KEY NAMES (never values) of the secret files in `dir`, sorted.
///
/// A missing directory yields an empty list; an unreadable one also
/// yields an empty list — enumeration is a best-effort surface, and the
/// per-key [`probe_key`] calls report `Unknown` for the keys the
/// launcher already knows about, which is where the honesty guarantee
/// lives.
fn list_keys_in_dir(dir: Option<PathBuf>) -> Vec<String> {
    let dir = match dir {
        Some(d) => d,
        None => return Vec::new(),
    };
    let rd = match std::fs::read_dir(&dir) {
        Ok(rd) => rd,
        Err(_) => return Vec::new(),
    };
    let mut out: Vec<String> = Vec::new();
    for ent in rd.flatten() {
        let name = match ent.file_name().into_string() {
            Ok(s) => s,
            Err(_) => continue,
        };
        if !is_secret_filename(&name) || !is_safe_component(&name) {
            continue;
        }
        match ent.file_type() {
            Ok(ft) if ft.is_dir() => continue,
            Ok(_) => {}
            Err(_) => continue,
        }
        out.push(name);
    }
    out.sort();
    out
}

/// KEY NAMES of every secret file in `<root>/shared`.
pub fn list_shared_keys() -> Vec<String> {
    list_keys_in_dir(shared_dir())
}

/// KEY NAMES of every secret file in `<root>/projects/<project_name>`.
pub fn list_project_keys(project_name: &str) -> Vec<String> {
    list_keys_in_dir(project_dir(project_name))
}

/// Read a file-store value.
///
/// Strips exactly ONE trailing newline, matching
/// `vct_secrets_resolve.sh::read_file_strip_one_newline` and
/// `agent_secrets.py::_file_store_get` byte-for-byte — a comparison
/// against the keychain must not report a difference that no consumer
/// would ever observe.
///
/// **The returned String is secret material.** The single sanctioned use
/// is an in-memory equality comparison (see
/// `secrets_cmd::divergence_between_stores`). Never log, format, return
/// over IPC, or persist it.
pub fn read_key_value(dir: &Path, key: &str) -> Option<String> {
    if !is_safe_component(key) {
        return None;
    }
    let raw = std::fs::read_to_string(dir.join(key)).ok()?;
    Some(match raw.strip_suffix('\n') {
        Some(stripped) => stripped.to_string(),
        None => raw,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch() -> PathBuf {
        let p = std::env::temp_dir().join(format!(
            "vct-file-store-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(p.join("shared")).unwrap();
        std::fs::create_dir_all(p.join("projects").join("Proj")).unwrap();
        p
    }

    #[test]
    fn secrets_root_prefers_vct_secrets_dir_over_home() {
        let root = scratch();
        let _g = crate::test_env::env_guard(&[(
            "VCT_SECRETS_DIR",
            Some(root.to_str().unwrap()),
        )]);
        assert_eq!(secrets_root().unwrap(), root);
        assert_eq!(shared_dir().unwrap(), root.join("shared"));
        assert_eq!(
            project_dir("Proj").unwrap(),
            root.join("projects").join("Proj")
        );
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn empty_vct_secrets_dir_falls_back_to_home() {
        let _g = crate::test_env::env_guard(&[("VCT_SECRETS_DIR", Some(""))]);
        // Falls through to <home>/.vct-secrets rather than treating "" as
        // the root (which would probe the process CWD).
        let got = secrets_root().expect("home must resolve in a test env");
        assert!(got.ends_with(".vct-secrets"), "got {}", got.display());
    }

    #[test]
    fn probe_reports_present_absent_and_never_confuses_a_directory_for_a_value() {
        let root = scratch();
        let _g = crate::test_env::env_guard(&[(
            "VCT_SECRETS_DIR",
            Some(root.to_str().unwrap()),
        )]);
        std::fs::write(root.join("shared").join("present_key"), "v").unwrap();
        std::fs::create_dir_all(root.join("shared").join("dir_key")).unwrap();

        let sh = shared_dir().unwrap();
        assert_eq!(probe_key(&sh, "present_key"), Presence::Present);
        assert_eq!(probe_key(&sh, "missing_key"), Presence::Absent);
        assert_eq!(probe_key(&sh, "dir_key"), Presence::Absent);
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn project_probe_does_not_fall_through_to_shared() {
        let root = scratch();
        let _g = crate::test_env::env_guard(&[(
            "VCT_SECRETS_DIR",
            Some(root.to_str().unwrap()),
        )]);
        // Only the SHARED copy exists.
        std::fs::write(root.join("shared").join("k"), "v").unwrap();
        assert_eq!(probe_key(&shared_dir().unwrap(), "k"), Presence::Present);
        assert_eq!(
            probe_key(&project_dir("Proj").unwrap(), "k"),
            Presence::Absent,
            "the project namespace must report on its OWN file only — the \
             shared copy is surfaced by the shared row"
        );
        // …and the fall-through question is answered by its OWN probe,
        // which is what the per-project status surfaces read.
        assert_eq!(probe_shared_fallback("Proj", "k"), Presence::Present);
        std::fs::remove_dir_all(&root).ok();
    }

    // ── The tier-2 SHARED fall-through, and its opt-out marker ─────────

    #[test]
    fn shared_fallback_serves_a_project_that_has_not_opted_out() {
        let root = scratch();
        let _g = crate::test_env::env_guard(&[(
            "VCT_SECRETS_DIR",
            Some(root.to_str().unwrap()),
        )]);
        std::fs::write(root.join("shared").join("SHARED_ONLY"), "v").unwrap();
        assert!(!shared_fallback_disabled("Proj"));
        assert_eq!(
            probe_shared_fallback("Proj", "SHARED_ONLY"),
            Presence::Present,
            "projects/<NAME>/ misses, shared/ hits — every resolver serves it"
        );
        assert_eq!(probe_shared_fallback("Proj", "NOWHERE"), Presence::Absent);
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn the_marker_makes_the_shared_tier_absent_for_that_project_only() {
        let root = scratch();
        let _g = crate::test_env::env_guard(&[(
            "VCT_SECRETS_DIR",
            Some(root.to_str().unwrap()),
        )]);
        std::fs::write(root.join("shared").join("SHARED_ONLY"), "v").unwrap();
        std::fs::create_dir_all(root.join("projects").join("OptedOut")).unwrap();
        std::fs::write(
            root.join("projects")
                .join("OptedOut")
                .join(NO_SHARED_FALLBACK_MARKER),
            b"",
        )
        .unwrap();

        assert!(shared_fallback_disabled("OptedOut"));
        assert_eq!(
            probe_shared_fallback("OptedOut", "SHARED_ONLY"),
            Presence::Absent,
            "the file exists, but it does NOT resolve for this project — a \
             status surface must not claim otherwise"
        );
        // The marker is per-project: the neighbour is unaffected.
        assert!(!shared_fallback_disabled("Proj"));
        assert_eq!(
            probe_shared_fallback("Proj", "SHARED_ONLY"),
            Presence::Present
        );
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn the_shared_pseudo_project_cannot_opt_the_shared_tier_out_of_itself() {
        // Mirrors `agent_secrets._shared_fallback_disabled`: a stray
        // `projects/shared/.no-shared-fallback` must not disable every
        // shared read. An empty name has nothing to opt out of either.
        let root = scratch();
        let _g = crate::test_env::env_guard(&[(
            "VCT_SECRETS_DIR",
            Some(root.to_str().unwrap()),
        )]);
        std::fs::create_dir_all(root.join("projects").join("shared")).unwrap();
        std::fs::write(
            root.join("projects")
                .join("shared")
                .join(NO_SHARED_FALLBACK_MARKER),
            b"",
        )
        .unwrap();
        assert!(!shared_fallback_disabled("shared"));
        assert!(!shared_fallback_disabled(""));
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn an_unevaluable_gate_is_unknown_not_a_free_pass() {
        let root = scratch();
        let _g = crate::test_env::env_guard(&[(
            "VCT_SECRETS_DIR",
            Some(root.to_str().unwrap()),
        )]);
        std::fs::write(root.join("shared").join("SHARED_ONLY"), "v").unwrap();
        // A traversing name has no locatable marker, so whether the shared
        // tier applies cannot be decided — and "we could not look" must
        // never render as a confident Present.
        assert_eq!(
            probe_shared_fallback("../evil", "SHARED_ONLY"),
            Presence::Unknown
        );
        assert_eq!(probe_shared_fallback("", "SHARED_ONLY"), Presence::Unknown);
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn traversal_components_are_refused() {
        let root = scratch();
        let _g = crate::test_env::env_guard(&[(
            "VCT_SECRETS_DIR",
            Some(root.to_str().unwrap()),
        )]);
        assert!(!is_safe_component("../etc/passwd"));
        assert!(!is_safe_component(".."));
        assert!(!is_safe_component(""));
        assert!(!is_safe_component("a/b"));
        assert!(is_safe_component("GITHUB_TOKEN_FINEGRAINED"));

        // A real file OUTSIDE the namespace: without the component guard,
        // `dir.join("../ESCAPED")` resolves onto it and the probe reports
        // Present (and `read_key_value` would READ it). The assertions
        // below are only meaningful because this file exists.
        std::fs::write(root.join("ESCAPED"), "outside-the-namespace").unwrap();
        let sh = shared_dir().unwrap();
        assert!(sh.join("../ESCAPED").exists(), "the escape target must be reachable by path");
        assert_eq!(probe_key(&sh, "../ESCAPED"), Presence::Absent);
        assert_eq!(read_key_value(&sh, "../ESCAPED"), None);
        assert_eq!(probe_key(&sh, "../../etc/passwd"), Presence::Absent);
        assert!(project_dir("../evil").is_none());
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn listing_skips_markers_recovery_copies_and_the_readme() {
        let root = scratch();
        let _g = crate::test_env::env_guard(&[(
            "VCT_SECRETS_DIR",
            Some(root.to_str().unwrap()),
        )]);
        let sh = root.join("shared");
        for name in [
            "github_pat",
            "github_pat.broken-19h40",
            "github_pat.recovered-no-workflow",
            ".no-shared-fallback",
            "_README.md",
            "vercel_token",
        ] {
            std::fs::write(sh.join(name), "v").unwrap();
        }
        assert_eq!(
            list_shared_keys(),
            vec!["github_pat".to_string(), "vercel_token".to_string()]
        );
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn read_strips_exactly_one_trailing_newline() {
        let root = scratch();
        let sh = root.join("shared");
        std::fs::write(sh.join("one"), "value\n").unwrap();
        std::fs::write(sh.join("two"), "value\n\n").unwrap();
        std::fs::write(sh.join("none"), "value").unwrap();
        assert_eq!(read_key_value(&sh, "one").as_deref(), Some("value"));
        assert_eq!(read_key_value(&sh, "two").as_deref(), Some("value\n"));
        assert_eq!(read_key_value(&sh, "none").as_deref(), Some("value"));
        assert_eq!(read_key_value(&sh, "absent"), None);
        std::fs::remove_dir_all(&root).ok();
    }
}
