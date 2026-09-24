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
//! the caller has already built with `.silent()`. The spawns this module
//! makes itself (all v0.2.97, all through the shared transport
//! `run_vco_lib_json`) each build their `Command` with `.silent()` on the
//! same line:
//!
//!   * `vco_lib.config_projection` — [`write_settings_env_block`],
//!     [`strip_proven_secret_values`];
//!   * `vco_lib.unregister_env` — [`strip_routing_env_keys`] (the
//!     unregister's `.claude/env` + JSON env-block routing-key strip);
//!   * `vco_lib.env_projection_check` — [`read_settings_env_blocks`];
//!   * `vco_lib.hooks_settings` — [`list_settings_hooks`];
//!   * `vco_lib.env_template` — [`apply_project_env_template`],
//!     [`write_project_env_reference`], [`strip_project_env_keys`],
//!     [`repair_project_env_kg`], [`sentinel_project_env_keys`] (the project
//!     `.env`'s one writer) and the read-only
//!     [`read_project_env_assignment`];
//!   * `vco_lib.compose_env` — [`set_infrastructure_env_key`] (the one writer
//!     of `infrastructure/.env`).

use std::io::{Read as _, Write as _};
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
///
/// No endpoint travels (v0.2.97): the child's `vco_lib.service_endpoints`
/// reads the SAME `service_endpoints` rows in the same launcher.db this
/// process resolves from, so there is nothing to hand over. (Until the rows
/// existed the launcher handed its `VCT_WEAVIATE_URL` / `vct-config.toml`
/// statement across as `VCT_WEAVIATE_URL`; neither is a resolver input now,
/// and an env hand-off is exactly how one process's view leaked into
/// another's.)
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

/// Wall-clock cap for one env-block spawn: a single small JSON
/// read-modify-write that takes ~150 ms. Past this the child is stuck.
const ENV_BLOCK_TIMEOUT: Duration = Duration::from_secs(30);

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

/// The stdin request the unregister's strips read (`vco_lib.unregister_env
/// strip-routing`, `vco_lib.env_template strip`): the key NAMES in scope.
pub fn build_strip_env_keys_request(keys: &[&str]) -> String {
    serde_json::json!({ "keys": keys }).to_string()
}

/// v0.2.97: set / strip a launcher-owned key set in one JSON env surface
/// (`claude_settings_json` = `<folder>/.claude/settings.json` `env`,
/// `vscode_settings_json` = `<folder>/.vscode/settings.json`
/// `claude-code.env`) through the ONE implementation,
/// `vco_lib.config_projection.write_env_block`.
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
/// `root` is the orchestrator clone root when the caller knows it: it
/// becomes the child's cwd, so `python -m vco_lib` imports the checkout's
/// package ahead of anything else on `sys.path` (the rule the hooks editor
/// documents); `None` falls back to the project folder.
pub fn write_settings_env_block(
    root: Option<&Path>,
    project_folder: &Path,
    surface: &str,
    pairs: &[(&str, String)],
    owned_keys: &[&str],
) -> Result<Vec<String>, String> {
    let python = vco_lib_python()?;
    let mut cmd = Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.config_projection")
        .arg("write-env-block")
        .arg("--project-folder")
        .arg(project_folder)
        .arg("--surface")
        .arg(surface);
    let body = build_write_env_block_request(pairs, owned_keys);
    run_env_block_command(cmd, &python, root, project_folder, &body, "written")
}

/// v0.2.97 (review R6): the unregister's routing-key strip for
/// `.claude/env` and both JSON env blocks — `python -m vco_lib.unregister_env
/// strip-routing` (stdin `{"keys": [...]}`). VCO's `.claude/env` block goes
/// whole; elsewhere a key goes only when its value equals what VCO projects
/// for `project_id` (resolved from the launcher DB with `root` as the
/// orchestrator root — the refresh's own resolution — so call it BEFORE the
/// project's row is deleted). Returns the whole `ok: true` reply: `removed` /
/// `left` `{file: [KEY]}`, `projection` `"resolved"|"unavailable"`,
/// `projection_error`, `errors` — names only, never a value. Superseded the
/// by-name `config_projection strip-env-keys` spawn and a Rust `.claude/env`
/// rewrite.
pub fn strip_routing_env_keys(
    root: Option<&Path>,
    project_folder: &Path,
    project_id: Option<&str>,
    keys: &[&str],
) -> Result<serde_json::Value, String> {
    let python = vco_lib_python()?;
    let mut cmd = Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.unregister_env")
        .arg("strip-routing")
        .arg("--project-folder")
        .arg(project_folder);
    if let Some(id) = project_id {
        cmd.arg("--project-id").arg(id);
    }
    if let Some(r) = root {
        cmd.arg("--orchestrator-root").arg(r);
    }
    let body = build_strip_env_keys_request(keys);
    run_vco_lib_json(cmd, &python, root, project_folder, &body, parse_ok_reply)
}

/// v0.2.97: remove from `project_folder`'s env files the secret values VCO
/// can PROVE it wrote, and report the rest — `python -m
/// vco_lib.config_projection strip-proven-secret-values`. Returns the whole
/// `ok: true` reply: `removed` `{file: [KEY]}`, `left` `{file: {KEY:
/// verdict}}`, `reasons` `{verdict: sentence}`, `errors` `[message]` — key
/// names, verdicts and wording only, never a value (the comparison with the
/// stored value happens inside the child, through the hub). The unregister
/// flow acts on it (`projects_v2::strip_proven_secret_values`).
pub fn strip_proven_secret_values(
    root: Option<&Path>,
    project_folder: &Path,
) -> Result<serde_json::Value, String> {
    let python = vco_lib_python()?;
    let mut cmd = Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.config_projection")
        .arg("strip-proven-secret-values")
        .arg("--project-folder")
        .arg(project_folder);
    run_vco_lib_json(cmd, &python, root, project_folder, "", parse_ok_reply)
}

/// v0.2.97: the env objects of each folder's JSON env surfaces
/// (`claude_settings_json`, `vscode_settings_json`), read by the ONE JSONC
/// reader — `python -m vco_lib.env_projection_check read-env`. The launcher's
/// own readers of these blocks used a strict JSON parse, so a settings.json
/// with a comment or a trailing comma (which Claude Code and VS Code accept)
/// read as a parse error.
///
/// Returns `{folder: {surface: {"status", "path", "env", "error"}}}` exactly as
/// the Python side builds it (`read_json_env_blocks`); `status` is `ok`,
/// `missing` or `unreadable`. One spawn for any number of folders.
pub fn read_settings_env_blocks(
    root: Option<&Path>,
    project_folders: &[&Path],
) -> Result<serde_json::Map<String, serde_json::Value>, String> {
    let first = project_folders
        .first()
        .ok_or_else(|| "read_settings_env_blocks: no folder given".to_string())?;
    let python = vco_lib_python()?;
    let mut cmd = Command::new(&python).silent();
    cmd.arg("-m").arg("vco_lib.env_projection_check").arg("read-env");
    for folder in project_folders {
        cmd.arg("--project-folder").arg(folder);
    }
    let reply = run_vco_lib_json(cmd, &python, root, first, "", parse_ok_reply)?;
    match reply.get("folders") {
        Some(serde_json::Value::Object(map)) => Ok(map.clone()),
        _ => Err(format!("read-env reply has no `folders` object: {}", reply)),
    }
}

/// The env object `read-env` returned for `surface` of `folder`, or the
/// reason there is none: `Ok(None)` for a missing file, `Err(message)` for a
/// file that exists but could not be read.
pub fn env_block_of(
    blocks: &serde_json::Map<String, serde_json::Value>,
    folder: &Path,
    surface: &str,
) -> Result<Option<serde_json::Map<String, serde_json::Value>>, String> {
    let row = blocks
        .get(&folder.display().to_string())
        .and_then(|f| f.get(surface))
        .ok_or_else(|| format!("read-env returned nothing for {} {}", folder.display(), surface))?;
    match row.get("status").and_then(serde_json::Value::as_str) {
        Some("ok") => Ok(Some(
            row.get("env").and_then(serde_json::Value::as_object).cloned().unwrap_or_default(),
        )),
        Some("missing") => Ok(None),
        _ => Err(row
            .get("error")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("unreadable")
            .to_string()),
    }
}

/// v0.2.97 (R4 F32h): every hook entry of `<folder>/.claude/settings.json`,
/// read by the ONE JSONC reader — `python -m vco_lib.hooks_settings list
/// --with-items` (the verb the Hooks tab lists through). The `project_hooks`
/// mirror calls this only when a strict parse fails, so a settings.json with
/// a comment or a trailing comma (valid for Claude Code) is mirrored instead
/// of skipped. Each entry carries `event`, `matcher`, `command`,
/// `timeout_seconds` and the whole inner hook object as `item`. `Err` names
/// why the file could not be read (a broken file stays unmirrored, visibly).
pub fn list_settings_hooks(
    root: Option<&Path>,
    project_folder: &Path,
) -> Result<Vec<serde_json::Value>, String> {
    let python = vco_lib_python()?;
    let mut cmd = Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.hooks_settings")
        .arg("list")
        .arg("--with-items")
        .arg("--project-folder")
        .arg(project_folder);
    run_vco_lib_json(cmd, &python, root, project_folder, "", |out, err| {
        let text = String::from_utf8_lossy(out);
        let reply: serde_json::Value = serde_json::from_str(text.trim()).map_err(|e| {
            format!(
                "hooks list produced unreadable output ({}). stdout: {} stderr: {}",
                e,
                text.trim(),
                String::from_utf8_lossy(err).trim()
            )
        })?;
        if reply.get("ok").and_then(serde_json::Value::as_bool) != Some(true) {
            return Err(reply
                .get("error")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("the hooks reader refused the file")
                .to_string());
        }
        Ok(reply
            .get("hooks")
            .and_then(serde_json::Value::as_array)
            .cloned()
            .unwrap_or_default())
    })
}

/// The launcher-resolved service ports a project `.env`'s managed block
/// renders (`ProjectEnvSettings`: app_state overrides / adopted services),
/// forwarded to the Python resolver, whose own defaults are the stock ports.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EnvTemplatePorts {
    pub weaviate: u16,
    pub ollama: u16,
    pub code_embed: u16,
}

/// The flags every `vco_lib.env_template` verb takes after
/// `--project-id` / `--project-folder`. Pure, so the argv is testable
/// without an interpreter. `--orchestrator-root` only when known (the CLI
/// defaults it to `None`; an empty string would be a wrong root).
pub fn env_template_flags(root: Option<&Path>, ports: EnvTemplatePorts) -> Vec<String> {
    let mut flags = Vec::new();
    if let Some(root) = root {
        flags.push("--orchestrator-root".to_string());
        flags.push(root.display().to_string());
    }
    for (flag, port) in [
        ("--weaviate-port", ports.weaviate),
        ("--ollama-port", ports.ollama),
        ("--code-embed-port", ports.code_embed),
    ] {
        flags.push(flag.to_string());
        flags.push(port.to_string());
    }
    flags
}

/// v0.2.97: write `<project_folder>/.env`'s VCO-managed block through the
/// ONE `.env` writer — `python -m vco_lib.env_template apply` (A-tier: the
/// Rust `ensure_project_env_template` append-only mirror that ran beside it
/// is retired). The Python side creates a missing file (scaffold + block),
/// folds the retired writers' legacy lines into the block, and never renders
/// a key the user assigns outside it. Returns the `ok: true` reply
/// (`report`: `env`, `added`, `user_set`, `migrated`, `action`); a refusal or
/// crash is `Err` with the child's own message.
pub fn apply_project_env_template(
    root: Option<&Path>,
    project_folder: &Path,
    project_id: &str,
    ports: EnvTemplatePorts,
) -> Result<serde_json::Value, String> {
    let python = vco_lib_python()?;
    let mut cmd = Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.env_template")
        .arg("apply")
        .arg("--project-id")
        .arg(project_id)
        .arg("--project-folder")
        .arg(project_folder);
    cmd.args(env_template_flags(root, ports));
    run_vco_lib_json(cmd, &python, root, project_folder, "", parse_ok_reply)
}

/// v0.2.97: the Safe-add twin of [`apply_project_env_template`] —
/// `python -m vco_lib.env_template reference` writes
/// `<project_folder>/.env.vco.reference` (what a new `.env` would hold) and
/// NEVER touches the live `.env`, which may be committed.
pub fn write_project_env_reference(
    root: Option<&Path>,
    project_folder: &Path,
    project_id: &str,
    ports: EnvTemplatePorts,
) -> Result<serde_json::Value, String> {
    let python = vco_lib_python()?;
    let mut cmd = Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.env_template")
        .arg("reference")
        .arg("--project-id")
        .arg(project_id)
        .arg("--project-folder")
        .arg(project_folder);
    cmd.args(env_template_flags(root, ports));
    run_vco_lib_json(cmd, &python, root, project_folder, "", parse_ok_reply)
}

/// v0.2.97: the assignment of one managed key that WINS in
/// `<project_folder>/.env` (the last active line) and whether it sits inside
/// the VCO-managed block — `python -m vco_lib.env_template effective`, the
/// same parser the writer uses. Read-only; only managed (non-secret) keys are
/// answered. `Ok((None, false))` when nothing assigns the key.
pub fn read_project_env_assignment(
    root: Option<&Path>,
    project_folder: &Path,
    key: &str,
) -> Result<(Option<String>, bool), String> {
    let python = vco_lib_python()?;
    let mut cmd = Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.env_template")
        .arg("effective")
        .arg("--project-folder")
        .arg(project_folder)
        .arg("--key")
        .arg(key);
    let reply = run_vco_lib_json(cmd, &python, root, project_folder, "", parse_ok_reply)?;
    let value = reply.get("value").and_then(serde_json::Value::as_str).map(str::to_string);
    let in_block = reply.get("in_block").and_then(serde_json::Value::as_bool).unwrap_or(false);
    Ok((value, in_block))
}

/// v0.2.97: the "Migrate from .env" sentinel rewrite through the ONE `.env`
/// writer — `python -m vco_lib.env_template sentinel` (stdin
/// `{"keys": [...]}`): each hub-confirmed key's value becomes the keychain
/// sentinel; `export`, an unquoted value's trailing comment and every other
/// line are kept, the file keeps its mode. Replaces a Rust mirror of
/// `vco_lib.secrets_audit` plus a Rust write. Never sees a value.
pub fn sentinel_project_env_keys(
    root: Option<&Path>,
    project_folder: &Path,
    keys: &[&str],
) -> Result<usize, String> {
    let python = vco_lib_python()?;
    let mut cmd = Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.env_template")
        .arg("sentinel")
        .arg("--project-folder")
        .arg(project_folder);
    let body = build_strip_env_keys_request(keys);
    let reply = run_vco_lib_json(cmd, &python, root, project_folder, &body, parse_ok_reply)?;
    Ok(reply.get("replaced").and_then(serde_json::Value::as_u64).unwrap_or(0) as usize)
}

/// v0.2.97: the B12 stale-`KG_COLLECTION` repair through the ONE `.env`
/// writer — `python -m vco_lib.env_template repair-kg`. The caller decides
/// the canonical name and which values are stale (its sanitizer); Python
/// only rewrites the line. Returns `"repaired"` / `"unchanged"`.
pub fn repair_project_env_kg(
    root: Option<&Path>,
    project_folder: &Path,
    canonical: &str,
    stale: &[&str],
) -> Result<String, String> {
    let python = vco_lib_python()?;
    let mut cmd = Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.env_template")
        .arg("repair-kg")
        .arg("--project-folder")
        .arg(project_folder)
        .arg("--canonical")
        .arg(canonical);
    for value in stale {
        cmd.arg("--stale").arg(value);
    }
    let reply = run_vco_lib_json(cmd, &python, root, project_folder, "", parse_ok_reply)?;
    Ok(reply.get("action").and_then(serde_json::Value::as_str).unwrap_or("unchanged").to_string())
}

/// What the unregister's `.env` strip did — key NAMES only.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct EnvStripOutcome {
    /// VCO's keys removed (its managed block, its retired writers' sections).
    pub removed: Vec<String>,
    /// The user's OWN assignments of a managed key — left in place.
    pub left: Vec<String>,
    /// `<KEY>_old` lines (the user's earlier values) still in the file.
    pub preserved: Vec<String>,
}

/// v0.2.97 (review R5 F40, R6 F47): the unregister's `.env` strip through the
/// ONE `.env` writer — `python -m vco_lib.env_template strip` (stdin
/// `{"keys": [...]}`). It removes what VCO authored ONLY: the managed block
/// whole, the retired writers' recognised sections, and the comment VCO put
/// above kept `<KEY>_old` values. A user's own assignment of a managed key
/// elsewhere is left and reported ([`EnvStripOutcome::left`]), as are the
/// `_old` values. Replaces a Rust read-modify-write of the same file.
pub fn strip_project_env_keys(
    root: Option<&Path>,
    project_folder: &Path,
    keys: &[&str],
) -> Result<EnvStripOutcome, String> {
    let python = vco_lib_python()?;
    let mut cmd = Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.env_template")
        .arg("strip")
        .arg("--project-folder")
        .arg(project_folder);
    let body = build_strip_env_keys_request(keys);
    run_vco_lib_json(cmd, &python, root, project_folder, &body, |out, err| {
        parse_ok_reply(out, err).map(|reply| EnvStripOutcome {
            removed: list_at(&reply, "removed"),
            left: list_at(&reply, "left"),
            preserved: list_at(&reply, "preserved"),
        })
    })
}

/// v0.2.97 (review R5 F40): set one launcher-owned key (e.g.
/// `VCT_VOLUMES_PATH`) in `<infra_dir>/.env` through that file's one writer,
/// `python -m vco_lib.compose_env set`. Returns `"set"` / `"unchanged"`.
pub fn set_infrastructure_env_key(
    root: Option<&Path>,
    infra_dir: &Path,
    key: &str,
    value: &str,
) -> Result<String, String> {
    let python = vco_lib_python()?;
    let mut cmd = Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.compose_env")
        .arg("set")
        .arg("--infra-dir")
        .arg(infra_dir)
        .arg("--key")
        .arg(key)
        .arg("--value")
        .arg(value);
    let reply = run_vco_lib_json(cmd, &python, root, infra_dir, "", parse_ok_reply)?;
    Ok(reply.get("action").and_then(serde_json::Value::as_str).unwrap_or("set").to_string())
}

/// The interpreter for an env-block spawn: the shared RT-4 ladder. Under
/// `cfg(test)` a bare `python3` is accepted as the last resort — the verbs
/// need only the standard library, and a unit test must not depend on where
/// `CARGO_TARGET_DIR` puts the test binary (the ladder's walk-up rung).
fn vco_lib_python() -> Result<std::path::PathBuf, String> {
    #[cfg(test)]
    {
        // An ABSOLUTE interpreter, resolved once. The fallback is the bare
        // name `python3`, which the spawn looks up on PATH at spawn time.
        // Tests used to blank the process PATH for a moment, and a bridge
        // test spawning concurrently intermittently failed with "spawn
        // failed"; no test does that now (v0.2.97 review R6,
        // `tests/test_rust_tests_never_mutate_process_path.py`). A test
        // that injected an empty lookup PATH on its own thread
        // (`paths::with_lookup_path`) gets the bare name, uncached.
        static PYTHON: std::sync::OnceLock<std::path::PathBuf> = std::sync::OnceLock::new();
        if let Some(found) = PYTHON.get() {
            return Ok(found.clone());
        }
        let resolved = vct_launcher_core::python_resolve::resolve_python_for_vco_lib_or("python3");
        if resolved.is_absolute() {
            return Ok(PYTHON.get_or_init(|| resolved).clone());
        }
        // The launcher's one PATH lookup (v0.2.97 review R6).
        let on_path = resolved.to_str().and_then(vct_launcher_core::paths::which_on_path);
        Ok(match on_path {
            Some(absolute) => PYTHON.get_or_init(|| absolute).clone(),
            None => resolved,
        })
    }
    #[cfg(not(test))]
    {
        vct_launcher_core::python_resolve::resolve_python_for_vco_lib().ok_or_else(|| {
            "no Python interpreter with vco_lib found (checked $VCT_VENV and the \
             orchestrator venv) — the settings editor cannot run; check the install"
                .to_string()
        })
    }
}

/// The child's cwd, which decides WHICH `vco_lib` a `python -m vco_lib…`
/// imports. Production: the caller's orchestrator root, else the project.
/// Under `cfg(test)`: always this checkout (compile-time path, test builds
/// only), so the tests exercise the code beside them regardless of
/// `CARGO_TARGET_DIR` and of an ambient `$VCT_INSTALL_ROOT` pointing at an
/// older tree (v0.2.97 review F13: `invalid choice: 'write-env-block'`).
pub(crate) fn vco_lib_cwd(root: Option<&Path>, project_folder: &Path) -> std::path::PathBuf {
    #[cfg(test)]
    {
        let _ = (root, project_folder);
        test_checkout_root()
    }
    #[cfg(not(test))]
    {
        root.map(Path::to_path_buf).unwrap_or_else(|| project_folder.to_path_buf())
    }
}

/// Test helper: the canonical env `vco_lib.config_projection from-db`
/// resolves for `project_id` from the test's launcher DB — the values the
/// unregister strip compares against (review R6). `root` as in production.
#[cfg(test)]
pub(crate) fn test_projected_env(root: Option<&Path>, project_id: &str) -> serde_json::Value {
    let python = vco_lib_python().expect("a python for vco_lib");
    let mut cmd = Command::new(&python).silent();
    cmd.arg("-m")
        .arg("vco_lib.config_projection")
        .arg("from-db")
        .arg("--project-id")
        .arg(project_id);
    if let Some(r) = root {
        cmd.arg("--orchestrator-root").arg(r);
    }
    let here = test_checkout_root();
    run_vco_lib_json(cmd, &python, root, &here, "", |out, err| {
        serde_json::from_slice::<serde_json::Value>(out)
            .map(|v| v["canonical_env"].clone())
            .map_err(|e| format!("from-db: {} ({})", e, String::from_utf8_lossy(err)))
    })
    .expect("from-db resolves the test project")
}

/// The `VCT_STATE_DIR` a test child runs with: the test's own (set through
/// `test_env::env_guard`), else a per-process scratch dir. GUARD: it must lie
/// under the temp dir — a child of a unit test reading a real state dir
/// (the developer's `~/.vct/launcher.db`) is a test defect, so it panics.
#[cfg(test)]
pub(crate) fn test_child_state_dir() -> std::path::PathBuf {
    scratch_state_dir(std::env::var_os("VCT_STATE_DIR"), &std::env::temp_dir())
        .unwrap_or_else(|e| panic!("{}", e))
}

/// The pure decision behind [`test_child_state_dir`]: `configured` (the
/// test's `VCT_STATE_DIR`, if any) or a per-process scratch dir under `temp`;
/// `Err` when the result is not under `temp`. A pure function of its inputs,
/// so its test never has to set a process-wide variable another test's child
/// could observe (review R4 F30).
#[cfg(test)]
pub(crate) fn scratch_state_dir(
    configured: Option<std::ffi::OsString>,
    temp: &Path,
) -> Result<std::path::PathBuf, String> {
    let state = configured
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| temp.join(format!("vct-bridge-test-state-{}", std::process::id())));
    if state.starts_with(temp) {
        Ok(state)
    } else {
        Err(format!(
            "a vco_lib child of a unit test would read a non-scratch state dir ({}): set a \
             temp VCT_STATE_DIR in the test",
            state.display()
        ))
    }
}

/// The repository root this test binary was compiled from.
#[cfg(test)]
pub(crate) fn test_checkout_root() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("..")
}

/// Spawn `cmd`, feed `body` on stdin, collect stdout/stderr, bound by
/// [`ENV_BLOCK_TIMEOUT`], and parse the one-JSON-object reply.
///
/// Both output pipes are drained on their own threads WHILE the child runs
/// (v0.2.97 review F11): reading them only after exit let a child that wrote
/// more than a pipe buffer (a long traceback) block forever and be reported
/// as "timed out". A failed stdin write (the child exited before reading —
/// e.g. an argparse rejection) is not fatal either: the child's own stderr
/// is still collected and is the error the caller sees.
fn run_env_block_command(
    cmd: Command,
    python: &Path,
    root: Option<&Path>,
    project_folder: &Path,
    body: &str,
    list_field: &str,
) -> Result<Vec<String>, String> {
    run_vco_lib_json(cmd, python, root, project_folder, body, |out, err| {
        parse_env_block_output(out, err, list_field)
    })
}

/// v0.2.97: the transport half of [`run_env_block_command`], shared by every
/// bridge verb that answers with one `{"ok": …}` JSON object — the env-block
/// edits AND the JSONC-aware read ([`read_settings_env_blocks`]). `parse`
/// turns the collected stdout/stderr into the caller's result.
fn run_vco_lib_json<T>(
    mut cmd: Command,
    python: &Path,
    root: Option<&Path>,
    project_folder: &Path,
    body: &str,
    parse: impl FnOnce(&[u8], &[u8]) -> Result<T, String>,
) -> Result<T, String> {
    reinject_minimal_env(&mut cmd);
    // Unit tests must never reach the developer's live hub (a verb that
    // resolves a stored secret would otherwise ask it): the discard port makes
    // every such lookup "unknown" — no evidence, nothing removed. Nor may the
    // child read the developer's real state dir (launcher.db, hub token):
    // a test that set no `VCT_STATE_DIR` gets a scratch one (review R3 note).
    #[cfg(test)]
    {
        cmd.env("VCT_HUB_PORT", "9");
        let state = test_child_state_dir();
        cmd.env("VCT_STATE_DIR", &state);
    }
    cmd.current_dir(vco_lib_cwd(root, project_folder));
    cmd.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = cmd
        .spawn()
        .map_err(|e| format!("settings editor: spawn failed (python={}): {}", python.display(), e))?;

    let drain = |pipe: Option<Box<dyn std::io::Read + Send>>| {
        std::thread::spawn(move || {
            let mut buf = Vec::new();
            if let Some(mut p) = pipe {
                let _ = p.read_to_end(&mut buf);
            }
            buf
        })
    };
    let out_reader = drain(child.stdout.take().map(|p| Box::new(p) as Box<dyn std::io::Read + Send>));
    let err_reader = drain(child.stderr.take().map(|p| Box::new(p) as Box<dyn std::io::Read + Send>));
    let stdin_error = child.stdin.take().and_then(|mut sink| sink.write_all(body.as_bytes()).err());
    // `sink` is dropped above: the child reads stdin to EOF.

    let deadline = Instant::now() + ENV_BLOCK_TIMEOUT;
    let timed_out = loop {
        match child.try_wait() {
            Ok(Some(_)) => break false,
            Ok(None) if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                break true;
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("settings editor: wait failed: {}", e));
            }
        }
    };
    let stdout = out_reader.join().unwrap_or_default();
    let stderr = err_reader.join().unwrap_or_default();
    if timed_out {
        return Err(format!(
            "settings editor: timed out after {} s. stderr: {}",
            ENV_BLOCK_TIMEOUT.as_secs(),
            String::from_utf8_lossy(&stderr).trim()
        ));
    }
    parse(&stdout, &stderr).map_err(|e| match stdin_error {
        Some(w) => format!("{} (the request could not be sent: {})", e, w),
        None => e,
    })
}

/// The env-block verbs print exactly one JSON object on stdout on every path.
/// `ok: true` → the key list under `list_field` (`written` / `removed`);
/// anything else → `Err` with the child's own message (a refusal names the
/// file and why), or the raw output when it is not that shape (a crash
/// before the emit is reported, never degraded).
pub fn parse_env_block_output(
    stdout: &[u8],
    stderr: &[u8],
    list_field: &str,
) -> Result<Vec<String>, String> {
    parse_ok_reply(stdout, stderr).map(|reply| list_at(&reply, list_field))
}

/// The string list under `field` of an `ok: true` reply (empty when absent).
fn list_at(reply: &serde_json::Value, field: &str) -> Vec<String> {
    reply
        .get(field)
        .and_then(serde_json::Value::as_array)
        .map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
        .unwrap_or_default()
}

/// The one-JSON-object wire contract every bridge verb follows: `ok: true` →
/// the whole object; anything else → `Err` with the child's own message (a
/// refusal names the file and why), or the raw output when it is not that
/// shape (a crash before the emit is reported, never degraded).
pub fn parse_ok_reply(stdout: &[u8], stderr: &[u8]) -> Result<serde_json::Value, String> {
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
        return Ok(parsed);
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
        let _env_lock = vct_launcher_core::test_env::env_lock();
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
        let _env_lock = vct_launcher_core::test_env::env_lock();
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

    /// v0.2.97 (service endpoints SSOT): NO endpoint crosses into a
    /// `vco_lib` child — neither the retired `VCT_WEAVIATE_URL` statement nor
    /// the projected transport `WEAVIATE_URL`. The child reads the same
    /// `service_endpoints` rows from the same launcher.db.
    #[test]
    fn hands_no_endpoint_to_the_child() {
        let _g = vct_launcher_core::test_env::state_dir_guard_with(&[
            ("VCT_WEAVIATE_URL", Some("http://vm.lan:9000")),
            ("WEAVIATE_URL", Some("http://stale-projection:1")),
        ]);
        let mut cmd = Command::new("python3");
        reinject_minimal_env(&mut cmd);
        let get = |key: &str| {
            cmd.get_envs()
                .find(|(k, _)| k.to_string_lossy() == key)
                .and_then(|(_, v)| v.map(|vv| vv.to_string_lossy().to_string()))
        };
        assert_eq!(get("VCT_WEAVIATE_URL"), None);
        assert_eq!(get("WEAVIATE_URL"), None);
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

    /// Review R3 note: a test child never reads the developer's real state
    /// dir — the scratch default lies under the temp dir, and a non-scratch
    /// `VCT_STATE_DIR` is refused.
    #[test]
    fn a_test_childs_state_dir_is_always_scratch() {
        let temp = Path::new("/tmp/scratch-root");
        assert_eq!(
            scratch_state_dir(None, temp).unwrap().parent(),
            Some(temp),
            "no configured dir ⇒ a scratch dir under temp"
        );
        assert_eq!(
            scratch_state_dir(Some("/tmp/scratch-root/mine".into()), temp).unwrap(),
            Path::new("/tmp/scratch-root/mine")
        );
        assert!(
            scratch_state_dir(Some("/home/someone/.vct".into()), temp).is_err(),
            "a real state dir must be refused"
        );
    }

    /// v0.2.97 review F11: a child that writes far more than a pipe buffer to
    /// stderr before answering is drained while it runs — it completes and
    /// its answer is parsed, instead of blocking until the 30 s deadline and
    /// being reported as "timed out".
    #[test]
    fn a_child_flooding_stderr_is_drained_not_timed_out() {
        let python = vco_lib_python().unwrap();
        let mut cmd = Command::new(&python);
        cmd.arg("-c").arg(
            "import sys; sys.stdin.read(); sys.stderr.write('x' * 400000); \
             sys.stderr.flush(); print('{\"ok\": true, \"written\": [\"A\"]}')",
        );
        let started = Instant::now();
        let out = run_env_block_command(cmd, &python, None, &std::env::temp_dir(), "{}", "written");
        assert_eq!(out, Ok(vec!["A".to_string()]));
        assert!(started.elapsed() < Duration::from_secs(20), "must not ride the deadline");
    }

    /// A child that exits before reading its (large) request — the stdin
    /// write fails with a broken pipe — still reports ITS OWN stderr.
    #[test]
    fn a_child_exiting_before_reading_stdin_keeps_its_stderr() {
        let python = vco_lib_python().unwrap();
        let mut cmd = Command::new(&python);
        cmd.arg("-c").arg("import sys; sys.stderr.write('loud-failure-reason'); sys.exit(3)");
        let body = "x".repeat(1 << 20);
        let err = run_env_block_command(cmd, &python, None, &std::env::temp_dir(), &body, "written")
            .unwrap_err();
        assert!(err.contains("loud-failure-reason"), "{}", err);
    }

    /// A refusal must come back as `Err` carrying the child's own message
    /// (it names the file and why) — that string is what reaches the GUI.
    #[test]
    fn write_env_block_output_parses_ok_refusal_and_garbage() {
        assert_eq!(
            parse_env_block_output(br#"{"ok": true, "written": ["A", "B"]}"#, b"", "written"),
            Ok(vec!["A".to_string(), "B".to_string()])
        );
        let refused = parse_env_block_output(
            br#"{"ok": false, "error": "settings_write_refused", "message": "/p/.claude/settings.json was NOT updated: it is not valid JSON"}"#,
            b"",
            "written",
        )
        .unwrap_err();
        assert!(refused.contains("NOT updated") && refused.contains("settings_write_refused"));
        let garbage = parse_env_block_output(b"Traceback ...", b"ModuleNotFoundError", "written")
            .unwrap_err();
        assert!(garbage.contains("unreadable output") && garbage.contains("ModuleNotFoundError"));
    }
}

/// v0.2.97: the project `.env` verbs end to end — a real `launcher.db` in a
/// scratch state dir, the real `python -m vco_lib.env_template` child.
#[cfg(test)]
mod env_template_tests {
    use super::*;
    use vct_launcher_core::db::models::ProjectHost;
    use vct_launcher_core::test_env::{state_dir_guard, StateDirGuard};

    const PORTS: EnvTemplatePorts = EnvTemplatePorts { weaviate: 8081, ollama: 11435, code_embed: 11440 };

    /// A scratch state dir whose `launcher.db` holds project `name` at a
    /// fresh folder inside it. Keep the guard alive for the whole test.
    fn fixture(name: &str) -> (StateDirGuard, String, std::path::PathBuf) {
        let guard = state_dir_guard();
        let folder = guard.path().join("project");
        std::fs::create_dir_all(&folder).unwrap();
        let db = Db::open().unwrap();
        let id = uuid::Uuid::new_v4().to_string();
        let slug = db.generate_unique_slug(name).unwrap();
        db.insert_project(&id, name, &folder.display().to_string(), ProjectHost::Base, &slug)
            .unwrap();
        drop(db);
        (guard, id, folder)
    }

    /// Every ACTIVE value of `key`, in file order.
    fn assignments(text: &str, key: &str) -> Vec<String> {
        let prefix = format!("{}=", key);
        text.lines()
            .filter_map(|l| l.trim().strip_prefix(&prefix).map(str::to_string))
            .collect()
    }

    #[test]
    fn env_template_flags_carry_the_root_only_when_known() {
        let ports = EnvTemplatePorts { weaviate: 18081, ollama: 11435, code_embed: 11440 };
        assert_eq!(
            env_template_flags(Some(Path::new("/orch")), ports),
            ["--orchestrator-root", "/orch", "--weaviate-port", "18081", "--ollama-port",
             "11435", "--code-embed-port", "11440"]
        );
        assert!(!env_template_flags(None, ports).iter().any(|f| f == "--orchestrator-root"));
    }

    #[test]
    fn apply_creates_the_env_with_scaffold_and_block_at_the_launcher_ports() {
        let (_guard, id, folder) = fixture("Acme");
        let ports = EnvTemplatePorts { weaviate: 18081, ..PORTS };
        let reply = apply_project_env_template(None, &folder, &id, ports).unwrap();
        assert_eq!(reply["report"]["action"], serde_json::json!(["created"]));

        let text = std::fs::read_to_string(folder.join(".env")).unwrap();
        assert_eq!(assignments(&text, "KG_COLLECTION"), ["Acme_KnowledgeGraph"]);
        assert_eq!(assignments(&text, "PROJECT_NAME"), ["Acme"]);
        // PR-3: a launcher-resolved non-default port reaches the file.
        assert_eq!(assignments(&text, "WEAVIATE_URL"), ["http://localhost:18081"]);
        // Bug 33 scaffold: optional keys are commented placeholders only.
        assert!(text.contains("# OPENAI_API_KEY=\n") && text.contains("# GITHUB_TOKEN=\n"));
        assert!(assignments(&text, "OPENAI_API_KEY").is_empty());
        // B7: the telemetry placeholder is the canonical key, never the alias.
        assert!(text.contains("# VCT_TELEMETRY=") && !text.contains("VIBECODED_TELEMETRY"));
        assert!(text.contains(&format!("# RL_PROJECT_ROOT={}", folder.display())));
    }

    #[test]
    fn apply_keeps_user_lines_folds_legacy_lines_and_is_idempotent() {
        let (_guard, id, folder) = fixture("Acme");
        let user = "OPENAI_API_KEY=sk-user\nKG_COLLECTION=MyCustom_KG\n";
        std::fs::write(
            folder.join(".env"),
            format!(
                "{user}\n# added by vco 2026-05-06: appended missing canonical keys\n\
                 # CODE_EMBED_URL=\nPROJECT_NAME=<project>\n# GITHUB_TOKEN=\n"
            ),
        )
        .unwrap();

        let reply = apply_project_env_template(None, &folder, &id, PORTS).unwrap();
        let first = std::fs::read_to_string(folder.join(".env")).unwrap();
        assert!(first.starts_with(user), "user lines untouched:\n{first}");
        assert_eq!(assignments(&first, "KG_COLLECTION"), ["MyCustom_KG"]);
        assert_eq!(assignments(&first, "PROJECT_NAME"), ["Acme"]);
        assert_eq!(assignments(&first, "OPENAI_API_KEY"), ["sk-user"]);
        assert_eq!(assignments(&first, "CODE_EMBED_URL"), ["http://localhost:11440"]);
        assert!(first.contains("# GITHUB_TOKEN=\n") && !first.contains("<project>\n"));
        assert_eq!(reply["report"]["user_set"], serde_json::json!(["KG_COLLECTION"]));

        let again = apply_project_env_template(None, &folder, &id, PORTS).unwrap();
        assert_eq!(std::fs::read_to_string(folder.join(".env")).unwrap(), first);
        assert_eq!(again["report"]["action"], serde_json::json!(["unchanged"]));
    }

    #[test]
    fn reference_writes_the_sidecar_and_never_the_live_env() {
        let (_guard, id, folder) = fixture("Acme");
        let live = "KG_COLLECTION=LegacyBare\nUSER_KEY=keep\n";
        std::fs::write(folder.join(".env"), live).unwrap();

        write_project_env_reference(None, &folder, &id, PORTS).unwrap();

        assert_eq!(std::fs::read_to_string(folder.join(".env")).unwrap(), live);
        let sidecar = std::fs::read_to_string(folder.join(".env.vco.reference")).unwrap();
        assert_eq!(assignments(&sidecar, "KG_COLLECTION"), ["Acme_KnowledgeGraph"]);
        assert_eq!(assignments(&sidecar, "PROJECT_NAME"), ["Acme"]);
        assert!(sidecar.contains("safe_add_skipped_env_merge"));
    }

    /// Review R5 F40: the unregister's `.env` strip removes VCO's managed
    /// block WHOLE (a by-name strip left its markers and comments behind) and
    /// keeps the user's lines. Review R6 F47: a user's OWN assignment of a
    /// managed key is left and reported, never removed.
    #[test]
    fn strip_removes_the_whole_block_and_keeps_user_lines() {
        let (_guard, id, folder) = fixture("Acme");
        std::fs::write(folder.join(".env"), "USER_KEY=keep\nPROJECT_NAME=Mine\n").unwrap();
        apply_project_env_template(None, &folder, &id, PORTS).unwrap();
        let outcome = strip_project_env_keys(None, &folder, &["KG_COLLECTION", "PROJECT_NAME"]).unwrap();
        assert!(outcome.removed.iter().any(|k| k == "KG_COLLECTION"), "{outcome:?}");
        assert_eq!(outcome.left, vec!["PROJECT_NAME".to_string()]);
        assert!(!outcome.removed.iter().any(|k| k == "PROJECT_NAME"), "{outcome:?}");
        assert_eq!(
            std::fs::read_to_string(folder.join(".env")).unwrap(),
            "USER_KEY=keep\nPROJECT_NAME=Mine\n"
        );
    }

    /// The "Migrate from .env" rewrite, through the one writer: only the
    /// hub-confirmed keys change, structure kept (the byte shapes the retired
    /// Rust mirror pinned, now the Python writer's).
    #[test]
    fn sentinel_rewrites_only_the_confirmed_keys() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join(".env"),
            "export OPENAI_API_KEY=sk-canary-not-real-7c1f  # team\nB_SECRET=two\nPLAIN=keep\n",
        )
        .unwrap();
        let replaced = sentinel_project_env_keys(None, dir.path(), &["OPENAI_API_KEY"]).unwrap();
        assert_eq!(replaced, 1);
        assert_eq!(
            std::fs::read_to_string(dir.path().join(".env")).unwrap(),
            "export OPENAI_API_KEY=__vco_keychain__  # team\nB_SECRET=two\nPLAIN=keep\n"
        );
    }

    #[test]
    fn reference_does_not_create_a_live_env() {
        let (_guard, id, folder) = fixture("X");
        write_project_env_reference(None, &folder, &id, PORTS).unwrap();
        assert!(folder.join(".env.vco.reference").exists());
        assert!(!folder.join(".env").exists(), "safe-add must not create a live .env");
    }

    #[test]
    fn an_unknown_project_is_an_error_naming_the_reason_and_writes_nothing() {
        let (_guard, _id, folder) = fixture("X");
        let err = apply_project_env_template(None, &folder, "ghost-id", PORTS).unwrap_err();
        assert!(err.contains("project_not_found"), "{err}");
        assert!(!folder.join(".env").exists());
    }
}
