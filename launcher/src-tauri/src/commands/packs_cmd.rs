// SPDX-License-Identifier: AGPL-3.0-or-later
//! Tauri commands for the opt-in agent/skill PACKS catalogue
//! (v0.2.101 catalogue plan §3.6).
//!
//! Python is the SSOT for pack definitions (`templates/packs/packs.toml`,
//! parsed by `vco_lib/packs.py` — lane L1). Rust NEVER mirrors the table:
//!   * [`list_project_packs`] reads the catalogue + the project manifest
//!     through ONE bridge call owned by `services::vco_lib_bridge`
//!     (`packs_status` → `python -m vco_lib.packs status --folder <path>
//!     --json`) and parses only the JSON.
//!   * [`set_project_pack_enabled`] shells the ORDINARY bundle engine,
//!     `python -m vco_lib.project_init install-bundle --folder <path>
//!     --update --pack <name>` (resp. `--remove-pack <name>`), through the
//!     per-folder single-flight bundle-engine turn
//!     (`commands::single_flight::bundle_engine_turn`) so two engines never
//!     run on one folder, then re-populates project state so the
//!     Agents/Skills tabs reflect the members that landed (or were
//!     removed-with-backup by `--remove-pack`).
//!
//! ## The two CLI contracts this file codes against (plan §3.2–§3.5)
//!
//! 1. `vco_lib.packs status` — one JSON object on stdout:
//!    `{"ok": true, "packs": [{"name": str, "description": str,
//!    "members": [str], "installed": bool}, …]}` where `members` are the
//!    pack's agent file-stems / skill directory names and `installed` is
//!    true iff the pack is recorded in the project's
//!    `.claude/.vco-manifest.json` `packs` map. A refusal is
//!    `{"ok": false, "error": str, "message": str}`. The shape is pinned by
//!    the ONE committed fixture `tests/fixtures/packs_status_contract.json`
//!    — this file's parser test AND lane L1's Python emitter test load it,
//!    so a field rename on either side fails a suite, not the Packs tab at
//!    runtime.
//! 2. `install-bundle --pack/--remove-pack` — the ordinary bundle JSON
//!    envelope (`errors[]`, `warnings[]`, `notes[]`, and on install
//!    `packs_installed: [names]`), exit 0 on success.

use std::path::PathBuf;

use serde_json::Value as JsonValue;
use tauri::{command, State};
use vct_launcher_core::process::CommandExt as _;

use crate::db::Db;

/// One pack row for the GUI's Packs tab. Mirrored in
/// `launcher/src/lib/types/project-state.ts` (`PackInfo`) — the wiring test
/// `launcher/src/lib/packs.wiring.test.ts` pins the two stay in lockstep.
#[derive(Debug, Clone, serde::Serialize)]
pub struct PackInfo {
    pub name: String,
    pub description: String,
    /// Member names: agent file stems + skill directory names.
    pub members: Vec<String>,
    /// True iff the pack is recorded in the project manifest's `packs` map.
    pub installed: bool,
}

/// Which bundle-engine flag a pack toggle maps to (plan §3.3 / §3.5).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PackAction {
    /// `--pack <name>`: deliver the pack's members, record in the manifest.
    Install,
    /// `--remove-pack <name>`: remove members (backup when user-modified),
    /// drop the pack + member entries from the manifest.
    Remove,
}

/// The `enabled` flag → engine-action selection (plan §3.3 / §3.5), split
/// out as its own decision so the disable-path mapping is pinned by a test
/// that drives THIS function — not inferred from an argv shape (L3 review
/// SF-4; argv-shape tests miss the wiring above them).
pub(crate) fn action_for(enabled: bool) -> PackAction {
    if enabled {
        PackAction::Install
    } else {
        PackAction::Remove
    }
}

/// The ONE argv builder for a pack toggle (the whole bridge behind one
/// function, so the argv is unit-tested without an interpreter). The base
/// is NOT mirrored here: it is `projects_v2::build_bundle_argv` in Update
/// mode — the same builder every other bundle spawn uses — with the pack
/// flag appended after `--json` (L3 review SF-3: never a fresh mirror when
/// the shared builder is reachable; the byte-parity test below pins it).
pub(crate) fn pack_bundle_argv(
    folder_str: &str,
    templates_str: &str,
    pack: &str,
    action: PackAction,
) -> Vec<String> {
    let mut argv = crate::commands::projects_v2::build_bundle_argv(
        folder_str,
        templates_str,
        crate::commands::projects_v2::BundleMode::Update,
    );
    argv.push(
        match action {
            PackAction::Install => "--pack",
            PackAction::Remove => "--remove-pack",
        }
        .into(),
    );
    argv.push(pack.into());
    argv
}

/// The `vco_lib.packs status` spawn lives in ONE home,
/// `services::vco_lib_bridge::packs_status` (L3 review one-home rule for
/// `-m vco_lib` verbs); this command resolves roots, calls it, and shapes
/// the reply through the pure parser below.

/// Pure parser for the `vco_lib.packs status` reply — the JSON contract in
/// this module's docs. Pure so the wire shape is testable without a Python
/// interpreter (the CLI itself is lane L1's deliverable).
pub(crate) fn parse_packs_status_reply(reply: &JsonValue) -> Result<Vec<PackInfo>, String> {
    let packs = reply
        .get("packs")
        .and_then(JsonValue::as_array)
        .ok_or_else(|| format!("packs status reply has no `packs` array: {}", reply))?;
    let mut out = Vec::with_capacity(packs.len());
    for p in packs {
        let name = p
            .get("name")
            .and_then(JsonValue::as_str)
            .ok_or_else(|| format!("packs status reply has a pack without `name`: {}", p))?;
        out.push(PackInfo {
            name: name.to_string(),
            description: p
                .get("description")
                .and_then(JsonValue::as_str)
                .unwrap_or("")
                .to_string(),
            members: p
                .get("members")
                .and_then(JsonValue::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(|m| m.as_str().map(str::to_string))
                        .collect()
                })
                .unwrap_or_default(),
            installed: p
                .get("installed")
                .and_then(JsonValue::as_bool)
                .unwrap_or(false),
        });
    }
    Ok(out)
}

/// Resolve the project row or fail loudly (mirrors
/// `rescan_project_from_filesystem`'s posture — a wrong project_id must not
/// read as "no packs").
fn project_folder_of(db: &Db, project_id: &str) -> Result<(String, String, PathBuf), String> {
    let project = db
        .get_project(project_id)
        .map_err(|e| format!("get_project: {}", e))?
        .ok_or_else(|| format!("project '{}' not found", project_id))?;
    Ok((project.name, project.folder_path.clone(), PathBuf::from(project.folder_path)))
}

#[command]
pub async fn list_project_packs(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<PackInfo>, String> {
    let (_name, _folder_str, folder) = project_folder_of(db.inner(), &project_id)?;
    // Root resolution (L3 review N-5, deliberate — mirrors the pre-existing
    // split): the READ path resolves DB-cache-first
    // (`resolve_orchestrator_root`, same as the staleness listing), while
    // the WRITE path (`set_project_pack_enabled`) uses the exe-anchored
    // `find_local_repo_root`, same as every other `install-bundle` engine
    // run. On a machine with two clones the two could in principle name
    // different clones; unify ONLY together with the existing
    // staleness-listing vs bundle-update split in projects_v2 (changing one
    // side alone would diverge the packs tab from its siblings).
    let root = crate::services::vco_lib_bridge::resolve_orchestrator_root(db.inner())
        .ok_or_else(|| {
            "the orchestrator clone could not be located, so the packs catalogue \
             (vco_lib.packs) cannot be read — check the install"
                .to_string()
        })?;
    // Blocking-bounded spawn off the async executor (the command is async;
    // the child is not).
    tokio::task::spawn_blocking(move || {
        let reply = crate::services::vco_lib_bridge::packs_status(&root, &folder)?;
        parse_packs_status_reply(&reply)
    })
    .await
    .map_err(|e| format!("packs status task join failed: {}", e))?
}

#[command]
pub async fn set_project_pack_enabled(
    project_id: String,
    pack: String,
    enabled: bool,
    db: State<'_, Db>,
) -> Result<Vec<String>, String> {
    let (name, folder_str, folder) = project_folder_of(db.inner(), &project_id)?;
    if !folder.is_dir() {
        return Err(format!(
            "project folder no longer exists on disk: {} \
             (was the project moved/deleted? edit the path via Settings or remove the project)",
            folder_str
        ));
    }
    // Both roots coincide for production callers (same rule as
    // `run_install_bundle_core` with override=None): the clone is the cwd
    // that makes `vco_lib` importable AND the `--orchestrator-root` the
    // bundle engine reads templates from.
    let root: PathBuf = crate::commands::installer::find_local_repo_root()
        .map_err(|e| format!("orchestrator root not found: {}", e))?;
    let action = action_for(enabled);
    let argv = pack_bundle_argv(&folder_str, &root.to_string_lossy(), &pack, action);

    // v0.2.100 W3R-06: one engine per folder — the per-project update,
    // "Update all" and the module toggle all take turns through the same
    // per-folder mutex; a pack toggle is another engine run.
    let _engine_turn = crate::commands::single_flight::bundle_engine_turn(&folder).await;

    let Some(py_cmd) = vct_launcher_core::python_resolve::resolve_python_for_vco_lib() else {
        return Err(
            "no Python interpreter with vco_lib found (checked $VCT_VENV and the \
             orchestrator venv) — the pack change cannot run; check the install"
                .to_string(),
        );
    };
    let mut cmd = tokio::process::Command::new(&py_cmd).silent();
    cmd.args(&argv).current_dir(&root).stdin(std::process::Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    let out = cmd
        .output()
        .await
        .map_err(|e| format!("pack change subprocess failed to start: {}", e))?;
    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();

    let mut warnings: Vec<String> = Vec::new();
    match serde_json::from_str::<JsonValue>(&stdout) {
        Ok(v) => {
            if let Some(errs) = v.get("errors").and_then(|x| x.as_array()) {
                for e in errs {
                    let p = e.get("path").and_then(|c| c.as_str()).unwrap_or("?");
                    let msg = e.get("error").and_then(|c| c.as_str()).unwrap_or("?");
                    warnings.push(format!("pack {} file error on {}: {}", pack, p, msg));
                }
            }
            for key in ["notes", "warnings"] {
                if let Some(ws) = v.get(key).and_then(|x| x.as_array()) {
                    for w in ws {
                        if let Some(s) = w.as_str() {
                            warnings.push(format!("pack {}: {}", pack, s));
                        }
                    }
                }
            }
            if !out.status.success() {
                return Err(format!(
                    "pack change for '{}' exited {} (errors: {:?})",
                    pack, out.status, warnings
                ));
            }
        }
        Err(parse_err) => {
            let tail = stderr.lines().rev().take(3).collect::<Vec<_>>().join(" | ");
            return Err(format!(
                "pack change for '{}' produced unparseable output ({}). stderr tail: {}",
                pack, parse_err, tail
            ));
        }
    }

    // Re-populate so the Agents/Skills tabs reflect the pack's members
    // (fresh rows for installed members; the populate-time prune drops
    // rows whose files `--remove-pack` deleted). Soft-fail: a populate
    // hiccup must not fail an engine run that succeeded. Called inline,
    // exactly like `rescan_project_from_filesystem` does.
    let populate_report =
        crate::commands::project_state_populate::populate_project_state_from_filesystem(
            &project_id,
            &name,
            &folder,
            db.inner(),
        );
    for w in &populate_report.warnings {
        warnings.push(format!("re-scan: {}", w));
    }

    db.audit(
        "project_pack_set",
        Some(&project_id),
        None,
        &serde_json::json!({ "pack": pack, "enabled": enabled }),
    )?;

    Ok(warnings)
}

#[cfg(test)]
mod tests {
    use super::*;

    // ─── pack_bundle_argv: the bridge's ONE argv contract ──────────────

    /// SF-3: the base is the SHARED builder's Update-mode output, byte for
    /// byte — a hand-copied base here would silently diverge from every
    /// other bundle spawn on the next base change.
    #[test]
    fn pack_argv_base_is_byte_identical_to_the_shared_update_builder() {
        let pack_argv = pack_bundle_argv("/tmp/proj", "/tmp/root", "dev-advisors", PackAction::Install);
        let base = crate::commands::projects_v2::build_bundle_argv(
            "/tmp/proj",
            "/tmp/root",
            crate::commands::projects_v2::BundleMode::Update,
        );
        assert_eq!(
            &pack_argv[..pack_argv.len() - 2],
            &base[..],
            "pack argv base must BE build_bundle_argv(Update), not a mirror"
        );
        // The ordinary update argv itself stays what it was.
        assert_eq!(
            base,
            vec![
                "-m", "vco_lib.project_init", "install-bundle", "--folder", "/tmp/proj",
                "--orchestrator-root", "/tmp/root", "--project-folder", "/tmp/proj",
                "--update", "--json",
            ],
            "the shared builder's Update argv changed — repin deliberately"
        );
    }

    #[test]
    fn install_argv_carries_update_pack_and_json() {
        let argv = pack_bundle_argv("/tmp/proj", "/tmp/root", "dev-advisors", PackAction::Install);
        // …--update BEFORE --json (Update-mode byte order)…
        let update_at = argv.iter().position(|a| a == "--update").unwrap();
        let json_at = argv.iter().position(|a| a == "--json").unwrap();
        assert!(update_at < json_at, "update flag must precede --json: {:?}", argv);
        // …and the pack flag AFTER --json with the pack name as its value.
        assert!(json_at < argv.len() - 2);
        assert_eq!(argv[argv.len() - 2], "--pack");
        assert_eq!(argv[argv.len() - 1], "dev-advisors");
        assert!(!argv.contains(&"--remove-pack".to_string()));
    }

    /// RED-proof shape for the disabled path (plan §9.4): a disable maps
    /// to `--remove-pack`, never `--pack`.
    #[test]
    fn remove_argv_carries_remove_pack_not_pack() {
        let argv = pack_bundle_argv("/tmp/proj", "/tmp/root", "migration", PackAction::Remove);
        assert_eq!(argv[argv.len() - 2], "--remove-pack");
        assert_eq!(argv[argv.len() - 1], "migration");
        assert!(!argv.contains(&"--pack".to_string()));
        assert!(argv.contains(&"--update".to_string()));
    }

    // ─── action_for: the enabled → engine-action selection (SF-4) ──────
    // Plan §9.4's "disabled-path runs --remove-pack (mocked bridge)" — the
    // CLI does not exist until lane L1, so the pinned surface is the
    // selection the command makes, driven through THIS function rather
    // than inferred from an argv shape.

    #[test]
    fn enabled_true_selects_install() {
        assert_eq!(action_for(true), PackAction::Install);
    }

    /// The disable path: `enabled=false` MUST select Remove — a GUI
    /// uninstall that silently installed would be a destructive defect.
    #[test]
    fn enabled_false_selects_remove() {
        assert_eq!(action_for(false), PackAction::Remove);
    }

    // ─── parse_packs_status_reply: the bridge's JSON contract ──────────

    #[test]
    fn parses_packs_array_with_members_and_installed() {
        let reply = serde_json::json!({
            "ok": true,
            "packs": [
                {
                    "name": "dev-advisors",
                    "description": "Second-opinion advisors for development decisions",
                    "members": ["accessibility-checker", "architect", "debug-expert"],
                    "installed": false
                },
                {
                    "name": "migration",
                    "description": "Code migration agents",
                    "members": ["code-migrator"],
                    "installed": true
                }
            ]
        });
        let packs = parse_packs_status_reply(&reply).unwrap();
        assert_eq!(packs.len(), 2);
        assert_eq!(packs[0].name, "dev-advisors");
        assert_eq!(packs[0].members.len(), 3);
        assert!(!packs[0].installed);
        assert!(packs[1].installed);
        // The IPC shape the TS mirror types must match.
        let json = serde_json::to_value(&packs[0]).unwrap();
        assert_eq!(
            json,
            serde_json::json!({
                "name": "dev-advisors",
                "description": "Second-opinion advisors for development decisions",
                "members": ["accessibility-checker", "architect", "debug-expert"],
                "installed": false
            })
        );
    }

    #[test]
    fn reply_without_packs_array_is_an_error() {
        let reply = serde_json::json!({"ok": true});
        assert!(parse_packs_status_reply(&reply).is_err());
    }

    #[test]
    fn pack_without_name_is_an_error() {
        let reply = serde_json::json!({"ok": true, "packs": [{"description": "d"}]});
        assert!(parse_packs_status_reply(&reply).is_err());
    }

    /// Optional fields degrade, not fail: a pack with no description and no
    /// members list renders as an empty row (the table stays readable while
    /// L1's emitter evolves).
    #[test]
    fn optional_fields_default_instead_of_failing() {
        let reply = serde_json::json!({"ok": true, "packs": [{"name": "empty-pack"}]});
        let packs = parse_packs_status_reply(&reply).unwrap();
        assert_eq!(packs[0].description, "");
        assert!(packs[0].members.is_empty());
        assert!(!packs[0].installed);
    }

    // ─── the committed cross-lane contract fixture (SF-6) ─────────────
    //
    // ONE fixture (`tests/fixtures/packs_status_contract.json`) pins L1's
    // Python emitter and this parser to the SAME wire shape: the Rust side
    // parses the fixture here; lane L1's Python test must emit output that
    // validates against the same file. A field rename on either side then
    // fails a suite instead of breaking the Packs tab at runtime.

    /// Repo-root fixture path from this crate's manifest dir
    /// (`launcher/src-tauri` → repo root → tests/fixtures).
    fn contract_fixture() -> std::path::PathBuf {
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .join("tests")
            .join("fixtures")
            .join("packs_status_contract.json")
    }

    #[test]
    fn parser_accepts_the_committed_contract_fixture() {
        let raw = std::fs::read_to_string(contract_fixture())
            .expect("tests/fixtures/packs_status_contract.json must exist beside the Python suite");
        let fixture: JsonValue =
            serde_json::from_str(&raw).expect("the contract fixture is valid JSON");

        let reply = fixture
            .get("status_reply")
            .expect("fixture carries status_reply")
            .clone();
        let packs = parse_packs_status_reply(&reply)
            .expect("the Rust parser accepts the committed contract");
        assert_eq!(packs.len(), 3, "fixture pins three packs (two full + one minimal)");

        let dev = &packs[0];
        assert_eq!(dev.name, "dev-advisors");
        assert_eq!(dev.description, "Second-opinion advisors for development decisions");
        assert_eq!(dev.members.len(), 5);
        assert!(!dev.installed);
        let ai = &packs[1];
        assert_eq!(ai.name, "ai-engineering");
        assert!(ai.installed);
        // The minimal row: name only — optional fields default.
        let migration = &packs[2];
        assert_eq!(migration.name, "migration");
        assert_eq!(migration.description, "");
        assert!(migration.members.is_empty());
        assert!(!migration.installed);

        // And the row shape serializes exactly as the TS `PackInfo` mirror
        // types it (the wiring test pins the field sets on the TS side).
        assert_eq!(
            serde_json::to_value(dev).unwrap(),
            serde_json::json!({
                "name": "dev-advisors",
                "description": "Second-opinion advisors for development decisions",
                "members": ["accessibility-checker", "ai-rag-advisor", "architect", "debug-expert", "security-reviewer"],
                "installed": false
            })
        );
    }

    #[test]
    fn refusal_arm_of_the_contract_fixture_is_a_loud_error() {
        let raw = std::fs::read_to_string(contract_fixture()).expect("contract fixture");
        let fixture: JsonValue = serde_json::from_str(&raw).unwrap();
        let refusal = serde_json::to_string(
            fixture.get("refusal_reply").expect("fixture carries refusal_reply"),
        )
        .unwrap();
        let err = crate::services::vco_lib_bridge::parse_ok_reply_named(
            "packs status",
            refusal.as_bytes(),
            b"",
        )
        .expect_err("an ok:false reply must be an error, never a degraded listing");
        assert!(
            err.contains("packs_table_unreadable"),
            "the child's own error code must surface: {}",
            err
        );
        assert!(
            !err.contains("settings editor"),
            "packs failures must not wear the settings editor's wording (N-1): {}",
            err
        );
    }
}
