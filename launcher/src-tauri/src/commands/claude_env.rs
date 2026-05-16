//! Per-project `.claude/env` key/value reader+writer (PR-6, v0.2.11).
//!
//! Backs the HooksTab toggle for `VCO_LEAN_CTX_DEFAULT`. The PR-1 hook
//! `templates/hooks/lean-ctx-rewrite.{sh,ps1}` consults
//! `<project>/.claude/env` for that key (line-based parsing, default = "on"
//! when absent). This module exposes the minimum surface the GUI needs to
//! flip that knob without re-deriving the wider env-pair builder used by
//! `installer::write_project_env_files`.
//!
//! File format:
//!   * Simple `KEY=VALUE` lines (no quoting, no escaping).
//!   * `#`-prefixed comments and blank lines preserved verbatim.
//!   * Trailing newline preserved (or added if file existed without one).
//!   * UTF-8 only (which the rest of the launcher already assumes).
//!
//! Atomicity: writes go to `<file>.tmp` in the same directory and are
//! `rename`'d into place, so a crash mid-write never leaves a half-written
//! `.claude/env`. Idempotent: writing the same value twice is a no-op (no
//! file mtime churn). Soft-fail on read when the file is missing (`Ok(None)`),
//! since "absent" is a valid GUI state distinct from "set to a value".
//!
//! Cross-OS: paths are joined via `PathBuf` (no string concat). The file
//! always lives at `<project_folder>/.claude/env` on every supported OS.

use std::fs;
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};

use tauri::{command, State};

use crate::db::Db;

/// Locate `<project_folder>/.claude/env` for a given project id.
///
/// Returns the absolute path even if the file does not yet exist on disk —
/// callers decide whether absence is an error (it's not, for our use).
fn resolve_env_file(db: &Db, project_id: &str) -> Result<PathBuf, String> {
    let row = db
        .get_project(project_id)
        .map_err(|e| format!("get project {}: {}", project_id, e))?
        .ok_or_else(|| format!("project not found: {}", project_id))?;
    let mut p = PathBuf::from(row.folder_path);
    p.push(".claude");
    p.push("env");
    Ok(p)
}

/// Parse a single line of `.claude/env`. Returns `Some((key, value))` if the
/// line is a non-comment `KEY=VALUE` form; `None` otherwise (blank lines,
/// comments, malformed lines).
fn parse_kv_line(line: &str) -> Option<(&str, &str)> {
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

/// Read the value of `key` from `path`, if the file exists and the key is
/// present. Returns:
///   * `Ok(Some(value))` — file present, key found (last occurrence wins,
///     mirroring shell `source` semantics).
///   * `Ok(None)` — file missing OR key absent.
///   * `Err(_)` — IO error other than NotFound (permission denied, etc.).
fn read_key(path: &Path, key: &str) -> Result<Option<String>, String> {
    let raw = match fs::read_to_string(path) {
        Ok(s) => s,
        Err(e) if e.kind() == ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(format!("read {}: {}", path.display(), e)),
    };
    let mut found: Option<String> = None;
    for line in raw.lines() {
        if let Some((k, v)) = parse_kv_line(line) {
            if k == key {
                // Preserve the value verbatim (no trim) so values with
                // intentional trailing spaces — unusual but legal — survive
                // a read/write round-trip. Hook-side `[ "$X" = "off" ]`
                // will fail for `off ` anyway, which is the user's call.
                found = Some(v.to_string());
            }
        }
    }
    Ok(found)
}

/// Atomically write `path` so it contains exactly one `KEY=VALUE` line for
/// `key`. If `value` is `Some`, the line is upserted (replacing any prior
/// occurrence of the same key, OR appended at end-of-file if absent). If
/// `value` is `None`, every occurrence of the key is removed; non-key lines
/// (blank, comment, unrelated `K=V`) are preserved verbatim, including the
/// trailing newline.
///
/// Idempotent: if the on-disk content already matches the desired output,
/// the file is not rewritten (no mtime churn). Creates the parent
/// `.claude/` directory on the write path if missing.
fn write_key(path: &Path, key: &str, value: Option<&str>) -> Result<(), String> {
    let original = match fs::read_to_string(path) {
        Ok(s) => Some(s),
        Err(e) if e.kind() == ErrorKind::NotFound => None,
        Err(e) => return Err(format!("read {}: {}", path.display(), e)),
    };

    let new_content = build_new_content(original.as_deref(), key, value);

    // Idempotent: skip the write entirely when nothing would change.
    if let Some(existing) = original.as_deref() {
        if existing == new_content {
            return Ok(());
        }
    } else if new_content.is_empty() {
        // Removing a key from an absent file would create an empty file —
        // pointless. Treat as a no-op.
        return Ok(());
    }

    // Ensure parent directory exists (.claude/ may not yet have been
    // created for a freshly-registered project that never had a bundle
    // install).
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("create dir {}: {}", parent.display(), e))?;
    }

    // Atomic replace via `<file>.tmp` + rename. Sibling tmp file keeps the
    // rename atomic on the same filesystem.
    let tmp = tmp_sibling(path);
    {
        let mut f = fs::File::create(&tmp)
            .map_err(|e| format!("create {}: {}", tmp.display(), e))?;
        f.write_all(new_content.as_bytes())
            .map_err(|e| format!("write {}: {}", tmp.display(), e))?;
        f.sync_all()
            .map_err(|e| format!("sync {}: {}", tmp.display(), e))?;
    }
    fs::rename(&tmp, path)
        .map_err(|e| format!("rename {} -> {}: {}", tmp.display(), path.display(), e))?;
    Ok(())
}

/// Build a sibling temp path for atomic rename. `<file>.tmp` lives in the
/// same directory so the final `rename` stays on one filesystem.
fn tmp_sibling(path: &Path) -> PathBuf {
    let mut tmp = path.to_path_buf();
    let file_name = path
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "env".to_string());
    tmp.set_file_name(format!("{}.tmp", file_name));
    tmp
}

/// Pure transformation: given the original file contents (or `None` for an
/// absent file), produce the new contents after upserting/removing `key`.
/// Extracted so unit tests can exercise the shape without touching disk.
fn build_new_content(original: Option<&str>, key: &str, value: Option<&str>) -> String {
    let original_text = original.unwrap_or("");

    // Iterate logical lines. `lines()` (unlike `split('\n')`) does NOT
    // synthesise a trailing empty string after a final `\n`, so the
    // "input ended with newline" case doesn't accidentally seed a blank
    // line into the output. Genuine internal blank lines (between two
    // newlines mid-file) are preserved by `lines()` as empty strings,
    // exactly what we want.
    let mut out_lines: Vec<String> = Vec::new();
    let mut replaced = false;

    for line in original_text.lines() {
        match parse_kv_line(line) {
            Some((k, _)) if k == key => match value {
                Some(v) => {
                    if !replaced {
                        out_lines.push(format!("{}={}", key, v));
                        replaced = true;
                    }
                    // Drop additional duplicate occurrences (collapses
                    // accidental dupes into a single line).
                }
                None => {
                    // Removing — drop the line entirely.
                }
            },
            _ => out_lines.push(line.to_string()),
        }
    }

    if value.is_some() && !replaced {
        out_lines.push(format!("{}={}", key, value.unwrap()));
    }

    let mut result = out_lines.join("\n");
    // Always emit a trailing newline when content is non-empty, to match
    // POSIX text-file convention. Empty result (file was empty and we
    // removed the only key, or no content at all) stays empty.
    if !result.is_empty() {
        result.push('\n');
    }
    result
}

// ─── Tauri commands ──────────────────────────────────────────────────────

/// Read a single key from `<project>/.claude/env`.
///
/// Returns:
///   * `Ok(Some(value))` — file exists and key is present.
///   * `Ok(None)` — file absent OR key absent. Both are valid GUI states
///     ("default behaviour applies"); the toast layer treats neither as
///     an error.
///   * `Err(message)` — project lookup failed OR the env file is unreadable
///     for a reason other than NotFound (e.g. permission denied).
#[command]
pub async fn get_claude_env_value(
    project_id: String,
    key: String,
    db: State<'_, Db>,
) -> Result<Option<String>, String> {
    let path = resolve_env_file(&db, &project_id)?;
    read_key(&path, &key)
}

/// Upsert / remove a single key in `<project>/.claude/env`.
///
/// `value`:
///   * `Some(v)` — replace the existing line OR append at end-of-file.
///   * `None`    — remove every occurrence of the key from the file.
///
/// Other lines (comments, blanks, unrelated keys) are preserved verbatim.
/// Atomic write via `<file>.tmp` + `rename`. Idempotent.
#[command]
pub async fn set_claude_env_value(
    project_id: String,
    key: String,
    value: Option<String>,
    db: State<'_, Db>,
) -> Result<(), String> {
    let path = resolve_env_file(&db, &project_id)?;
    write_key(&path, &key, value.as_deref())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    /// Convenience: write `content` to `<tmp>/.claude/env` and return the
    /// path. The parent dir is created.
    fn seed_env_file(dir: &Path, content: &str) -> PathBuf {
        let claude_dir = dir.join(".claude");
        fs::create_dir_all(&claude_dir).unwrap();
        let path = claude_dir.join("env");
        fs::write(&path, content).unwrap();
        path
    }

    // ─── read_key ────────────────────────────────────────────────────

    #[test]
    fn read_key_missing_file_returns_none() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join(".claude").join("env");
        // File does NOT exist.
        let v = read_key(&path, "VCO_LEAN_CTX_DEFAULT").unwrap();
        assert_eq!(v, None);
    }

    #[test]
    fn read_key_present_with_key_returns_value() {
        let tmp = TempDir::new().unwrap();
        let path = seed_env_file(
            tmp.path(),
            "FOO=bar\nVCO_LEAN_CTX_DEFAULT=off\nBAZ=qux\n",
        );
        let v = read_key(&path, "VCO_LEAN_CTX_DEFAULT").unwrap();
        assert_eq!(v.as_deref(), Some("off"));
    }

    #[test]
    fn read_key_present_without_key_returns_none() {
        let tmp = TempDir::new().unwrap();
        let path = seed_env_file(tmp.path(), "FOO=bar\nBAZ=qux\n");
        let v = read_key(&path, "VCO_LEAN_CTX_DEFAULT").unwrap();
        assert_eq!(v, None);
    }

    #[test]
    fn read_key_skips_comments_and_blanks() {
        let tmp = TempDir::new().unwrap();
        let path = seed_env_file(
            tmp.path(),
            "# this is a comment\n\n# VCO_LEAN_CTX_DEFAULT=on (in a comment)\nVCO_LEAN_CTX_DEFAULT=off\n",
        );
        let v = read_key(&path, "VCO_LEAN_CTX_DEFAULT").unwrap();
        assert_eq!(v.as_deref(), Some("off"));
    }

    #[test]
    fn read_key_last_occurrence_wins() {
        // Mirrors POSIX `source` semantics — last `KEY=VALUE` line wins.
        let tmp = TempDir::new().unwrap();
        let path = seed_env_file(
            tmp.path(),
            "VCO_LEAN_CTX_DEFAULT=on\nFOO=bar\nVCO_LEAN_CTX_DEFAULT=off\n",
        );
        let v = read_key(&path, "VCO_LEAN_CTX_DEFAULT").unwrap();
        assert_eq!(v.as_deref(), Some("off"));
    }

    // ─── write_key (build_new_content) ───────────────────────────────

    #[test]
    fn build_appends_when_key_missing_and_file_present() {
        let original = "FOO=bar\nBAZ=qux\n";
        let out = build_new_content(Some(original), "VCO_LEAN_CTX_DEFAULT", Some("off"));
        assert_eq!(out, "FOO=bar\nBAZ=qux\nVCO_LEAN_CTX_DEFAULT=off\n");
    }

    #[test]
    fn build_replaces_existing_key_in_place() {
        let original = "FOO=bar\nVCO_LEAN_CTX_DEFAULT=on\nBAZ=qux\n";
        let out = build_new_content(Some(original), "VCO_LEAN_CTX_DEFAULT", Some("off"));
        assert_eq!(out, "FOO=bar\nVCO_LEAN_CTX_DEFAULT=off\nBAZ=qux\n");
    }

    #[test]
    fn build_creates_file_when_absent() {
        let out = build_new_content(None, "VCO_LEAN_CTX_DEFAULT", Some("off"));
        assert_eq!(out, "VCO_LEAN_CTX_DEFAULT=off\n");
    }

    #[test]
    fn build_removes_key_when_value_is_none() {
        let original = "FOO=bar\nVCO_LEAN_CTX_DEFAULT=off\nBAZ=qux\n";
        let out = build_new_content(Some(original), "VCO_LEAN_CTX_DEFAULT", None);
        assert_eq!(out, "FOO=bar\nBAZ=qux\n");
    }

    #[test]
    fn build_preserves_comments_and_blanks() {
        let original = "# top comment\n\nFOO=bar\n# inline comment\nBAZ=qux\n";
        let out = build_new_content(Some(original), "VCO_LEAN_CTX_DEFAULT", Some("on"));
        assert_eq!(
            out,
            "# top comment\n\nFOO=bar\n# inline comment\nBAZ=qux\nVCO_LEAN_CTX_DEFAULT=on\n"
        );
    }

    #[test]
    fn build_collapses_duplicate_keys_into_single_line() {
        let original = "VCO_LEAN_CTX_DEFAULT=on\nFOO=bar\nVCO_LEAN_CTX_DEFAULT=off\n";
        let out = build_new_content(Some(original), "VCO_LEAN_CTX_DEFAULT", Some("off"));
        // First occurrence is replaced with the new value, subsequent
        // duplicates are dropped — leaves a clean single-line state.
        assert_eq!(out, "VCO_LEAN_CTX_DEFAULT=off\nFOO=bar\n");
    }

    #[test]
    fn build_remove_when_file_absent_yields_empty() {
        let out = build_new_content(None, "VCO_LEAN_CTX_DEFAULT", None);
        assert_eq!(out, "");
    }

    // ─── write_key end-to-end (atomic + idempotent) ──────────────────

    #[test]
    fn write_key_creates_file_and_parent_dir_when_absent() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join(".claude").join("env");
        // No .claude/ yet.
        write_key(&path, "VCO_LEAN_CTX_DEFAULT", Some("off")).unwrap();
        let content = fs::read_to_string(&path).unwrap();
        assert_eq!(content, "VCO_LEAN_CTX_DEFAULT=off\n");
    }

    #[test]
    fn write_key_idempotent_skip_when_unchanged() {
        let tmp = TempDir::new().unwrap();
        let path = seed_env_file(tmp.path(), "VCO_LEAN_CTX_DEFAULT=off\n");
        let mtime_before = fs::metadata(&path).unwrap().modified().unwrap();
        // Sleep tiny amount so a rewrite would visibly bump mtime on
        // filesystems with second-resolution mtime (ext4 inline mtime is
        // ms/ns; this is paranoia for portability).
        std::thread::sleep(std::time::Duration::from_millis(20));
        write_key(&path, "VCO_LEAN_CTX_DEFAULT", Some("off")).unwrap();
        let mtime_after = fs::metadata(&path).unwrap().modified().unwrap();
        assert_eq!(
            mtime_before, mtime_after,
            "idempotent write should leave file untouched"
        );
    }

    #[test]
    fn write_key_remove_then_read_returns_none() {
        let tmp = TempDir::new().unwrap();
        let path = seed_env_file(
            tmp.path(),
            "FOO=bar\nVCO_LEAN_CTX_DEFAULT=off\nBAZ=qux\n",
        );
        write_key(&path, "VCO_LEAN_CTX_DEFAULT", None).unwrap();
        let v = read_key(&path, "VCO_LEAN_CTX_DEFAULT").unwrap();
        assert_eq!(v, None);
        let content = fs::read_to_string(&path).unwrap();
        assert_eq!(content, "FOO=bar\nBAZ=qux\n");
    }

    #[test]
    fn write_key_does_not_leave_tmp_sibling_on_success() {
        let tmp = TempDir::new().unwrap();
        let path = seed_env_file(tmp.path(), "FOO=bar\n");
        write_key(&path, "VCO_LEAN_CTX_DEFAULT", Some("on")).unwrap();
        let tmp_path = path.with_file_name("env.tmp");
        assert!(
            !tmp_path.exists(),
            "atomic-rename should remove the .tmp sibling"
        );
    }

    #[test]
    fn write_key_round_trip_via_read_key() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join(".claude").join("env");
        write_key(&path, "VCO_LEAN_CTX_DEFAULT", Some("on")).unwrap();
        assert_eq!(
            read_key(&path, "VCO_LEAN_CTX_DEFAULT").unwrap().as_deref(),
            Some("on")
        );
        write_key(&path, "VCO_LEAN_CTX_DEFAULT", Some("off")).unwrap();
        assert_eq!(
            read_key(&path, "VCO_LEAN_CTX_DEFAULT").unwrap().as_deref(),
            Some("off")
        );
        write_key(&path, "VCO_LEAN_CTX_DEFAULT", None).unwrap();
        assert_eq!(read_key(&path, "VCO_LEAN_CTX_DEFAULT").unwrap(), None);
    }

    // ─── Project-id resolution ───────────────────────────────────────

    #[test]
    fn resolve_env_file_returns_error_for_missing_project() {
        let db = Db::open_in_memory().unwrap();
        let err =
            resolve_env_file(&db, "00000000-0000-0000-0000-000000000abc").unwrap_err();
        assert!(
            err.contains("project not found"),
            "expected 'project not found' in error, got: {}",
            err
        );
    }

    #[test]
    fn resolve_env_file_joins_claude_env_to_project_folder() {
        use crate::db::models::ProjectHost;
        let db = Db::open_in_memory().unwrap();
        let tmp = TempDir::new().unwrap();
        let folder = tmp.path().to_string_lossy().to_string();
        db.insert_project(
            "11111111-1111-1111-1111-111111111111",
            "Acme",
            &folder,
            ProjectHost::Base,
            "acme",
        )
        .unwrap();
        let p = resolve_env_file(&db, "11111111-1111-1111-1111-111111111111").unwrap();
        assert_eq!(p, tmp.path().join(".claude").join("env"));
    }
}
