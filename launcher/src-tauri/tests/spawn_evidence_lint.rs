// SPDX-License-Identifier: AGPL-3.0-or-later
// Part of VibeCoded Orchestrator.
//! Source-shape lint: a spawned-process failure that falls back to "no
//! stderr" must still carry the process's EXIT STATUS.
//!
//! ## Why (v0.2.100 WP-05, L4 I-06 / I-10)
//!
//! The v0.2.98 "update will not install" incident took hours to diagnose
//! because a `git fetch` failed with exit 1 and an empty stderr, and the
//! launcher logged exactly that: nothing. The fetch path was fixed in
//! v0.2.99; seven sibling sites kept the same shape — "first stderr line, or
//! `no stderr`" next to `status.code().unwrap_or(-1)`, which turns a signal
//! kill into a made-up `-1` and a silent exit into a message with no datum at
//! all. `ExitStatus`'s Display (`exit status: 1`, `signal: 9 (SIGKILL)`,
//! Windows `exit code: 1`) is the evidence; every site now formats it.
//!
//! ## The rule
//!
//! In PRODUCTION code (everything before a file's column-zero `#[cfg(test)]`
//! module) of the three crates, every line that still contains the phrase
//! `no stderr` must have, within its window (12 lines before, 6 after):
//!   * a reference to the process status (the token `status`), and
//!   * NONE of the lossy renderings: `.code().unwrap_or(…)`,
//!     `.code().map(…)` — each drops the signal a kill carries.
//!
//! A ratchet against the obvious regression, not a proof: a status formatted
//! through an unusual alias is not recognised, and the fix is then to name
//! it `status`. `check_source` is pure and its own red/green cases below pin
//! both directions of the rule.

use std::path::{Path, PathBuf};

const PHRASE: &str = "no stderr";
const BEFORE: usize = 12;
const AFTER: usize = 6;

/// Strip `//` comments (line count preserved), so prose that MENTIONS the
/// phrase — this file's module doc, history notes — is not code.
fn strip_line_comments(src: &str) -> Vec<String> {
    src.lines()
        .map(|l| match l.find("//") {
            Some(i) => l[..i].to_string(),
            None => l.to_string(),
        })
        .collect()
}

/// Index of the first column-zero `#[cfg(test)]` / `#[cfg(all(test, …))]`
/// whose next non-blank line opens a `mod` — the file's test module. Items
/// gated by a lone `#[cfg(test)]` above production code are not the cut.
fn test_module_start(lines: &[String]) -> usize {
    for (i, l) in lines.iter().enumerate() {
        if l.starts_with("#[cfg(") && l.contains("test") {
            let next = lines[i + 1..].iter().find(|n| !n.trim().is_empty());
            if next.is_some_and(|n| n.trim_start().starts_with("mod ")) {
                return i;
            }
        }
    }
    lines.len()
}

/// Every violation in `src` as `name:line: reason`.
fn check_source(name: &str, src: &str) -> Vec<String> {
    let lines = strip_line_comments(src);
    let end = test_module_start(&lines);
    let prod = &lines[..end];
    let mut out = Vec::new();
    for (i, line) in prod.iter().enumerate() {
        if !line.to_ascii_lowercase().contains(PHRASE) {
            continue;
        }
        let lo = i.saturating_sub(BEFORE);
        let hi = (i + AFTER + 1).min(prod.len());
        let window = prod[lo..hi].join("\n");
        // Whitespace-insensitive view for the multi-line method chains.
        let squashed: String = window.chars().filter(|c| !c.is_whitespace()).collect();
        let has_status = window
            .split(|c: char| !(c.is_alphanumeric() || c == '_'))
            .any(|tok| tok == "status");
        if !has_status {
            out.push(format!(
                "{name}:{}: `{PHRASE}` fallback with no exit status in reach — format the \
                 process's ExitStatus (`{{}}` of `status`) into the same message",
                i + 1
            ));
        }
        for lossy in [".code().unwrap_or", ".code().map("] {
            if squashed.contains(lossy) {
                out.push(format!(
                    "{name}:{}: `{lossy}` beside a `{PHRASE}` fallback drops a signal kill — \
                     format the ExitStatus itself",
                    i + 1
                ));
            }
        }
    }
    out
}

fn rust_files(dir: &Path, acc: &mut Vec<PathBuf>) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for entry in rd.flatten() {
        let p = entry.path();
        if p.is_dir() {
            rust_files(&p, acc);
        } else if p.extension().is_some_and(|e| e == "rs") {
            acc.push(p);
        }
    }
}

fn crate_roots() -> Vec<PathBuf> {
    // CARGO_MANIFEST_DIR is `<repo>/launcher/src-tauri/` at test time.
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    vec![
        base.join("src"),
        base.join("vct-launcher-core").join("src"),
        base.join("vct-hub").join("src"),
    ]
}

#[test]
fn every_no_stderr_fallback_carries_the_exit_status() {
    let mut files = Vec::new();
    for root in crate_roots() {
        assert!(root.is_dir(), "lint root missing: {}", root.display());
        rust_files(&root, &mut files);
    }
    assert!(files.len() > 50, "suspiciously few sources scanned: {}", files.len());

    let mut violations = Vec::new();
    let mut sites = 0usize;
    for f in &files {
        let src = std::fs::read_to_string(f).unwrap();
        let name = f.strip_prefix(env!("CARGO_MANIFEST_DIR")).unwrap_or(f).display().to_string();
        sites += strip_line_comments(&src)
            .iter()
            .filter(|l| l.to_ascii_lowercase().contains(PHRASE))
            .count();
        violations.extend(check_source(&name, &src));
    }
    // The seven I-06 sites plus the fetch sentinel exist today; if the count
    // collapses to zero the scan is looking in the wrong place.
    assert!(sites >= 8, "expected the known `{PHRASE}` sites, scanned {sites}");
    assert!(
        violations.is_empty(),
        "spawned-process failures without exit-status evidence:\n  {}",
        violations.join("\n  ")
    );
}

/// The pre-fix shapes are RED (both lossy renderings, and a bare fallback
/// with no status in reach); the fixed shapes are GREEN; a test module is
/// out of scope; a comment mentioning the phrase is not code.
#[test]
fn the_rule_catches_the_pre_fix_shapes_and_passes_the_fixed_ones() {
    let lossy_code = r#"
fn f(out: Output) -> String {
    let stderr = String::from_utf8_lossy(&out.stderr);
    format!("x exited {}: {}", out.status.code().unwrap_or(-1),
        stderr.lines().next().unwrap_or("no stderr"))
}
"#;
    let lossy_map = r#"
fn f(out: Output) -> String {
    let head = "no stderr";
    format!("x exited {}: {}", out.status
        .code()
        .map(|c| c.to_string())
        .unwrap_or_else(|| "signal".into()), head)
}
"#;
    let bare = r#"
fn f(stderr: &str) -> String {
    format!("x failed: {}", if stderr.is_empty() { "no stderr" } else { stderr })
}
"#;
    let fixed = r#"
fn f(out: Output) -> String {
    let stderr = String::from_utf8_lossy(&out.stderr);
    format!("x failed ({}): {}", out.status,
        stderr.lines().next().unwrap_or("no stderr"))
}
"#;
    let only_in_tests = r#"
fn f() {}

#[cfg(test)]
mod tests {
    fn g() -> &'static str { "no stderr" }
}
"#;
    let only_in_comment = "// a failure used to read `no stderr`\nfn f() {}\n";

    assert!(!check_source("lossy_code", lossy_code).is_empty());
    assert!(!check_source("lossy_map", lossy_map).is_empty());
    assert!(!check_source("bare", bare).is_empty());
    assert_eq!(check_source("fixed", fixed), Vec::<String>::new());
    assert_eq!(check_source("only_in_tests", only_in_tests), Vec::<String>::new());
    assert_eq!(check_source("only_in_comment", only_in_comment), Vec::<String>::new());
}
