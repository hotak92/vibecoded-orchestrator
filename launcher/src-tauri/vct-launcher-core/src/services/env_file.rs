// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! The ONE `KEY=VALUE` env-file reader (v0.2.102 W6-B2).
//!
//! Shared by the launcher (`commands::claude_env` — `.claude/env`, and
//! `module_gui` reading the orchestrator's `infrastructure/.env`) and the hub
//! (the infra watchdog reading the `CODE_EMBED_IN_STACK` marker install writes
//! to `infrastructure/.env`). Format: simple `KEY=VALUE` lines, no quoting;
//! `#` comments and blank lines ignored; the last occurrence wins (shell
//! `source` semantics).

use std::fs;
use std::io::ErrorKind;
use std::path::Path;

/// Parse one line. `Some((key, value))` for a non-comment `KEY=VALUE` line;
/// `None` for blank lines, comments and malformed lines. The value is kept
/// verbatim (no trim).
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

/// The value of `key` in the env file at `path`:
///   * `Ok(Some(value))` — file present, key found (last occurrence wins);
///   * `Ok(None)` — file missing OR key absent;
///   * `Err(_)` — an IO error other than NotFound.
pub fn read_key(path: &Path, key: &str) -> Result<Option<String>, String> {
    let raw = match fs::read_to_string(path) {
        Ok(s) => s,
        Err(e) if e.kind() == ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(format!("read {}: {}", path.display(), e)),
    };
    let mut found: Option<String> = None;
    for line in raw.lines() {
        if let Some((k, v)) = parse_kv_line(line) {
            if k == key {
                found = Some(v.to_string());
            }
        }
    }
    Ok(found)
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
        assert_eq!(read_key(&p, "B").unwrap().as_deref(), Some(" x"));
        assert_eq!(read_key(&p, "C").unwrap(), None);
        assert_eq!(read_key(&dir.path().join("missing"), "A").unwrap(), None);
    }
}
