// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! V47-G-final (v0.2.75 / P2): the pure `.env` AUDIT for the launcher-side
//! "Migrate from .env" button (which keys hold a secret-shaped value).
//!
//! The audit is a Rust MIRROR of `vco_lib/secrets_audit.py::audit_env_secrets`
//! (MUST MATCH it — the parse rules and placeholder set; pinned against the
//! shared `tests/fixtures/env_secrets_parity.json` by
//! `env_secrets_parity_matches_shared_fixture` here and
//! `tests/test_env_secrets_migrate_parity.py`). The secret-shape predicate is
//! NOT duplicated: both sides consume `crate::mcp_registration::
//! is_secret_shaped_env_key` (Python twin parity-tested in
//! `tests/test_secret_shaped_needles_parity.py`).
//!
//! The sentinel REWRITE is not here any more (v0.2.97): it writes the
//! project's `.env`, which has ONE writer — `vco_lib.env_template.
//! replace_values_with_sentinel`, reached through
//! `services::vco_lib_bridge::sentinel_project_env_keys`. Its safety
//! property (USER DATA NEVER LOST) is the same: only lines whose key the hub
//! confirmed migrated are touched; a key the audit missed keeps its raw value.

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
        // (The fixture's `rewrite` half is the Python writer's to check since
        // v0.2.97 — serde ignores it here.)
    }

    #[derive(serde::Deserialize)]
    struct AuditPair {
        key: String,
        value: String,
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

            // (Rewrite parity: the sentinel rewrite has ONE implementation
            // since v0.2.97 — `vco_lib.env_template.replace_values_with_sentinel`,
            // which the launcher reaches through the bridge — so its fixture
            // cases are checked by tests/test_env_secrets_migrate_parity.py only.)
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
