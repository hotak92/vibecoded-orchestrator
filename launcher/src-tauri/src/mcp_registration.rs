//! Safe JSON editor for `~/.claude.json` and per-project `.mcp.json`.
//!
//! Concerns:
//!   1. Concurrent writes — lock via a sidecar `.lock` file.
//!   2. Data loss on crash — always write to `<file>.tmp` then rename.
//!   3. Schema preservation — read existing JSON, mutate `mcpServers.<name>`
//!      only, write back with the same indentation when possible.
//!
//! This module is NOT a generic JSON editor. It intentionally operates only
//! on the `mcpServers` object, leaving everything else in the file untouched.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

pub fn user_claude_json() -> PathBuf {
    directories::UserDirs::new()
        .map(|d| d.home_dir().join(".claude.json"))
        .unwrap_or_else(|| PathBuf::from(".claude.json"))
}

/// Resolve the project-scoped `.mcp.json` path for a given project folder.
///
/// Currently unused: the only caller was the archived `commands::mcp_reg`
/// Tauri wrapper. Restore from launch-assets/launcher-archived-rust/mcp_reg.rs
/// if/when a direct-from-FE registration path is wired up.
#[allow(dead_code)]
pub fn project_mcp_json(project_folder: &Path) -> PathBuf {
    project_folder.join(".mcp.json")
}

fn lock_path(target: &Path) -> PathBuf {
    let mut p = target.to_path_buf();
    p.set_extension(match target.extension().and_then(|s| s.to_str()) {
        Some(ext) => format!("{}.lock", ext),
        None => "lock".to_string(),
    });
    p
}

/// Acquire a simple advisory file lock. Blocks up to `max_wait_ms`.
/// Lock file content is the current PID.
fn acquire_lock(target: &Path, max_wait_ms: u64) -> Result<LockGuard, String> {
    let lock = lock_path(target);
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

struct LockGuard {
    path: PathBuf,
    _file: fs::File,
}

impl Drop for LockGuard {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

/// Register an MCP server in a target JSON file.
///
/// `entry` is the full JSON object that will be stored at
/// `mcpServers[mcp_name]`. Existing entry is replaced.
///
/// Creates the file + the `mcpServers` object if absent.
pub fn register_mcp(
    target: &Path,
    mcp_name: &str,
    entry: &serde_json::Value,
) -> Result<(), String> {
    let _lock = acquire_lock(target, 5000)?;

    let mut root = read_json_or_empty(target)?;
    let mcp_servers = root
        .as_object_mut()
        .ok_or("target file root is not a JSON object")?;
    if !mcp_servers.contains_key("mcpServers") {
        mcp_servers.insert("mcpServers".into(), serde_json::json!({}));
    }
    let servers = mcp_servers
        .get_mut("mcpServers")
        .and_then(|v| v.as_object_mut())
        .ok_or("mcpServers is not an object")?;

    servers.insert(mcp_name.to_string(), entry.clone());
    atomic_write_json(target, &root)?;
    Ok(())
}

pub fn deregister_mcp(target: &Path, mcp_name: &str) -> Result<(), String> {
    if !target.exists() {
        return Ok(());
    }
    let _lock = acquire_lock(target, 5000)?;
    let mut root = read_json_or_empty(target)?;
    if let Some(obj) = root.as_object_mut() {
        if let Some(servers) = obj.get_mut("mcpServers").and_then(|v| v.as_object_mut()) {
            servers.remove(mcp_name);
        }
    }
    atomic_write_json(target, &root)?;
    Ok(())
}

fn read_json_or_empty(path: &Path) -> Result<serde_json::Value, String> {
    if !path.exists() {
        return Ok(serde_json::json!({}));
    }
    let raw = fs::read_to_string(path).map_err(|e| format!("read {}: {}", path.display(), e))?;
    if raw.trim().is_empty() {
        return Ok(serde_json::json!({}));
    }
    serde_json::from_str(&raw).map_err(|e| format!("parse {}: {}", path.display(), e))
}

fn atomic_write_json(path: &Path, value: &serde_json::Value) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("create parent {}: {}", parent.display(), e))?;
    }
    // Back up the existing file before overwriting so a bad write is recoverable.
    if path.exists() {
        let mut bak = path.to_path_buf();
        bak.set_extension(match path.extension().and_then(|s| s.to_str()) {
            Some(ext) => format!("{}.bak", ext),
            None => "bak".to_string(),
        });
        fs::copy(path, &bak).map_err(|e| format!("backup {}: {}", bak.display(), e))?;
    }
    let mut tmp = path.to_path_buf();
    tmp.set_extension(match path.extension().and_then(|s| s.to_str()) {
        Some(ext) => format!("{}.tmp", ext),
        None => "tmp".to_string(),
    });
    let body = serde_json::to_string_pretty(value)
        .map_err(|e| format!("serialize: {}", e))?;
    fs::write(&tmp, &body).map_err(|e| format!("write tmp {}: {}", tmp.display(), e))?;
    fs::rename(&tmp, path).map_err(|e| format!("rename {} -> {}: {}", tmp.display(), path.display(), e))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn tmp_target() -> PathBuf {
        let p = std::env::temp_dir().join(format!(
            "vct-mcp-reg-test-{}.json",
            uuid::Uuid::new_v4().simple()
        ));
        // Don't create the file — register_mcp is supposed to handle absence.
        p
    }

    #[test]
    fn register_creates_file_and_writes_mcpservers_block() {
        let target = tmp_target();
        assert!(!target.exists());

        // Use a cross-OS path stub for the test — the real registrar will resolve
        // a real venv path; here we just verify the registrar writes whatever
        // command we pass through unchanged.
        let test_command = if cfg!(target_os = "windows") {
            "C:\\Python\\python.exe"
        } else {
            "/usr/bin/python3"
        };
        let entry = serde_json::json!({
            "type": "stdio",
            "command": test_command,
            "args": ["server.py"],
            "env": {"FOO": "bar"},
        });

        register_mcp(&target, "my-mcp", &entry).expect("register_mcp");

        let raw = fs::read_to_string(&target).expect("read back");
        let json: serde_json::Value = serde_json::from_str(&raw).unwrap();

        // Bug 6 contract: server lives under mcpServers.<id> with the
        // exact entry block we passed in. Earlier `add_custom_mcp_server`
        // only wrote `env` keys to settings.json, which Claude Code does
        // not read.
        let server = &json["mcpServers"]["my-mcp"];
        assert_eq!(server["type"], "stdio");
        assert_eq!(server["command"], test_command);
        assert_eq!(server["args"][0], "server.py");
        assert_eq!(server["env"]["FOO"], "bar");

        fs::remove_file(&target).ok();
    }

    #[test]
    fn register_then_deregister_removes_only_named_entry() {
        let target = tmp_target();
        let a = serde_json::json!({"type": "stdio", "command": "a"});
        let b = serde_json::json!({"type": "stdio", "command": "b"});
        register_mcp(&target, "alpha", &a).unwrap();
        register_mcp(&target, "beta", &b).unwrap();

        deregister_mcp(&target, "alpha").unwrap();

        let raw = fs::read_to_string(&target).unwrap();
        let json: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert!(json["mcpServers"].get("alpha").is_none());
        assert_eq!(json["mcpServers"]["beta"]["command"], "b");

        fs::remove_file(&target).ok();
    }

    #[test]
    fn register_preserves_existing_top_level_keys() {
        let target = tmp_target();
        // Pre-seed the file with unrelated user config — register_mcp
        // must NOT clobber it.
        fs::write(
            &target,
            serde_json::to_string_pretty(&serde_json::json!({
                "permissions": {"allow": ["Read", "Edit"]},
                "feedbackSurveyState": {"lastShownTime": 1234567890},
            }))
            .unwrap(),
        )
        .unwrap();

        let entry = serde_json::json!({"type": "stdio", "command": "x"});
        register_mcp(&target, "x", &entry).unwrap();

        let raw = fs::read_to_string(&target).unwrap();
        let json: serde_json::Value = serde_json::from_str(&raw).unwrap();

        // Existing keys preserved
        assert_eq!(json["permissions"]["allow"][0], "Read");
        assert_eq!(json["feedbackSurveyState"]["lastShownTime"], 1234567890);
        // New key added
        assert_eq!(json["mcpServers"]["x"]["command"], "x");

        fs::remove_file(&target).ok();
    }
}
