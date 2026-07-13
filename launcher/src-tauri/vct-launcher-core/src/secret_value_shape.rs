// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Rust mirror of the single-line secret-value shape SSOT (v0.2.80 Part A).
//!
//! Python is the source of truth: `vco_lib/secret_value_shape.py`. This module
//! reproduces `is_single_line_secret` EXACTLY — same check order, same reason
//! slugs — so a blob is rejected at the launcher's in-process write boundaries
//! before it can be laundered into the OS keychain or an env-pair write.
//!
//! Both are pinned to the shared fixture
//! `tests/fixtures/secret_value_shape_parity.json`; the parity `#[test]` in
//! `secrets_import.rs` fails if this mirror diverges from the Python SSOT.
//!
//! ## Home of the predicate (v0.2.80 A4 — moved down to core)
//!
//! v0.2.80 Part A first landed this module in the APP crate
//! (`commands/secret_value_shape.rs`). A4 MOVED it down into
//! `vct-launcher-core` so the keychain-write chokepoint `secrets::set` (which
//! lives in this crate — a lower layer than the app crate) can call it: core
//! cannot call *up* into the app crate, so the predicate had to come *down*.
//! The app crate now re-exports these symbols from here (a thin
//! `pub(crate) use` in `commands/secret_value_shape.rs`), so A2's original
//! call-sites in `installer.rs`/`secrets_import.rs` compile UNCHANGED. There is
//! still exactly ONE Rust copy of the predicate — it just lives one layer down.
//!
//! ## Why one shared module (SHARED-CODE rule)
//!
//! The predicate guards every in-process keychain write through the
//! `secrets::set` chokepoint (v0.2.80 A4), plus the earlier read-boundary
//! guards — `read_whole_file_trimmed` (secrets_import.rs) and
//! `github_pat_from_legacy_file` + `migrate_github_pat_file_to_keychain`
//! (installer.rs). Per the standing one-concern-one-home rule, there is exactly
//! ONE Rust copy of the predicate (this module), consumed by app + hub + core.
//! Do NOT duplicate it.
//!
//! ## Value-handling discipline (INVIOLABLE)
//!
//! No function here prints, logs, or formats the value. It operates purely on
//! the `&str` it is handed and returns a verdict `(ok, reason_slug)`. Callers
//! surface only the reason slug + byte-length, never the value.
//!
//! ## Why hand-rolled scans, not `regex`
//!
//! The sibling parity mirror `env_secrets_migrate.rs` hand-rolls its char scans
//! (no regex dep in the parity path). The predicate here is line-split + a
//! handful of prefix/anchor checks that are simple enough to hand-roll, so we
//! match that style: no `Regex::new` per-call cost, no lazy-static, trivially
//! reviewable against the Python patterns.

/// A `github_pat`-named single-line value at or above this length is almost
/// certainly a concatenated / duplicated write. A well-formed classic PAT is 40
/// chars; a fine-grained PAT is ~93. 200 is a wide margin no legitimate GitHub
/// token reaches. MUST equal `GITHUB_PAT_MAX_LEN` in the Python SSOT.
const GITHUB_PAT_MAX_LEN: usize = 200;

/// A GitHub classic PAT: `ghp_` + exactly 36 base62 chars → 40 total. Used by
/// [`classify_secret_value`] to flag a `ghp_`-prefixed single-line token whose
/// shape is wrong as `length_corruption`. Mirrors `_CLASSIC_PAT_RE`.
/// Consumed only by the taxonomy classifier (currently test-only).
#[cfg_attr(not(test), allow(dead_code))]
const CLASSIC_PAT_TOTAL_LEN: usize = 40;

/// Prefix that identifies a value as github-pat-shaped even without a key name.
/// Mirrors `_GHP_PREFIX`. Consumed by the taxonomy classifier (test-only until
/// Part C wires the doctor taxonomy to it).
#[cfg_attr(not(test), allow(dead_code))]
const GHP_PREFIX: &str = "ghp_";

// ---------------------------------------------------------------------------
// Low-level char / line predicates — each mirrors one Python regex.
// ---------------------------------------------------------------------------

/// Mirror of `_FORBIDDEN_CONTROL_RE = [\x00-\x08\x0b\x0c\x0e-\x1f\x7f]`.
///
/// The forbidden C0 controls plus DEL. `\n` (0x0a) and `\r` (0x0d) are
/// deliberately EXCLUDED — they define line structure and are handled by the
/// multi-line logic. `\t` (0x09) is also excluded (not in the Python class).
fn is_forbidden_control(c: char) -> bool {
    let b = c as u32;
    matches!(b, 0x00..=0x08 | 0x0b | 0x0c | 0x0e..=0x1f | 0x7f)
}

fn has_forbidden_control(value: &str) -> bool {
    value.chars().any(is_forbidden_control)
}

/// Mirror of `_GITHUB_PAT_KEY_RE = ^github_pat(?:[._-].*)?$` (case-insensitive),
/// applied to `key_name.strip()`.
///
/// True when the trimmed key is exactly `github_pat` (any case) OR
/// `github_pat` followed by one of `.`, `_`, `-` and any suffix.
fn is_github_pat_key(key_name: &str) -> bool {
    let trimmed = key_name.trim();
    if trimmed.is_empty() {
        return false;
    }
    let lower = trimmed.to_ascii_lowercase();
    let rest = match lower.strip_prefix("github_pat") {
        Some(r) => r,
        None => return false,
    };
    // `^github_pat$` — the whole trimmed key is exactly the prefix.
    if rest.is_empty() {
        return true;
    }
    // `github_pat[._-].*` — first char after the prefix is a separator.
    matches!(rest.as_bytes()[0], b'.' | b'_' | b'-')
}

/// Python's `str.rstrip("\r\n").rstrip()` on `value`.
///
/// First strips trailing `\r`/`\n` (any run), then strips trailing ASCII
/// whitespace (Python `str.rstrip()` with no args). We match Python's
/// whitespace class closely enough for secret values: strip trailing chars in
/// {space, `\t`, `\n`, `\r`, `\x0b`, `\x0c`}. Interior newlines are what the
/// caller cares about, and those are never touched here.
fn python_trailing_trim(value: &str) -> &str {
    // Step 1: rstrip("\r\n") — remove a trailing run of \r and \n only.
    let step1 = value.trim_end_matches(['\r', '\n']);
    // Step 2: rstrip() — remove trailing Python-whitespace.
    step1.trim_end_matches(|c: char| {
        c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\u{0b}' || c == '\u{0c}'
    })
}

/// True when `value` contains an interior newline (`\n` or `\r`). Mirrors the
/// Python `"\n" not in trimmed and "\r" not in trimmed` single-line gate
/// (inverted).
fn has_interior_newline(value: &str) -> bool {
    value.contains('\n') || value.contains('\r')
}

/// Python `str.splitlines()`: split on `\n`, `\r`, and `\r\n` boundaries, with
/// NO trailing empty element (unlike `str::lines`, splitlines drops a trailing
/// empty line the same way). We reproduce the boundary set Python uses for the
/// two call-sites this module needs (`_non_empty_lines`, `_has_blob_signature`).
///
/// Python's `splitlines()` also treats `\x0b`, `\x0c`, `\x1c`-`\x1e`,
/// ` `, ` `, `\x85` as line boundaries; for secret values those never
/// appear in a legitimate case and the forbidden-control gate already rejects
/// `\x0b`/`\x0c`/`\x1c`-`\x1e`/`\x85` before we get here. Splitting on `\n`/`\r`
/// (with `\r\n` collapsed) matches every fixture case.
fn splitlines(value: &str) -> Vec<&str> {
    let mut out: Vec<&str> = Vec::new();
    let bytes = value.as_bytes();
    let mut start = 0usize;
    let mut i = 0usize;
    let n = bytes.len();
    while i < n {
        let b = bytes[i];
        if b == b'\n' {
            out.push(&value[start..i]);
            i += 1;
            start = i;
        } else if b == b'\r' {
            out.push(&value[start..i]);
            i += 1;
            // Collapse \r\n into a single boundary.
            if i < n && bytes[i] == b'\n' {
                i += 1;
            }
            start = i;
        } else {
            i += 1;
        }
    }
    // Trailing segment: only appended when non-empty (splitlines drops a
    // trailing empty line produced by a final boundary).
    if start < n {
        out.push(&value[start..n]);
    }
    out
}

/// Mirror of `_non_empty_lines`: `[ln.rstrip() for ln in value.splitlines()
/// if ln.strip()]`. Trailing whitespace is stripped per line so a PEM whose
/// lines have trailing spaces still matches the BEGIN/END anchors; blank /
/// whitespace-only lines are dropped.
fn non_empty_lines(value: &str) -> Vec<String> {
    splitlines(value)
        .into_iter()
        .filter(|ln| !ln.trim().is_empty())
        .map(|ln| {
            ln.trim_end_matches(|c: char| {
                c == ' '
                    || c == '\t'
                    || c == '\n'
                    || c == '\r'
                    || c == '\u{0b}'
                    || c == '\u{0c}'
            })
            .to_string()
        })
        .collect()
}

/// Mirror of the legit-multiline BEGIN anchor:
/// `^-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE|PUBLIC KEY)-----$`.
fn matches_pem_begin(line: &str) -> bool {
    matches_pem_marker(line, "-----BEGIN ")
}

/// Mirror of the legit-multiline END anchor:
/// `^-----END [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE|PUBLIC KEY)-----$`.
fn matches_pem_end(line: &str) -> bool {
    matches_pem_marker(line, "-----END ")
}

/// Shared body for the PEM BEGIN/END anchors. `prefix` is `"-----BEGIN "` or
/// `"-----END "`. After the prefix: `[A-Z0-9 ]*` then one of the three key
/// phrases then `-----` then end-of-string.
fn matches_pem_marker(line: &str, prefix: &str) -> bool {
    let rest = match line.strip_prefix(prefix) {
        Some(r) => r,
        None => return false,
    };
    // Must end with the closing `-----`.
    let inner = match rest.strip_suffix("-----") {
        Some(i) => i,
        None => return false,
    };
    // `inner` = `[A-Z0-9 ]*` immediately followed by one of the key phrases.
    // The `[A-Z0-9 ]*` is greedy in the regex but the fixed key-phrase suffix
    // disambiguates: try each phrase, and require the remaining head to be
    // wholly `[A-Z0-9 ]`.
    for phrase in ["PRIVATE KEY", "CERTIFICATE", "PUBLIC KEY"] {
        if let Some(head) = inner.strip_suffix(phrase) {
            if head.bytes().all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b' ') {
                return true;
            }
        }
    }
    false
}

/// Mirror of `_is_legit_multiline`: first non-empty line is a PEM BEGIN marker
/// and last non-empty line is a PEM END marker (and there are >= 2 non-empty
/// lines).
fn is_legit_multiline(value: &str) -> bool {
    let lines = non_empty_lines(value);
    if lines.len() < 2 {
        return false;
    }
    let first = &lines[0];
    let last = &lines[lines.len() - 1];
    matches_pem_begin(first) && matches_pem_end(last)
}

/// Mirror of `_BLOB_KEY_EQ_RE = ^(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*=`,
/// applied at COLUMN 0 of a line (no leading-whitespace tolerance).
///
/// True when the line begins with an optional `export` + one-or-more spaces/
/// tabs, then an env-key identifier (`[A-Za-z_][A-Za-z0-9_]*`), then `=`.
fn matches_blob_key_eq(line: &str) -> bool {
    let bytes = line.as_bytes();
    let mut i = 0usize;
    let n = bytes.len();

    // Optional `export[ \t]+` at column 0.
    if line.starts_with("export") {
        let after = &bytes[6..]; // len("export") == 6
        // Require at least one space or tab immediately after `export`.
        if !after.is_empty() && (after[0] == b' ' || after[0] == b'\t') {
            i = 6;
            // Consume the run of spaces/tabs.
            while i < n && (bytes[i] == b' ' || bytes[i] == b'\t') {
                i += 1;
            }
        }
        // If `export` is NOT followed by a space/tab (e.g. `exportKEY=`), the
        // optional group does not match; fall through with i=0 and let the
        // identifier scan start at column 0 (matching the regex's optional
        // non-capturing group semantics).
    }

    // `[A-Za-z_]` — first identifier char.
    if i >= n {
        return false;
    }
    let first = bytes[i];
    if !(first.is_ascii_alphabetic() || first == b'_') {
        return false;
    }
    i += 1;
    // `[A-Za-z0-9_]*` — rest of the identifier.
    while i < n {
        let b = bytes[i];
        if b.is_ascii_alphanumeric() || b == b'_' {
            i += 1;
        } else {
            break;
        }
    }
    // Immediately followed by `=`.
    i < n && bytes[i] == b'='
}

/// Mirror of `_has_blob_signature`: any line AFTER the first matches the
/// column-0 `KEY=` / `export KEY=` pattern.
fn has_blob_signature(value: &str) -> bool {
    let lines = splitlines(value);
    lines.iter().skip(1).any(|line| matches_blob_key_eq(line))
}

// ---------------------------------------------------------------------------
// Public predicate — the direct mirror of `is_single_line_secret`.
// ---------------------------------------------------------------------------

/// Classify `value` as a writable single-well-formed secret vs a blob.
///
/// Returns `Ok(())` for a single-line value, an allowlisted legit multi-line
/// secret (PEM/cert/OpenSSH), or (with `allow_multiline`) a caller-vouched
/// multi-line value. Returns `Err(reason_slug)` for a blob-shaped value, where
/// the slug is machine-stable and NEVER contains the value:
///
///   * `"control_char"` — a forbidden control character anywhere.
///   * `"github_pat_over_200"` — an over-long single-line `github_pat`-named value.
///   * `"blob_key_eq_continuation"` — a column-0 `KEY=` continuation line.
///   * `"embedded_newline"` — multi-line with no recognised structure.
///
/// Check order is load-bearing and MUST match `vco_lib/secret_value_shape.py`.
///
/// v0.2.80 A4: promoted from `pub(crate)` to `pub` so the `secrets::set`
/// chokepoint in this crate AND the re-export in the app crate (plus the hub
/// crate) can all reach the single copy.
pub fn is_single_line_secret(
    value: &str,
    allow_multiline: bool,
    key_name: &str,
) -> Result<(), &'static str> {
    // 1. Control char anywhere → reject even under allow_multiline, even for a
    //    single line. (\n / \r are excluded from this class.)
    if has_forbidden_control(value) {
        return Err("control_char");
    }

    let github_pat_key = is_github_pat_key(key_name);

    // 2. Strip trailing whitespace/newlines. Interior \n / \r are what matter.
    let trimmed = python_trailing_trim(value);

    if !has_interior_newline(trimmed) {
        // Genuinely single-line. github_pat length heuristic: an over-long
        // single-line github_pat value is a concatenation. allow_multiline does
        // NOT bypass this.
        if github_pat_key && trimmed.chars().count() >= GITHUB_PAT_MAX_LEN {
            return Err("github_pat_over_200");
        }
        return Ok(());
    }

    // Multi-line from here down.
    if is_legit_multiline(trimmed) {
        return Ok(());
    }

    if allow_multiline {
        return Ok(());
    }

    if has_blob_signature(trimmed) {
        return Err("blob_key_eq_continuation");
    }

    // Multi-line, not a legit format, no column-0 KEY= line.
    Err("embedded_newline")
}

/// Corruption-taxonomy tag for `value` — mirror of `classify_secret_value`.
///
/// Returns one of `"ok"`, `"legit_multiline"`, `"blob"`, `"length_corruption"`.
/// This is the doctor/recovery-side classifier; the write-boundary guards only
/// need [`is_single_line_secret`]. Exposed here so the parity `#[test]` can
/// assert the taxonomy leg too (predicate parity is the required part; taxonomy
/// parity is a cheap bonus). Currently consumed only by the parity test; a
/// future doctor-side Rust surface (Part C) will call it in the non-test build.
///
/// v0.2.80 A4: promoted to `pub` alongside [`is_single_line_secret`] so the
/// re-export in the app crate keeps compiling (the parity test calls it).
#[cfg_attr(not(test), allow(dead_code))]
pub fn classify_secret_value(value: &str, key_name: &str) -> &'static str {
    let verdict = is_single_line_secret(value, false, key_name);
    let trimmed = python_trailing_trim(value);

    match verdict {
        Ok(()) => {
            if is_legit_multiline(trimmed) {
                return "legit_multiline";
            }
            // Single-line: a ghp_-prefixed token whose shape is wrong (not the
            // exact 40-char classic PAT) → length_corruption. Well-formed
            // classic PAT (or any non-ghp_ single value) → ok.
            if trimmed.starts_with(GHP_PREFIX) && !is_classic_pat(trimmed) {
                return "length_corruption";
            }
            "ok"
        }
        Err("github_pat_over_200") => "length_corruption",
        // control_char / blob_key_eq_continuation / embedded_newline → blob.
        Err(_) => "blob",
    }
}

/// Mirror of `_CLASSIC_PAT_RE = ^ghp_[A-Za-z0-9]{36}$` → exactly 40 chars,
/// `ghp_` + 36 base62. Consumed by the taxonomy classifier (test-only until
/// Part C).
#[cfg_attr(not(test), allow(dead_code))]
fn is_classic_pat(value: &str) -> bool {
    if value.len() != CLASSIC_PAT_TOTAL_LEN {
        return false;
    }
    let rest = match value.strip_prefix(GHP_PREFIX) {
        Some(r) => r,
        None => return false,
    };
    rest.len() == 36 && rest.bytes().all(|b| b.is_ascii_alphanumeric())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn single_line_plain_ok() {
        assert_eq!(is_single_line_secret("ghp_abcdef", false, ""), Ok(()));
        assert_eq!(is_single_line_secret("plain-token-123", false, ""), Ok(()));
    }

    #[test]
    fn trailing_newline_is_still_single_line() {
        assert_eq!(is_single_line_secret("value\n", false, ""), Ok(()));
        assert_eq!(is_single_line_secret("value\r\n", false, ""), Ok(()));
        assert_eq!(is_single_line_secret("value  \n", false, ""), Ok(()));
    }

    #[test]
    fn control_char_rejected_even_single_line() {
        assert_eq!(
            is_single_line_secret("val\u{0}ue", false, ""),
            Err("control_char")
        );
        // \t is NOT a forbidden control (excluded from the class).
        assert_eq!(is_single_line_secret("val\tue", false, ""), Ok(()));
    }

    #[test]
    fn control_char_not_bypassed_by_allow_multiline() {
        assert_eq!(
            is_single_line_secret("a\u{7f}b", true, ""),
            Err("control_char")
        );
    }

    #[test]
    fn embedded_newline_rejected() {
        assert_eq!(
            is_single_line_secret("line-one\nline-two", false, ""),
            Err("embedded_newline")
        );
    }

    #[test]
    fn blob_key_eq_rejected() {
        assert_eq!(
            is_single_line_secret("tok\nKEY=value", false, ""),
            Err("blob_key_eq_continuation")
        );
        assert_eq!(
            is_single_line_secret("tok\nexport KEY=value", false, ""),
            Err("blob_key_eq_continuation")
        );
    }

    #[test]
    fn github_pat_over_200_rejected() {
        let long = "ghp_".to_string() + &"A".repeat(210);
        assert_eq!(
            is_single_line_secret(&long, false, "github_pat"),
            Err("github_pat_over_200")
        );
        // Same length, NON-github_pat key → not rejected on length alone.
        assert_eq!(is_single_line_secret(&long, false, "some_jwt"), Ok(()));
    }

    #[test]
    fn github_pat_key_variants() {
        assert!(is_github_pat_key("github_pat"));
        assert!(is_github_pat_key("GitHub_PAT"));
        assert!(is_github_pat_key("github_pat.recovered-no-workflow"));
        assert!(is_github_pat_key("  github_pat  "));
        assert!(!is_github_pat_key("github_patx")); // no separator after prefix
        assert!(!is_github_pat_key("mygithub_pat"));
        assert!(!is_github_pat_key(""));
    }

    #[test]
    fn pem_is_legit_multiline() {
        let pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIB\nAAAA\n-----END RSA PRIVATE KEY-----";
        assert_eq!(is_single_line_secret(pem, false, "deploy_key"), Ok(()));
        assert_eq!(classify_secret_value(pem, "deploy_key"), "legit_multiline");
    }

    #[test]
    fn allow_multiline_escape_hatch() {
        // Unrecognised multi-line, no blob signature → embedded_newline unless
        // allow_multiline vouches for it.
        assert_eq!(
            is_single_line_secret("aaa\nbbb", false, ""),
            Err("embedded_newline")
        );
        assert_eq!(is_single_line_secret("aaa\nbbb", true, ""), Ok(()));
    }

    #[test]
    fn classify_length_corruption() {
        // ghp_ prefix, wrong length, single line → length_corruption.
        assert_eq!(classify_secret_value("ghp_tooshort", ""), "length_corruption");
        // exact classic PAT → ok.
        let classic = "ghp_".to_string() + &"a".repeat(36);
        assert_eq!(classify_secret_value(&classic, ""), "ok");
    }
}
