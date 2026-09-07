// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Safe read-modify-write primitives for **user-owned JSON files**.
//!
//! Extracted from `mcp_registration.rs` (which owned the only copy until a
//! second call-site appeared: the `~/.claude/settings.json` Artifact-tool
//! toggle in `commands::artifact_tool`). Per the repo's modularity rule the
//! primitives were lifted into one home and the original call-site migrated,
//! rather than a second implementation being written alongside.
//!
//! The three concerns every such edit shares:
//!
//!   1. **Concurrent writers** — a sidecar `<file>.lock` advisory lock.
//!   2. **Torn files on crash** — write `<file>.tmp`, then `rename`.
//!   3. **Recoverability** — copy the pre-edit bytes aside before the first
//!      overwrite ([`BackupPolicy`]).
//!
//! What these primitives deliberately do NOT do is decide *what* to mutate.
//! Each caller owns exactly the keys it manages and leaves the rest of the
//! document — including keys it has never heard of, and comment-style keys
//! like `"_comment"` that no schema knows about — untouched.
//!
//! ## Key ORDER is part of "untouched" (v0.2.92, WP-19)
//!
//! [`atomic_write_json`] re-serialises the whole document, so the order it
//! emits is whatever `serde_json` gives it. Every launcher crate enables
//! `serde_json`'s `preserve_order` feature (declared once in
//! `launcher/src-tauri/Cargo.toml` `[workspace.dependencies]`), which backs
//! `Value::Object` with an `IndexMap` instead of a `BTreeMap` — so a
//! read-modify-write round trip returns the user's keys in the user's order
//! instead of alphabetising them.
//!
//! That puts one obligation on every CALLER: use
//! `serde_json::Map::shift_remove`, never `Map::remove`. Under
//! `preserve_order` `remove` is an alias for `swap_remove`, which fills the
//! vacated slot with the map's LAST key — so a single key deletion silently
//! relocates an unrelated one, which is precisely the reordering the feature
//! exists to prevent. `shift_remove` is also the only one of the two that
//! matches the pre-v0.2.92 `BTreeMap` behaviour, so it is the choice that
//! changes nothing beyond the intended fix.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

/// How much history to keep before overwriting a user-owned file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackupPolicy {
    /// Refresh `<file>.<ext>` on every write. The pre-write bytes are always
    /// one `cp` away, but yesterday's state is gone. Used by the
    /// `~/.claude.json` MCP registration writer, which rewrites often.
    EveryWrite { ext: &'static str },
    /// Write `<file>.<ext>` **only if it does not already exist**, so the
    /// bytes preserved are the ones from before this subsystem ever touched
    /// the file. Used for `~/.claude/settings.json`: the user's hand-authored
    /// original is the state worth being able to go back to, and an
    /// every-write policy would overwrite it with a VCO-modified copy on the
    /// second toggle.
    Once { ext: &'static str },
}

/// Compose a sibling path by REPLACING the file extension with
/// `<old-ext>.<suffix>` (so `settings.json` → `settings.json.bak`, and an
/// extension-less `foo` → `foo.bak`).
///
/// Kept as one function because the lock / temp / backup paths must agree on
/// the composition — a mismatch between them is how a lock gets orphaned
/// under a name nothing cleans up.
pub fn sibling_path(target: &Path, suffix: &str) -> PathBuf {
    let mut p = target.to_path_buf();
    p.set_extension(match target.extension().and_then(|s| s.to_str()) {
        Some(ext) => format!("{}.{}", ext, suffix),
        None => suffix.to_string(),
    });
    p
}

fn lock_path(target: &Path) -> PathBuf {
    sibling_path(target, "lock")
}

/// Acquire a simple advisory file lock. Blocks up to `max_wait_ms`.
/// Lock file content is the current PID.
pub fn acquire_lock(target: &Path, max_wait_ms: u64) -> Result<LockGuard, String> {
    let lock = lock_path(target);
    if let Some(parent) = lock.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("create parent {}: {}", parent.display(), e))?;
    }
    let start = std::time::Instant::now();
    loop {
        match fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&lock)
        {
            Ok(mut f) => {
                let _ = writeln!(f, "{}", std::process::id());
                return Ok(LockGuard {
                    path: lock,
                    _file: f,
                });
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                if start.elapsed().as_millis() as u64 > max_wait_ms {
                    // Stale lock? If the PID inside is not alive, remove it.
                    if let Ok(content) = fs::read_to_string(&lock) {
                        if let Ok(pid) = content.trim().parse::<u32>() {
                            if !pid_alive(pid) {
                                let _ = fs::remove_file(&lock);
                                continue;
                            }
                        }
                    }
                    return Err(format!(
                        "timed out waiting for lock {} (held by another process)",
                        lock.display()
                    ));
                }
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(e) => return Err(format!("acquire lock {}: {}", lock.display(), e)),
        }
    }
}

fn pid_alive(_pid: u32) -> bool {
    // Conservative: assume alive. The cost of a false "alive" is a slightly
    // longer wait; the cost of a false "dead" is corrupting someone's JSON.
    true
}

pub struct LockGuard {
    path: PathBuf,
    _file: fs::File,
}

impl Drop for LockGuard {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

/// Read a JSON document, treating "absent" and "empty" as `{}`.
///
/// A file that EXISTS but does not parse is an error, never an implicit `{}`:
/// the caller must decide whether to refuse (user-owned file) or to replace
/// (file this subsystem authors). Silently substituting `{}` here would make
/// "clobber the user's broken config" the default for every future caller.
pub fn read_json_or_empty(path: &Path) -> Result<serde_json::Value, String> {
    if !path.exists() {
        return Ok(serde_json::json!({}));
    }
    let raw = fs::read_to_string(path).map_err(|e| format!("read {}: {}", path.display(), e))?;
    if raw.trim().is_empty() {
        return Ok(serde_json::json!({}));
    }
    serde_json::from_str(&raw).map_err(|e| format!("parse {}: {}", path.display(), e))
}

/// Serialize `value` and replace `path` atomically, honouring `policy` for
/// the pre-write sidecar copy.
pub fn atomic_write_json(
    path: &Path,
    value: &serde_json::Value,
    policy: BackupPolicy,
) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("create parent {}: {}", parent.display(), e))?;
    }
    write_backup(path, policy)?;
    let tmp = sibling_path(path, "tmp");
    let body = serde_json::to_string_pretty(value).map_err(|e| format!("serialize: {}", e))?;
    fs::write(&tmp, &body).map_err(|e| format!("write tmp {}: {}", tmp.display(), e))?;
    fs::rename(&tmp, path)
        .map_err(|e| format!("rename {} -> {}: {}", tmp.display(), path.display(), e))?;
    Ok(())
}

/// Back up the existing file before overwriting so a bad write is recoverable.
fn write_backup(path: &Path, policy: BackupPolicy) -> Result<(), String> {
    if !path.exists() {
        return Ok(());
    }
    let bak = match policy {
        BackupPolicy::EveryWrite { ext } => sibling_path(path, ext),
        BackupPolicy::Once { ext } => {
            let bak = sibling_path(path, ext);
            if bak.exists() {
                // The pre-VCO original is already preserved; do not overwrite
                // it with a copy we have since modified.
                return Ok(());
            }
            bak
        }
    };
    fs::copy(path, &bak).map_err(|e| format!("backup {}: {}", bak.display(), e))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_dir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!(
            "vct-jsonfile-{}-{}",
            tag,
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn sibling_path_appends_to_the_existing_extension() {
        assert_eq!(
            sibling_path(Path::new("/tmp/settings.json"), "bak"),
            PathBuf::from("/tmp/settings.json.bak")
        );
        assert_eq!(
            sibling_path(Path::new("/tmp/noext"), "lock"),
            PathBuf::from("/tmp/noext.lock")
        );
    }

    #[test]
    fn read_json_or_empty_treats_absent_and_blank_as_empty_object() {
        let dir = tmp_dir("read");
        let missing = dir.join("nope.json");
        assert_eq!(read_json_or_empty(&missing).unwrap(), serde_json::json!({}));

        let blank = dir.join("blank.json");
        fs::write(&blank, "   \n").unwrap();
        assert_eq!(read_json_or_empty(&blank).unwrap(), serde_json::json!({}));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn read_json_or_empty_errors_on_a_corrupt_file_rather_than_pretending_it_is_empty() {
        let dir = tmp_dir("corrupt");
        let path = dir.join("bad.json");
        fs::write(&path, "{ not json").unwrap();
        let err = read_json_or_empty(&path).expect_err("corrupt file must be an error");
        assert!(err.contains("parse"), "unexpected error text: {}", err);
        // And the bytes are still there — the reader never rewrites.
        assert_eq!(fs::read_to_string(&path).unwrap(), "{ not json");
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn backup_policy_once_preserves_the_first_seen_bytes_across_repeat_writes() {
        let dir = tmp_dir("once");
        let path = dir.join("settings.json");
        fs::write(&path, r#"{"original":true}"#).unwrap();
        let policy = BackupPolicy::Once { ext: "vco-backup" };

        atomic_write_json(&path, &serde_json::json!({"gen": 1}), policy).unwrap();
        atomic_write_json(&path, &serde_json::json!({"gen": 2}), policy).unwrap();

        let bak = fs::read_to_string(sibling_path(&path, "vco-backup")).unwrap();
        let bak: serde_json::Value = serde_json::from_str(&bak).unwrap();
        assert_eq!(
            bak,
            serde_json::json!({"original": true}),
            "Once must keep the pre-VCO original, not the previous VCO write"
        );
        let now: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(now, serde_json::json!({"gen": 2}));

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn backup_policy_every_write_tracks_the_latest_pre_write_bytes() {
        let dir = tmp_dir("every");
        let path = dir.join("claude.json");
        fs::write(&path, r#"{"original":true}"#).unwrap();
        let policy = BackupPolicy::EveryWrite { ext: "bak" };

        atomic_write_json(&path, &serde_json::json!({"gen": 1}), policy).unwrap();
        atomic_write_json(&path, &serde_json::json!({"gen": 2}), policy).unwrap();

        let bak: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(sibling_path(&path, "bak")).unwrap()).unwrap();
        assert_eq!(
            bak,
            serde_json::json!({"gen": 1}),
            "EveryWrite keeps the bytes from immediately before the last write"
        );

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn atomic_write_leaves_no_temp_file_behind() {
        let dir = tmp_dir("tmpclean");
        let path = dir.join("settings.json");
        atomic_write_json(
            &path,
            &serde_json::json!({"a": 1}),
            BackupPolicy::Once { ext: "vco-backup" },
        )
        .unwrap();
        assert!(
            !sibling_path(&path, "tmp").exists(),
            "temp sibling must be renamed away"
        );
        assert!(path.exists());
        fs::remove_dir_all(&dir).ok();
    }

    /// The writer-family round trip: read a deliberately NON-alphabetical
    /// document, change one value, write it back, and get the same key order
    /// out. This is the property `preserve_order` buys, asserted on the ONE
    /// primitive every user-owned-JSON caller goes through.
    #[test]
    fn a_read_modify_write_round_trip_preserves_the_documents_key_order() {
        let dir = tmp_dir("order");
        let path = dir.join("settings.json");
        // Neither alphabetical nor reverse-alphabetical, so a sort in either
        // direction fails the assertion.
        let original = r#"{"zebra":1,"apple":2,"mango":3,"banana":4}"#;
        fs::write(&path, original).unwrap();

        let mut doc = read_json_or_empty(&path).unwrap();
        doc.as_object_mut()
            .unwrap()
            .insert("mango".to_string(), serde_json::json!(99));
        atomic_write_json(&path, &doc, BackupPolicy::Once { ext: "vco-backup" }).unwrap();

        let back = read_json_or_empty(&path).unwrap();
        assert_eq!(
            back.as_object().unwrap().keys().collect::<Vec<_>>(),
            vec!["zebra", "apple", "mango", "banana"],
            "the writer must not sort the user's keys"
        );
        assert_eq!(back["mango"], serde_json::json!(99), "and the edit landed");
        assert_eq!(back["zebra"], serde_json::json!(1), "untouched keys intact");

        fs::remove_dir_all(&dir).ok();
    }

    /// Overwriting an EXISTING key keeps that key where it was — `IndexMap`
    /// insert-over-existing updates in place. Pinned because the alternative
    /// (append-on-overwrite) would drift a hot key to the bottom of a user's
    /// file one write at a time, which is the same damage in slow motion.
    #[test]
    fn overwriting_an_existing_key_updates_it_in_place_rather_than_appending() {
        let dir = tmp_dir("inplace");
        let path = dir.join("settings.json");
        fs::write(&path, r#"{"first":1,"target":2,"last":3}"#).unwrap();

        let mut doc = read_json_or_empty(&path).unwrap();
        doc.as_object_mut()
            .unwrap()
            .insert("target".to_string(), serde_json::json!("changed"));
        atomic_write_json(&path, &doc, BackupPolicy::Once { ext: "vco-backup" }).unwrap();

        let back = read_json_or_empty(&path).unwrap();
        assert_eq!(
            back.as_object().unwrap().keys().collect::<Vec<_>>(),
            vec!["first", "target", "last"]
        );
        fs::remove_dir_all(&dir).ok();
    }

    /// `shift_remove` closes the gap; `remove`/`swap_remove` would pull the
    /// tail forward. Asserted directly on `serde_json::Map` so the contract
    /// the module docs place on every caller has a test of its own, not only
    /// the indirect coverage from each call-site's own suite.
    #[test]
    fn shift_remove_closes_the_gap_where_swap_remove_would_pull_the_tail_forward() {
        let mut map: serde_json::Map<String, serde_json::Value> =
            serde_json::from_str(r#"{"a":1,"b":2,"c":3,"d":4}"#).unwrap();

        let mut swapped = map.clone();
        swapped.swap_remove("b");
        assert_eq!(
            swapped.keys().collect::<Vec<_>>(),
            vec!["a", "d", "c"],
            "swap_remove (what bare `remove` resolves to) relocates the last key"
        );

        map.shift_remove("b");
        assert_eq!(
            map.keys().collect::<Vec<_>>(),
            vec!["a", "c", "d"],
            "shift_remove leaves every survivor where it was"
        );
    }

    #[test]
    fn lock_is_released_when_the_guard_drops() {
        let dir = tmp_dir("lock");
        let path = dir.join("settings.json");
        {
            let _g = acquire_lock(&path, 500).expect("first lock");
            assert!(sibling_path(&path, "lock").exists());
        }
        assert!(
            !sibling_path(&path, "lock").exists(),
            "guard drop must remove the lock file"
        );
        // Re-acquirable afterwards.
        let _g = acquire_lock(&path, 500).expect("second lock");
        fs::remove_dir_all(&dir).ok();
    }
}
