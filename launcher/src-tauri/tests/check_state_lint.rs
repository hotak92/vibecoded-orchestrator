// SPDX-License-Identifier: AGPL-3.0-or-later
//! Source-shape lint: the `*_ok: bool` + `*_error: Option<String>` pair is
//! BANNED on status structs under `src/commands/`.
//!
//! ## What this pins, and why a test rather than a comment
//!
//! v0.2.83 invented that pair on `installer::UpdateStatus` to fix a real
//! defect: a failed remote probe was indistinguishable from a successful one.
//! The fix was correct. It then sat there for nine releases WITHOUT being
//! carried to `self_update::UpdateStatus`, whose identical defect went on to
//! cost a field user five weeks of silent non-updating.
//!
//! Two properties of the bool pair made that outcome likely:
//!
//! 1. **It is local by construction.** Two `pub` fields on one struct do not
//!    travel to a sibling module the way a shared TYPE does. Nothing about
//!    adding them to struct A creates any pressure to add them to struct B.
//! 2. **It cannot express "not applicable".** A non-git install has no remote
//!    and never will; with only `ok`/`error` available, that case had to
//!    borrow `ok = true` — claiming a successful check that never ran, which
//!    the frontend rendered as green.
//!
//! `vct_launcher_core::check_state::CheckState` fixes both. This test makes
//! the regression a BUILD FAILURE rather than a thing to remember: the next
//! person who reaches for a bool pair on a status struct is told, at the
//! point of the mistake, what to use instead.
//!
//! ## Scope
//!
//! Structs whose name ends in `Status`, declared under
//! `launcher/src-tauri/src/commands/`. Deliberately narrow: this bans the
//! shape where it caused harm (values a GUI renders as health) without
//! policing every bool in the codebase. A `success: bool` on a command RESULT
//! is a different thing and is not touched.
//!
//! Fixture-path resolution mirrors `deferral_registry_parity.rs`.

use std::path::{Path, PathBuf};

fn commands_dir() -> PathBuf {
    // CARGO_MANIFEST_DIR is `<repo>/launcher/src-tauri/` at test time.
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("src")
        .join("commands")
}

/// Every `.rs` file under `src/commands/`, recursively.
fn rust_sources(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.filter_map(|e| e.ok()) {
        let path = entry.path();
        if path.is_dir() {
            rust_sources(&path, out);
        } else if path.extension().and_then(|e| e.to_str()) == Some("rs") {
            out.push(path);
        }
    }
}

/// Strip `//` line comments so a comment that MENTIONS the banned shape (this
/// module's own history notes do, and so do several in `installer.rs`) is not
/// mistaken for the shape itself.
///
/// Crude on purpose: `//` inside a string literal is rare in these files and
/// only ever makes the lint MORE permissive, never less — a false negative
/// here is a missed lint, not a spurious failure. (`/* */` blocks are not
/// used for prose in this codebase.)
fn strip_line_comments(src: &str) -> String {
    src.lines()
        .map(|l| match l.find("//") {
            Some(i) => &l[..i],
            None => l,
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// Extract `(name, body)` for each `struct <Name> { ... }` in `src`, matching
/// braces so nested types do not truncate the body.
fn structs(src: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    let bytes = src.as_bytes();
    let mut search_from = 0usize;
    while let Some(rel) = src[search_from..].find("struct ") {
        let kw = search_from + rel;
        // Must be a token boundary — skip `.struct`, `mystruct ` etc.
        let preceded_ok = kw == 0
            || !(bytes[kw - 1] as char).is_alphanumeric() && bytes[kw - 1] != b'_';
        search_from = kw + "struct ".len();
        if !preceded_ok {
            continue;
        }
        let rest = &src[search_from..];
        let name: String = rest
            .chars()
            .take_while(|c| c.is_alphanumeric() || *c == '_')
            .collect();
        if name.is_empty() {
            continue;
        }
        let Some(open_rel) = rest.find('{') else {
            continue;
        };
        // A `;` before the `{` means a unit/tuple struct — no body.
        if rest[..open_rel].contains(';') {
            continue;
        }
        let open = search_from + open_rel;
        let mut depth = 0usize;
        let mut end = open;
        for (i, c) in src[open..].char_indices() {
            match c {
                '{' => depth += 1,
                '}' => {
                    depth -= 1;
                    if depth == 0 {
                        end = open + i;
                        break;
                    }
                }
                _ => {}
            }
        }
        if end > open {
            out.push((name, src[open + 1..end].to_string()));
        }
    }
    out
}

/// Field names in `body` declared as `<name>: bool`.
fn bool_fields(body: &str) -> Vec<String> {
    field_names_with_type(body, "bool")
}

/// Field names in `body` declared as `<name>: Option<String>`.
fn optional_string_fields(body: &str) -> Vec<String> {
    field_names_with_type(body, "Option<String>")
}

fn field_names_with_type(body: &str, ty: &str) -> Vec<String> {
    body.split(',')
        .filter_map(|frag| {
            let frag = frag.trim();
            let (lhs, rhs) = frag.rsplit_once(':')?;
            if rhs.trim() != ty {
                return None;
            }
            let name = lhs
                .rsplit(|c: char| c.is_whitespace())
                .next()?
                .trim()
                .to_string();
            if name.is_empty() {
                None
            } else {
                Some(name)
            }
        })
        .collect()
}

/// The scan itself, over any directory. Separated from the assertion so the
/// SAME code can be pointed at a synthetic directory that DOES contain the
/// banned shape — see `scan_finds_the_banned_shape_in_a_synthetic_tree`.
///
/// A lint with no negative case is a lint nobody has proven can fail.
fn scan(dir: &Path) -> Vec<String> {
    let mut files = Vec::new();
    rust_sources(dir, &mut files);
    let mut findings: Vec<String> = Vec::new();
    for file in &files {
        let Ok(raw) = std::fs::read_to_string(file) else {
            continue;
        };
        let src = strip_line_comments(&raw);
        for (name, body) in structs(&src) {
            if !name.ends_with("Status") {
                continue;
            }
            let bools = bool_fields(&body);
            let errors = optional_string_fields(&body);
            for b in &bools {
                let Some(stem) = b.strip_suffix("_ok") else {
                    continue;
                };
                let partner = format!("{stem}_error");
                if errors.iter().any(|e| e == &partner) {
                    findings.push(format!(
                        "{}: struct {name} has `{b}: bool` next to `{partner}: \
                         Option<String>`",
                        file.file_name().unwrap_or_default().to_string_lossy(),
                    ));
                }
            }
        }
    }
    findings
}

#[test]
fn no_status_struct_carries_an_ok_bool_next_to_an_error_string() {
    let dir = commands_dir();
    assert!(
        dir.is_dir(),
        "commands dir not found at {} — unexpected build layout",
        dir.display()
    );
    let mut files = Vec::new();
    rust_sources(&dir, &mut files);
    assert!(
        !files.is_empty(),
        "found no .rs files under {}",
        dir.display()
    );

    let findings = scan(&dir);

    assert!(
        findings.is_empty(),
        "The `*_ok: bool` + `*_error: Option<String>` pair is banned on status \
         structs — use `vct_launcher_core::check_state::CheckState` \
         (`Ok | NotApplicable | Unknown {{ error }}`) instead.\n\n\
         Why: a bool pair cannot express \"not applicable\", so that case has to \
         borrow `ok = true` and renders as a successful check that never ran. And \
         because it is two fields rather than a shared type, it does not travel: \
         v0.2.83 added exactly this pair to `installer::UpdateStatus`, it was never \
         carried to `self_update::UpdateStatus`, and the resulting blindness cost a \
         field user five weeks of silent non-updating.\n\n\
         Findings:\n  {}",
        findings.join("\n  ")
    );
}

/// RED-PROOF, permanent: point the REAL scan at a synthetic tree that
/// contains the banned shape and require it to be reported. Without this, a
/// scan that silently found nothing — a broken extractor, a wrong directory,
/// a typo'd suffix — would pass forever and read as "the codebase is clean".
#[test]
fn scan_finds_the_banned_shape_in_a_synthetic_tree() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let nested = tmp.path().join("sub");
    std::fs::create_dir_all(&nested).unwrap();

    std::fs::write(
        tmp.path().join("offender.rs"),
        "pub struct UpdateStatus {\n    \
             pub remote_check_ok: bool,\n    \
             pub remote_check_error: Option<String>,\n\
         }\n",
    )
    .unwrap();
    // Recursion must reach subdirectories (`commands/` has several).
    std::fs::write(
        nested.join("nested_offender.rs"),
        "pub struct DeepStatus {\n    \
             pub probe_ok: bool,\n    \
             pub probe_error: Option<String>,\n\
         }\n",
    )
    .unwrap();
    // Must NOT be reported: the tri-state, a non-Status struct with the
    // banned shape, an `*_ok` bool with no `*_error` partner, and a
    // commented-out declaration.
    std::fs::write(
        tmp.path().join("innocent.rs"),
        "pub struct GoodStatus {\n    \
             pub remote_check: CheckState,\n    \
             pub head_detached: bool,\n    \
             pub error: Option<String>,\n\
         }\n\
         pub struct NotAStatusStruct {\n    \
             pub thing_ok: bool,\n    \
             pub thing_error: Option<String>,\n\
         }\n\
         pub struct LonelyStatus {\n    \
             pub probe_ok: bool,\n\
         }\n\
         pub struct HistoricalStatus {\n    \
             // was: pub remote_check_ok: bool, remote_check_error: Option<String>\n    \
             pub remote_check: CheckState,\n\
         }\n",
    )
    .unwrap();

    let findings = scan(tmp.path());
    assert_eq!(
        findings.len(),
        2,
        "expected exactly the two planted offenders, got: {findings:?}"
    );
    assert!(
        findings.iter().any(|f| f.contains("remote_check_ok")),
        "the top-level offender must be reported: {findings:?}"
    );
    assert!(
        findings.iter().any(|f| f.contains("probe_ok")),
        "the offender in a SUBDIRECTORY must be reported too: {findings:?}"
    );
    assert!(
        !findings.iter().any(|f| f.contains("thing_ok")),
        "a non-`*Status` struct is out of scope by design: {findings:?}"
    );
}

/// Guard for the guard: the parser must actually FIND the structs it claims to
/// police. A lint whose extractor silently matches nothing passes forever and
/// proves nothing — the most expensive kind of test.
#[test]
fn the_lint_actually_parses_the_status_structs_it_polices() {
    let dir = commands_dir();
    let mut files = Vec::new();
    rust_sources(&dir, &mut files);

    let mut seen: Vec<String> = Vec::new();
    for file in &files {
        let Ok(raw) = std::fs::read_to_string(file) else {
            continue;
        };
        for (name, _) in structs(&strip_line_comments(&raw)) {
            if name.ends_with("Status") {
                seen.push(name);
            }
        }
    }

    // Both real subjects of WP-13 must be visible to the lint. If a refactor
    // renames or moves them, THIS fails first and says so, rather than the
    // ban silently ceasing to apply.
    for required in ["UpdateStatus"] {
        assert!(
            seen.iter().any(|n| n == required),
            "the lint did not find `struct {required}` under src/commands/ — its \
             extractor is broken or the struct moved. Found: {seen:?}"
        );
    }
    assert!(
        seen.len() >= 2,
        "expected at least the two `UpdateStatus` structs (installer + \
         self_update); found {seen:?}"
    );
}

/// The extractor's own unit tests, on synthetic input — so a failure of the
/// lint above is unambiguously about the codebase, not about the parser.
#[test]
fn extractor_recognises_the_banned_shape_and_the_allowed_ones() {
    let banned = r#"
        pub struct FooStatus {
            pub remote_check_ok: bool,
            pub remote_check_error: Option<String>,
        }
    "#;
    let (name, body) = structs(banned).into_iter().next().expect("parsed");
    assert_eq!(name, "FooStatus");
    assert!(bool_fields(&body).contains(&"remote_check_ok".to_string()));
    assert!(optional_string_fields(&body).contains(&"remote_check_error".to_string()));

    // Allowed: the tri-state, plus unrelated bools and unrelated errors.
    let allowed = r#"
        pub struct BarStatus {
            pub remote_check: CheckState,
            pub head_detached: bool,
            pub error: Option<String>,
            pub available: bool,
        }
    "#;
    let (_, body) = structs(allowed).into_iter().next().expect("parsed");
    let bools = bool_fields(&body);
    assert!(bools.contains(&"head_detached".to_string()));
    assert!(
        !bools.iter().any(|b| b.ends_with("_ok")),
        "nothing in the allowed shape is an `*_ok` bool"
    );

    // Nested braces must not truncate the body.
    let nested = r#"
        pub struct BazStatus {
            pub inner: HashMap<String, Vec<u8>>,
            pub thing_ok: bool,
            pub thing_error: Option<String>,
        }
    "#;
    let (_, body) = structs(nested).into_iter().next().expect("parsed");
    assert!(
        bool_fields(&body).contains(&"thing_ok".to_string())
            && optional_string_fields(&body).contains(&"thing_error".to_string()),
        "a field after a generic type must still be seen"
    );

    // A comment mentioning the shape must NOT count as the shape.
    let commented = r#"
        pub struct QuxStatus {
            // was: pub remote_check_ok: bool, pub remote_check_error: Option<String>,
            pub remote_check: CheckState,
        }
    "#;
    let (_, body) = structs(&strip_line_comments(commented))
        .into_iter()
        .next()
        .expect("parsed");
    assert!(
        bool_fields(&body).is_empty(),
        "a commented-out declaration must not trip the lint"
    );
}
