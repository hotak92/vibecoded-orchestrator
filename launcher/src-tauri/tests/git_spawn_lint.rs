// SPDX-License-Identifier: AGPL-3.0-or-later
//! Source-shape lint: PRODUCTION code in `commands/installer.rs` must not
//! construct `Command::new("git")` directly.
//!
//! ## What this pins, and why a test rather than a comment
//!
//! v0.2.92 (WP-13) introduced `commands/git_cmd.rs` as "the ONE home for the
//! launcher's git invocations", and the DECISION logic did move there: which
//! binary, which environment, `.silent()` so Windows stops flashing a console
//! window per call, capture-never-inherit, the detached-HEAD normaliser, the
//! `Result`-not-`unwrap_or(0)` contract.
//!
//! The SPAWNS did not. The adversarial review of that work found 31 sites in
//! `installer.rs` still calling `Command::new("git")` themselves — every one
//! of them bypassing whatever `git_cmd` decides. A centralised decision that
//! 31 call sites route around is not centralised; it is a 32nd copy that
//! happens to be the documented one. The next `.silent()`-class fix (or the
//! next environment pin, or the next timeout) would have landed in `git_cmd`
//! and reached none of them.
//!
//! Prose in a module doc cannot stop the 32nd raw spawn from being added —
//! nobody reads `git_cmd.rs` while writing a line in `installer.rs`. A failing
//! test does, at the point of the mistake.
//!
//! ## Scope
//!
//! `src/commands/installer.rs`, production region only — everything before the
//! file's top-level `#[cfg(test)]`. Test fixtures below that marker build real
//! git repositories and MUST spawn git directly; they are deliberately allowed
//! (and `the_test_region_really_does_contain_fixtures` proves that allowance is
//! load-bearing rather than a vacuous carve-out).
//!
//! Detection is the literal `Command::new("git")`, which covers every spelling
//! in use (`tokio::process::Command::new`, `std::process::Command::new`,
//! `StdCommand::new`, `TokioCommand::new`). An indirection like
//! `let g = "git"; Command::new(g)` is NOT caught — the lint is a ratchet
//! against the obvious regression, not a proof of absence.
//!
//! Fixture-path resolution mirrors `check_state_lint.rs`.

use std::path::PathBuf;

/// The banned construction, as it appears in source.
const RAW_SPAWN: &str = r#"Command::new("git")"#;

/// The top-level marker that opens the file's test module.
const TEST_MOD_MARKER: &str = "\n#[cfg(test)]";

fn installer_path() -> PathBuf {
    // CARGO_MANIFEST_DIR is `<repo>/launcher/src-tauri/` at test time.
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("src")
        .join("commands")
        .join("installer.rs")
}

/// Strip `//` line comments so a comment that MENTIONS the banned shape (this
/// file's own history notes do, and so do several in `installer.rs`) is not
/// mistaken for the shape itself.
///
/// Line COUNT is preserved so reported line numbers stay accurate.
///
/// Crude on purpose: `//` inside a string literal only ever makes the lint MORE
/// permissive, never less — a false negative here is a missed lint, not a
/// spurious failure.
fn strip_line_comments(src: &str) -> String {
    src.lines()
        .map(|l| match l.find("//") {
            Some(i) => &l[..i],
            None => l,
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// Split comment-stripped `src` into `(production, tests)` at the first
/// COLUMN-ZERO `#[cfg(test)]`. When the marker is absent the whole file is
/// production — the safe direction, because the fixtures would then trip the
/// lint loudly rather than the lint silently scanning nothing.
fn split_at_test_module(src: &str) -> (&str, &str) {
    match src.find(TEST_MOD_MARKER) {
        // +1 to keep the newline with the production side.
        Some(i) => (&src[..i + 1], &src[i + 1..]),
        None => (src, ""),
    }
}

/// Report every `Command::new("git")` in `region`, as `line N: <source>`.
///
/// `line_offset` is the 1-based number of `region`'s first line within the
/// original file, so findings point at real line numbers.
fn find_raw_spawns(region: &str, line_offset: usize) -> Vec<String> {
    region
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(RAW_SPAWN))
        .map(|(i, l)| format!("line {}: {}", line_offset + i, l.trim()))
        .collect()
}

/// The scan itself, over any source text. Separated from the assertion so the
/// SAME code can be pointed at synthetic input that DOES contain the banned
/// shape — see `scan_finds_a_planted_raw_spawn_and_ignores_the_allowed_ones`.
///
/// A lint with no negative case is a lint nobody has proven can fail.
fn scan(raw: &str) -> Vec<String> {
    let stripped = strip_line_comments(raw);
    let (production, _tests) = split_at_test_module(&stripped);
    find_raw_spawns(production, 1)
}

#[test]
fn installer_production_code_spawns_git_only_through_the_runner() {
    let path = installer_path();
    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("read {}: {e}", path.display()));

    let findings = scan(&raw);

    assert!(
        findings.is_empty(),
        "Raw `Command::new(\"git\")` is banned in production code in \
         src/commands/installer.rs — route the call through \
         `crate::commands::git_cmd` instead:\n\n  \
         run_git(repo, &[..])          -> trimmed stdout, Err on non-zero, 30s ceiling\n  \
         run_git_combined(repo, &[..]) -> as run_git, but stdout+stderr on failure (LC_ALL=C)\n  \
         run_git_raw(repo, &[..])      -> raw Output, Err only on spawn failure, untimed\n  \
         run_git_raw_env(repo, &[..], &[(\"K\", \"V\")]) -> run_git_raw plus env overrides\n\n\
         Why: `git_cmd` is where the launcher decides HOW git runs — `.silent()` \
         (no console-window flash on Windows), capture-never-inherit, the C-locale \
         pin, the timeout, and the error contract. A site that spawns git itself \
         gets none of those, and the next fix made in `git_cmd` will not reach it. \
         That is exactly how 31 sites in this file ended up bypassing a module \
         whose doc comment called itself \"the ONE home\".\n\n\
         If your call genuinely cannot be expressed by the four helpers above, \
         EXTEND git_cmd.rs rather than opening a 32nd raw spawn.\n\n\
         Findings ({} in the production region):\n  {}",
        findings.len(),
        findings.join("\n  "),
    );
}

/// RED-PROOF, permanent: point the REAL scan at synthetic source that contains
/// the banned shape and require it to be reported — plus the shapes that must
/// NOT be. Without this, a scan that silently found nothing (a wrong path, a
/// broken splitter, a typo'd needle) would pass forever and read as "the file
/// is clean".
#[test]
fn scan_finds_a_planted_raw_spawn_and_ignores_the_allowed_ones() {
    let synthetic = concat!(
        "fn a() {\n",
        "    let out = tokio::process::Command::new(\"git\").silent().output();\n",
        "}\n",
        "fn b() {\n",
        "    let out = StdCommand::new(\"git\").status();\n",
        "}\n",
        // Allowed: the runner, a non-git spawn, and a COMMENT naming the shape.
        "fn c() {\n",
        "    let out = run_git_raw(repo, &[\"status\"]).await;\n",
        "    let py = std::process::Command::new(\"python\").output();\n",
        "    // never write Command::new(\"git\") here\n",
        "}\n",
        "#[cfg(test)]\n",
        "mod tests {\n",
        // Allowed: fixtures below the marker.
        "    fn git() { StdCommand::new(\"git\").status(); }\n",
        "}\n",
    );

    let findings = scan(synthetic);
    assert_eq!(
        findings.len(),
        2,
        "expected exactly the two planted production offenders, got: {findings:?}"
    );
    assert!(
        findings.iter().any(|f| f.starts_with("line 2:")),
        "the tokio spawn must be reported with its line number: {findings:?}"
    );
    assert!(
        findings.iter().any(|f| f.starts_with("line 5:")),
        "the std spawn must be reported with its line number: {findings:?}"
    );
    assert!(
        !findings.iter().any(|f| f.contains("python")),
        "a non-git spawn is out of scope: {findings:?}"
    );
    assert!(
        !findings.iter().any(|f| f.contains("never write")),
        "a comment naming the shape must not trip the lint: {findings:?}"
    );
    assert!(
        !findings.iter().any(|f| f.contains("mod tests")),
        "fixtures below `#[cfg(test)]` are allowed: {findings:?}"
    );
}

/// Guard for the guard, half 1: the splitter must hand the lint a real
/// production region. A splitter that returned an empty (or near-empty)
/// production slice would make the assertion above pass vacuously forever —
/// the most expensive kind of test.
#[test]
fn the_production_region_is_the_real_one() {
    let raw = std::fs::read_to_string(installer_path()).expect("read installer.rs");
    let stripped = strip_line_comments(&raw);
    let (production, tests) = split_at_test_module(&stripped);

    assert!(
        production.contains("pub async fn check_for_updates"),
        "the production region no longer contains a known production symbol — \
         the `#[cfg(test)]` splitter is broken or installer.rs was restructured"
    );
    assert!(
        !production.contains("mod tests"),
        "the production region must stop AT the test module, not include it"
    );
    assert!(
        !tests.is_empty(),
        "no `#[cfg(test)]` found in installer.rs — the splitter fell back to \
         scanning the whole file"
    );
}

/// Guard for the guard, half 2: the test-fixture carve-out must be
/// load-bearing. If the fixtures ever stop spawning git directly, this lint's
/// exemption is dead weight and the scope comment above is a lie — say so here
/// rather than letting the carve-out silently widen.
#[test]
fn the_test_region_really_does_contain_fixtures() {
    let raw = std::fs::read_to_string(installer_path()).expect("read installer.rs");
    let stripped = strip_line_comments(&raw);
    let (_production, tests) = split_at_test_module(&stripped);

    let fixture_spawns = find_raw_spawns(tests, 1).len();
    assert!(
        fixture_spawns > 0,
        "installer.rs's test module no longer spawns git directly — the \
         production-only carve-out in this lint is now unnecessary and should \
         be tightened to the whole file"
    );
}
