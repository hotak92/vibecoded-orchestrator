// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! The ONE `KEY=VALUE` env-file reader (v0.2.102 W6-B2; grammar W6-Z1).
//!
//! Shared by the launcher (`commands::claude_env` — `.claude/env`;
//! `commands::module_service` — `ACTIVE_EMBEDDING` from `.claude/env`;
//! `module_gui` reading the orchestrator's `infrastructure/.env`) and the hub
//! (the infra watchdog reading the `CODE_EMBED_IN_STACK` marker install writes
//! to `infrastructure/.env`).
//!
//! READ grammar — MUST MATCH `vco_lib/envfile.py::parse_env_line` (the Python
//! home; this is the cross-language mirror, locked by the shared case table
//! `tests/fixtures/env_line_grammar_parity.json`, walked here by
//! `env_line_grammar_parity_table` and in Python by
//! `tests/test_v02102_env_line_grammar_parity.py`): strip the line; drop an
//! optional `export ` prefix (the form VCO's own `.claude/env` managed block
//! writes); skip blank, `#` and `=`-less lines; split on the FIRST `=`; trim
//! key and value; strip ONE matching pair of single or double quotes — no
//! escape reversal, no variable expansion. Whole-file reads take the LAST
//! assignment (shell `source` semantics: a user's override after the managed
//! block wins).
//!
//! [`parse_kv_line`] is a different thing: the WRITER's key-matching rule
//! (`commands::claude_env::build_new_content`, mirrored by
//! `vco_lib.envfile.upsert_env_key`), which writes and matches the plain
//! `KEY=VALUE` form only. Readers use [`parse_env_line`].

use std::fs;
use std::io::ErrorKind;
use std::path::Path;

/// The WRITER's key-matching rule (see the module doc): `Some((key, value))`
/// for a non-comment `KEY=VALUE` line; `None` for blank lines, comments and
/// malformed lines. The value is kept verbatim (no trim). Not a reader — read
/// values with [`parse_env_line`] / [`read_key`].
pub fn parse_kv_line(line: &str) -> Option<(&str, &str)> {
    let trimmed = line.trim_start();
    if trimmed.is_empty() || trimmed.starts_with('#') {
        return None;
    }
    let (k, v) = line.split_once('=')?;
    let key = k.trim();
    if key.is_empty() {
        return None;
    }
    Some((key, v))
}

/// Strip ONE matching pair of leading/trailing single or double quotes.
/// MUST MATCH `vco_lib/envfile.py::_strip_one_quote_pair` (without its
/// managed-block-only `writer_escapes` arm).
fn strip_one_quote_pair(value: &str) -> &str {
    let b = value.as_bytes();
    if b.len() >= 2 && b[0] == b[b.len() - 1] && (b[0] == b'"' || b[0] == b'\'') {
        &value[1..value.len() - 1]
    } else {
        value
    }
}

/// `(key, value)` of ONE line under the read grammar (module doc), or `None`
/// for a blank line, a `#` comment or a line with no `=`.
/// MUST MATCH `vco_lib/envfile.py::parse_env_line`.
pub fn parse_env_line(line: &str) -> Option<(&str, &str)> {
    let mut s = line.trim();
    if let Some(rest) = s.strip_prefix("export ") {
        s = rest.trim_start();
    }
    if s.is_empty() || s.starts_with('#') {
        return None;
    }
    let (k, v) = s.split_once('=')?;
    Some((k.trim(), strip_one_quote_pair(v.trim())))
}

/// The LAST assignment of `key` in `text` under [`parse_env_line`] — what a
/// shell holds after `source`. MUST MATCH
/// `vco_lib.envfile.env_value(text, key, last_wins=True)`.
pub fn last_value<'a>(text: &'a str, key: &str) -> Option<&'a str> {
    let mut found = None;
    for line in text.lines() {
        if let Some((k, v)) = parse_env_line(line) {
            if k == key {
                found = Some(v);
            }
        }
    }
    found
}

/// The value of `key` in the env file at `path` ([`last_value`]):
///   * `Ok(Some(value))` — file present, key found (last occurrence wins);
///   * `Ok(None)` — file missing OR key absent;
///   * `Err(_)` — an IO error other than NotFound.
pub fn read_key(path: &Path, key: &str) -> Result<Option<String>, String> {
    let raw = match fs::read_to_string(path) {
        Ok(s) => s,
        Err(e) if e.kind() == ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(format!("read {}: {}", path.display(), e)),
    };
    Ok(last_value(&raw, key).map(str::to_string))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn last_occurrence_wins_and_comments_are_skipped() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join(".env");
        fs::write(&p, "# c\nA=1\n\nB = x\nA=2\n").unwrap();
        assert_eq!(read_key(&p, "A").unwrap().as_deref(), Some("2"));
        // The value is trimmed, as in `vco_lib.envfile.parse_env_line`.
        assert_eq!(read_key(&p, "B").unwrap().as_deref(), Some("x"));
        assert_eq!(read_key(&p, "C").unwrap(), None);
        assert_eq!(read_key(&dir.path().join("missing"), "A").unwrap(), None);
    }

    /// A real managed block (the bytes `config_projection._build_managed_block`
    /// writes: `export KEY="value"`) followed by a user override after the END
    /// marker. Before W6-Z1 the module_service reader never stripped `export `
    /// and so never matched the block at all; and a first-match reader would
    /// ignore the override.
    #[test]
    fn reads_the_managed_block_form_and_the_override_after_it() {
        let table = table();
        let case = table["files"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["name"] == "override_after_managed_block")
            .expect("override case in the table");
        let text = case["text"].as_str().unwrap();
        assert!(text.contains("# vco-managed-begin"), "real managed block");
        assert!(text.contains("export ACTIVE_EMBEDDING=\"qwen3\""));
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("env");
        fs::write(&p, text).unwrap();
        assert_eq!(read_key(&p, "ACTIVE_EMBEDDING").unwrap().as_deref(), Some("arctic"));
        assert_eq!(
            read_key(&p, "KG_COLLECTION").unwrap().as_deref(),
            Some("Alpha_KnowledgeGraph")
        );
    }

    fn table() -> serde_json::Value {
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../tests/fixtures/env_line_grammar_parity.json");
        let text = fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
        serde_json::from_str(&text).expect("parity table parses")
    }

    /// The shared case table — the Python side walks the same file
    /// (`tests/test_v02102_env_line_grammar_parity.py`).
    #[test]
    fn env_line_grammar_parity_table() {
        let table = table();
        for case in table["lines"].as_array().unwrap() {
            let line = case["line"].as_str().unwrap();
            let got = parse_env_line(line).map(|(k, v)| vec![k.to_string(), v.to_string()]);
            let want = case["expect"].as_array().map(|a| {
                a.iter().map(|x| x.as_str().unwrap().to_string()).collect::<Vec<_>>()
            });
            assert_eq!(got, want, "line {line:?}");
        }
        for case in table["files"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let text = case["text"].as_str().unwrap();
            let key = case["key"].as_str().unwrap();
            let want = case["expect_last"].as_str();
            assert_eq!(last_value(text, key), want, "files case {name}");
            let dir = tempfile::tempdir().unwrap();
            let p = dir.path().join("env");
            fs::write(&p, text).unwrap();
            assert_eq!(read_key(&p, key).unwrap().as_deref(), want, "read_key {name}");
        }
    }
}
