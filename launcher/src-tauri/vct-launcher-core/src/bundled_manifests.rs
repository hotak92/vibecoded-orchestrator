// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! The launcher's bundled core-module manifests (`launcher/bundled_manifests/`),
//! embedded in the binary and materialized into `<vct_root>/bundled_manifests/`.
//!
//! ## Why (v0.2.97)
//!
//! `docs/features/01-launcher.md` and the directory's README said these
//! manifests "ship with the launcher binary and are copied to
//! `~/.vct/bundled_manifests/` on first launch", and every reader — the hub's
//! `/env` and catalog scan, `installed_module_manifest_paths`, the lifecycle
//! API — reads that directory. Nothing ever wrote it (known empty since
//! 2026-04-26, when the catalog grew built-in cards instead), and half the
//! files did not even parse, so a bundled module's settings never reached a
//! project. This module is the one load path: the files are embedded at
//! compile time (the binary a user installed carries exactly the manifests it
//! was built with — no clone discovery) and [`sync_bundled_manifests`] writes
//! them into the state directory on every hub/launcher start, so an update
//! refreshes them.

use std::path::{Path, PathBuf};

/// Every file of `launcher/bundled_manifests/*.json`, embedded. A test pins
/// this list against the directory, so a manifest added there cannot be
/// forgotten here.
pub const BUNDLED_MANIFESTS: &[(&str, &str)] = &[
    ("vct-code-embedding.json", include_str!("../../../bundled_manifests/vct-code-embedding.json")),
    ("vct-codegraph.json", include_str!("../../../bundled_manifests/vct-codegraph.json")),
    ("vct-hub-api.json", include_str!("../../../bundled_manifests/vct-hub-api.json")),
    ("vct-kg.json", include_str!("../../../bundled_manifests/vct-kg.json")),
    ("vct-search.json", include_str!("../../../bundled_manifests/vct-search.json")),
    ("vct-session-state.json", include_str!("../../../bundled_manifests/vct-session-state.json")),
];

/// `<vct_root>/bundled_manifests` — where every reader looks.
pub fn bundled_manifests_dir(vct_root: &Path) -> PathBuf {
    vct_root.join("bundled_manifests")
}

/// What one [`sync_bundled_manifests`] run did. A failure on one file never
/// stops the others (review R6 F49): with a read-only target holding a stale
/// copy, every OTHER manifest is still refreshed, and the failures are named
/// together in [`BundledSyncReport::warning`].
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct BundledSyncReport {
    /// File names written (new or refreshed).
    pub written: Vec<String>,
    /// Orphaned temp files removed (see [`sync_bundled_manifests`]).
    pub reaped: Vec<String>,
    /// One line per failure — the path and the OS error, never file content.
    pub errors: Vec<String>,
}

impl BundledSyncReport {
    /// The ONE aggregated warning for the caller's log: every failure,
    /// `None` when there was none.
    pub fn warning(&self) -> Option<String> {
        if self.errors.is_empty() {
            return None;
        }
        Some(format!(
            "{} bundled manifest step(s) failed (the other files were still synced): {}",
            self.errors.len(),
            self.errors.join("; ")
        ))
    }
}

/// A temp file this module leaves only when a process dies between its write
/// and its rename. It is kept at most this long even when its pid looks alive
/// (a write + rename takes microseconds; an older one is an orphan whose pid
/// has been recycled).
const ORPHAN_TMP_MAX_AGE: std::time::Duration = std::time::Duration::from_secs(600);

/// `.<bundled name>.tmp.<pid>` — the exact temp name [`sync_bundled_manifests`]
/// writes — parsed back to its pid. Anything else (another file, a name this
/// binary does not ship, a non-numeric suffix) is `None`: the reaper never
/// touches a file it did not create.
fn own_tmp_pid(file_name: &str) -> Option<u32> {
    let rest = file_name.strip_prefix('.')?;
    BUNDLED_MANIFESTS.iter().find_map(|(name, _)| {
        let pid = rest.strip_prefix(name)?.strip_prefix(".tmp.")?;
        if pid.is_empty() || !pid.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        pid.parse::<u32>().ok()
    })
}

/// Remove this module's orphaned temp files from `dir`: only names of the
/// `.<bundled name>.tmp.<pid>` shape, and only when that pid is not alive or
/// the file is older than [`ORPHAN_TMP_MAX_AGE`] — a concurrent writer's fresh
/// temp file is left for it to rename. Returns the names removed.
fn reap_orphaned_tmp_files(dir: &Path, errors: &mut Vec<String>) -> Vec<String> {
    let mut reaped = Vec::new();
    let Ok(entries) = std::fs::read_dir(dir) else {
        return reaped;
    };
    for entry in entries.flatten() {
        let Some(name) = entry.file_name().to_str().map(str::to_string) else {
            continue;
        };
        let Some(pid) = own_tmp_pid(&name) else {
            continue;
        };
        let Ok(meta) = std::fs::symlink_metadata(entry.path()) else {
            continue;
        };
        if !meta.is_file() {
            continue;
        }
        let too_old = meta
            .modified()
            .ok()
            .and_then(|m| std::time::SystemTime::now().duration_since(m).ok())
            .is_some_and(|age| age > ORPHAN_TMP_MAX_AGE);
        if crate::process::pid_is_alive(pid) && !too_old {
            continue;
        }
        match std::fs::remove_file(entry.path()) {
            Ok(()) => reaped.push(name),
            Err(e) => errors.push(format!("remove orphaned {}: {}", entry.path().display(), e)),
        }
    }
    reaped
}

/// Write the embedded manifests into `<vct_root>/bundled_manifests/`, each
/// only when its bytes differ (atomic temp + rename). EVERY file is attempted:
/// a failure is recorded in the report and the loop goes on. This module's
/// orphaned temp files (a process that died between write and rename) are
/// reaped first. A file there that this binary does not ship is left alone.
pub fn sync_bundled_manifests(vct_root: &Path) -> BundledSyncReport {
    let mut report = BundledSyncReport::default();
    let dir = bundled_manifests_dir(vct_root);
    if let Err(e) = std::fs::create_dir_all(&dir) {
        report.errors.push(format!("create {}: {}", dir.display(), e));
        return report;
    }
    report.reaped = reap_orphaned_tmp_files(&dir, &mut report.errors);
    for (name, body) in BUNDLED_MANIFESTS {
        let target = dir.join(name);
        if std::fs::read_to_string(&target).ok().as_deref() == Some(*body) {
            continue;
        }
        let tmp = dir.join(format!(".{}.tmp.{}", name, std::process::id()));
        if let Err(e) = std::fs::write(&tmp, body) {
            // A failed write can leave a partial temp file behind.
            let _ = std::fs::remove_file(&tmp);
            report.errors.push(format!("write {}: {}", tmp.display(), e));
            continue;
        }
        if let Err(e) = std::fs::rename(&tmp, &target) {
            let _ = std::fs::remove_file(&tmp);
            report.errors.push(format!("rename into {}: {}", target.display(), e));
            continue;
        }
        report.written.push((*name).to_string());
    }
    report
}

/// Whether `manifest_path` is one of the bundled core manifests (the hub treats
/// those as installed for every project — the catalog's "bundled" kind).
pub fn is_bundled_manifest_path(vct_root: &Path, manifest_path: &Path) -> bool {
    manifest_path.parent() == Some(bundled_manifests_dir(vct_root).as_path())
        && manifest_path
            .file_name()
            .and_then(|n| n.to_str())
            .is_some_and(|n| BUNDLED_MANIFESTS.iter().any(|(name, _)| *name == n))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source_dir() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..").join("bundled_manifests")
    }

    /// The embedded list IS the directory — no manifest added there is left
    /// out here (and none listed here is missing there).
    #[test]
    fn the_embedded_list_matches_the_bundled_directory() {
        let mut on_disk: Vec<String> = std::fs::read_dir(source_dir())
            .unwrap()
            .flatten()
            .filter_map(|e| e.file_name().to_str().map(str::to_string))
            .filter(|n| n.ends_with(".json"))
            .collect();
        on_disk.sort();
        let embedded: Vec<String> = BUNDLED_MANIFESTS.iter().map(|(n, _)| n.to_string()).collect();
        assert_eq!(on_disk, embedded);
    }

    /// Every bundled manifest parses with the real parser — three did not
    /// (a half `mcp_registration` block without `mcp_name`), which made the
    /// hub drop them silently.
    #[test]
    fn every_bundled_manifest_parses_strictly() {
        let _strict = crate::test_env::env_guard(&[("VCT_LAUNCHER_STRICT_MANIFEST", Some("1"))]);
        for (name, body) in BUNDLED_MANIFESTS {
            crate::manifest::ModuleManifest::from_json(body)
                .unwrap_or_else(|e| panic!("{} does not parse: {}", name, e));
        }
    }

    /// v0.2.97 (lane T): the hub's port is a setting (`VCT_HUB_PORT`) and the
    /// hub walks past a taken port, so the hub-api manifest's
    /// `runtime.health_check.url` and `provides[http_api].base_url` name
    /// `{hub_port}` — resolved through `services::hub_port`, the ladder the
    /// supervisor uses — and never the default as a literal. With `:7700`
    /// spelled out, this resolves to the default port and fails.
    #[test]
    fn hub_api_urls_follow_the_running_hubs_port() {
        let guard = crate::test_env::state_dir_guard_with(&[("VCT_HUB_PORT", None)]);
        std::fs::write(guard.path().join("hub.port"), "8123\n").unwrap();
        let (_, body) = BUNDLED_MANIFESTS
            .iter()
            .find(|(name, _)| *name == "vct-hub-api.json")
            .unwrap();
        let manifest = crate::manifest::ModuleManifest::from_json(body).unwrap();
        let ctx = crate::manifest::PlaceholderCtx::new(&manifest.id);

        let health = manifest.runtime.health_check.as_ref().and_then(|h| h.url.as_deref());
        assert_eq!(
            health.map(|u| ctx.resolve(u)).as_deref(),
            Some("http://127.0.0.1:8123/api/v1/health")
        );
        let base = manifest
            .provides
            .iter()
            .find(|p| p["kind"] == "http_api")
            .and_then(|p| p["base_url"].as_str());
        assert_eq!(base.map(|u| ctx.resolve(u)).as_deref(), Some("http://127.0.0.1:8123/api/v1"));
    }

    /// v0.2.97 (lane T): the code-embedding manifest's health URL and its
    /// `CODE_EMBED_PORT` setting default name the port the SERVICE actually
    /// defaults to — `server.py`'s `os.getenv("CODE_EMBED_PORT", …)` and the
    /// compose file's `${CODE_EMBED_PORT:-…}`. The URL said 11438, a port
    /// nothing served.
    #[test]
    fn code_embedding_manifest_names_the_services_real_default_port() {
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
        let default_after = |file: &str, marker: &str, end: char| -> String {
            let text = std::fs::read_to_string(repo.join(file)).unwrap();
            let at = text.find(marker).unwrap_or_else(|| panic!("{marker:?} not in {file}"));
            let rest = &text[at + marker.len()..];
            rest[..rest.find(end).unwrap()].to_string()
        };
        let service = default_after(
            "claude_mcp_servers/code_embedding_service/server.py",
            "os.getenv(\"CODE_EMBED_PORT\", \"",
            '"',
        );
        let compose = default_after("infrastructure/docker-compose.yml", "${CODE_EMBED_PORT:-", '}');
        assert_eq!(service, compose, "server.py and compose disagree on the default");

        let (_, body) = BUNDLED_MANIFESTS
            .iter()
            .find(|(name, _)| *name == "vct-code-embedding.json")
            .unwrap();
        let manifest: serde_json::Value = serde_json::from_str(body).unwrap();
        let setting = manifest["settings"]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| s["key"] == "CODE_EMBED_PORT")
            .unwrap();
        assert_eq!(setting["default"].to_string(), service, "setting default");
        // v0.2.97 (lane W): the URL names the port through `{code_embed_port}`,
        // which resolves to that default on a machine with no override and to
        // the override when there is one.
        let url = manifest["runtime"]["health_check"]["url"].as_str().unwrap();
        assert_eq!(url, "http://localhost:{code_embed_port}/health", "health_check.url");
        let _g = crate::test_env::state_dir_guard();
        let ctx = crate::manifest::PlaceholderCtx::new("vct-code-embedding");
        assert_eq!(ctx.resolve(url), format!("http://localhost:{service}/health"));
        let db = crate::db::Db::open().unwrap();
        db.app_state_set(
            crate::services::service_endpoints::APP_STATE_KEY_CODE_EMBED_PORT,
            "21440",
        )
        .unwrap();
        assert_eq!(ctx.resolve(url), "http://localhost:21440/health");
    }

    #[test]
    fn sync_writes_every_manifest_once_and_is_idempotent() {
        let tmp = tempfile::tempdir().unwrap();
        let first = sync_bundled_manifests(tmp.path()).written;
        assert_eq!(first.len(), BUNDLED_MANIFESTS.len());
        for (name, body) in BUNDLED_MANIFESTS {
            let p = bundled_manifests_dir(tmp.path()).join(name);
            assert_eq!(std::fs::read_to_string(&p).unwrap(), *body);
            assert!(is_bundled_manifest_path(tmp.path(), &p));
        }
        assert_eq!(sync_bundled_manifests(tmp.path()), BundledSyncReport::default(), "nothing rewritten");
        // A stale copy is refreshed; a foreign file is left alone.
        let stale = bundled_manifests_dir(tmp.path()).join("vct-kg.json");
        std::fs::write(&stale, "{}").unwrap();
        let foreign = bundled_manifests_dir(tmp.path()).join("user-extra.json");
        std::fs::write(&foreign, "{}").unwrap();
        assert_eq!(sync_bundled_manifests(tmp.path()).written, vec!["vct-kg.json".to_string()]);
        assert_eq!(std::fs::read_to_string(&foreign).unwrap(), "{}");
        assert!(!is_bundled_manifest_path(tmp.path(), &foreign));
    }

    /// A pid no process has (above every OS's pid ceiling, below the
    /// `pid_is_alive` sentinel guard) — "a writer that died".
    const DEAD_PID: u32 = 2_147_483_000;

    /// Review R6 F49: a failure on ONE file used to return before the rest
    /// were checked. Here `vct-kg.json` cannot be replaced (a non-empty
    /// directory sits at its path) — the other five are still written, the
    /// one failure is named in the single warning, and no temp file is left.
    #[test]
    fn one_failing_file_does_not_stop_the_others() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = bundled_manifests_dir(tmp.path());
        let blocker = dir.join("vct-kg.json");
        std::fs::create_dir_all(&blocker).unwrap();
        std::fs::write(blocker.join("keep"), "x").unwrap();

        let report = sync_bundled_manifests(tmp.path());

        let expected: Vec<String> = BUNDLED_MANIFESTS
            .iter()
            .map(|(n, _)| n.to_string())
            .filter(|n| n != "vct-kg.json")
            .collect();
        assert_eq!(report.written, expected);
        assert_eq!(report.errors.len(), 1, "{:?}", report.errors);
        assert!(report.errors[0].contains("vct-kg.json"), "{:?}", report.errors);
        let warning = report.warning().expect("one aggregated warning");
        assert!(warning.contains("vct-kg.json") && warning.starts_with("1 bundled manifest step(s) failed"));
        for (name, body) in BUNDLED_MANIFESTS.iter().filter(|(n, _)| *n != "vct-kg.json") {
            assert_eq!(std::fs::read_to_string(dir.join(name)).unwrap(), *body);
        }
        let leftovers: Vec<String> = std::fs::read_dir(&dir)
            .unwrap()
            .flatten()
            .filter_map(|e| e.file_name().to_str().map(str::to_string))
            .filter(|n| n.contains(".tmp."))
            .collect();
        assert!(leftovers.is_empty(), "temp files left: {:?}", leftovers);
        assert_eq!(std::fs::read_to_string(blocker.join("keep")).unwrap(), "x");
    }

    /// Review R6 F49: a process that died between write and rename left
    /// `.<name>.tmp.<pid>` forever. Now: a dead writer's temp file and one
    /// older than the safe age are reaped; a live writer's FRESH one is left.
    #[test]
    fn orphaned_temp_files_are_reaped_and_a_live_fresh_one_is_left() {
        let tmp = tempfile::tempdir().unwrap();
        assert!(sync_bundled_manifests(tmp.path()).errors.is_empty());
        let dir = bundled_manifests_dir(tmp.path());
        let me = std::process::id();

        let dead = format!(".vct-kg.json.tmp.{}", DEAD_PID);
        std::fs::write(dir.join(&dead), "partial").unwrap();
        let old = format!(".vct-search.json.tmp.{}", me);
        let old_file = std::fs::File::create(dir.join(&old)).unwrap();
        old_file
            .set_modified(std::time::SystemTime::now() - ORPHAN_TMP_MAX_AGE - std::time::Duration::from_secs(60))
            .unwrap();
        drop(old_file);
        let fresh = format!(".vct-codegraph.json.tmp.{}", me);
        std::fs::write(dir.join(&fresh), "in flight").unwrap();

        let report = sync_bundled_manifests(tmp.path());

        let mut reaped = report.reaped.clone();
        reaped.sort();
        let mut expected = vec![dead.clone(), old.clone()];
        expected.sort();
        assert_eq!(reaped, expected);
        assert!(report.errors.is_empty() && report.written.is_empty(), "{:?}", report);
        assert!(!dir.join(&dead).exists() && !dir.join(&old).exists());
        assert_eq!(std::fs::read_to_string(dir.join(&fresh)).unwrap(), "in flight");
    }

    /// Leave-alone half: the reaper touches ONLY its own exact temp shape.
    #[test]
    fn the_reaper_never_touches_a_file_it_did_not_create() {
        let tmp = tempfile::tempdir().unwrap();
        assert!(sync_bundled_manifests(tmp.path()).errors.is_empty());
        let dir = bundled_manifests_dir(tmp.path());
        let foreign_files = [
            format!(".user-extra.json.tmp.{}", DEAD_PID), // not a bundled name
            format!("vct-kg.json.tmp.{}", DEAD_PID),       // no leading dot
            ".vct-kg.json.tmp.abc".to_string(),            // not a pid
            ".vct-kg.json.tmp.".to_string(),               // empty pid
            format!(".vct-kg.json.tmp.{}.bak", DEAD_PID),  // extra suffix
            format!(".vct-kg.json.swp.{}", DEAD_PID),      // another tool's temp
            "user-extra.json".to_string(),
        ];
        for name in &foreign_files {
            std::fs::write(dir.join(name), "mine").unwrap();
        }
        let a_dir = dir.join(format!(".vct-kg.json.tmp.{}", DEAD_PID + 1));
        std::fs::create_dir(&a_dir).unwrap();

        let report = sync_bundled_manifests(tmp.path());

        assert!(report.reaped.is_empty(), "{:?}", report.reaped);
        assert!(report.errors.is_empty(), "{:?}", report.errors);
        for name in &foreign_files {
            assert_eq!(std::fs::read_to_string(dir.join(name)).unwrap(), "mine", "{}", name);
        }
        assert!(a_dir.is_dir());
    }

    #[test]
    fn own_tmp_pid_parses_only_the_exact_shape() {
        assert_eq!(own_tmp_pid(".vct-kg.json.tmp.42"), Some(42));
        assert_eq!(own_tmp_pid(".vct-session-state.json.tmp.7"), Some(7));
        assert_eq!(own_tmp_pid("vct-kg.json.tmp.42"), None);
        assert_eq!(own_tmp_pid(".vct-kg.json.tmp.4x"), None);
        assert_eq!(own_tmp_pid(".vct-kg.json.tmp.+4"), None);
        assert_eq!(own_tmp_pid(".other.json.tmp.42"), None);
        assert_eq!(own_tmp_pid(".vct-kg.json.tmp.99999999999"), None);
    }

    /// An unwritable ROOT is one error, not a panic, and nothing else runs.
    #[test]
    fn an_uncreatable_directory_is_one_error() {
        let tmp = tempfile::tempdir().unwrap();
        let root_is_a_file = tmp.path().join("root");
        std::fs::write(&root_is_a_file, "x").unwrap();
        let report = sync_bundled_manifests(&root_is_a_file);
        assert!(report.written.is_empty() && report.reaped.is_empty());
        assert_eq!(report.errors.len(), 1, "{:?}", report.errors);
        assert!(report.warning().unwrap().contains("create"));
    }

    /// Review R6 F50: the bundled set pointed at `vct-ollama`, a manifest
    /// deleted in v0.2.11 (`vct-kg.json` `depends_on`, and the scan rules'
    /// `[deprecated.ollama] opt_in_manifest`). Every module reference the
    /// bundled set or the scan rules make must resolve to a manifest this
    /// binary embeds: each file's `id` is its file stem, every `depends_on`
    /// id is an embedded id, and every `opt_in_manifest` is
    /// `launcher/bundled_manifests/<embedded file>`.
    #[test]
    fn every_referenced_module_id_is_an_embedded_manifest() {
        let mut ids = Vec::new();
        for (name, body) in BUNDLED_MANIFESTS {
            let v: serde_json::Value = serde_json::from_str(body).unwrap();
            let id = v["id"].as_str().unwrap_or_else(|| panic!("{} has no id", name)).to_string();
            assert_eq!(format!("{}.json", id), *name, "a bundled manifest's id is its file stem");
            ids.push(id);
        }
        let mut dangling = Vec::new();
        for (name, body) in BUNDLED_MANIFESTS {
            let v: serde_json::Value = serde_json::from_str(body).unwrap();
            let deps = v["requirements"]["depends_on"].as_array().cloned().unwrap_or_default();
            for dep in deps {
                let dep = dep.as_str().unwrap_or_default().to_string();
                if !ids.contains(&dep) {
                    dangling.push(format!("{} requirements.depends_on -> {:?}", name, dep));
                }
            }
        }
        for (mcp, dep) in crate::mcp_scan_rules::deprecated_default_mcps() {
            let Some(path) = dep.opt_in_manifest.as_deref() else {
                continue;
            };
            let resolves = path
                .strip_prefix("launcher/bundled_manifests/")
                .is_some_and(|file| BUNDLED_MANIFESTS.iter().any(|(n, _)| *n == file));
            if !resolves {
                dangling.push(format!("mcp_scan_rules [deprecated.{}] opt_in_manifest -> {:?}", mcp, path));
            }
        }
        assert!(dangling.is_empty(), "references to manifests that are not embedded: {:#?}", dangling);
    }

    /// v0.2.97 review R6: every MCP claim an embedded manifest makes names
    /// an MCP a bundled module actually serves. `vct-codegraph` registered a
    /// `codegraph` MCP nothing provides (its tools belong to vct-kg's
    /// `weaviate-kg`), and uninstall DEREGISTERS the MCP a registration
    /// names — so the fix is no registration, never a second claim on
    /// `weaviate-kg`. Python twin: `tests/test_v0297_manifest_mcp_truth.py`.
    #[test]
    fn every_mcp_claim_names_an_mcp_a_bundled_module_serves() {
        let registered = crate::mcp_scan_rules::default_mcp_entry_names();
        let parsed: Vec<(&str, serde_json::Value)> = BUNDLED_MANIFESTS
            .iter()
            .map(|(name, body)| (*name, serde_json::from_str(body).unwrap()))
            .collect();
        let runs_mcp = |v: &serde_json::Value| {
            v["runtime"]["type"].as_str().unwrap_or_default().starts_with("mcp")
        };
        let served: Vec<String> = parsed
            .iter()
            .filter(|(_, v)| runs_mcp(v))
            .filter_map(|(_, v)| v["mcp_registration"]["mcp_name"].as_str())
            .filter(|n| registered.iter().any(|r| r == n))
            .map(str::to_string)
            .collect();
        let mut problems = Vec::new();
        for (name, v) in &parsed {
            let reg = v["mcp_registration"]["mcp_name"].as_str();
            if let Some(mcp) = reg {
                if !runs_mcp(v) {
                    problems.push(format!("{name}: registers {mcp:?} but runs no MCP server"));
                }
                if !registered.iter().any(|r| r == mcp) {
                    problems.push(format!("{name}: registers {mcp:?}, which VCO never registers"));
                }
            }
            if v["uninstall"]["deregister_mcp"].as_bool() == Some(true) && reg.is_none() {
                problems.push(format!("{name}: deregister_mcp with no MCP of its own"));
            }
            for entry in v["provides"].as_array().cloned().unwrap_or_default() {
                if entry["kind"] == "mcp_tools" {
                    let prefix = entry["tool_prefix"].as_str().unwrap_or_default();
                    if !served.iter().any(|s| s == prefix) {
                        problems.push(format!("{name}: mcp_tools under unserved MCP {prefix:?}"));
                    }
                }
            }
        }
        assert!(problems.is_empty(), "false MCP claims in embedded manifests: {:#?}", problems);
    }
}
