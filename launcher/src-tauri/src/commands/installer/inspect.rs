//! Orchestrator/project inspection + third-party detection.
//!
//! Verbatim extraction (v0.2.77 Part 7d) of the config-health inspection
//! (`ConfigHealth`, `OrchestratorState`, `read_bundled_version`,
//! `version_is_outdated`, `check_file_health`, `inspect_orchestrator_at`),
//! the project-leftovers scan (`ProjectLeftovers`, `inspect_project_leftovers`),
//! and the third-party project detection (`ManifestStatus`,
//! `classify_vco_manifest`, `ThirdPartyDetection`,
//! `detect_third_party_project_signals`) that previously lived inline in
//! `installer.rs`. Behaviour is unchanged; the facade re-exports every symbol
//! (incl. the inspect_orchestrator_at / inspect_project_leftovers /
//! detect_third_party_project_signals Tauri commands).
//!
//! CROSS-LANGUAGE PARITY: `detect_third_party_project_signals`'s body mirrors
//! `install.py::_detect_third_party_project`, locked by
//! `tests/test_v0246_v47gfinal_rust_python_drift.py`, whose reader globs the
//! installer submodule set so the mirror is still discovered here.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use tauri::command;

use super::find_local_repo_root;

// ---------------------------------------------------------------------------
// Bug 20: inspect orchestrator state at a path. Used by the project-create
// modal so the user can see whether a target folder already has a working
// orchestrator install, and what shape it's in (current / outdated /
// corrupt-config). Bug 21 reuses the same struct for the per-project
// "update" banner.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConfigHealth {
    pub file: String,
    pub ok: bool,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrchestratorState {
    pub installed: bool,
    pub version: Option<String>,
    /// "current" | "outdated" | "unknown"
    pub version_status: String,
    pub bundled_version: Option<String>,
    pub config_health: Vec<ConfigHealth>,
}

/// Read the bundled launcher's `vct-module.json` version. Used as the
/// reference when classifying a project's installed version as
/// current/outdated/unknown.
pub fn read_bundled_version() -> Option<String> {
    let root = find_local_repo_root().ok()?;
    let raw = std::fs::read_to_string(root.join("vct-module.json")).ok()?;
    let v: serde_json::Value = serde_json::from_str(&raw).ok()?;
    v.get("version").and_then(|x| x.as_str()).map(|s| s.to_string())
}

/// Compare two semver-ish strings (e.g. "0.0.7" vs "0.1.0"). Returns
/// `true` if `installed < bundled`. Falls back to lexicographic if the
/// strings don't parse as semver-style triplets.
pub(crate) fn version_is_outdated(installed: &str, bundled: &str) -> bool {
    fn parse(v: &str) -> Vec<u64> {
        v.split('.')
            .map(|p| p.chars().take_while(|c| c.is_ascii_digit()).collect::<String>())
            .map(|s| s.parse::<u64>().unwrap_or(0))
            .collect()
    }
    let i = parse(installed);
    let b = parse(bundled);
    let len = i.len().max(b.len());
    for idx in 0..len {
        let ii = *i.get(idx).unwrap_or(&0);
        let bb = *b.get(idx).unwrap_or(&0);
        if ii < bb {
            return true;
        }
        if ii > bb {
            return false;
        }
    }
    false
}

pub(crate) fn check_file_health(path: &Path, parser: impl FnOnce(&str) -> Result<(), String>) -> ConfigHealth {
    let label = path.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
    if !path.exists() {
        return ConfigHealth {
            file: label,
            ok: false,
            error: Some("missing".into()),
        };
    }
    match std::fs::read_to_string(path) {
        Err(e) => ConfigHealth {
            file: label,
            ok: false,
            error: Some(format!("read error: {}", e)),
        },
        Ok(content) => match parser(&content) {
            Ok(()) => ConfigHealth { file: label, ok: true, error: None },
            Err(e) => ConfigHealth { file: label, ok: false, error: Some(e) },
        },
    }
}

#[command]
pub fn inspect_orchestrator_at(path: String) -> OrchestratorState {
    let root = PathBuf::from(&path);
    let claude_dir = root.join(".claude");
    // `vct-module.json` is the canonical VCO-clone marker (validate_source_repo
    // also gates on its presence, plus install.py + first-install.sh). A
    // project folder may have `.claude/` left behind by a non-destructive
    // unregister (PR #150) — that should NOT count as "orchestrator
    // installed here". Use the canonical marker only.
    let installed = root.join("vct-module.json").exists();

    if !installed {
        return OrchestratorState {
            installed: false,
            version: None,
            version_status: "unknown".into(),
            bundled_version: read_bundled_version(),
            config_health: vec![],
        };
    }

    // Version detection: prefer vct-module.json version field. Fallback
    // to None if missing or unreadable.
    let installed_version = std::fs::read_to_string(root.join("vct-module.json"))
        .ok()
        .and_then(|raw| serde_json::from_str::<serde_json::Value>(&raw).ok())
        .and_then(|v| v.get("version").and_then(|x| x.as_str()).map(|s| s.to_string()));

    let bundled = read_bundled_version();
    let version_status = match (installed_version.as_deref(), bundled.as_deref()) {
        (Some(i), Some(b)) if i == b => "current",
        (Some(i), Some(b)) if version_is_outdated(i, b) => "outdated",
        (Some(_), Some(_)) => "current",
        _ => "unknown",
    }
    .to_string();

    // Config health checks. Each parser is intentionally cheap — we just
    // want to flag malformed files, not fully validate them.
    let mut health = Vec::new();

    health.push(check_file_health(
        &claude_dir.join("settings.json"),
        |s| serde_json::from_str::<serde_json::Value>(s).map(|_| ()).map_err(|e| e.to_string()),
    ));
    health.push(check_file_health(
        &root.join("CLAUDE.md"),
        |s| {
            if s.trim().is_empty() {
                Err("empty file".into())
            } else {
                Ok(())
            }
        },
    ));
    health.push(check_file_health(
        &root.join("vct-module.json"),
        |s| serde_json::from_str::<serde_json::Value>(s).map(|_| ()).map_err(|e| e.to_string()),
    ));
    // Agents: count successfully-readable .md files in .claude/agents/,
    // flag if the directory exists but is unreadable. We don't parse
    // every agent here.
    let agents_dir = claude_dir.join("agents");
    if agents_dir.exists() {
        let agents_ok = std::fs::read_dir(&agents_dir).is_ok();
        health.push(ConfigHealth {
            file: ".claude/agents/".into(),
            ok: agents_ok,
            error: if agents_ok { None } else { Some("unreadable".into()) },
        });
    }

    OrchestratorState {
        installed: true,
        version: installed_version,
        version_status,
        bundled_version: bundled,
        config_health: health,
    }
}

/// State of leftover orchestrator-managed content at a candidate project
/// path. Lets the Add-Project flow distinguish:
///
///   * empty / fresh folder → no leftovers, install proceeds normally
///   * folder with leftover preserved content (PR-150 unregister policy
///     keeps `.claude/agents`, `.claude/skills`, `.claude/CONTEXT_STATE.md`,
///     `CLAUDE.md`, etc. when a project was previously registered then
///     unregistered) → wizard surfaces a "previously registered" banner so
///     the user knows the install will reuse those files rather than
///     surprising them at install time
///
/// Closes follow-up #13 (2026-05-07): "Repair adopt-choice for
/// previously-registered or incomplete-install projects". Pre-fix, the
/// Add-Project flow gave no signal that prior content existed; the
/// install reported preserved-file counts only AFTER the bundle write.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectLeftovers {
    /// Folder is non-empty AND contains at least one launcher-shipped path.
    pub has_leftovers: bool,
    /// Per-category counts. Zero means category has no leftovers.
    pub agent_count: u32,
    pub skill_count: u32,
    pub hook_count: u32,
    pub script_count: u32,
    /// Convenience flags for single-file artifacts.
    pub has_context_state: bool,
    pub has_claude_md: bool,
    pub has_vco_manifest: bool,
}

#[command]
pub fn inspect_project_leftovers(path: String) -> ProjectLeftovers {
    let root = PathBuf::from(&path);

    let mut out = ProjectLeftovers {
        has_leftovers: false,
        agent_count: 0,
        skill_count: 0,
        hook_count: 0,
        script_count: 0,
        has_context_state: false,
        has_claude_md: false,
        has_vco_manifest: false,
    };

    if !root.is_dir() {
        return out;
    }

    let claude_dir = root.join(".claude");

    let count_md_files = |dir: &Path| -> u32 {
        match std::fs::read_dir(dir) {
            Ok(rd) => rd
                .flatten()
                .filter(|e| {
                    e.file_type().map(|t| t.is_file()).unwrap_or(false)
                        && e.path().extension().is_some_and(|x| x == "md")
                })
                .count() as u32,
            Err(_) => 0,
        }
    };
    let count_dir_entries = |dir: &Path| -> u32 {
        match std::fs::read_dir(dir) {
            Ok(rd) => rd.flatten().count() as u32,
            Err(_) => 0,
        }
    };

    out.agent_count = count_md_files(&claude_dir.join("agents"));
    out.skill_count = count_dir_entries(&claude_dir.join("skills"));
    out.hook_count = count_dir_entries(&claude_dir.join("hooks"));
    out.script_count = count_dir_entries(&claude_dir.join("scripts"));
    out.has_context_state = claude_dir.join("CONTEXT_STATE.md").is_file();
    out.has_claude_md = root.join("CLAUDE.md").is_file();
    out.has_vco_manifest = claude_dir.join(".vco-manifest.json").is_file();

    out.has_leftovers = out.agent_count > 0
        || out.skill_count > 0
        || out.hook_count > 0
        || out.script_count > 0
        || out.has_context_state
        || out.has_claude_md
        || out.has_vco_manifest;

    out
}

// ---------------------------------------------------------------------------
// v0.2.46 V47-G-final: third-party detection signals for Add-Project wizard
// ---------------------------------------------------------------------------
//
// Mirrors the Python `_detect_third_party_project` heuristic in install.py.
// Used by the launcher GUI to decide whether to show the adopt-project modal
// when the user clicks "Add Project" and picks a directory that contains
// existing-project signals (CLAUDE.md, .env, .venv/, .claude/, knowledge/).
//
// Rust-side scan is intentionally CHEAP (no rglob, no recursive walks except
// for knowledge/) — the modal only needs to show whether to prompt the user,
// not run the full V47-G-final detail enumeration. When the user clicks
// Adopt, install.py runs and does the canonical detection again with its
// own logic. This command is purely a UI gate.

/// v0.2.46 post-adversarial L2: three-way classification of
/// `.claude/.vco-manifest.json`. Mirrors `_v47g_classify_manifest` in
/// install.py — both sides must agree (the M1 drift gate enforces
/// signal-count equality, but the classification itself is also part
/// of the contract).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ManifestStatus {
    /// File doesn't exist — truly 3rd-party project.
    Absent,
    /// File exists, parseable, has at least one expected top-level key.
    Valid,
    /// File exists but is empty / malformed / unrecognized.
    Broken,
}

pub(crate) fn classify_vco_manifest(path: &std::path::Path) -> ManifestStatus {
    if !path.is_file() {
        return ManifestStatus::Absent;
    }
    let raw = match std::fs::read_to_string(path) {
        Ok(s) => s,
        Err(_) => return ManifestStatus::Broken,
    };
    if raw.trim().is_empty() {
        return ManifestStatus::Broken;
    }
    let parsed: serde_json::Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => return ManifestStatus::Broken,
    };
    let obj = match parsed.as_object() {
        Some(o) => o,
        None => return ManifestStatus::Broken,
    };
    // At least ONE of the expected top-level keys must be present.
    // Kept in sync with _V47G_MANIFEST_EXPECTED_KEYS in install.py.
    let expected_keys = ["vco_version", "schema_version", "files", "bundled_files"];
    if expected_keys.iter().any(|k| obj.contains_key(*k)) {
        ManifestStatus::Valid
    } else {
        ManifestStatus::Broken
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ThirdPartyDetection {
    /// True iff any signal triggered + .vco-manifest.json is NOT present
    /// (existing VCO projects never count as third-party).
    pub has_signals: bool,
    /// True iff the install path contains a .vco-manifest.json — short-
    /// circuits has_signals to false. Surfaced separately so the launcher
    /// can show a distinct "this is a VCO project, use Update instead"
    /// hint when needed.
    pub manifest_present: bool,
    /// One-line label per detected signal (display-ready).
    pub signals: Vec<String>,
    /// Short summary like "4 signals detected".
    pub summary: String,
}

#[command]
pub fn detect_third_party_project_signals(install_path: String) -> ThirdPartyDetection {
    let root = PathBuf::from(&install_path);
    let mut out = ThirdPartyDetection {
        has_signals: false,
        manifest_present: false,
        signals: Vec::new(),
        summary: String::from("no signals"),
    };

    if !root.is_dir() {
        return out;
    }

    // v0.2.46 post-adversarial L4 (orchestrator-clone exclusion):
    // The VCO orchestrator clone itself has every signal the heuristic
    // looks for but does NOT carry a .vco-manifest.json. Match the
    // Python sibling exactly: presence of install.py + first-install.sh
    // + vct-module.json proves this is the VCO clone, not a 3rd-party
    // project. Suppresses the adopt prompt + the GUI modal pop-up on
    // the orchestrator clone itself. Kept in sync with the Python
    // helper _detect_third_party_project in install.py.
    let install_py = root.join("install.py");
    let first_install = root.join("first-install.sh");
    let vct_module = root.join("vct-module.json");
    if install_py.is_file() && first_install.is_file() && vct_module.is_file() {
        out.summary = "orchestrator clone (not a 3rd-party project)".into();
        return out;
    }

    // v0.2.46 post-adversarial L2: classify the manifest. The Python
    // canonical helper (_v47g_classify_manifest in install.py) returns one
    // of {"absent", "valid", "broken"}. We mirror only the YES/NO/BROKEN
    // distinction here — the launcher's modal cares about the same three
    // states. A WELL-FORMED manifest short-circuits to "existing VCO
    // project"; a missing manifest passes through to normal detection; a
    // BROKEN manifest gets called out as an extra signal (so the user
    // sees the bad-state explicitly rather than silent fall-through).
    let manifest = root.join(".claude").join(".vco-manifest.json");
    let manifest_status = classify_vco_manifest(&manifest);
    if manifest_status == ManifestStatus::Valid {
        out.manifest_present = true;
        out.summary = "vco-manifest present (existing VCO project)".into();
        return out;
    }

    // L2 broken-manifest signal — must match the Python signal count or
    // the v0.2.46 M1 drift gate test (tests/test_v0246_v47gfinal_rust_python_drift.py)
    // fails. Emit it BEFORE the regular detection so it heads the list.
    if manifest_status == ManifestStatus::Broken {
        out.signals.push(
            ".claude/.vco-manifest.json (present but unparseable / malformed — VCO state may need repair)".into()
        );
    }

    // Signal 1: .claude/ with content.
    let claude_dir = root.join(".claude");
    if claude_dir.is_dir() {
        let entry_count = std::fs::read_dir(&claude_dir)
            .map(|rd| rd.flatten().count())
            .unwrap_or(0);
        if entry_count > 0 {
            out.signals.push(format!(
                ".claude/ (existing orchestrator artifacts, {entry_count} entries)"
            ));
        }
    }

    // Signal 2: non-empty CLAUDE.md.
    let claude_md = root.join("CLAUDE.md");
    if claude_md.is_file() {
        let size = std::fs::metadata(&claude_md).map(|m| m.len()).unwrap_or(0);
        if size > 0 {
            out.signals.push(format!("CLAUDE.md (existing project instructions, {size} bytes)"));
        }
    }

    // Signal 3: .env with content.
    let env_path = root.join(".env");
    if env_path.is_file() {
        let size = std::fs::metadata(&env_path).map(|m| m.len()).unwrap_or(0);
        if size > 0 {
            // We don't run the secrets heuristic in Rust — install.py does the
            // canonical scan. Just flag presence.
            out.signals.push(format!(".env (with content, {size} bytes)"));
        }
    }

    // Signal 4: venv-like directory (.venv / venv / env) with pyvenv.cfg.
    for name in [".venv", "venv", "env"] {
        let candidate = root.join(name);
        if candidate.is_dir() && candidate.join("pyvenv.cfg").is_file() {
            out.signals.push(format!("{name}/ (Python virtualenv)"));
            break;
        }
    }

    // Signal 5: knowledge/ with at least one .md file (shallow scan).
    let knowledge_dir = root.join("knowledge");
    if knowledge_dir.is_dir() {
        if let Ok(rd) = std::fs::read_dir(&knowledge_dir) {
            // Shallow first — if there are direct .md files we're done.
            let mut found = false;
            for entry in rd.flatten() {
                let p = entry.path();
                if p.extension().is_some_and(|x| x == "md") {
                    found = true;
                    break;
                }
                // Single-level subdir scan: knowledge/concepts/*.md etc.
                if p.is_dir() {
                    if let Ok(sub_rd) = std::fs::read_dir(&p) {
                        if sub_rd.flatten().any(|e| {
                            e.path().extension().is_some_and(|x| x == "md")
                        }) {
                            found = true;
                            break;
                        }
                    }
                }
            }
            if found {
                out.signals.push("knowledge/ (with markdown files)".into());
            }
        }
    }

    out.has_signals = !out.signals.is_empty();
    out.summary = if out.has_signals {
        let n = out.signals.len();
        format!("{n} signal{} detected", if n == 1 { "" } else { "s" })
    } else {
        "no signals".into()
    };
    out
}

