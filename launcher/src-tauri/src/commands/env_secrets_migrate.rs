// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! V47-G-final (v0.2.75 / P2): pure `.env` audit + sentinel-rewrite for the
//! launcher-side "Migrate from .env" button.
//!
//! This is the Rust MIRROR of `vco_lib/secrets_audit.py`. The install.py CLI
//! arm uses the Python module; the launcher GUI arm (`secrets_cmd::
//! migrate_env_secrets_from_dotenv`) uses this one so the in-process Tauri
//! command doesn't have to shell out to Python. The two MUST stay
//! behaviourally identical — a `.env` a user migrates from the GUI and one
//! they migrate from the CLI must produce byte-identical rewrites.
//!
//! ## MUST MATCH `vco_lib/secrets_audit.py`
//!
//! Any change to the parse rules, placeholder set, or sentinel-rewrite
//! behaviour here MUST be mirrored in `vco_lib/secrets_audit.py` (and vice
//! versa). The secret-shape predicate itself is NOT duplicated — both sides
//! consume the single B-3-guarded needle home
//! (`crate::mcp_registration::is_secret_shaped_env_key`, whose Python twin
//! `install.py::_is_secret_shaped_env_key` is CI-parity-tested against it in
//! `tests/test_secret_shaped_needles_parity.py`).
//!
//! ## Critical safety property (USER DATA NEVER LOST)
//!
//! Same as the Python module: [`rewrite_env_with_sentinels`] only ever
//! touches lines whose key is in the caller-supplied `migrated_keys` set
//! (which is derived from the hub's confirmed-migrated list). A key the
//! audit missed (e.g. a multi-line value) is never sentinel'd — its raw
//! value stays exactly where the user put it.

use crate::mcp_registration::is_secret_shaped_env_key;

/// Sentinel written to `.env` after a key migrates to the keychain.
/// Downstream resolvers (`templates/scripts/vct_secrets_resolve.sh`) treat
/// it as "unresolved — ask the hub". Bracketed with double underscores so
/// it can never collide with a legitimate API-key / token / password value.
/// MUST match `vco_lib/secrets_audit.py::KEYCHAIN_SENTINEL`.
pub const KEYCHAIN_SENTINEL: &str = "__vco_keychain__";

/// Placeholder values that are NOT real secrets — installers / templates
/// write these as documentation hints. Matching is case-insensitive after
/// stripping surrounding whitespace + quotes. MUST match
/// `vco_lib/secrets_audit.py::_PLACEHOLDER_VALUES`.
const PLACEHOLDER_VALUES: &[&str] = &[
    "",
    "changeme",
    "change-me",
    "your-api-key-here",
    "your_api_key_here",
    "<your-api-key>",
    "<your_api_key>",
    "<your-key>",
    "<your-secret>",
    "<your-token>",
    "<your-password>",
    "<placeholder>",
    "placeholder",
    "todo",
    "fixme",
    "xxx",
    "yyy",
    "...",
    KEYCHAIN_SENTINEL, // already migrated → skip on re-runs
];

/// One `(key, value)` pair from `.env` that looks like a credential and
/// carries a real (non-placeholder) value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnvSecret {
    pub key: String,
    pub value: String,
}

/// Strip a single pair of matching surrounding quotes from a value.
/// `KEY="foo"` → `foo`; `KEY='bar'` → `bar`; `KEY=baz` → `baz`.
/// Does NOT interpret escape sequences (matches the Python `_strip_inline_quotes`).
fn strip_inline_quotes(raw: &str) -> String {
    let val = raw.trim();
    let bytes = val.as_bytes();
    if bytes.len() >= 2 {
        let first = bytes[0];
        let last = bytes[bytes.len() - 1];
        if (first == b'"' && last == b'"') || (first == b'\'' && last == b'\'') {
            return val[1..val.len() - 1].to_string();
        }
    }
    val.to_string()
}

/// True if `value` looks like an installer-written placeholder.
/// Case-insensitive; stripped of surrounding whitespace + quotes.
fn is_placeholder_value(value: &str) -> bool {
    let val = strip_inline_quotes(value).trim().to_ascii_lowercase();
    PLACEHOLDER_VALUES.iter().any(|p| *p == val)
}

/// Parse `.env` text and return entries whose key is secret-shaped AND whose
/// value is non-placeholder.
///
/// Comments (`#` at start of trimmed line) are skipped. `export KEY=val` is
/// supported. Lines without an `=` are skipped. Line-based, like
/// `python-dotenv` default and the Python mirror — multi-line values are
/// intentionally not stitched (under-flagging is the safe failure mode).
pub fn audit_env_secrets(text: &str) -> Vec<EnvSecret> {
    let mut out: Vec<EnvSecret> = Vec::new();
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        // Support `export KEY=val`.
        let body = if let Some(rest) = line.strip_prefix("export ") {
            rest.trim_start()
        } else {
            line
        };
        let eq = match body.find('=') {
            Some(i) if i > 0 => i,
            _ => continue, // no `=`, or empty key
        };
        let key = body[..eq].trim();
        if key.is_empty() || !is_secret_shaped_env_key(key) {
            continue;
        }
        let raw_value = &body[eq + 1..];
        // Strip inline comment after an unquoted value: KEY=val  # note.
        // Quoted values may contain `#`; only trim post-value for unquoted.
        let mut stripped = raw_value.trim().to_string();
        if !(stripped.starts_with('"') || stripped.starts_with('\'')) {
            if let Some(hash_pos) = stripped.find('#') {
                stripped = stripped[..hash_pos].trim_end().to_string();
            }
        }
        let value = strip_inline_quotes(&stripped);
        if is_placeholder_value(&value) {
            continue;
        }
        out.push(EnvSecret {
            key: key.to_string(),
            value,
        });
    }
    out
}

/// Result of a sentinel rewrite: the new file text, how many lines were
/// replaced, and which requested keys were not found in the file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RewriteResult {
    pub text: String,
    pub replaced: usize,
    pub missed: Vec<String>,
}

/// Replace migrated keys' values in `.env` text with [`KEYCHAIN_SENTINEL`],
/// preserving leading whitespace, the `export` prefix, and any trailing
/// inline comment on an unquoted value. Only the FIRST occurrence of each
/// key is replaced (a duplicate key is left as-is). Lines whose key is not
/// in `migrated_keys` are passed through byte-identical.
///
/// Returns the rewritten text plus bookkeeping. MUST match
/// `vco_lib/secrets_audit.py::rewrite_env_with_sentinels`. The atomic-write
/// to disk is the caller's job (this fn is pure so it's unit-testable).
pub fn rewrite_env_with_sentinels(text: &str, migrated_keys: &[String]) -> RewriteResult {
    use std::collections::BTreeSet;
    let keyset: BTreeSet<&str> = migrated_keys.iter().map(|s| s.as_str()).collect();
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut out_lines: Vec<String> = Vec::new();
    let mut replaced = 0usize;

    for raw in text.lines() {
        let line = raw;
        let stripped = line.trim_start();
        if stripped.is_empty() || stripped.starts_with('#') {
            out_lines.push(line.to_string());
            continue;
        }
        let leading_ws_len = line.len() - stripped.len();
        let leading_ws = &line[..leading_ws_len];
        let mut body = stripped;
        let mut export_prefix = "";
        if let Some(rest) = body.strip_prefix("export ") {
            export_prefix = "export ";
            body = rest.trim_start();
        }
        let eq = match body.find('=') {
            Some(i) if i > 0 => i,
            _ => {
                out_lines.push(line.to_string());
                continue;
            }
        };
        let key = body[..eq].trim();
        if !keyset.contains(key) {
            out_lines.push(line.to_string());
            continue;
        }
        if seen.contains(key) {
            // Duplicate key — preserve as-is (first occurrence already done).
            out_lines.push(line.to_string());
            continue;
        }
        seen.insert(key.to_string());
        let raw_value = &body[eq + 1..];
        // Detect trailing inline comment on an unquoted value.
        let mut trailing_comment = String::new();
        let val_stripped = raw_value.trim();
        if !(val_stripped.starts_with('"') || val_stripped.starts_with('\'')) {
            if let Some(hash_pos) = val_stripped.find('#') {
                trailing_comment = format!("  {}", &val_stripped[hash_pos..]);
            }
        }
        out_lines.push(format!(
            "{}{}{}={}{}",
            leading_ws, export_prefix, key, KEYCHAIN_SENTINEL, trailing_comment
        ));
        replaced += 1;
    }

    let missed: Vec<String> = keyset
        .iter()
        .filter(|k| !seen.contains(**k))
        .map(|k| k.to_string())
        .collect();

    // Preserve a trailing newline if the original had one.
    let mut new_text = out_lines.join("\n");
    if text.ends_with('\n') {
        new_text.push('\n');
    }

    RewriteResult {
        text: new_text,
        replaced,
        missed,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn audit_flags_secret_shaped_keys_with_real_values() {
        let env = "\
# a comment
export GITHUB_TOKEN=ghp_realvalue
OPENAI_API_KEY=\"sk-abc123\"
PLAIN_VAR=not-a-secret
DB_PASSWORD='hunter2'
NOT_A_KEY_HERE=whatever
";
        let got = audit_env_secrets(env);
        assert_eq!(
            got,
            vec![
                EnvSecret { key: "GITHUB_TOKEN".into(), value: "ghp_realvalue".into() },
                EnvSecret { key: "OPENAI_API_KEY".into(), value: "sk-abc123".into() },
                EnvSecret { key: "DB_PASSWORD".into(), value: "hunter2".into() },
            ]
        );
    }

    #[test]
    fn audit_skips_placeholder_and_already_migrated_values() {
        let env = "\
API_TOKEN=changeme
STRIPE_KEY=<your-api-key>
OLD_SECRET=__vco_keychain__
GOOD_TOKEN=real
";
        let got = audit_env_secrets(env);
        assert_eq!(got, vec![EnvSecret { key: "GOOD_TOKEN".into(), value: "real".into() }]);
    }

    #[test]
    fn audit_strips_trailing_comment_on_unquoted_value_only() {
        let env = "\
A_TOKEN=abc  # inline note
B_SECRET=\"has # inside quotes\"
";
        let got = audit_env_secrets(env);
        assert_eq!(
            got,
            vec![
                EnvSecret { key: "A_TOKEN".into(), value: "abc".into() },
                EnvSecret { key: "B_SECRET".into(), value: "has # inside quotes".into() },
            ]
        );
    }

    #[test]
    fn rewrite_replaces_only_migrated_keys_preserving_structure() {
        // Trailing-comment preservation applies to UNQUOTED values only —
        // for a quoted value the comment is dropped (matches the Python
        // mirror `rewrite_env_with_sentinels`, whose trailing-comment branch
        // is gated on the value NOT starting with a quote). `KEEP_TOKEN`
        // exercises the unquoted+comment path where the comment IS kept.
        let env = "\
# header
export OPENAI_API_KEY=\"sk-abc123\"  # quoted, comment dropped
KEEP_TOKEN=raw  # unquoted, comment kept
PLAIN=keepme
DB_PASSWORD=hunter2
";
        let res = rewrite_env_with_sentinels(
            env,
            &[
                "OPENAI_API_KEY".to_string(),
                "KEEP_TOKEN".to_string(),
                "DB_PASSWORD".to_string(),
            ],
        );
        assert_eq!(res.replaced, 3);
        assert!(res.missed.is_empty());
        assert_eq!(
            res.text,
            "\
# header
export OPENAI_API_KEY=__vco_keychain__
KEEP_TOKEN=__vco_keychain__  # unquoted, comment kept
PLAIN=keepme
DB_PASSWORD=__vco_keychain__
"
        );
    }

    #[test]
    fn rewrite_leaves_untouched_keys_byte_identical_and_reports_missed() {
        let env = "SECRET_A=one\nPLAIN=two\n";
        let res = rewrite_env_with_sentinels(
            env,
            &["SECRET_A".to_string(), "ABSENT_TOKEN".to_string()],
        );
        assert_eq!(res.replaced, 1);
        assert_eq!(res.missed, vec!["ABSENT_TOKEN".to_string()]);
        assert_eq!(res.text, "SECRET_A=__vco_keychain__\nPLAIN=two\n");
    }

    #[test]
    fn rewrite_only_first_occurrence_of_duplicate_key() {
        let env = "TOKEN=first\nTOKEN=second\n";
        let res = rewrite_env_with_sentinels(env, &["TOKEN".to_string()]);
        assert_eq!(res.replaced, 1);
        assert_eq!(res.text, "TOKEN=__vco_keychain__\nTOKEN=second\n");
    }

    #[test]
    fn rewrite_preserves_absence_of_trailing_newline() {
        let env = "TOKEN=v"; // no trailing newline
        let res = rewrite_env_with_sentinels(env, &["TOKEN".to_string()]);
        assert_eq!(res.text, "TOKEN=__vco_keychain__");
    }

    // ── Cross-language parity (v0.2.75 Part 7 / Part 10) ─────────────────
    //
    // The Rust side of the shared `tests/fixtures/env_secrets_parity.json`.
    // `tests/test_env_secrets_migrate_parity.py` consumes the SAME fixture for
    // the Python `vco_lib.secrets_audit` — so a divergence between this mirror
    // and the Python auditor/rewriter fails one of the two runners. Comment-only
    // "MUST MATCH" parity is a fork risk (B-3 lesson); this fixture makes the
    // contract executable. Fixture path resolved from `CARGO_MANIFEST_DIR`
    // (= `launcher/src-tauri/`) → two parents up to the repo root (same walk as
    // `tests/project_naming_parity.rs`).

    #[derive(serde::Deserialize)]
    struct SecretsFixture {
        #[serde(rename = "_format_version", default)]
        format_version: u32,
        cases: Vec<SecretsCase>,
    }

    #[derive(serde::Deserialize)]
    struct SecretsCase {
        name: String,
        input: String,
        audit_expected: Vec<AuditPair>,
        rewrite: RewriteCase,
    }

    #[derive(serde::Deserialize)]
    struct AuditPair {
        key: String,
        value: String,
    }

    #[derive(serde::Deserialize)]
    struct RewriteCase {
        migrated_keys: Vec<String>,
        expected_text: String,
        expected_replaced: usize,
        expected_missed: Vec<String>,
    }

    fn load_secrets_fixture() -> SecretsFixture {
        let manifest_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let repo_root = manifest_dir
            .parent()
            .and_then(|p| p.parent())
            .expect("CARGO_MANIFEST_DIR must have two parents (repo layout)");
        let fixture_path = repo_root
            .join("tests")
            .join("fixtures")
            .join("env_secrets_parity.json");
        assert!(
            fixture_path.exists(),
            "Parity fixture missing: {} — shared with \
             tests/test_env_secrets_migrate_parity.py",
            fixture_path.display()
        );
        let raw = std::fs::read_to_string(&fixture_path)
            .unwrap_or_else(|e| panic!("read {}: {}", fixture_path.display(), e));
        let fix: SecretsFixture = serde_json::from_str(&raw)
            .unwrap_or_else(|e| panic!("parse {}: {}", fixture_path.display(), e));
        assert_eq!(
            fix.format_version, 1,
            "Fixture _format_version != 1 — coordinate the bump with the Python side"
        );
        assert!(!fix.cases.is_empty(), "Fixture has no cases");
        fix
    }

    #[test]
    fn env_secrets_parity_matches_shared_fixture() {
        let fix = load_secrets_fixture();
        let mut failures: Vec<String> = Vec::new();

        for case in &fix.cases {
            // Audit parity: same (key, value) pairs, same order.
            let got = audit_env_secrets(&case.input);
            let expected: Vec<EnvSecret> = case
                .audit_expected
                .iter()
                .map(|p| EnvSecret {
                    key: p.key.clone(),
                    value: p.value.clone(),
                })
                .collect();
            if got != expected {
                failures.push(format!(
                    "  [{}] audit: got {:?}, expected {:?}",
                    case.name, got, expected
                ));
            }

            // Rewrite parity: same text, same replaced count, same missed set.
            let res = rewrite_env_with_sentinels(&case.input, &case.rewrite.migrated_keys);
            if res.text != case.rewrite.expected_text {
                failures.push(format!(
                    "  [{}] rewrite text: got {:?}, expected {:?}",
                    case.name, res.text, case.rewrite.expected_text
                ));
            }
            if res.replaced != case.rewrite.expected_replaced {
                failures.push(format!(
                    "  [{}] rewrite replaced: got {}, expected {}",
                    case.name, res.replaced, case.rewrite.expected_replaced
                ));
            }
            let mut got_missed = res.missed.clone();
            got_missed.sort();
            let mut want_missed = case.rewrite.expected_missed.clone();
            want_missed.sort();
            if got_missed != want_missed {
                failures.push(format!(
                    "  [{}] rewrite missed: got {:?}, expected {:?}",
                    case.name, got_missed, want_missed
                ));
            }
        }

        assert!(
            failures.is_empty(),
            "Rust .env secret auditor/rewriter diverges from the shared fixture \
             in {} case(s):\n{}\nIf intentional, regenerate the fixture AND update \
             vco_lib/secrets_audit.py in the same commit.",
            failures.len(),
            failures.join("\n")
        );
    }
}
