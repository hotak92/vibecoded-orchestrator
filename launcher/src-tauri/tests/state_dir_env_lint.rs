// SPDX-License-Identifier: AGPL-3.0-or-later
//! Source-shape lint: mutating `VCT_STATE_DIR` is allowed in EXACTLY ONE file.
//!
//! ## What this pins, and why a test rather than a comment
//!
//! `vct_launcher_core::paths::vct_root_dir()` resolves `$VCT_STATE_DIR` first
//! and `<home>/.vct` second. Tests redirect the var so their writes land in a
//! scratch dir. Environment variables are PROCESS-global, so the discipline
//! only holds if every test RESTORES the prior value — a test that ends with a
//! bare `remove_var("VCT_STATE_DIR")` destroys any outer redirect, and every
//! test after it in that binary resolves `vct_root_dir()` to the developer's
//! REAL `~/.vct`.
//!
//! That is not hypothetical. Before v0.2.92 this workspace had 49 such
//! `remove_var` sites plus 20 `set_var`s with no restore at all, across 21
//! files. Measured on the unfixed tree — `cargo test --workspace` under
//! `VCT_STATE_DIR=<scratch>` with `HOME` pointed at a decoy:
//!
//!   * the scratch dir received ONE file (`keyring.pace`);
//!   * the decoy home received a COMPLETE live state directory —
//!     `launcher.db` (626 KB, freshly migrated), `hub.pid`, `hub.port`,
//!     `hub.token`, `hub.db` + WAL, `logs/hub.<date>.log`.
//!
//! Unshielded, those writes land on the user's real install: `Db::open` runs
//! migrations, prune and backfill against their production `launcher.db`.
//!
//! Step 23 (v0.2.21) already shipped the correct helpers
//! (`vct_launcher_core::test_env`). They were OPTIONAL, and 21 files went on
//! hand-rolling the block anyway. **A convention that is not enforced is a
//! convention that decays**, so this makes reintroducing the shape a BUILD
//! FAILURE at the point of the mistake, with the replacement named.
//!
//! ## Scope
//!
//! `set_var("VCT_STATE_DIR"` and `remove_var("VCT_STATE_DIR")` in any `.rs`
//! file under `launcher/src-tauri/`, excluding `target/` and the one
//! sanctioned home, `vct-launcher-core/src/test_env.rs`.
//!
//! Both spellings are banned, not just `remove_var`: a `set_var` with no
//! restore leaves the next test pointed at a deleted tempdir, and it is the
//! shape a new hand-rolled block starts from. Every mutation goes through the
//! helpers, which is what makes "restores the PRIOR value" checkable in one
//! place instead of 118.
//!
//! Comments are stripped before matching, so prose that MENTIONS the banned
//! call (this module's own header does) is not a finding.
//!
//! Fixture-path resolution mirrors `check_state_lint.rs`.

use std::path::{Path, PathBuf};

/// `<repo>/launcher/src-tauri/` at test time.
fn src_tauri_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

/// The single file allowed to mutate the variable. Relative to
/// [`src_tauri_dir`]; compared component-wise so the check is separator-
/// agnostic (Windows `\` vs POSIX `/`).
const SANCTIONED: &[&str] = &["vct-launcher-core", "src", "test_env.rs"];

fn is_sanctioned(path: &Path, root: &Path) -> bool {
    let Ok(rel) = path.strip_prefix(root) else {
        return false;
    };
    rel.components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .eq(SANCTIONED.iter().map(|s| s.to_string()))
}

/// Every `.rs` file under `dir`, recursively, skipping build output.
fn rust_sources(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.filter_map(|e| e.ok()) {
        let path = entry.path();
        let name = entry.file_name();
        if path.is_dir() {
            // `target/` holds vendored dependency sources; `.git` is noise.
            if name == std::ffi::OsStr::new("target") || name == std::ffi::OsStr::new(".git") {
                continue;
            }
            rust_sources(&path, out);
        } else if path.extension().and_then(|e| e.to_str()) == Some("rs") {
            out.push(path);
        }
    }
}

/// Strip `//` line comments so prose mentioning the banned call is not a
/// finding. Crude on purpose (a `//` inside a string literal truncates the
/// line early) — which can only make the lint MORE permissive, never produce
/// a spurious failure.
fn strip_line_comments(src: &str) -> String {
    src.lines()
        .map(|l| match l.find("//") {
            Some(i) => &l[..i],
            None => l,
        })
        .collect::<Vec<_>>()
        .join("\n")
}

const BANNED: &[&str] = &[
    "set_var(\"VCT_STATE_DIR\"",
    "remove_var(\"VCT_STATE_DIR\")",
];

/// The scan itself, over any directory. Separated from the assertion so the
/// SAME code can be pointed at a synthetic tree that DOES contain the banned
/// shape — see `scan_finds_planted_offenders_in_a_synthetic_tree`.
///
/// A lint with no negative case is a lint nobody has proven can fail. This
/// repo has already shipped a scanner that silently matched nothing.
fn scan(root: &Path) -> Vec<String> {
    let mut files = Vec::new();
    rust_sources(root, &mut files);
    let mut findings = Vec::new();
    for file in &files {
        if is_sanctioned(file, root) {
            continue;
        }
        let Ok(raw) = std::fs::read_to_string(file) else {
            continue;
        };
        let src = strip_line_comments(&raw);
        for (idx, line) in src.lines().enumerate() {
            for needle in BANNED {
                if line.contains(needle) {
                    findings.push(format!(
                        "{}:{}: {}",
                        file.strip_prefix(root).unwrap_or(file).display(),
                        idx + 1,
                        needle
                    ));
                }
            }
        }
    }
    findings.sort();
    findings
}

#[test]
fn vct_state_dir_is_mutated_only_through_the_shared_test_env_helpers() {
    let root = src_tauri_dir();
    assert!(root.is_dir(), "unexpected build layout: {}", root.display());

    let findings = scan(&root);
    assert!(
        findings.is_empty(),
        "`VCT_STATE_DIR` may only be mutated inside \
         `vct-launcher-core/src/test_env.rs`.\n\n\
         Use the shared helpers instead — they take the workspace-wide \
         `GLOBAL_ENV_MUTEX` and restore the PRIOR value from `Drop` (so a \
         panicking assertion cannot skip the restore):\n\n  \
         let sd = vct_launcher_core::test_env::state_dir_guard();   // async-safe\n  \
         vct_launcher_core::test_env::with_state_dir(|root| {{ ... }});\n  \
         let _e = vct_launcher_core::test_env::env_guard(&[(\"VCT_STATE_DIR\", v)]);\n\n\
         Why this is banned rather than merely discouraged: a bare \
         `remove_var` restores the var to UNSET, which is only correct when it \
         WAS unset. When an outer redirect existed — the frozen pre-tag gate \
         run, CI, or a developer protecting their install — unsetting sends \
         every subsequent test in that binary to the real `~/.vct`, where the \
         suite has been measured spawning a hub and migrating the user's \
         production `launcher.db`.\n\n\
         Findings:\n  {}",
        findings.join("\n  ")
    );
}

/// RED-PROOF, permanent: point the REAL scan at a synthetic tree that contains
/// the banned shapes and require EXACTLY the planted offenders back. Without
/// this, a scan that silently found nothing — wrong root, a typo'd needle, a
/// too-eager comment stripper, an exclusion that swallowed everything — would
/// pass forever and read as "the workspace is clean".
#[test]
fn scan_finds_planted_offenders_in_a_synthetic_tree() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();

    // Offender 1: a bare unset at top level.
    std::fs::write(
        root.join("offender_remove.rs"),
        "fn t() {\n    std::env::remove_var(\"VCT_STATE_DIR\");\n}\n",
    )
    .unwrap();

    // Offender 2: a set with no restore, in a SUBDIRECTORY (recursion must
    // reach it — the real tree nests three crates deep).
    let nested = root.join("crate-b").join("src");
    std::fs::create_dir_all(&nested).unwrap();
    std::fs::write(
        nested.join("offender_set.rs"),
        "fn t() {\n    std::env::set_var(\"VCT_STATE_DIR\", td.path());\n}\n",
    )
    .unwrap();

    // NEAR-MISSES — each must be silent, and each is a shape that really
    // occurs in this workspace:
    std::fs::write(
        root.join("innocent.rs"),
        // 1. the sanctioned helpers, by name
        "fn a() { let _g = state_dir_guard(); }\n\
         fn b() { with_state_dir(|root| { let _ = root; }); }\n\
         fn c() { let _e = env_guard(&[(\"VCT_STATE_DIR\", None)]); }\n\
         // 2. a COMMENT naming the banned call:\n\
         //    std::env::remove_var(\"VCT_STATE_DIR\");\n\
         // 3. a DIFFERENT variable — only VCT_STATE_DIR is policed here:\n\
         fn d() { std::env::remove_var(\"VCT_SECRETS_DIR\"); }\n\
         fn e() { std::env::set_var(\"VCT_HUB_PORT\", \"7700\"); }\n\
         // 4. a non-literal name (the helpers' own restore loop):\n\
         fn f(k: &str) { std::env::remove_var(k); }\n\
         // 5. merely READING it is fine and must stay fine:\n\
         fn g() -> Option<String> { std::env::var(\"VCT_STATE_DIR\").ok() }\n",
    )
    .unwrap();

    // A non-Rust file with the banned text must be ignored (docs and shell
    // hooks legitimately mention the variable).
    std::fs::write(
        root.join("README.md"),
        "call std::env::remove_var(\"VCT_STATE_DIR\") to clean up\n",
    )
    .unwrap();

    // Build output is excluded by name.
    let target = root.join("target").join("debug");
    std::fs::create_dir_all(&target).unwrap();
    std::fs::write(
        target.join("vendored.rs"),
        "std::env::remove_var(\"VCT_STATE_DIR\");\n",
    )
    .unwrap();

    let findings = scan(root);
    assert_eq!(
        findings.len(),
        2,
        "expected exactly the two planted offenders, got: {findings:?}"
    );
    assert!(
        findings.iter().any(|f| f.contains("offender_remove.rs")
            && f.contains("remove_var")),
        "the top-level `remove_var` offender must be reported: {findings:?}"
    );
    assert!(
        findings.iter().any(|f| f.contains("offender_set.rs")
            && f.contains("set_var")),
        "the `set_var` offender in a SUBDIRECTORY must be reported: {findings:?}"
    );
}

/// Guard for the guard: prove the scan is actually reading this workspace's
/// sources and honouring the exclusion, rather than passing because it walked
/// an empty tree. A lint whose extractor finds nothing passes forever.
#[test]
fn the_lint_actually_walks_the_workspace_and_excludes_only_test_env() {
    let root = src_tauri_dir();
    let mut files = Vec::new();
    rust_sources(&root, &mut files);

    assert!(
        files.len() > 100,
        "expected to walk the whole crate tree, found only {} .rs files under {}",
        files.len(),
        root.display()
    );
    for expected in [
        root.join("src").join("hub_launcher.rs"),
        root.join("vct-hub").join("src").join("auth.rs"),
        root.join("vct-launcher-core").join("src").join("paths.rs"),
    ] {
        assert!(
            files.contains(&expected),
            "the walk must reach {} (all three crates)",
            expected.display()
        );
    }

    // The exclusion must match EXACTLY one file, and that file must really be
    // the sanctioned home — i.e. it must still contain the mutations the rest
    // of the workspace is forbidden. If `test_env.rs` is ever renamed or
    // gutted, this fails rather than the ban silently covering nothing.
    let sanctioned: Vec<_> = files.iter().filter(|f| is_sanctioned(f, &root)).collect();
    assert_eq!(
        sanctioned.len(),
        1,
        "exactly one sanctioned file expected, got {sanctioned:?}"
    );
    let body = std::fs::read_to_string(sanctioned[0]).expect("read test_env.rs");
    for needle in BANNED {
        assert!(
            body.contains(needle),
            "the sanctioned file must actually contain `{needle}` — otherwise \
             the exclusion is protecting nothing and the ban is untested"
        );
    }
}
