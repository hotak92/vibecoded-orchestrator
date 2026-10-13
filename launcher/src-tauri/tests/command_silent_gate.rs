// SPDX-License-Identifier: AGPL-3.0-or-later
//! v0.2.67 structural gate: every GUI-spawned `Command::new` must be
//! console-silent on Windows.
//!
//! ## Why this gate exists
//!
//! The launcher is built with `windows_subsystem = "windows"`. A child
//! process spawned from such a parent inherits a console-allocation
//! flag: without `CREATE_NO_WINDOW` (0x08000000), `CreateProcessW`
//! allocates a fresh `conhost.exe` window for every child, which
//! flashes on screen for the child's lifetime. With launcher boot
//! firing 11+ probe subprocesses (`where`, `git`, `docker`, `python`,
//! the container reaper, …) the user sees a cascade of flashing console
//! windows — the "fork bomb of windows" reported repeatedly.
//!
//! The fix is the chainable `CommandExt::silent()` helper
//! (`vct_launcher_core::process`): it sets `CREATE_NO_WINDOW` on Windows
//! and is a no-op on Linux/macOS, WITHOUT touching stdout/stderr
//! capture. The convention (codified in `process.rs` and the 2026-05-26
//! audit) is: **every `Command::new` spawned from the GUI launcher or
//! the hub gets `.silent()`** unless it is deliberately managing its own
//! creation flags (a visible installer console, a detached process) or
//! is an OS-specific spawn that never runs on Windows.
//!
//! Call sites added AFTER the original audit kept forgetting `.silent()`
//! (v0.2.52+ reaper, deferral writers, codegraph git probe). This test
//! makes "forgot `.silent()`" impossible to merge: it scans the launcher
//! + core + hub crate sources and FAILS, naming each offending
//! `file:line`, when a production `Command::new` lacks a silencing
//! marker.
//!
//! ## What it scans
//!
//! The three crates whose binaries are spawned from / as the GUI:
//!   * `src/`               — the launcher binary crate
//!   * `vct-launcher-core/src/` — shared core (probes, reaper, runtime)
//!   * `vct-hub/src/`       — the detached hub (also GUI-adjacent)
//!
//! `vct-updater/` is intentionally out of scope: it is a tiny detached
//! relaunch helper that, by design, only uses `DETACHED_PROCESS` /
//! `CREATE_NEW_PROCESS_GROUP` creation flags (never a plain
//! `Command::new`), so there is nothing for this gate to enforce there.
//!
//! ## What counts as "silenced" (no violation)
//!
//! A `Command::new(...)` binding is accepted if ANY of:
//!   1. `.silent()` appears in its builder chain (the canonical fix).
//!   2. `.creation_flags(` appears in / near its binding. This covers
//!      BOTH the inline `#[cfg(windows)] { cmd.creation_flags(...) }`
//!      form (equivalent to `.silent()`) AND the DELIBERATE-console
//!      sites (`CREATE_NEW_CONSOLE` in the installer terminal launcher,
//!      `DETACHED_PROCESS` in the hub/update handoff) — those are
//!      consciously choosing window behaviour, which is exactly the
//!      thing this gate wants people to do consciously.
//!   3. An explicit allow-marker `vct-allow-no-silent:` comment is
//!      present on the `Command::new` line or one of the few lines
//!      immediately above it. Use this ONLY for a documented exception
//!      (a temporary cross-track ownership note, or an OS-specific spawn
//!      that never runs on Windows). The reason text after the colon is
//!      mandatory so the exception is self-documenting.
//!
//! ## What it skips
//!
//!   * `Command::new` inside `//` line comments and `/* */` blocks, or
//!     inside string literals (false matches, not real spawns).
//!   * Anything under a `tests/` directory or a `*_test.rs` /
//!     `test_*.rs` file.
//!   * `Command::new` inside a `#[cfg(test)]` or
//!     `#[cfg(any(test, debug_assertions))]` item (test-only code). The item
//!     ends at its own `;` or balanced `}` (`support::test_gated_lines`), so
//!     production code after a gated `use`, helper or test module is still
//!     scanned.
//!
//! ## How to allowlist a legitimate exception
//!
//! Add a comment containing `vct-allow-no-silent: <why>` on (or just
//! above) the `Command::new` line, e.g.:
//! ```ignore
//! // vct-allow-no-silent: macOS-only ioreg probe, never spawned on Windows
//! let out = std::process::Command::new("ioreg")...
//! ```
//! Prefer `.silent()` (or an explicit `.creation_flags(...)`) over a
//! marker whenever the site can actually run on Windows.

use std::path::{Path, PathBuf};

mod support;
use support::{
    is_test_cfg_attr, is_test_or_debug_cfg_attr, strip_comments_and_strings, test_gated_lines,
    test_or_debug_gated_lines,
};

/// The launcher's `CARGO_MANIFEST_DIR` is `<repo>/launcher/src-tauri/`.
/// Every scanned tree is relative to that.
fn src_tauri_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

/// Crate source roots in scope for the gate (relative to src-tauri/).
const SCAN_ROOTS: &[&str] = &["src", "vct-launcher-core/src", "vct-hub/src"];

/// The allow-marker token. The reason text must follow the colon.
const ALLOW_MARKER: &str = "vct-allow-no-silent:";

/// One detected violation: a production `Command::new` with no silencing
/// marker in its binding.
#[derive(Debug)]
struct Violation {
    file: String, // path relative to the repo, for a stable message
    line: usize,  // 1-based
    snippet: String,
}

/// Recursively collect `*.rs` files under `root`, skipping any directory
/// named `tests` (integration-test trees are not production code) and
/// any `target` build dir that might be nested.
fn collect_rs_files(root: &Path, out: &mut Vec<PathBuf>) {
    let entries = match std::fs::read_dir(root) {
        Ok(e) => e,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            let name = path
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or_default();
            if name == "tests" || name == "target" {
                continue;
            }
            collect_rs_files(&path, out);
        } else if path.extension().and_then(|e| e.to_str()) == Some("rs") {
            let fname = path
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or_default();
            // Skip unit-test-only files by naming convention.
            if fname.ends_with("_test.rs") || fname.starts_with("test_") {
                continue;
            }
            out.push(path);
        }
    }
}

/// Does the binding starting at `cmd_line` (0-based, in `stripped`) have a
/// silencing marker (`.silent()` or `.creation_flags(`) in its chain or
/// inline `#[cfg(windows)]` flag block? Scans forward a bounded window,
/// stopping at the binding's consumer or the end of its enclosing
/// statement group, whichever the heuristic reaches first.
fn binding_is_silenced(stripped: &[String], cmd_line: usize) -> bool {
    const LOOKAHEAD: usize = 40;
    let n = stripped.len();

    // Determine the binding name, if the form is `let mut <name> =
    // Command::new(...)`. When there is a name, the silencing flag may be
    // applied on a *later* statement (`name.creation_flags(...)`), so we
    // scan until we see the consumer call on that name. When there is no
    // name (fluent `Command::new(...).silent()...`), the chain is the
    // statement that contains the consumer.
    let line = &stripped[cmd_line];
    let bound_name: Option<String> = line
        .split_once("let ")
        .and_then(|(_, rest)| rest.split_once('='))
        .map(|(lhs, _)| lhs.replace("mut", "").trim().to_string())
        .filter(|s| !s.is_empty() && s.chars().all(|c| c.is_alphanumeric() || c == '_'));

    let consumers = [
        ".spawn(",
        ".output(",
        ".status(",
        ".exec(",
        ".exec_replace(",
    ];

    let end = (cmd_line + LOOKAHEAD).min(n);
    for (offset, idx) in (cmd_line..end).enumerate() {
        let s = &stripped[idx];
        if s.contains(".silent()") || s.contains(".creation_flags(") {
            return true;
        }
        // Stop conditions: once we hit the consumer for this binding, the
        // chain is over — no marker means a violation.
        let hit_consumer = consumers.iter().any(|c| s.contains(c));
        if hit_consumer && offset > 0 {
            // For a named binding, the consumer line is `name.status()` or
            // `cmd.spawn()` etc; for a fluent chain the consumer is on the
            // same statement. Either way, once consumed without a marker
            // seen → not silenced. (offset > 0 lets the very first line
            // also carry a fluent `.status()`.)
            return false;
        }
        if hit_consumer && offset == 0 {
            return false;
        }
        // A `match cmd.status()` / `cmd.output().await` form: the consumer
        // is matched above. If we see a *new* `let ` binding (other than
        // the one we started on) the chain has certainly ended.
        if offset > 0 && s.trim_start().starts_with("let ") {
            // Unless this new let is itself consuming the binding via the
            // bound name (e.g. `let out = cmd.output()...`), which the
            // consumer check above already handled.
            if let Some(name) = &bound_name {
                if !s.contains(name.as_str()) {
                    return false;
                }
            } else {
                return false;
            }
        }
    }
    // Ran off the lookahead window without seeing a marker OR a consumer.
    // Be conservative: treat as NOT silenced so an unusual long chain is
    // surfaced rather than silently passed.
    false
}

/// Is there an allow-marker on `cmd_line` or up to 4 lines above it?
/// Markers live in comments, so we check the ORIGINAL (un-stripped) lines.
fn has_allow_marker(original: &[String], cmd_line: usize) -> bool {
    let start = cmd_line.saturating_sub(4);
    for line in original.iter().take(cmd_line + 1).skip(start) {
        if let Some(pos) = line.find(ALLOW_MARKER) {
            // Require non-empty reason text after the colon.
            let reason = line[pos + ALLOW_MARKER.len()..].trim();
            if !reason.is_empty() {
                return true;
            }
        }
    }
    false
}

#[test]
fn every_production_command_new_is_silent() {
    let base = src_tauri_dir();
    let mut files: Vec<PathBuf> = Vec::new();
    for root in SCAN_ROOTS {
        collect_rs_files(&base.join(root), &mut files);
    }
    files.sort();
    assert!(
        !files.is_empty(),
        "command-silent gate found no .rs files under {:?}/{{{}}} — \
         wrong CARGO_MANIFEST_DIR or layout changed",
        base,
        SCAN_ROOTS.join(", "),
    );

    let mut violations: Vec<Violation> = Vec::new();

    for file in &files {
        let src = match std::fs::read_to_string(file) {
            Ok(s) => s,
            Err(_) => continue,
        };
        let original: Vec<String> = src.lines().map(|s| s.to_string()).collect();
        let stripped = strip_comments_and_strings(&src);
        // Release-build question: test-or-debug-only items cannot print in a
        // user's release run, so they are blanked too.
        let gated = test_or_debug_gated_lines(&stripped);

        let rel = file
            .strip_prefix(&base)
            .unwrap_or(file)
            .to_string_lossy()
            .to_string();

        for (idx, line) in stripped.iter().enumerate() {
            if !line.contains("Command::new") {
                continue;
            }
            if gated.get(idx).copied().unwrap_or(false) {
                continue; // test-only code
            }
            if has_allow_marker(&original, idx) {
                continue; // documented exception
            }
            if binding_is_silenced(&stripped, idx) {
                continue;
            }
            let snippet = original
                .get(idx)
                .map(|s| s.trim().chars().take(100).collect::<String>())
                .unwrap_or_default();
            violations.push(Violation {
                file: rel.clone(),
                line: idx + 1,
                snippet,
            });
        }
    }

    assert!(
        violations.is_empty(),
        "command-silent gate: {} production `Command::new` site(s) lack a \
         silencing marker (.silent() / .creation_flags(...) / \
         `// {ALLOW_MARKER} <reason>`):\n{}\n\n\
         On Windows each such spawn flashes a conhost.exe console. Add \
         `.silent()` (preferred) from `vct_launcher_core::process::CommandExt`, \
         or — for a deliberately-windowed / OS-specific spawn that never runs \
         on Windows — add a `// {ALLOW_MARKER} <why>` comment on or just above \
         the line.",
        violations.len(),
        violations
            .iter()
            .map(|v| format!("  - {}:{}: {}", v.file, v.line, v.snippet))
            .collect::<Vec<_>>()
            .join("\n"),
    );
}

/// Self-test: the comment/string stripper must NOT see `Command::new`
/// hidden inside a line comment or a string literal (those are false
/// matches), but MUST still see real code.
#[test]
fn stripper_ignores_comments_and_strings() {
    let sample = r####"
fn x() {
    // tokio::process::Command::new("python3") in a comment
    let s = "Command::new in a string";
    let real = std::process::Command::new("git").silent().arg("x").status();
    /* block Command::new also ignored */
    let _ = s;
    let _ = real;
}
"####;
    let stripped = strip_comments_and_strings(sample);
    let real_hits: Vec<usize> = stripped
        .iter()
        .enumerate()
        .filter(|(_, l)| l.contains("Command::new"))
        .map(|(i, _)| i)
        .collect();
    assert_eq!(
        real_hits.len(),
        1,
        "stripper should expose exactly the one REAL Command::new, found \
         hits on lines {:?}\nstripped:\n{}",
        real_hits,
        stripped.join("\n"),
    );
}

/// Self-test: `binding_is_silenced` recognises both the fluent
/// `.silent()` form and the inline `#[cfg(windows)] {{
/// cmd.creation_flags(..) }}` form, and flags a bare binding.
#[test]
fn binding_silence_detection_matches_both_forms() {
    // Fluent .silent() form.
    let fluent: Vec<String> = "\
let s = std::process::Command::new(\"git\")
    .silent()
    .arg(\"x\")
    .status();"
        .lines()
        .map(|s| s.to_string())
        .collect();
    assert!(binding_is_silenced(&fluent, 0), "fluent .silent() not detected");

    // Inline creation_flags form on a named binding.
    let inline: Vec<String> = "\
let mut cmd = std::process::Command::new(\"git\");
cmd.arg(\"x\");
#[cfg(windows)]
{
    cmd.creation_flags(0x0800_0000);
}
let _ = cmd.status();"
        .lines()
        .map(|s| s.to_string())
        .collect();
    assert!(
        binding_is_silenced(&inline, 0),
        "inline creation_flags not detected"
    );

    // Bare binding — no marker before the consumer.
    let bare: Vec<String> = "\
let s = std::process::Command::new(\"git\")
    .arg(\"x\")
    .status();"
        .lines()
        .map(|s| s.to_string())
        .collect();
    assert!(!binding_is_silenced(&bare, 0), "bare binding wrongly accepted");
}

/// Self-test: the allow-marker is honoured on the line and just above it,
/// but only with a non-empty reason.
#[test]
fn allow_marker_requires_reason() {
    let with_reason: Vec<String> = "\
// vct-allow-no-silent: macOS-only probe
let out = std::process::Command::new(\"ioreg\").output();"
        .lines()
        .map(|s| s.to_string())
        .collect();
    assert!(has_allow_marker(&with_reason, 1), "marker above line not honoured");

    let no_reason: Vec<String> = "\
// vct-allow-no-silent:
let out = std::process::Command::new(\"ioreg\").output();"
        .lines()
        .map(|s| s.to_string())
        .collect();
    assert!(
        !has_allow_marker(&no_reason, 1),
        "empty-reason marker must NOT be honoured"
    );
}

#[test]
fn test_cfg_attribute_recognition() {
    assert!(is_test_cfg_attr("#[cfg(test)]"));
    assert!(!is_test_cfg_attr("#[cfg(any(test, debug_assertions))]"));
    assert!(is_test_or_debug_cfg_attr("#[cfg(any(test, debug_assertions))]"));
    assert!(is_test_or_debug_cfg_attr("#[cfg(all(unix, any(test, debug_assertions)))]"));
    assert!(is_test_or_debug_cfg_attr("#[cfg(test)]"));
    assert!(!is_test_or_debug_cfg_attr("#[cfg(debug_assertions)]"));
    assert!(is_test_cfg_attr("#[cfg(all(test, unix))]"));
    assert!(is_test_cfg_attr("#[cfg(all(unix, test))]"));
    assert!(is_test_cfg_attr("#[cfg(all(target_os = \"linux\", test))]"));
    assert!(!is_test_cfg_attr("#[cfg(all(not(test), unix))]"));
    assert!(!is_test_cfg_attr("#[cfg(all(unix, feature = \"test\"))]"));
    assert!(!is_test_cfg_attr("#[cfg(any(test, feature = \"x\"))]"));
    assert!(!is_test_cfg_attr("#[cfg(unix)]"));
}

/// Self-test (v0.2.102 W6-Z2): a gated item ends at its own `;` or balanced
/// `}`, never at the next item's. Before the fix a gated `use …;` ran on to
/// the following production fn and hid its `Command::new` from the gate.
#[test]
fn gated_items_end_at_their_own_terminator() {
    let src = "\
#[cfg(test)]
use std::fmt::Debug;

fn production() {
    let _ = std::process::Command::new(\"git\").status();
}

#[cfg(test)]
mod tests {
    fn t() { let _ = std::process::Command::new(\"x\"); }
}

fn after_the_test_module() {
    let _ = std::process::Command::new(\"y\").status();
}

#[cfg(test)] const INLINE: u8 = 1;
fn same_line_item_follows() {}
";
    let stripped = strip_comments_and_strings(src);
    let gated = test_gated_lines(&stripped);
    let line_of = |needle: &str| {
        stripped
            .iter()
            .position(|l| l.contains(needle))
            .unwrap_or_else(|| panic!("{needle} not in fixture"))
    };
    assert!(gated[line_of("use std::fmt::Debug;")], "the gated `use` itself is test code");
    assert!(!gated[line_of("fn production()")], "a gated `use` must not swallow the next fn");
    assert!(
        !gated[line_of("fn production()") + 1],
        "the production spawn after a gated `use` must stay visible"
    );
    assert!(gated[line_of("fn t()")], "the test module is test code");
    assert!(
        !gated[line_of("fn after_the_test_module()")],
        "production code after the test module must stay visible"
    );
    assert!(gated[line_of("const INLINE")], "a same-line gated item is test code");
    assert!(!gated[line_of("fn same_line_item_follows()")]);

    // Fail closed: an item that never terminates gates nothing.
    let unterminated: Vec<String> = ["#[cfg(test)]", "fn broken() {", "    let x = 1;"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    assert_eq!(test_gated_lines(&unterminated), vec![false, false, false]);

    // A file-level `#![cfg(test)]` gates the whole file (both readings).
    let whole: Vec<String> = ["#![cfg(test)]", "fn f() {", "    let _ = 1;", "}"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    assert_eq!(test_gated_lines(&whole), vec![true; 4]);
    assert_eq!(test_or_debug_gated_lines(&whole), vec![true; 4]);

    // v0.2.102 W6-LOWS (R3 C-4): it is file-level only BEFORE the first item.
    // Comments (stripped to blanks), blank lines and other inner attributes,
    // including a multi-line one, may precede it.
    let lines = |v: &[&str]| -> Vec<String> { v.iter().map(|s| s.to_string()).collect() };
    let preceded = strip_comments_and_strings(
        "//! crate docs\n\n#![allow(\n    dead_code\n)]\n  #! [ cfg ( test ) ]\nfn f() {}\n",
    );
    assert_eq!(test_gated_lines(&preceded), vec![true; preceded.len()]);
    // Inside an inline module it gates that module, never the whole file:
    // the production lines around it stay visible.
    let inline = lines(&[
        "fn production() {",
        "    let _ = std::process::Command::new(\"git\").status();",
        "}",
        "mod x {",
        "    #![cfg(test)]",
        "    fn t() {}",
        "}",
        "fn after() {}",
    ]);
    let g = test_gated_lines(&inline);
    assert!(!g[0] && !g[1] && !g[7], "an inline module's #![cfg(test)] blanked production: {g:?}");
    assert!(!test_or_debug_gated_lines(&inline)[1]);
    // An item before it: no longer file-level.
    let late = lines(&["use std::fmt;", "#![cfg(test)]", "fn f() {}"]);
    assert_eq!(test_gated_lines(&late), vec![false; 3]);
    // A first-line shebang is skipped (as the Rust lexer does); a `#!` that
    // is not on the first line is not an inner attribute.
    let shebang = lines(&["#!/usr/bin/env run-cargo-script", "#![cfg(test)]", "fn f() {}"]);
    assert_eq!(test_gated_lines(&shebang), vec![true; 3]);
    let late_bang = lines(&["", "#!/bin/x", "#![cfg(test)]", "fn f() {}"]);
    assert_eq!(test_gated_lines(&late_bang), vec![false; 4]);

    // Debug-gated items: production for the strict reading, blanked for the
    // release-build reading.
    let dbg: Vec<String> = ["#[cfg(any(test, debug_assertions))]", "fn d() {", "}"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    assert_eq!(test_gated_lines(&dbg), vec![false; 3]);
    assert_eq!(test_or_debug_gated_lines(&dbg), vec![true; 3]);
}
