// SPDX-License-Identifier: AGPL-3.0-or-later
//! Shared builder for `python -m vco_lib.<module>` subprocess spawns.
//!
//! ## Why this module exists (v0.2.77 Part 7c task 2)
//!
//! The launcher shells out to `python -m vco_lib.<module>` from many
//! command files. The genuinely drift-prone, subtle part of that pattern
//! is NOT the `-m vco_lib.<module>` argv (that is one obvious line per
//! site) — it is the **env sandbox**: `env_clear()` followed by a hand-
//! curated ALLOWLIST of environment keys re-injected one by one so that
//!
//!   * per-launcher quirks (an inherited `KG_COLLECTION` from the
//!     launcher's own `.claude/env`, a stray `PROJECT_NAME`, …) do NOT
//!     leak into the child and disrupt the Python-side resolver, AND
//!   * the handful of keys the child legitimately needs
//!     (`PATH`, `VCT_STATE_DIR`, `VCT_HUB_PORT`, `VCT_HUB_TOKEN`,
//!     `VCT_INSTALL_ROOT`, temp dirs, and the home-dir keys that make
//!     `~/.vct/launcher.db` resolvable) still reach it.
//!
//! That allowlist is exactly the kind of list that silently drifts when
//! copy-pasted: add a key at one call-site, forget the others, and one
//! spawn resolves the DB while another can't. The canonical instance is
//! `projects_v2::apply_project_env_via_python` (the config-projection
//! writer). This module lifts its `env_clear` + re-injection block into
//! one home so future `-m vco_lib.<module>` spawns that need the same
//! sandbox call [`reinject_minimal_env`] instead of re-deriving the
//! allowlist.
//!
//! ## What is deliberately NOT here
//!
//! Not every `python -m vco_lib.<module>` spawn wants an `env_clear`
//! sandbox. The `project_init` subcommand spawns (bootstrap-collections,
//! install-bundle, migrate-*, drop-collections) INHERIT the full parent
//! env and set `.current_dir(orchestrator_root)` so the in-tree
//! `vco_lib` namespace package resolves; they are a different shape and
//! are intentionally left on their own env plumbing. Likewise the
//! wrapper-script spawns (codegraph analyzer, kg-sync, kg-summary) and
//! the inline `-c` deferral emitters do not use `-m vco_lib` at all.
//! This module targets ONLY the env-sandbox shape.
//!
//! ## `.silent()` note
//!
//! Callers own the `Command` and its `.silent()` marker (the
//! `command_silent_gate` integration test scans by path, and this file
//! is in scope). [`reinject_minimal_env`] takes a `&mut Command` that
//! the caller has already built with `.silent()`. The one spawn this
//! module makes itself, [`write_settings_env_block`] (v0.2.97), builds its
//! `Command` with `.silent()` on the same line.

use std::io::Write as _;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use vct_launcher_core::db::Db;
use vct_launcher_core::process::CommandExt as _;

/// Clear the child's inherited environment and re-inject ONLY the
/// allowlisted keys that a `python -m vco_lib.<module>` subprocess needs.
///
/// This is the canonical env sandbox for launcher → `vco_lib` spawns.
/// Mutates `cmd` in place (chaining is inconvenient because `env_clear`
/// / `env` return `&mut Command`, and the caller usually already holds a
/// `let mut cmd`).
///
/// The allowlist (kept in ONE place so it can't drift across call-sites):
///   * `PATH` — the child needs it to find `python`'s own helpers.
///   * `VCT_STATE_DIR` — launcher-state root override (else the resolver
///     falls back to `~/.vct/`).
///   * `VCT_HUB_PORT` / `VCT_HUB_TOKEN` — hub-aware resolver hints.
///   * `VCT_INSTALL_ROOT` — so `python -m vco_lib...` resolves `vco_lib`
///     as an implicit-namespace package from the orchestrator clone
///     (`vco_lib` is NOT pip-installed).
///   * `TEMP` / `TMP` / `TMPDIR` — so the child's atomic-write tempfiles
///     land somewhere writable (Windows + macOS especially).
///   * home-dir keys — `HOME` (POSIX) or
///     `USERPROFILE`/`APPDATA`/`LOCALAPPDATA`/`HOMEDRIVE`/`HOMEPATH`
///     (Windows) — so the `~/.vct/launcher.db` fallback resolves.
///
/// Anything NOT on this list (e.g. an inherited `KG_COLLECTION`) is
/// dropped by the preceding `env_clear`, which is the whole point.
pub fn reinject_minimal_env(cmd: &mut Command) {
    cmd.env_clear();

    // A key is re-injected only when present in the parent env; a missing
    // key stays missing (never re-injected as empty), preserving the
    // "absent means absent" contract the Python resolver relies on.
    for key in [
        "PATH",
        "VCT_STATE_DIR",
        "VCT_HUB_PORT",
        "VCT_HUB_TOKEN",
        "VCT_INSTALL_ROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
    ] {
        if let Ok(v) = std::env::var(key) {
            cmd.env(key, v);
        }
    }

    // Home-dir keys so `~/.vct/launcher.db` (and the atomic-write temp
    // fallback) resolve. Split per-OS: Windows needs the USERPROFILE
    // family; POSIX needs HOME.
    #[cfg(target_os = "windows")]
    {
        for key in ["USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH"] {
            if let Ok(v) = std::env::var(key) {
                cmd.env(key, v);
            }
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        if let Ok(v) = std::env::var("HOME") {
            cmd.env("HOME", v);
        }
    }
}

/// Resolve the orchestrator clone root for a `vco_lib` spawn, DB-cache
/// first. Thin pass-through to the canonical Rust resolver so bridge
/// callers don't each reach into `commands::installer`.
///
/// Returns `None` for a standalone binary with no discoverable clone —
/// callers should then OMIT any `--orchestrator-root` flag (the Python
/// CLI defaults it to `None`), never pass an empty string.
pub fn resolve_orchestrator_root(db: &Db) -> Option<std::path::PathBuf> {
    crate::commands::installer::resolve_orchestrator_root(db)
}

/// Wall-clock cap for one `write-env-block` spawn: a single small JSON
/// read-modify-write that takes ~150 ms. Past this the child is stuck.
const WRITE_ENV_BLOCK_TIMEOUT: Duration = Duration::from_secs(30);

/// The stdin request `python -m vco_lib.config_projection write-env-block`
/// reads: the values to set, and the full key set the caller owns (an owned
/// key absent from `pairs` is REMOVED). Pure, so the wire shape is testable
/// without a Python interpreter.
pub fn build_write_env_block_request(pairs: &[(&str, String)], owned_keys: &[&str]) -> String {
    let set: serde_json::Map<String, serde_json::Value> = pairs
        .iter()
        .map(|(k, v)| ((*k).to_string(), serde_json::Value::String(v.clone())))
        .collect();
    serde_json::json!({ "set": set, "owned_keys": owned_keys }).to_string()
}

/// v0.2.97: set / strip a launcher-owned key set in one JSON env surface
/// (`claude_settings_json` = `<folder>/.claude/settings.json` `env`) through
/// the ONE implementation, `vco_lib.config_projection.write_env_block`.
///
/// Why a subprocess (A-tier) rather than a Rust copy: the Rust copies of this
/// read-merge-write are how the destroy-on-unparseable defect lived on after
/// the Python writer was fixed — an unparseable `.claude/settings.json` was
/// replaced with `{}` plus the block. The Python side refuses such a file
/// (byte-identical, a `settings_write_refused_<surface>` deferral in the
/// project's ledger), edits a JSONC file in place, and keeps the strict-JSON
/// byte layout this launcher always wrote. A refusal comes back as `Err`
/// carrying the Python message, so the caller's warning surface shows it.
///
/// `cwd` is the orchestrator clone root when one resolves, so
/// `python -m vco_lib` imports the checkout's package ahead of anything else
/// on `sys.path` (the rule the hooks editor documents), else the project.
pub fn write_settings_env_block(
    db: &Db,
    project_folder: &Path,
    surface: &str,
    pairs: &[(&str, String)],
    owned_keys: &[&str],
) -> Result<Vec<String>, String> {
    let python = vct_launcher_core::python_resolve::resolve_python_for_vco_lib().ok_or_else(|| {
        "no Python interpreter with vco_lib found (checked $VCT_VENV and the \
         orchestrator venv) — the settings editor cannot run; check the install"
            .to_string()
    })?;
    let mut cmd = Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.config_projection")
        .arg("write-env-block")
        .arg("--project-folder")
        .arg(project_folder)
        .arg("--surface")
        .arg(surface);
    reinject_minimal_env(&mut cmd);
    cmd.current_dir(resolve_orchestrator_root(db).unwrap_or_else(|| project_folder.to_path_buf()));
    cmd.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("settings editor: spawn failed (python={}): {}", python.display(), e))?;
    if let Some(mut sink) = child.stdin.take() {
        let body = build_write_env_block_request(pairs, owned_keys);
        // Dropped at the end of this block: the child reads stdin to EOF.
        sink.write_all(body.as_bytes())
            .map_err(|e| format!("settings editor: could not send the request: {}", e))?;
    }
    let deadline = Instant::now() + WRITE_ENV_BLOCK_TIMEOUT;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("settings editor: timed out after 30 s".to_string());
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(e) => return Err(format!("settings editor: wait failed: {}", e)),
        }
    }
    let output = child
        .wait_with_output()
        .map_err(|e| format!("settings editor: could not read its output: {}", e))?;
    parse_write_env_block_output(&output.stdout, &output.stderr)
}

/// `write-env-block` prints exactly one JSON object on stdout on every path.
/// `ok: true` → the keys written; anything else → `Err` with the child's own
/// message (a refusal names the file and why), or the raw output when it is
/// not that shape (a crash before the emit is reported, never degraded).
pub fn parse_write_env_block_output(stdout: &[u8], stderr: &[u8]) -> Result<Vec<String>, String> {
    let out = String::from_utf8_lossy(stdout);
    let err = String::from_utf8_lossy(stderr);
    let parsed: serde_json::Value = serde_json::from_str(out.trim()).map_err(|e| {
        format!(
            "settings editor produced unreadable output ({}). stdout: {} stderr: {}",
            e,
            out.trim(),
            err.trim()
        )
    })?;
    if parsed.get("ok").and_then(serde_json::Value::as_bool) == Some(true) {
        return Ok(parsed
            .get("written")
            .and_then(serde_json::Value::as_array)
            .map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
            .unwrap_or_default());
    }
    let code = parsed.get("error").and_then(serde_json::Value::as_str).unwrap_or("unknown");
    let message = parsed
        .get("message")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("the settings editor refused the write");
    Err(format!("{} ({})", message, code))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `reinject_minimal_env` must DROP a key that is not on the
    /// allowlist. We assert on the built `Command`'s `get_envs()` view
    /// (which reflects `env_clear` + explicit `env`).
    #[test]
    fn drops_non_allowlisted_key() {
        // Set a sentinel disallowed key; after the sandbox it must NOT
        // appear among the re-injected overrides.
        std::env::set_var("KG_COLLECTION", "SENTINEL_SHOULD_BE_DROPPED");

        let mut cmd = Command::new("python3");
        reinject_minimal_env(&mut cmd);

        // After env_clear, get_envs() lists exactly the keys we
        // re-injected (value Some) — nothing is inherited implicitly.
        let envs: Vec<(String, Option<String>)> = cmd
            .get_envs()
            .map(|(k, v)| {
                (
                    k.to_string_lossy().to_string(),
                    v.map(|vv| vv.to_string_lossy().to_string()),
                )
            })
            .collect();

        assert!(
            !envs.iter().any(|(k, _)| k == "KG_COLLECTION"),
            "KG_COLLECTION leaked into the sandboxed child env: {:?}",
            envs
        );

        std::env::remove_var("KG_COLLECTION");
    }

    /// An allowlisted key present in the parent env IS re-injected.
    #[test]
    fn keeps_allowlisted_key() {
        std::env::set_var("VCT_INSTALL_ROOT", "/tmp/sentinel-install-root");

        let mut cmd = Command::new("python3");
        reinject_minimal_env(&mut cmd);

        let hit = cmd.get_envs().any(|(k, v)| {
            k.to_string_lossy() == "VCT_INSTALL_ROOT"
                && v.map(|vv| vv.to_string_lossy() == "/tmp/sentinel-install-root")
                    .unwrap_or(false)
        });
        assert!(hit, "VCT_INSTALL_ROOT should be re-injected by the sandbox");

        std::env::remove_var("VCT_INSTALL_ROOT");
    }

    /// v0.2.97: the stdin request is exactly what
    /// `config_projection._cli_write_env_block` reads — the values to set,
    /// and the whole owned set (an owned key absent from `set` is removed).
    #[test]
    fn write_env_block_request_carries_set_and_the_whole_owned_set() {
        let body = build_write_env_block_request(
            &[("VCT_RL_MODULE_DEPRECATED", "1".to_string())],
            &["VCT_RL_MODULE_DEPRECATED", "VCT_RL_MODULE_DEPRECATION_URL"],
        );
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["set"], serde_json::json!({"VCT_RL_MODULE_DEPRECATED": "1"}));
        assert_eq!(
            v["owned_keys"],
            serde_json::json!(["VCT_RL_MODULE_DEPRECATED", "VCT_RL_MODULE_DEPRECATION_URL"])
        );
        let empty: serde_json::Value =
            serde_json::from_str(&build_write_env_block_request(&[], &["K"])).unwrap();
        assert_eq!(empty["set"], serde_json::json!({}), "strip-all is an empty set");
    }

    /// A refusal must come back as `Err` carrying the child's own message
    /// (it names the file and why) — that string is what reaches the GUI.
    #[test]
    fn write_env_block_output_parses_ok_refusal_and_garbage() {
        assert_eq!(
            parse_write_env_block_output(br#"{"ok": true, "written": ["A", "B"]}"#, b""),
            Ok(vec!["A".to_string(), "B".to_string()])
        );
        let refused = parse_write_env_block_output(
            br#"{"ok": false, "error": "settings_write_refused", "message": "/p/.claude/settings.json was NOT updated: it is not valid JSON"}"#,
            b"",
        )
        .unwrap_err();
        assert!(refused.contains("NOT updated") && refused.contains("settings_write_refused"));
        let garbage = parse_write_env_block_output(b"Traceback ...", b"ModuleNotFoundError")
            .unwrap_err();
        assert!(garbage.contains("unreadable output") && garbage.contains("ModuleNotFoundError"));
    }
}
