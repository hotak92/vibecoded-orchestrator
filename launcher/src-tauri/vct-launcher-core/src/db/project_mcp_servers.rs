//! Per-project MCP server registry.
//!
//! Mirrors `<folder>/.claude/settings.json::mcpServers` and
//! `<folder>/.mcp.json` (Anthropic project-scoped MCP config) into the
//! `project_mcp_servers` table so the launcher's "Custom MCP" tab can
//! render user-added entries without re-parsing JSON at every render.
//!
//! Schema: `migrations/010_project_mcp_servers.sql`. Cascade-delete on
//! `projects.id` mirrors the rest of the per-project tables.
//!
//! KNOWN_ISSUES.md (v0.2.x) entry resolved:
//!     "Custom MCP tab is not populated by initial project registration —
//!      `project_state_populate` mirrors `.claude/settings.json::mcpServers`
//!      into the launcher's per-project DB on `create_project_v2`, but
//!      doesn't flag user-added entries (anything beyond bundled
//!      `weaviate-kg` / `ollama` / `search` / `code-embedding` /
//!      `playwright`) as `is_user_added=true`. Tab reads with that
//!      filter so user-added servers show up blank."
//!
//! The bundled set is defined as the canonical source-of-truth in
//! `BUNDLED_MCP_NAMES` below. `is_user_added` is computed by the
//! populate step; this module only persists the flag.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;

use super::Db;

/// MCP server names shipped by the orchestrator. Anything else gets
/// `is_user_added = true` and surfaces in the Custom MCP tab.
///
/// Source-of-truth: `vco_lib/mcp_scan_rules.toml` [bundled].all_names
/// (v0.2.83 WP-B5). This compiled `&[&str]` is the zero-runtime-failure
/// mirror (the launcher is the repair tool — it must build this set even
/// when the project venv / on-disk .toml is unavailable, exactly the
/// `bundled_versions` precedent). The same-crate drift test
/// `bundled_mcp_names_matches_table` binds it to
/// `crate::mcp_scan_rules::bundled_mcp_names()`, so a table edit that isn't
/// mirrored here trips a test in this crate — no silent drift. Keep sorted.
///
/// References:
///  - Uninstall scrub uses a DISTINCT set — `mcp_scan_rules.toml`
///    [bundled].uninstall_scrub_names (install.py) — NOT this list, because
///    the scrub rationale differs (code-embedding is a backend service,
///    vct-coordination is Pro-tier). Don't conflate them.
///  - `vct-coordination` is an orchestrator-shipped MCP (Pro-tier).
///  - `playwright` is the default-enabled browser-automation MCP
///    (`KNOWN_ISSUES.md` "First-install grew by ~150 MB for Playwright
///    MCP" entry).
pub const BUNDLED_MCP_NAMES: &[&str] = &[
    "code-embedding",
    "excalidraw",
    "mermaid",
    "ollama",
    "playwright",
    "search",
    "vct-coordination",
    "weaviate-kg",
];

/// MCPs that are bundled but DEFAULT-DISABLED per project. The launcher's
/// project-create flow registers them with `enabled=false` so the user
/// has to opt in via the launcher GUI before they fire.
///
/// Plan §3 Phase 1 item 2: "mermaid — Default-enabled: off (opt-in per
/// project, unlike Playwright). Reasoning: not all projects need diagrams;
/// mermaid MCP boot adds ~150 ms."
///
/// Plan §3 Phase 2 (2026-05-25): excalidraw follows the same posture —
/// opt-in per project. The wrapper spawns a vendored Node MCP
/// (~10 MB on disk via the in-tree fork) plus its own subprocess on
/// project use, so default-off keeps idle projects free of the cost.
///
/// Keep this list narrow — most bundled MCPs should default to on so the
/// orchestrator "just works". This list is the explicit opt-out for the
/// few that don't apply universally.
///
/// Source-of-truth: `vco_lib/mcp_scan_rules.toml` [bundled].default_disabled
/// (v0.2.83 WP-B5), same compiled-mirror + drift-test discipline as
/// `BUNDLED_MCP_NAMES` (see `bundled_mcp_default_disabled_matches_table`).
pub const BUNDLED_MCP_DEFAULT_DISABLED: &[&str] = &[
    "excalidraw",
    "mermaid",
];

/// True iff `name` is bundled AND ships default-disabled (per
/// `BUNDLED_MCP_DEFAULT_DISABLED`). Used by the per-project install/
/// populate flow to decide the initial `enabled` flag for a new row.
pub fn is_default_disabled_mcp(name: &str) -> bool {
    BUNDLED_MCP_DEFAULT_DISABLED.iter().any(|b| *b == name)
}

/// True iff `name` is one of the bundled orchestrator MCPs.
pub fn is_bundled_mcp(name: &str) -> bool {
    BUNDLED_MCP_NAMES.iter().any(|b| *b == name)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectMcpServer {
    pub project_id: String,
    pub mcp_name: String,
    pub is_user_added: bool,
    pub source: String,
    pub source_module: Option<String>,
    pub source_file: Option<String>,
    pub enabled: bool,
    pub command: Option<String>,
    pub config: JsonValue,
    pub installed_at: i64,
    pub updated_at: i64,
}

/// Reserved `config_json` key carrying a row's retirement badge.
///
/// v0.2.91 WP-E item 2. Written by [`Db::retire_project_mcp_server`], read by
/// the convergence engine to make the retire pass IDEMPOTENT (a row that
/// already carries the badge for the same `removed_in` is left alone — which
/// also means a deliberate user re-enable after retirement is never undone).
/// The underscore prefix marks it as VCO-owned metadata inside a blob that
/// otherwise mirrors the user's MCP entry.
pub const MCP_RETIRED_CONFIG_KEY: &str = "_vct_retired";

/// Outcome of [`Db::register_project_mcp_server_honoring_defaults`].
///
/// `default_disabled_applied` is true only when THAT call created the row AND
/// the name ships default-disabled — i.e. exactly when `enabled` was flipped
/// to 0 post-UPSERT. Callers use it for reporting; nothing branches on it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct McpSeedOutcome {
    pub default_disabled_applied: bool,
}

const VALID_SOURCE: &[&str] = &["bundled", "user", "paid-module", "project"];

fn json_from_str(s: &str) -> JsonValue {
    serde_json::from_str(s).unwrap_or(JsonValue::Object(serde_json::Map::new()))
}

fn json_to_str(v: &JsonValue) -> String {
    serde_json::to_string(v).unwrap_or_else(|_| "{}".to_string())
}

/// The `$._vct_retired` badge currently stored on a row, if any.
///
/// Read under the CALLER's lock guard so the read-modify-write of
/// `config_json` it feeds cannot interleave with another writer — the same
/// discipline [`Db::retire_project_mcp_server`] uses for the write half.
///
/// A missing row, an unreadable row, or a `config_json` that is not an object
/// all resolve to `None`: no badge to preserve. Never an error — a preserve
/// step must not be able to fail a registration.
fn stored_retirement_badge(
    conn: &rusqlite::Connection,
    project_id: &str,
    mcp_name: &str,
) -> Option<JsonValue> {
    let cfg_s: String = conn
        .query_row(
            "SELECT config_json FROM project_mcp_servers
             WHERE project_id = ?1 AND mcp_name = ?2",
            params![project_id, mcp_name],
            |r| r.get(0),
        )
        .optional()
        .ok()
        .flatten()?;
    json_from_str(&cfg_s).get(MCP_RETIRED_CONFIG_KEY).cloned()
}

/// `config` with a previously-stored retirement badge merged back in.
///
/// v0.2.91 wave-3 (MAJOR-3). The badge is DURABLE VCO-owned metadata living
/// inside a blob that otherwise mirrors the user's MCP entry, so the UPSERT's
/// `config_json = excluded.config_json` wiped it — and the two writers run
/// back-to-back in `apply_post_bundle_steps` (populate, then converge). The
/// composed effect was a silent loop: populate re-UPSERTs a deprecated name
/// still present in the project's files → badge gone → the engine no longer
/// sees `AlreadyRetired` → it retires the row AGAIN, re-disabling an MCP the
/// user had deliberately re-enabled, and writing a fresh audit row, on EVERY
/// bundle update.
///
/// Preserving it here rather than in the seeding helper is deliberate: the raw
/// UPSERT is already the ONE home for "durable row state survives
/// re-registration" (it is where `enabled` is kept off the `DO UPDATE` list),
/// and every caller — populate, the registration DB-sync, the engine's own
/// seed — needs the same guarantee.
///
/// An incoming config that already carries the key wins: that is a deliberate
/// re-badge by a caller that knows what it is writing.
fn with_preserved_retirement_badge(
    conn: &rusqlite::Connection,
    project_id: &str,
    mcp_name: &str,
    config: &JsonValue,
) -> JsonValue {
    if config.get(MCP_RETIRED_CONFIG_KEY).is_some() {
        return config.clone();
    }
    let Some(badge) = stored_retirement_badge(conn, project_id, mcp_name) else {
        return config.clone();
    };
    let mut merged = config.clone();
    match merged.as_object_mut() {
        Some(obj) => {
            obj.insert(MCP_RETIRED_CONFIG_KEY.to_string(), badge);
        }
        None => {
            // Incoming config is not an object (legacy/hand-edited shape).
            // Same rescue as the retire path: keep it under a sibling key.
            let mut obj = serde_json::Map::new();
            obj.insert("_prior_config".to_string(), merged.clone());
            obj.insert(MCP_RETIRED_CONFIG_KEY.to_string(), badge);
            merged = JsonValue::Object(obj);
        }
    }
    merged
}

impl Db {
    /// Idempotent UPSERT of a single MCP server entry. Preserves the
    /// `enabled` column on conflict (mirrors register_project_agent /
    /// register_project_hook contract — user toggles survive re-populate)
    /// and, since v0.2.91 wave-3, the `$._vct_retired` badge inside
    /// `config_json` — see [`with_preserved_retirement_badge`] for why the two
    /// belong in the same home.
    ///
    /// `is_user_added` is the discriminator the Custom MCP tab filters
    /// on. Caller computes it via `is_bundled_mcp(name)`.
    #[allow(clippy::too_many_arguments)]
    pub fn register_project_mcp_server(
        &self,
        project_id: &str,
        mcp_name: &str,
        is_user_added: bool,
        source: &str,
        source_module: Option<&str>,
        source_file: Option<&str>,
        command: Option<&str>,
        config: &JsonValue,
    ) -> Result<ProjectMcpServer, String> {
        if !VALID_SOURCE.iter().any(|s| *s == source) {
            return Err(format!(
                "invalid mcp.source: '{}' (allowed: {:?})",
                source, VALID_SOURCE
            ));
        }
        let now = Utc::now().timestamp_millis();
        let guard = self.lock();
        // v0.2.91 wave-3 (MAJOR-3): the durable retirement badge survives
        // re-registration, exactly like `enabled` does. One indexed lookup on
        // an O(10s)-row table per registration — paid unconditionally on
        // purpose: gating it on "is this name currently deprecated?" would
        // couple the preservation invariant to a table that moves, which is
        // the double-gate shape that made the migration-010 backfill useless.
        let effective_config =
            with_preserved_retirement_badge(&guard, project_id, mcp_name, config);
        let cfg = json_to_str(&effective_config);
        guard
            .execute(
                "INSERT INTO project_mcp_servers
                 (project_id, mcp_name, is_user_added, source, source_module,
                  source_file, enabled, command, config_json,
                  installed_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, 1, ?7, ?8, ?9, ?9)
                 ON CONFLICT(project_id, mcp_name) DO UPDATE SET
                    is_user_added = excluded.is_user_added,
                    source        = excluded.source,
                    source_module = excluded.source_module,
                    source_file   = excluded.source_file,
                    command       = excluded.command,
                    config_json   = excluded.config_json,
                    updated_at    = excluded.updated_at",
                params![
                    project_id,
                    mcp_name,
                    is_user_added as i32,
                    source,
                    source_module,
                    source_file,
                    command,
                    cfg,
                    now,
                ],
            )
            .map_err(|e| format!("register_project_mcp_server: {}", e))?;
        Ok(ProjectMcpServer {
            project_id: project_id.to_string(),
            mcp_name: mcp_name.to_string(),
            is_user_added,
            source: source.to_string(),
            source_module: source_module.map(str::to_string),
            source_file: source_file.map(str::to_string),
            enabled: true,
            command: command.map(str::to_string),
            config: effective_config,
            installed_at: now,
            updated_at: now,
        })
    }

    /// Idempotent UPSERT that also applies the fresh-insert default-disabled
    /// rule — v0.2.91 WP-E item 3, the ONE home for that discipline.
    ///
    /// Before this, the discipline lived inline in
    /// `project_state_populate::populate_mcp_servers` ONLY. The other seeding
    /// path — `mcp_registration::register_default_orchestrator_mcps`' DB-sync
    /// — called the raw UPSERT, whose SQL inserts `enabled = 1`
    /// unconditionally. Result: on the orchestrator ROOT project (the only
    /// project that path ever writes) `mermaid` and `excalidraw` landed
    /// `enabled = 1`, contradicting [`BUNDLED_MCP_DEFAULT_DISABLED`] and
    /// `docs/GETTING_STARTED.md`'s "registered but default-disabled per
    /// project" claim. Two seeding paths, one discipline, one copy of it.
    ///
    /// Contract (identical to the pre-extraction populate behaviour):
    ///   * The UPSERT itself NEVER writes `enabled` on conflict, so a user
    ///     toggle survives re-registration (pinned by
    ///     `upsert_preserves_enabled_flag_on_re_register`).
    ///   * `enabled = 0` is applied ONLY on a fresh insert of a BUNDLED
    ///     default-disabled name. A user-added row that happens to be named
    ///     `mermaid` is never touched (provenance wins).
    ///   * The existence probe runs only when it can change the outcome, and
    ///     a probe ERROR resolves to "not fresh" — leaving `enabled` alone is
    ///     the conservative branch (probe failure is never evidence).
    ///
    /// ACCEPTED RESIDUE (v0.2.91 wave-3, MINOR-7 — a decision, not an
    /// oversight): this closes the defect for rows created from now on. Roots
    /// installed BEFORE it already carry `mermaid` / `excalidraw` rows at
    /// `enabled = 1`, and those are deliberately NOT remediated. There is no
    /// evidence that distinguishes "the old bug wrote 1" from "the user
    /// enabled it on purpose" — the column holds the same value either way —
    /// so a remediation pass would silently disable diagram MCPs some users
    /// deliberately turned on. Flipping a user-visible toggle on a guess is
    /// worse than a stale default on installs that already exist; the toggle
    /// is one click in the launcher's Diagrams tab for anyone who wants it off.
    #[allow(clippy::too_many_arguments)]
    pub fn register_project_mcp_server_honoring_defaults(
        &self,
        project_id: &str,
        mcp_name: &str,
        is_user_added: bool,
        source: &str,
        source_module: Option<&str>,
        source_file: Option<&str>,
        command: Option<&str>,
        config: &JsonValue,
    ) -> Result<McpSeedOutcome, String> {
        // Only bundled, default-disabled names can have their initial
        // `enabled` flipped — skip the probe entirely otherwise (keeps the
        // per-row cost identical to the pre-extraction inline block).
        let candidate = !is_user_added && is_default_disabled_mcp(mcp_name);
        let was_fresh_insert = if candidate {
            // Probe failure → "not fresh" → don't touch `enabled`.
            !self
                .project_mcp_server_exists(project_id, mcp_name)
                .unwrap_or(true)
        } else {
            false
        };

        self.register_project_mcp_server(
            project_id,
            mcp_name,
            is_user_added,
            source,
            source_module,
            source_file,
            command,
            config,
        )?;

        // Applied AFTER the upsert so the SQL stays a plain
        // `enabled = 1`-on-INSERT statement.
        if was_fresh_insert {
            self.set_project_mcp_server_enabled(project_id, mcp_name, false)?;
        }
        Ok(McpSeedOutcome {
            default_disabled_applied: was_fresh_insert,
        })
    }

    /// All MCP servers for a project. Custom MCP tab can post-filter on
    /// `is_user_added` client-side (single round-trip is fine here:
    /// per-project MCP rows are O(10s), not O(thousands)).
    pub fn list_project_mcp_servers(
        &self,
        project_id: &str,
    ) -> Result<Vec<ProjectMcpServer>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT project_id, mcp_name, is_user_added, source, source_module,
                        source_file, enabled, command, config_json,
                        installed_at, updated_at
                 FROM project_mcp_servers WHERE project_id = ?1
                 ORDER BY mcp_name ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], |r| {
                let cfg_s: String = r.get(8)?;
                let enabled_i: i32 = r.get(6)?;
                let user_i: i32 = r.get(2)?;
                Ok(ProjectMcpServer {
                    project_id: r.get(0)?,
                    mcp_name: r.get(1)?,
                    is_user_added: user_i != 0,
                    source: r.get(3)?,
                    source_module: r.get(4)?,
                    source_file: r.get(5)?,
                    enabled: enabled_i != 0,
                    command: r.get(7)?,
                    config: json_from_str(&cfg_s),
                    installed_at: r.get(9)?,
                    updated_at: r.get(10)?,
                })
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    /// Custom MCP tab feed: only entries flagged user-added.
    pub fn list_user_added_mcp_servers(
        &self,
        project_id: &str,
    ) -> Result<Vec<ProjectMcpServer>, String> {
        Ok(self
            .list_project_mcp_servers(project_id)?
            .into_iter()
            .filter(|m| m.is_user_added)
            .collect())
    }

    pub fn set_project_mcp_server_enabled(
        &self,
        project_id: &str,
        mcp_name: &str,
        enabled: bool,
    ) -> Result<(), String> {
        let guard = self.lock();
        let n = guard
            .execute(
                "UPDATE project_mcp_servers SET enabled = ?1, updated_at = ?2
                 WHERE project_id = ?3 AND mcp_name = ?4",
                params![
                    enabled as i32,
                    Utc::now().timestamp_millis(),
                    project_id,
                    mcp_name
                ],
            )
            .map_err(|e| format!("set_project_mcp_server_enabled: {}", e))?;
        if n == 0 {
            return Err(format!(
                "mcp server '{}' not registered for project {}",
                mcp_name, project_id
            ));
        }
        Ok(())
    }

    /// Retire a bundled row: `enabled = 0` **plus** a durable retirement badge
    /// merged into `config_json` under [`MCP_RETIRED_CONFIG_KEY`]. The row is
    /// NEVER deleted (v0.2.91 WP-E item 2, no-auto-destroy: actual deletion
    /// stays behind install.py's consent-gated `--remove-deprecated-mcps`).
    ///
    /// Read-modify-write of `config_json` happens under ONE lock guard so a
    /// concurrent writer cannot lose the badge or the merge.
    ///
    /// `badge` is stored verbatim; the caller composes it (the engine's
    /// `retired_badge`) so the version + reason come from the shared
    /// `mcp_scan_rules.toml` `[deprecated.*]` registry rather than from
    /// prose typed at the DB layer.
    ///
    /// Returns `true` when a row was updated, `false` when no such row exists
    /// (a safe no-op — never an error, so a converge pass over a project that
    /// dropped the row mid-sweep does not fail).
    pub fn retire_project_mcp_server(
        &self,
        project_id: &str,
        mcp_name: &str,
        badge: &JsonValue,
    ) -> Result<bool, String> {
        let guard = self.lock();
        let existing: Option<String> = guard
            .query_row(
                "SELECT config_json FROM project_mcp_servers
                 WHERE project_id = ?1 AND mcp_name = ?2",
                params![project_id, mcp_name],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| format!("retire_project_mcp_server(select): {}", e))?;
        let Some(cfg_s) = existing else {
            return Ok(false);
        };
        let mut cfg = json_from_str(&cfg_s);
        match cfg.as_object_mut() {
            Some(obj) => {
                obj.insert(MCP_RETIRED_CONFIG_KEY.to_string(), badge.clone());
            }
            None => {
                // config_json held a non-object (legacy/hand-edited row).
                // Preserve it under a sibling key rather than dropping it.
                let mut obj = serde_json::Map::new();
                obj.insert("_prior_config".to_string(), cfg.clone());
                obj.insert(MCP_RETIRED_CONFIG_KEY.to_string(), badge.clone());
                cfg = JsonValue::Object(obj);
            }
        }
        let n = guard
            .execute(
                "UPDATE project_mcp_servers
                 SET enabled = 0, config_json = ?1, updated_at = ?2
                 WHERE project_id = ?3 AND mcp_name = ?4",
                params![
                    json_to_str(&cfg),
                    Utc::now().timestamp_millis(),
                    project_id,
                    mcp_name
                ],
            )
            .map_err(|e| format!("retire_project_mcp_server(update): {}", e))?;
        Ok(n > 0)
    }

    pub fn unregister_project_mcp_server(
        &self,
        project_id: &str,
        mcp_name: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM project_mcp_servers
                 WHERE project_id = ?1 AND mcp_name = ?2",
                params![project_id, mcp_name],
            )
            .map_err(|e| format!("unregister_project_mcp_server: {}", e))?;
        Ok(())
    }

    /// True iff a row exists for (project_id, mcp_name). Used by the
    /// populate flow to decide whether a default-disabled MCP gets
    /// its initial `enabled=false` flag (only on fresh inserts —
    /// re-population MUST preserve the user's toggle).
    pub fn project_mcp_server_exists(
        &self,
        project_id: &str,
        mcp_name: &str,
    ) -> Result<bool, String> {
        let guard = self.lock();
        let exists: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM project_mcp_servers
                 WHERE project_id = ?1 AND mcp_name = ?2",
                params![project_id, mcp_name],
                |r| r.get(0),
            )
            .map_err(|e| format!("project_mcp_server_exists: {}", e))?;
        Ok(exists > 0)
    }

    /// Row count for one project.
    ///
    /// Historically the gate of the migration-010 startup backfill ("zero
    /// rows ⇒ needs a populate-from-disk pass"). v0.2.91 WP-E retired that
    /// gate: a zero-row project is NOT the only project that can be out of
    /// date, and the convergence engine decides per-NAME (comparing the rows
    /// it read against the current default set) rather than on a count. Kept
    /// as a plain accessor — do NOT reintroduce it as a convergence gate.
    pub fn count_project_mcp_servers(&self, project_id: &str) -> Result<i64, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT COUNT(*) FROM project_mcp_servers WHERE project_id = ?1",
                params![project_id],
                |r| r.get::<_, i64>(0),
            )
            .optional()
            .map_err(|e| format!("count_project_mcp_servers: {}", e))?
            .ok_or_else(|| "count_project_mcp_servers: query returned no row".to_string())
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Tests
// ═══════════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;

    fn make_db_with_project(project_id: &str, name: &str) -> Db {
        let db = Db::open_in_memory().expect("in-memory db");
        let slug = db.generate_unique_slug(name).unwrap();
        let folder = if cfg!(windows) { r"C:\tmp\x" } else { "/tmp/x" };
        db.insert_project(project_id, name, folder, ProjectHost::Base, &slug)
            .unwrap();
        db
    }

    #[test]
    fn bundled_set_is_sorted_and_unique() {
        // Catches a future PR that adds a duplicate or out-of-order entry.
        let mut sorted = BUNDLED_MCP_NAMES.to_vec();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(
            sorted.as_slice(),
            BUNDLED_MCP_NAMES,
            "BUNDLED_MCP_NAMES must be sorted + unique"
        );
    }

    // ── v0.2.83 (WP-B5): the bundled name sets are sourced from the shared
    // rule table. These drift tests bind the compiled `&[&str]` mirrors to
    // `crate::mcp_scan_rules` (which parses vco_lib/mcp_scan_rules.toml), so a
    // table edit that isn't mirrored here trips a failure in THIS crate — the
    // same "sourced from + compiled-copy-drift-test" discipline the Rust
    // ALLOWED_ENV_KEYS / DEFAULT_MCP_ENTRY_NAMES use for the same table.

    #[test]
    fn bundled_mcp_names_matches_table() {
        let table: Vec<&str> = crate::mcp_scan_rules::bundled_mcp_names()
            .iter()
            .map(|s| s.as_str())
            .collect();
        assert_eq!(
            BUNDLED_MCP_NAMES, table.as_slice(),
            "BUNDLED_MCP_NAMES (compiled) drifted from mcp_scan_rules.toml \
             [bundled].all_names. Update both in one commit."
        );
    }

    #[test]
    fn bundled_default_disabled_matches_table() {
        let table: Vec<&str> = crate::mcp_scan_rules::bundled_mcp_default_disabled()
            .iter()
            .map(|s| s.as_str())
            .collect();
        assert_eq!(
            BUNDLED_MCP_DEFAULT_DISABLED, table.as_slice(),
            "BUNDLED_MCP_DEFAULT_DISABLED (compiled) drifted from \
             mcp_scan_rules.toml [bundled].default_disabled. Update both in \
             one commit."
        );
    }

    #[test]
    fn is_bundled_mcp_recognises_known_names() {
        for name in BUNDLED_MCP_NAMES {
            assert!(is_bundled_mcp(name), "{} should be bundled", name);
        }
        assert!(!is_bundled_mcp("my-custom-mcp"));
        assert!(!is_bundled_mcp("ecosystem-app-1-live"));
        // Empty string and case sensitivity sanity.
        assert!(!is_bundled_mcp(""));
        assert!(!is_bundled_mcp("Weaviate-KG")); // case-sensitive
    }

    #[test]
    fn default_disabled_is_subset_of_bundled() {
        // Anything in BUNDLED_MCP_DEFAULT_DISABLED must ALSO be in
        // BUNDLED_MCP_NAMES — the disabled list is an opt-out, not a
        // separate namespace. Catches a future PR that adds an entry
        // here without registering it as bundled.
        for name in BUNDLED_MCP_DEFAULT_DISABLED {
            assert!(
                is_bundled_mcp(name),
                "default-disabled MCP '{}' must also be in BUNDLED_MCP_NAMES",
                name
            );
        }
    }

    #[test]
    fn default_disabled_includes_mermaid() {
        // Plan §3 Phase 1 item 2 contract.
        assert!(is_default_disabled_mcp("mermaid"));
    }

    #[test]
    fn default_disabled_includes_excalidraw() {
        // Plan §3 Phase 2 contract: excalidraw is opt-in per project.
        assert!(is_default_disabled_mcp("excalidraw"));
        assert!(is_bundled_mcp("excalidraw"));
    }

    #[test]
    fn default_disabled_excludes_playwright() {
        // Playwright stays default-enabled (plan calls this out
        // explicitly as the contrast case).
        assert!(!is_default_disabled_mcp("playwright"));
    }

    #[test]
    fn default_disabled_excludes_unknown() {
        assert!(!is_default_disabled_mcp("never-shipped"));
        assert!(!is_default_disabled_mcp(""));
    }

    #[test]
    fn register_and_list_round_trip() {
        let db = make_db_with_project("p1", "Acme");
        let cfg = serde_json::json!({
            "command": "/usr/bin/python3",
            "args": ["-m", "weaviate_mcp.server"],
            "env": {"OLLAMA_URL": "http://localhost:11435"},
        });
        let row = db
            .register_project_mcp_server(
                "p1",
                "weaviate-kg",
                false,
                "bundled",
                None,
                Some(".claude/settings.json"),
                Some("/usr/bin/python3"),
                &cfg,
            )
            .unwrap();
        assert_eq!(row.mcp_name, "weaviate-kg");
        assert!(!row.is_user_added);
        assert!(row.enabled);
        assert_eq!(row.source, "bundled");
        assert_eq!(row.command.as_deref(), Some("/usr/bin/python3"));

        let listed = db.list_project_mcp_servers("p1").unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].mcp_name, "weaviate-kg");
        assert_eq!(listed[0].config, cfg);
    }

    #[test]
    fn list_user_added_filters_correctly() {
        let db = make_db_with_project("p1", "Acme");
        let cfg = serde_json::json!({"command": "x"});
        db.register_project_mcp_server(
            "p1",
            "weaviate-kg",
            false,
            "bundled",
            None,
            None,
            None,
            &cfg,
        )
        .unwrap();
        db.register_project_mcp_server(
            "p1",
            "my-custom",
            true,
            "user",
            None,
            None,
            None,
            &cfg,
        )
        .unwrap();
        db.register_project_mcp_server(
            "p1",
            "another-custom",
            true,
            "user",
            None,
            None,
            None,
            &cfg,
        )
        .unwrap();

        let user_only = db.list_user_added_mcp_servers("p1").unwrap();
        assert_eq!(user_only.len(), 2);
        let names: Vec<&str> = user_only.iter().map(|m| m.mcp_name.as_str()).collect();
        assert!(names.contains(&"my-custom"));
        assert!(names.contains(&"another-custom"));
        assert!(!names.contains(&"weaviate-kg"));
    }

    #[test]
    fn upsert_preserves_enabled_flag_on_re_register() {
        let db = make_db_with_project("p1", "Acme");
        let cfg = serde_json::json!({"command": "x"});
        db.register_project_mcp_server(
            "p1", "my-mcp", true, "user", None, None, None, &cfg,
        )
        .unwrap();
        // User disables it via the GUI.
        db.set_project_mcp_server_enabled("p1", "my-mcp", false)
            .unwrap();
        // Re-register (mimics re-populate).
        db.register_project_mcp_server(
            "p1", "my-mcp", true, "user", None, None, None, &cfg,
        )
        .unwrap();
        let rows = db.list_project_mcp_servers("p1").unwrap();
        assert_eq!(rows.len(), 1);
        assert!(
            !rows[0].enabled,
            "user's disabled flag must survive re-register (mirrors agents/skills/hooks)"
        );
    }

    #[test]
    fn upsert_updates_config_and_user_flag_on_conflict() {
        let db = make_db_with_project("p1", "Acme");
        // First registration: bundled (is_user_added=false).
        db.register_project_mcp_server(
            "p1",
            "weaviate-kg",
            false,
            "bundled",
            None,
            Some(".claude/settings.json"),
            Some("/old/path"),
            &serde_json::json!({"command": "/old/path"}),
        )
        .unwrap();
        // Re-register with new command + same name (e.g. user moved venv).
        // is_user_added stays bundled.
        db.register_project_mcp_server(
            "p1",
            "weaviate-kg",
            false,
            "bundled",
            None,
            Some(".claude/settings.json"),
            Some("/new/path"),
            &serde_json::json!({"command": "/new/path"}),
        )
        .unwrap();
        let rows = db.list_project_mcp_servers("p1").unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].command.as_deref(), Some("/new/path"));
        assert!(!rows[0].is_user_added);
    }

    #[test]
    fn unregister_removes_row() {
        let db = make_db_with_project("p1", "Acme");
        db.register_project_mcp_server(
            "p1",
            "x",
            true,
            "user",
            None,
            None,
            None,
            &serde_json::json!({}),
        )
        .unwrap();
        db.unregister_project_mcp_server("p1", "x").unwrap();
        assert!(db.list_project_mcp_servers("p1").unwrap().is_empty());
    }

    #[test]
    fn count_returns_zero_for_fresh_project() {
        let db = make_db_with_project("p1", "Acme");
        assert_eq!(db.count_project_mcp_servers("p1").unwrap(), 0);
    }

    #[test]
    fn count_reflects_inserts() {
        let db = make_db_with_project("p1", "Acme");
        db.register_project_mcp_server(
            "p1", "a", false, "bundled", None, None, None, &serde_json::json!({}),
        )
        .unwrap();
        db.register_project_mcp_server(
            "p1", "b", true, "user", None, None, None, &serde_json::json!({}),
        )
        .unwrap();
        assert_eq!(db.count_project_mcp_servers("p1").unwrap(), 2);
    }

    #[test]
    fn cascade_delete_on_project_removal() {
        let db = make_db_with_project("p1", "Acme");
        db.register_project_mcp_server(
            "p1", "a", true, "user", None, None, None, &serde_json::json!({}),
        )
        .unwrap();
        // Drop project; FK CASCADE wipes the mcp_servers row.
        let guard = db.lock();
        guard
            .execute("DELETE FROM projects WHERE id = ?1", params!["p1"])
            .unwrap();
        drop(guard);
        assert_eq!(db.count_project_mcp_servers("p1").unwrap(), 0);
    }

    #[test]
    fn set_enabled_errors_on_unknown_row() {
        let db = make_db_with_project("p1", "Acme");
        let err = db
            .set_project_mcp_server_enabled("p1", "ghost", false)
            .unwrap_err();
        assert!(
            err.contains("not registered"),
            "expected 'not registered' in error, got: {}",
            err
        );
    }

    #[test]
    fn invalid_source_rejected() {
        let db = make_db_with_project("p1", "Acme");
        let err = db
            .register_project_mcp_server(
                "p1",
                "x",
                true,
                "garbage",
                None,
                None,
                None,
                &serde_json::json!({}),
            )
            .unwrap_err();
        assert!(err.contains("invalid mcp.source"), "got: {}", err);
    }

    // ── v0.2.83 (WP-B3): third-party MCP "present AND working" guarantee ──
    //
    // A user's own MCP (searxng, Jira, Gmail, …) must not be silenced by the
    // per-project gate: the BUNDLED_MCP_DEFAULT_DISABLED opt-out applies ONLY
    // to VCO's own bundled servers, never to third-party names. And when the
    // populate flow registers a third-party row it must land ENABLED so the
    // MCP actually fires. These pins guard against a future edit that widens
    // the default-disabled gate to unknown names.

    #[test]
    fn third_party_names_are_never_default_disabled() {
        // searxng is the spicy case: VCO no longer ships it, so a user's own
        // searxng MCP is third-party property — it must NOT be default-off.
        for name in ["searxng", "jira", "gmail", "my-custom-mcp"] {
            assert!(
                !is_default_disabled_mcp(name),
                "third-party MCP '{}' must never be default-disabled (it is \
                 user property and must fire out of the box)",
                name
            );
            assert!(
                !is_bundled_mcp(name),
                "third-party MCP '{}' must not be classified as a VCO bundled \
                 server",
                name
            );
        }
    }

    #[test]
    fn third_party_mcp_registers_enabled_and_user_added() {
        // A populate/register of a user's own searxng lands enabled=true and
        // is_user_added=true — present AND working.
        let db = make_db_with_project("p1", "Acme");
        let cfg = serde_json::json!({
            "command": "/home/dev/my-searxng-mcp/serve.py",
            "args": ["--port", "8888"],
        });
        let row = db
            .register_project_mcp_server(
                "p1",
                "searxng",
                true, // caller computes is_user_added = !is_bundled_mcp("searxng")
                "user",
                None,
                None,
                Some("/home/dev/my-searxng-mcp/serve.py"),
                &cfg,
            )
            .unwrap();
        assert!(row.enabled, "third-party MCP must register ENABLED (working)");
        assert!(row.is_user_added);

        // Survives a re-populate (mimics install --update re-running populate):
        // the user's row is untouched — re-registering only bundled VCO servers
        // never drops or disables an unknown one.
        db.register_project_mcp_server(
            "p1", "weaviate-kg", false, "bundled", None, None, None,
            &serde_json::json!({"command": "x"}),
        )
        .unwrap();
        let rows = db.list_project_mcp_servers("p1").unwrap();
        let searxng = rows.iter().find(|r| r.mcp_name == "searxng").expect("searxng row survives");
        assert!(searxng.enabled, "user's searxng must stay enabled after a VCO re-populate");
        assert!(searxng.is_user_added);
    }
}
