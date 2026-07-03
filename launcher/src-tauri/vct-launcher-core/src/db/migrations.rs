//! Forward-only schema migrations for launcher.db.
//!
//! Each migration is an idempotent SQL string plus a unique sequence number.
//! We track applied migrations in a `_schema_migrations` table and only run
//! the ones newer than the current version.
//!
//! Migrations MUST be append-only: never edit a shipped migration. If a
//! schema change needs revision, add a new migration that alters the table.

use rusqlite::Connection;

struct Migration {
    version: u32,
    description: &'static str,
    sql: &'static str,
}

/// Ordered list of all migrations the launcher knows about. The `version`
/// field MUST be monotonically increasing and unique.
const MIGRATIONS: &[Migration] = &[
    Migration {
        version: 1,
        description: "initial schema: projects, module_installs, module_settings, access grants, tier_cache, audit_log",
        sql: include_str!("migrations/001_initial.sql"),
    },
    Migration {
        version: 2,
        description: "per-project orchestrator state: agents, skills, hooks, permissions, secret refs, KG/codegraph bindings",
        sql: include_str!("migrations/002_project_state.sql"),
    },
    Migration {
        version: 3,
        description: "projects.slug for URL-addressable /p/<slug>/... routes",
        sql: include_str!("migrations/003_project_slug.sql"),
    },
    Migration {
        version: 4,
        description: "audit_log: add actor column (OS user) for multi-user audit trails",
        sql: include_str!("migrations/004_audit_actor.sql"),
    },
    Migration {
        version: 5,
        description: "tier_cache: extend orchestrator_tier CHECK to allow 'admin' (Bug 33)",
        sql: include_str!("migrations/005_tier_cache_admin.sql"),
    },
    Migration {
        version: 6,
        description: "code_graph_builds: per-project initial-build status (Gap 2)",
        sql: include_str!("migrations/006_code_graph_build_status.sql"),
    },
    Migration {
        version: 7,
        description: "secret_active_state: per-secret active flag (Lifecycle B + Storage A — Bug 3 follow-up)",
        sql: include_str!("migrations/007_secret_active_state.sql"),
    },
    Migration {
        version: 8,
        description: "app_state: key-value table for VCT_STATE_DIR-isolated launcher flags (Bug 14 fix — onboarding_complete moves out of localStorage)",
        sql: include_str!("migrations/008_app_state.sql"),
    },
    Migration {
        version: 9,
        description: "secrets: per-(secret × requester_project) active flag + secret_grants table for cross-project sharing (0.2.1)",
        sql: include_str!("migrations/009_per_project_active_and_grants.sql"),
    },
    Migration {
        version: 10,
        description: "project_mcp_servers: per-project mirror of `.claude/settings.json::mcpServers` + `.mcp.json` with is_user_added flag for Custom MCP tab (KNOWN_ISSUES v0.2.x)",
        sql: include_str!("migrations/010_project_mcp_servers.sql"),
    },
    Migration {
        version: 11,
        description: "kg_syncs: per-project initial KG / docs sync status (KG auto-sync on add-project, 2026-05-12)",
        sql: include_str!("migrations/011_kg_syncs.sql"),
    },
    Migration {
        version: 12,
        description: "kg_summaries: per-project initial KG-summary backfill status (auto-backfill on add-project, v0.2.3 / 2026-05-12)",
        sql: include_str!("migrations/012_kg_summaries.sql"),
    },
    Migration {
        version: 13,
        description: "projects.host CHECK extended to allow 'orchestrator_root' (auto-registered at launcher startup, v0.2.11 / 2026-05-15)",
        sql: include_str!("migrations/013_orchestrator_root.sql"),
    },
    Migration {
        version: 14,
        description: "projects.rl_port: per-project RL reranker server port (v0.2.21, Phase 1D)",
        sql: include_str!("migrations/014_project_rl_port.sql"),
    },
    Migration {
        version: 15,
        description: "module_installs.container_name: resolved container name for container_pull modules (v0.2.21, Phase 1E)",
        sql: include_str!("migrations/015_module_install_container_name.sql"),
    },
    Migration {
        version: 16,
        description: "module_weights_state: per-(project × module × embedding_source) weights version / poll / finetune state (v0.2.21, Phase 3C)",
        sql: include_str!("migrations/016_module_weights_state.sql"),
    },
    Migration {
        version: 17,
        description: "module_ports: generic per-(project × module) HTTP port (v0.2.26, declarative dispatcher generalization — replaces RL-only projects.rl_port as SoT)",
        sql: include_str!("migrations/017_module_ports.sql"),
    },
    Migration {
        version: 18,
        description: "module deprecation surface: deprecation_events (append-only audit) + module_deprecation_seen (one-shot notification gate) (v0.2.31)",
        sql: include_str!("migrations/018_module_deprecation.sql"),
    },
    Migration {
        version: 19,
        description: "module-shipped DB migrations: module_db_migrations tracking + module_access_tokens for hub bearer auth (v0.2.31)",
        sql: include_str!("migrations/019_module_db_migrations.sql"),
    },
    Migration {
        version: 20,
        description: "drop legacy module_weights_state: replaced by container-owned rl_weights_state shipped by vct-rl-reranker v0.2.6 (v0.2.31)",
        sql: include_str!("migrations/020_drop_legacy_module_weights_state.sql"),
    },
    Migration {
        version: 21,
        description: "module_installs: extend status CHECK with 'broken' (v0.2.33 Agent C — startup reconciler marks rows with missing on-disk manifest as 'broken' so the GUI funnels users to Reinstall instead of Restart)",
        sql: include_str!("migrations/021_module_installs_broken_status.sql"),
    },
    Migration {
        version: 22,
        description: "diagrams registry: project_diagrams + diagram_snapshots + diagram_access + project_mcp_tool_grants + project_modules + diagram_index_retry (Excalidraw/Mermaid Phase 1.1 + 1.5.A retry-queue)",
        sql: include_str!("migrations/022_diagrams.sql"),
    },
    Migration {
        version: 23,
        description: "module-shipped MCP tool allowlist defaults: module_mcp_tool_defaults table populated from vct-module.json::mcp_registration.tool_allowlist at install time, read by hub /mcp-tool-grants route (v0.2.34 Agent E — Phase 4 generalisation)",
        sql: include_str!("migrations/023_module_mcp_tool_defaults.sql"),
    },
    Migration {
        version: 24,
        description: "license_keys + license_key_validations: per-paid-module license keys (L1, v0.2.40). Each paid module owns its own row keyed by module_id; reserved '__orchestrator__' slot preserves the legacy single-key behaviour. tier_cache stays the EFFECTIVE projection.",
        sql: include_str!("migrations/024_license_keys.sql"),
    },
    Migration {
        version: 25,
        description: "rl_events: queryable telemetry store for RL retrieval + citation events (v0.2.47). Replaces the JSONL corpus at ~/.claude/retrieval_rl_data/rl_events.jsonl. MCP-side telemetry writers POST events via the hub's POST /api/v1/rl/events route; the hub is the sole writer (preserves launcher single-writer rule). offline_trainer reads via a future GET endpoint; the dashboard widget joins against projects for per-project event-rate displays.",
        sql: include_str!("migrations/025_rl_events.sql"),
    },
    Migration {
        version: 26,
        description: "project_codegraph_extra_paths: read-only filesystem paths contributing entities to a project's codegraph (v0.2.47). Use case: index a sibling clone into the active project's codegraph without making it a launcher project. PRIMARY KEY (project_id, path); ON DELETE CASCADE on projects.id. Resolver field is additive; hooks query enabled rows by path-prefix. Plan: .claude/context/plans/v0.2.47-project-extra-codegraph-paths-2026-06-05.md.",
        sql: include_str!("migrations/026_project_codegraph_extra_paths.sql"),
    },
    Migration {
        version: 27,
        description: "module_installs.project_id nullable + partial unique indexes for global-scope installs (v0.2.49 Stream A). NULL project_id == one install per machine, used by modules whose manifest declares install.scope = 'global' (vct-rl-reranker v0.2.10+). Pre-existing rows survive verbatim (the recreate-and-copy mirrors migration 013's pattern). Plan: .claude/context/plans/v0.2.49-global-install-per-project-routing-plan-2026-06-06.md.",
        sql: include_str!("migrations/027_module_installs_nullable_project.sql"),
    },
    Migration {
        version: 28,
        description: "orchestrator_root_kg_collection setting row in app_state (v0.2.49 access-matrix Phase 1 / item #1). Persists the canonical name of the orchestrator-root shared KG collection (default 'VibeCodedOrchestrator_KnowledgeGraph') so every consumer that needs to ask 'is this the shared root?' can compare against a single source of truth instead of duplicating a hard-coded constant. Closes audit finding S-1 (substring heuristic). White-label installers override the default via install.py at install time. Plan: .claude/context/plans/v0.2.49-access-matrix-overhaul-2026-06-08.md.",
        sql: include_str!("migrations/028_orchestrator_root_kg_collection.sql"),
    },
    Migration {
        version: 29,
        description: "kg_collection_access audit columns created_at + updated_at (v0.2.49 access-matrix Step A.5). ALTER TABLE adds the columns INTEGER NOT NULL DEFAULT 0. Legacy rows backfill to 0 (sentinel for 'pre-audit-trail'); v0.2.49+ INSERTs bind both to wall-clock millis. Required so the plan's is_user_configured(row) := row.updated_at != row.created_at predicate is implementable for future-cycle per-row decisions. Phase 7 force-upgrade migration in this same release doesn't depend on the audit data per user directive 'force-update everything to new default permissions'.",
        sql: include_str!("migrations/029_kg_collection_access_audit_columns.sql"),
    },
    Migration {
        version: 30,
        description: "projects.folder_missing_at_last_boot column (v0.2.49 access-matrix Phase 6 S-4). ALTER TABLE adds INTEGER NOT NULL DEFAULT 0 (SQLite-stored BOOLEAN). Set/cleared by the launcher's boot sanity check (lib.rs async spawn) that walks every project row and Path::is_dir-checks folder_path. Frontend reads via the boot-flagged project list command (read_project_folder_missing_flags) and renders a non-blocking warning banner on the affected project card. All existing rows backfill to 0; the boot probe rewrites accurately on the next launcher boot.",
        sql: include_str!("migrations/030_project_folder_missing_at_last_boot.sql"),
    },
    Migration {
        version: 31,
        description: "Force-upgrade shared kg_collection_access + drop cross-project peer rows (v0.2.49 access-matrix Step D / Phase 7, updated by Step F SF1 + Q4). TWO PASSES: (1) UPDATE shared rows access_level='read'→'write' with sentinel updated_at=created_at+1; (2) DELETE rows where (project_id, collection_name) is not in the v0.2.49 default keep-list (own primary, own dev = REPLACE(_KnowledgeGraph→_Development), own shared, OR corruption carve-out: rows for projects with no role='primary' binding). Then sentinel-stamps all surviving rows whose updated_at=0. Per user directive 2026-06-08 verbatim 'force update to default to read+write permissions on own + shared collection, no cross-project permissions (excluding root/shared), then is_user_configured = FALSE for them, since those were set to their default programmatically'. Idempotent on re-runs (Pass 1 WHERE excludes already-upgraded; Pass 2 NOT IN keep-list excludes already-kept). Sentinel value 'created_at+1' (NOT wall-clock millis pre-Step-F) so is_user_configured reads FALSE post-migration → F-2c's preserve-user-configured-peers logic doesn't silently skip migrated rows. Plan: .claude/context/plans/v0.2.49-access-matrix-overhaul-2026-06-08.md item #20 + Step F SF1 + Q4.",
        sql: include_str!("migrations/031_force_upgrade_shared_kg_read_to_write.sql"),
    },
    Migration {
        version: 32,
        description: "module_installs.kg_collections column (v0.2.49 access-matrix Step F MF3 follow-up). ALTER TABLE adds TEXT column for JSON-encoded array of Weaviate collection names a module declares it writes to (read from vct-module.json manifest at install time). Persisted in launcher DB at install time so populate_kg_collection_access_for_project can back-fill new projects' access rows without re-parsing manifest from disk. Pre-v0.2.49 module installs backfill to NULL; reinstall populates the column. User directive 2026-06-08: refactor disk-read MF3 to DB-storage now rather than queueing v0.2.50 follow-up.",
        sql: include_str!("migrations/032_module_installs_kg_collections.sql"),
    },
    Migration {
        version: 33,
        description: "artifact_schema_versions table (v0.2.52 V52-AG). Single registry for every derived-state artifact's schema version (Weaviate collections, code-graph indices, JSON-content shapes, controlled-vocabulary columns, project bootstrap version). Keyed by (project_id NULL = orchestrator-wide, artifact_type, artifact_name). Install/update flows compare stored version against canonical constants in vco_lib/schema_versions.py and trigger drop+recreate (derived state) OR upgrade-in-place (user-curated state) on mismatch. Foundation for the user-locked 2026-06-09 'from now on consistent' rule: no back-compat layers, no migration ladders — bump constant, drop+recreate. See v0.2.52 backlog § V52-AG + § V52-AF for the consumer wiring.",
        sql: include_str!("migrations/033_artifact_schema_versions.sql"),
    },
    Migration {
        version: 34,
        description: "module_settings.project_id nullable + partial unique indexes for global-scope settings (v0.2.52 V52-AD). NULL project_id == host-wide default for a (module_id, setting_key) pair, consumed by hub resolver as fallback when no per-project row exists. Mirrors migration 027's pattern for module_installs. Reader fall-back order: per-project → global default → fail-open true. The recreate-and-copy mirrors migration 027's discipline. Plan: V52-AD in .claude/context/plans/v0.2.52-backlog-2026-06-09.md.",
        sql: include_str!("migrations/034_module_settings_nullable_project.sql"),
    },
    Migration {
        version: 35,
        description: "index module_access_tokens.token_secret (v0.2.65 audit N1-3). The hub authenticates every module-DB / RL request via `WHERE token_secret = ?1` (module_db_api.rs::lookup_token); migration 019 indexed only module_id, leaving the hot-path auth lookup a full table scan. Plain additive CREATE INDEX IF NOT EXISTS — idempotent, no table rebuild, not self-transactional.",
        sql: include_str!("migrations/035_module_access_tokens_secret_index.sql"),
    },
    Migration {
        version: 36,
        description: "project_setups table (Defect B, v0.2.68). One row per project tracks the lifecycle of the async setup task that `create_project_v2` now detaches (bootstrap-collections + install-bundle + post-bundle phase) so the New Project modal returns FAST instead of blocking ~51s on a cold Weaviate/Ollama backend. Mirrors code_graph_builds (006) + kg_syncs (011): same {started,finished}_at, same FK cascade. Status set adds 'done'/'deferred'/'failed' terminal states ('deferred' = informational amber, a phase deferred cleanly e.g. Weaviate bootstrap on a cold backend — NOT a failure). A 'pending'/'running' row is the re-entrancy LOCK (refuses a 2nd concurrent setup for the same project; mirrors v0.2.67 install_in_flight) AND gates the boot-resume sweeps for code-graph/kg-sync/kg-summary so a crash mid-setup can't resurrect the 2026-05-06 spawn-before-bundle race. `warnings` is a JSON array carried on the terminal setup-progress event for the frontend to re-toast (F5). Plain additive CREATE TABLE — idempotent, not self-transactional.",
        sql: include_str!("migrations/036_project_setups.sql"),
    },
    Migration {
        version: 37,
        description: "code_graph_builds.pid column (R-4, v0.2.73). Nullable INTEGER: NULL = launcher-spawned build (lifecycle tied to the launcher; boot sweep fails stale 'running' ghosts as before), NOT NULL = detached analyzer registered via the hub's codegraph-build endpoint (install.py post-update resync) — survives launcher restarts; the boot sweep flips its 'running' row to 'failed' only when the pid is positively dead. Closes RT-1/RT-5 (install-spawned P7 resync invisible to the GUI + silent mid-walk death). Plain additive ALTER TABLE — idempotent via the runner's version check, not self-transactional. LAUNCHER_DB_TABLE_SET_VERSION bumps 36->37 atomically with this migration (B-2).",
        sql: include_str!("migrations/037_code_graph_build_pid.sql"),
    },
    Migration {
        version: 38,
        description: "extend code_graph_builds.status CHECK with 'partial' (C-11 / RT-3, v0.2.73). A code-graph build can now finish PARTIAL: inserts succeeded (files_analyzed meaningful) but the stale-row DELETE pass failed (PRUNE_FAILURES=N, N>0). The analyzer keeps exit 0 and emits the machine-readable PRUNE_FAILURES line; the launcher stdout reader (commands/codegraph.rs) flips the row success->partial when N>0. SQLite CHECK constraints are immutable, so this is a table-rebuild (mirror of 021): the replacement table carries EVERY column the live table has after migration 037 — INCLUDING the pid column — then copy/drop/rename and recreate idx_code_graph_builds_status. No FK toggles / not self-transactional: nothing references code_graph_builds via an inbound FOREIGN KEY, so it rides the runner's outer transaction like 021. LAUNCHER_DB_TABLE_SET_VERSION bumps 37->38 atomically with this migration (B-2).",
        sql: include_str!("migrations/038_code_graph_build_partial_status.sql"),
    },
];

/// Migrations whose .sql manages its OWN `BEGIN`/`COMMIT` boundary.
///
/// These are the table-rebuild migrations that must issue
/// `PRAGMA foreign_keys = OFF/ON` — SQLite silently ignores that pragma
/// inside a transaction, so the pragma has to sit OUTSIDE the
/// transaction and the .sql owns the boundary itself. Wrapping them in
/// the runner's outer transaction would (a) error with "cannot start a
/// transaction within a transaction" and (b) neuter the pragma.
///
/// Crash-safety for these three: the .sql's own BEGIN/COMMIT makes the
/// CONTENT atomic (journal rollback on crash). The version record is a
/// separate statement; a crash in the millisecond window between
/// content-COMMIT and version-INSERT re-runs the migration on next
/// open, which is safe by construction for the create-copy-drop-rename
/// rebuild pattern (re-running copies the data again and converges).
/// `self_transactional_list_matches_sql` in the test module pins the
/// list against the .sql contents so a future BEGIN-containing
/// migration can't silently land outside it.
const SELF_TRANSACTIONAL_MIGRATIONS: &[u32] = &[13, 27, 34];

fn is_self_transactional(version: u32) -> bool {
    SELF_TRANSACTIONAL_MIGRATIONS.contains(&version)
}

/// Apply every migration whose version is greater than the current max applied.
///
/// v0.2.54 Track D: each migration is applied ATOMICALLY — the SQL batch
/// and its `_schema_migrations` version record commit together (or roll
/// back together). Pre-fix, `execute_batch` ran with NO transaction
/// (rusqlite does not wrap batches) and the version was recorded by a
/// separate statement: a crash or error mid-batch (e.g. between the
/// three `ALTER TABLE ADD COLUMN`s of migration 029) left the migration
/// half-applied AND unrecorded, so every subsequent `Db::open` retried
/// the whole batch, hit `duplicate column name`, and the launcher could
/// never boot again — no rollback, no repair path.
pub fn apply(conn: &Connection) -> Result<(), String> {
    // Bootstrap the tracking table. This statement is itself "migration 0"
    // and stays outside the MIGRATIONS list so it's always safe to re-run.
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS _schema_migrations (
            version     INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at  INTEGER NOT NULL
         );",
    )
    .map_err(|e| format!("create _schema_migrations: {}", e))?;

    let current_version: u32 = conn
        .query_row(
            "SELECT COALESCE(MAX(version), 0) FROM _schema_migrations",
            [],
            |row| row.get(0),
        )
        .map_err(|e| format!("read current version: {}", e))?;

    for m in MIGRATIONS {
        if m.version <= current_version {
            continue;
        }
        tracing_apply(m);
        apply_one(conn, m)?;
    }

    Ok(())
}

/// Record a migration as applied. Caller decides the transactional
/// context (inside the outer wrap for normal migrations; standalone for
/// self-transactional ones).
fn record_version(conn: &Connection, m: &Migration) -> Result<(), String> {
    conn.execute(
        "INSERT INTO _schema_migrations (version, description, applied_at) VALUES (?1, ?2, ?3)",
        rusqlite::params![
            m.version,
            m.description,
            chrono::Utc::now().timestamp_millis(),
        ],
    )
    .map_err(|e| format!("record migration {}: {}", m.version, e))?;
    Ok(())
}

/// Apply ONE migration atomically (content + version record).
fn apply_one(conn: &Connection, m: &Migration) -> Result<(), String> {
    if is_self_transactional(m.version) {
        // The .sql owns its BEGIN/COMMIT (see SELF_TRANSACTIONAL_MIGRATIONS).
        conn.execute_batch(m.sql).map_err(|e| {
            format!(
                "apply migration {} ({}): {}",
                m.version, m.description, e
            )
        })?;
        return record_version(conn, m);
    }

    // BEGIN IMMEDIATE: take the write lock up front so a concurrent
    // reader can't promote-deadlock us halfway through the batch.
    conn.execute_batch("BEGIN IMMEDIATE")
        .map_err(|e| format!("begin migration {}: {}", m.version, e))?;

    let applied = conn
        .execute_batch(m.sql)
        .map_err(|e| {
            format!(
                "apply migration {} ({}): {}",
                m.version, m.description, e
            )
        })
        .and_then(|_| record_version(conn, m));

    match applied {
        Ok(()) => conn
            .execute_batch("COMMIT")
            .map_err(|e| format!("commit migration {}: {}", m.version, e)),
        Err(e) => {
            // Roll back the half-applied batch so the NEXT open retries
            // from a clean slate instead of dying on `duplicate column
            // name` forever. Rollback failure is appended (it matters
            // for diagnosis) but the original error stays primary.
            if let Err(rb) = conn.execute_batch("ROLLBACK") {
                return Err(format!("{} (rollback also failed: {})", e, rb));
            }
            Err(e)
        }
    }
}

fn tracing_apply(m: &Migration) {
    eprintln!("[launcher-db] applying migration {}: {}", m.version, m.description);
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Helper: open a fresh in-memory connection, apply every migration up
    /// to and INCLUDING `up_to_version`. Used to simulate "user on
    /// launcher v0.2.10" state (up_to=12) before manually applying 013.
    fn apply_up_to(conn: &Connection, up_to_version: u32) -> Result<(), String> {
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS _schema_migrations (
                version     INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at  INTEGER NOT NULL
             );",
        )
        .map_err(|e| format!("create _schema_migrations: {}", e))?;
        for m in MIGRATIONS {
            if m.version > up_to_version {
                break;
            }
            conn.execute_batch(m.sql)
                .map_err(|e| format!("apply {}: {}", m.version, e))?;
            conn.execute(
                "INSERT INTO _schema_migrations (version, description, applied_at) VALUES (?1, ?2, ?3)",
                rusqlite::params![m.version, m.description, 0_i64],
            )
            .map_err(|e| format!("record {}: {}", m.version, e))?;
        }
        Ok(())
    }

    /// On a fresh DB, migration 013 runs as part of `apply()` and the
    /// `projects` table accepts `host='orchestrator_root'`.
    #[test]
    fn migration_013_accepts_orchestrator_root_on_fresh_db() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("apply migrations");

        // Sanity: migration 13 is recorded.
        let v: u32 = conn
            .query_row(
                "SELECT MAX(version) FROM _schema_migrations",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(v >= 13, "expected at least version 13, got {}", v);

        // Inserting a row with the new host value succeeds.
        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES (?1, ?2, ?3, ?4, ?5, ?5, ?6)",
            rusqlite::params!["id-root", "VCO", "/tmp/vco", "orchestrator_root", now, "orchestrator-root"],
        )
        .expect("insert orchestrator_root row");

        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM projects WHERE host = 'orchestrator_root'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    /// Migration 013 still rejects unknown host strings. The CHECK is
    /// extended (not relaxed).
    #[test]
    fn migration_013_still_rejects_unknown_hosts() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("apply migrations");

        let now: i64 = 1_700_000_000_000;
        let err = conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES (?1, ?2, ?3, ?4, ?5, ?5, ?6)",
            rusqlite::params!["id-bad", "Bad", "/tmp/bad", "garbage", now, "bad"],
        );
        assert!(err.is_err(), "INSERT with host='garbage' must fail the CHECK");
        let msg = format!("{}", err.unwrap_err());
        assert!(msg.to_lowercase().contains("check"), "expected CHECK error, got: {}", msg);
    }

    /// Upgrade path: a DB stopped at version 12 (a v0.2.10 user) gets
    /// `host='base'` and `host='mao'` rows preserved verbatim when 013
    /// rebuilds the table. All columns (id, name, folder_path, slug,
    /// created_at, updated_at, host) survive the create-copy-drop-rename.
    #[test]
    fn migration_013_preserves_existing_rows_on_upgrade() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply_up_to(&conn, 12).expect("apply up to v12");

        // Seed pre-013 rows.
        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES (?1, ?2, ?3, ?4, ?5, ?5, ?6)",
            rusqlite::params!["id-base", "Alpha", "/tmp/alpha", "base", now, "alpha"],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES (?1, ?2, ?3, ?4, ?5, ?5, ?6)",
            rusqlite::params!["id-mao", "Beta", "/tmp/beta", "mao", now + 1, "beta"],
        )
        .unwrap();

        // Now apply remaining migrations (which is just 013).
        apply(&conn).expect("apply remaining migrations");

        // Rows survive verbatim.
        let mut stmt = conn
            .prepare("SELECT id, name, folder_path, host, slug FROM projects ORDER BY id")
            .unwrap();
        let rows: Vec<(String, String, String, String, String)> = stmt
            .query_map([], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
            })
            .unwrap()
            .map(|r| r.unwrap())
            .collect();

        assert_eq!(rows.len(), 2);
        assert_eq!(
            rows[0],
            (
                "id-base".to_string(),
                "Alpha".to_string(),
                "/tmp/alpha".to_string(),
                "base".to_string(),
                "alpha".to_string(),
            )
        );
        assert_eq!(
            rows[1],
            (
                "id-mao".to_string(),
                "Beta".to_string(),
                "/tmp/beta".to_string(),
                "mao".to_string(),
                "beta".to_string(),
            )
        );

        // And the new value is accepted.
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES (?1, ?2, ?3, ?4, ?5, ?5, ?6)",
            rusqlite::params!["id-root", "VCO", "/tmp/vco", "orchestrator_root", now + 2, "orchestrator-root"],
        )
        .expect("insert orchestrator_root row after upgrade");
    }

    /// FK landscape: rows in dependent tables that reference projects(id)
    /// continue to resolve after migration 013 rebuilds projects.
    /// We can't easily test ON DELETE CASCADE end-to-end without seeding
    /// rows in every dependent table, but we can verify a representative
    /// FK (codegraph_access) holds across the rename.
    #[test]
    fn migration_013_preserves_fk_resolution_on_upgrade() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply_up_to(&conn, 12).expect("apply up to v12");

        // Seed two projects + a codegraph_access edge between them.
        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES ('p1', 'P1', '/tmp/p1', 'base', ?1, ?1, 'p1')",
            rusqlite::params![now],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES ('p2', 'P2', '/tmp/p2', 'base', ?1, ?1, 'p2')",
            rusqlite::params![now],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO codegraph_access (grantor_project_id, grantee_project_id, access_level, granted_at)
             VALUES ('p1', 'p2', 'read', ?1)",
            rusqlite::params![now],
        )
        .unwrap();

        // Apply 013.
        apply(&conn).expect("apply remaining migrations");

        // FK edge still resolves: deleting p1 cascades to codegraph_access.
        let edge_before: i64 = conn
            .query_row("SELECT COUNT(*) FROM codegraph_access", [], |r| r.get(0))
            .unwrap();
        assert_eq!(edge_before, 1);

        conn.execute("DELETE FROM projects WHERE id = 'p1'", [])
            .expect("delete p1 (FK cascade should remove codegraph_access row)");

        let edge_after: i64 = conn
            .query_row("SELECT COUNT(*) FROM codegraph_access", [], |r| r.get(0))
            .unwrap();
        assert_eq!(edge_after, 0, "FK CASCADE must remove the access edge after p1 delete");
    }

    /// Stress test: rows in EVERY FK-bearing dependent table survive the
    /// projects rebuild verbatim. This is the regression net for the
    /// 2026-05-16 fix that moved `PRAGMA foreign_keys=OFF` outside the
    /// transaction. With FK enforcement enabled during DROP TABLE the
    /// child rows would either be cascade-deleted (some SQLite builds)
    /// or left dangling and rejected by the post-migration
    /// `foreign_key_check`. Either way the assertions below fail.
    #[test]
    fn migration_013_preserves_fk_rows_across_all_dependent_tables() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply_up_to(&conn, 12).expect("apply up to v12");

        let now: i64 = 1_700_000_000_000;
        // Two pre-013 projects we'll attach FK rows to.
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES ('grantor', 'Grantor', '/tmp/grantor', 'base', ?1, ?1, 'grantor')",
            rusqlite::params![now],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES ('grantee', 'Grantee', '/tmp/grantee', 'base', ?1, ?1, 'grantee')",
            rusqlite::params![now],
        )
        .unwrap();

        // Seed a representative FK row in every dependent table that
        // existed at v12. Each REFERENCES projects(id) ON DELETE CASCADE,
        // so the rebuild step is the critical moment.
        let cases: &[(&str, &str)] = &[
            (
                "codegraph_access",
                "INSERT INTO codegraph_access (grantor_project_id, grantee_project_id, access_level, granted_at)
                 VALUES ('grantor', 'grantee', 'read', 0)",
            ),
            (
                // v0.2.49 Step F SF1 + Q4 update (2026-06-08):
                // migration 031 Pass 2 DELETEs rows that aren't in
                // the v0.2.49 default keep-list (own primary, own
                // dev, own shared). Pre-Step-F this fixture used
                // 'TestCollection' which is NOT grantor's primary
                // binding ('TestKG' below) → Pass 2 would destroy
                // the FK row → migration_013 test would fail not
                // because of an FK regression but because of a
                // semantic-content regression in 031.
                //
                // The fix: align the seed to the v0.2.49 keep-list
                // shape. Use 'TestKG_KnowledgeGraph' as the access
                // row's collection_name + 'TestKG_KnowledgeGraph'
                // as grantor's primary binding (below). Then both
                // migration 013's FK rebuild AND migration 031's
                // Pass 2 keep-list preserve the row.
                "kg_collection_access",
                "INSERT INTO kg_collection_access (project_id, collection_name, access_level)
                 VALUES ('grantor', 'TestKG_KnowledgeGraph', 'read')",
            ),
            (
                "project_permissions",
                "INSERT INTO project_permissions (project_id, subject, kind, value, granted_at)
                 VALUES ('grantor', 'project', 'allowed_tool', 'Read', 0)",
            ),
            (
                // v0.2.49 Step F: aligned to the kg_collection_access
                // seed above so migration 031 Pass 2's keep-list
                // matches (own primary binding == the access row's
                // collection_name).
                "project_kg_bindings",
                "INSERT INTO project_kg_bindings (project_id, role, collection_name, updated_at)
                 VALUES ('grantor', 'primary', 'TestKG_KnowledgeGraph', 0)",
            ),
            (
                "project_codegraph_bindings",
                "INSERT INTO project_codegraph_bindings (project_id, collection_prefix, updated_at)
                 VALUES ('grantor', 'TestPrefix', 0)",
            ),
        ];

        for (table, sql) in cases {
            conn.execute(sql, [])
                .unwrap_or_else(|e| panic!("seed {}: {}", table, e));
        }

        // Apply 013 — the create-copy-drop-rename moment.
        apply(&conn).expect("apply migrations");

        // Every FK-bearing row must still be there.
        for (table, _) in cases {
            let count: i64 = conn
                .query_row(&format!("SELECT COUNT(*) FROM {}", table), [], |r| r.get(0))
                .unwrap_or_else(|e| panic!("count {}: {}", table, e));
            assert_eq!(
                count, 1,
                "FK row in {} must survive migration 013 rebuild (got {})",
                table, count
            );
        }

        // PRAGMA foreign_key_check returns one row per dangling FK.
        // Empty result = full integrity. We collect all rows; the
        // assertion message includes the offenders if any survive.
        let mut stmt = conn.prepare("PRAGMA foreign_key_check").unwrap();
        let orphans: Vec<String> = stmt
            .query_map([], |row| {
                // foreign_key_check columns: table, rowid, parent, fkid
                let t: String = row.get(0)?;
                let rid: i64 = row.get(1).unwrap_or(0);
                let parent: String = row.get(2).unwrap_or_default();
                Ok(format!("{}(rowid={})→{}", t, rid, parent))
            })
            .unwrap()
            .filter_map(|r| r.ok())
            .collect();
        assert!(
            orphans.is_empty(),
            "PRAGMA foreign_key_check found dangling FKs after migration 013: {:?}",
            orphans
        );

        // Migration must leave foreign_keys ENABLED on the connection
        // (the runner's next operation — INSERT into _schema_migrations
        // and downstream commands — depend on it).
        let fk_state: i64 = conn
            .query_row("PRAGMA foreign_keys", [], |r| r.get(0))
            .unwrap();
        assert_eq!(
            fk_state, 1,
            "foreign_keys must be re-enabled at end of migration 013 (got {})",
            fk_state
        );

        // Sanity: cascade still works on the rebuilt table. Deleting
        // the grantor cascades to all 5 dependent rows seeded above.
        conn.execute("DELETE FROM projects WHERE id = 'grantor'", [])
            .expect("delete grantor (cascade should remove FK rows)");
        for (table, _) in cases {
            let count: i64 = conn
                .query_row(&format!("SELECT COUNT(*) FROM {}", table), [], |r| r.get(0))
                .unwrap_or_else(|e| panic!("post-delete count {}: {}", table, e));
            assert_eq!(
                count, 0,
                "FK CASCADE on rebuilt projects must remove row in {} after parent delete (got {})",
                table, count
            );
        }
    }

    /// Slug UNIQUE constraint survives the migration: inserting a second
    /// row with slug='orchestrator-root' is rejected.
    #[test]
    fn migration_013_preserves_slug_uniqueness() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("apply migrations");

        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES ('a', 'A', '/tmp/a', 'orchestrator_root', ?1, ?1, 'orchestrator-root')",
            rusqlite::params![now],
        )
        .expect("first insert");

        let dup = conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES ('b', 'B', '/tmp/b', 'orchestrator_root', ?1, ?1, 'orchestrator-root')",
            rusqlite::params![now],
        );
        assert!(dup.is_err(), "duplicate slug must be rejected by UNIQUE index");
    }

    // ─── Migration 020: drop legacy module_weights_state (v0.2.31 Agent J) ──

    /// After a fresh `apply()`, `module_weights_state` MUST NOT exist on
    /// the schema. Migration 020 drops it; weights state now lives in
    /// `rl_weights_state` shipped by vct-rl-reranker v0.2.6 via its
    /// module-shipped migration (Agent I's mechanism).
    #[test]
    fn migration_020_drops_module_weights_state_on_fresh_db() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("apply migrations");

        // Table must NOT be in sqlite_master.
        let exists: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master \
                 WHERE type = 'table' AND name = 'module_weights_state'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            exists, 0,
            "module_weights_state must be dropped after migration 020"
        );

        // And recorded as applied.
        let max_v: u32 = conn
            .query_row(
                "SELECT COALESCE(MAX(version), 0) FROM _schema_migrations",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(max_v >= 20, "expected at least version 20, got {}", max_v);
    }

    /// Upgrade path: a DB stopped at version 19 (post-Agent-I state)
    /// has the `module_weights_state` table. Running `apply()` to 20+
    /// must drop it cleanly even if rows are present (since we're
    /// dropping the entire table, not migrating data).
    #[test]
    fn migration_020_drops_module_weights_state_with_existing_rows() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply_up_to(&conn, 19).expect("apply up to v19");

        // Sanity: the table exists at v19.
        let exists_v19: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master \
                 WHERE type = 'table' AND name = 'module_weights_state'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(exists_v19, 1, "table must exist at v19");

        // Seed a project + a weights-state row so we exercise the
        // drop-with-data path. The FK on module_weights_state.project_id
        // means we need a real project row to satisfy the constraint.
        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
             VALUES ('p1', 'P1', '/tmp/p1', 'base', 'p1', ?1, ?1)",
            rusqlite::params![now],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO module_weights_state \
                (project_id, module_id, embedding_source, version, last_checked_at, last_finetuned_at) \
             VALUES ('p1', 'vct-rl-reranker', 'qwen3', 'v1', ?1, ?1)",
            rusqlite::params![now],
        )
        .unwrap();

        // Apply migration 020.
        apply(&conn).expect("apply remaining migrations");

        // Table is gone.
        let exists_after: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master \
                 WHERE type = 'table' AND name = 'module_weights_state'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            exists_after, 0,
            "module_weights_state must be dropped after 020 even with existing rows"
        );

        // Projects survive (FK was unidirectional: weights_state → projects).
        let proj_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM projects", [], |r| r.get(0))
            .unwrap();
        assert_eq!(proj_count, 1, "projects must be untouched by the DROP");
    }

    /// Migration 020 is idempotent. Re-running `apply()` on a DB that
    /// already applied it is a no-op (the IF EXISTS guard).
    #[test]
    fn migration_020_is_idempotent() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("first apply");
        // Second apply must succeed without error (recorded migrations
        // are skipped via version check; but the SQL itself is also
        // IF EXISTS-guarded as a belt-and-braces measure).
        apply(&conn).expect("second apply (idempotent)");
    }

    // ─── v0.2.49 Stream A: migration 027 ─────────────────────────────────

    /// On a fresh DB, migration 027 leaves `module_installs.project_id`
    /// nullable and the partial unique indexes in place. An INSERT with
    /// NULL project_id succeeds; a second INSERT with NULL project_id +
    /// same module_id is rejected by the partial-global unique index.
    #[test]
    fn migration_027_accepts_null_project_id_on_fresh_db() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("apply");

        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO module_installs
                (id, project_id, module_id, module_version, install_path, status, enabled, installed_at)
             VALUES ('g1', NULL, 'vct-rl-reranker', '0.2.10', '/path', 'installed', 1, ?1)",
            rusqlite::params![now],
        )
        .expect("global insert must succeed");

        // Second insert with NULL project_id + same module_id must FAIL
        // (the partial unique index `idx_mi_unique_global` enforces this).
        let dup = conn.execute(
            "INSERT INTO module_installs
                (id, project_id, module_id, module_version, install_path, status, enabled, installed_at)
             VALUES ('g2', NULL, 'vct-rl-reranker', '0.2.11', '/path2', 'installed', 1, ?1)",
            rusqlite::params![now],
        );
        assert!(
            dup.is_err(),
            "second global insert for same module_id must be rejected by partial unique index"
        );
    }

    /// Migration 027 preserves all pre-existing per-project rows when
    /// upgrading from version 26 (the post-add-project-codegraph-paths
    /// schema).
    #[test]
    fn migration_027_preserves_existing_per_project_rows_on_upgrade() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply_up_to(&conn, 26).expect("apply up to v26");

        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES ('p1', 'P1', '/tmp/p1', 'base', ?1, ?1, 'p1')",
            rusqlite::params![now],
        )
        .expect("seed project");
        conn.execute(
            "INSERT INTO module_installs
                (id, project_id, module_id, module_version, install_path, status, enabled, installed_at, container_name)
             VALUES ('mi1', 'p1', 'vct-rl-reranker', '0.2.7', '/x', 'installed', 1, ?1, 'reranker-p1')",
            rusqlite::params![now],
        )
        .expect("seed install row");

        // Apply 027.
        apply(&conn).expect("apply remaining");

        // Row survives verbatim.
        let (project_id, module_id, container_name): (Option<String>, String, Option<String>) =
            conn.query_row(
                "SELECT project_id, module_id, container_name FROM module_installs WHERE id = 'mi1'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .expect("read back");
        assert_eq!(project_id.as_deref(), Some("p1"));
        assert_eq!(module_id, "vct-rl-reranker");
        assert_eq!(container_name.as_deref(), Some("reranker-p1"));
    }

    /// After migration 027 a per-project row + a global row for the SAME
    /// module_id can coexist (the partial unique indexes allow this).
    #[test]
    fn migration_027_allows_per_project_and_global_rows_for_same_module() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("apply");

        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES ('p1', 'P1', '/tmp/p1', 'base', ?1, ?1, 'p1')",
            rusqlite::params![now],
        )
        .expect("seed project");
        // Per-project row.
        conn.execute(
            "INSERT INTO module_installs
                (id, project_id, module_id, module_version, install_path, status, enabled, installed_at)
             VALUES ('pp', 'p1', 'mod-x', '0.1.0', '/pp', 'installed', 1, ?1)",
            rusqlite::params![now],
        )
        .expect("per-project insert");
        // Global row.
        conn.execute(
            "INSERT INTO module_installs
                (id, project_id, module_id, module_version, install_path, status, enabled, installed_at)
             VALUES ('g', NULL, 'mod-x', '0.2.0', '/g', 'installed', 1, ?1)",
            rusqlite::params![now],
        )
        .expect("global insert");

        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM module_installs WHERE module_id = 'mod-x'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 2, "both rows coexist");
    }

    /// Per-project UNIQUE(project_id, module_id) is still enforced after
    /// migration 027 (the partial index `idx_mi_unique_per_project`
    /// preserves the constraint for non-NULL project_id).
    #[test]
    fn migration_027_per_project_unique_constraint_still_holds() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("apply");

        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES ('p1', 'P1', '/tmp/p1', 'base', ?1, ?1, 'p1')",
            rusqlite::params![now],
        )
        .expect("seed");
        conn.execute(
            "INSERT INTO module_installs
                (id, project_id, module_id, module_version, install_path, status, enabled, installed_at)
             VALUES ('pp1', 'p1', 'mod-x', '0.1.0', '/pp1', 'installed', 1, ?1)",
            rusqlite::params![now],
        )
        .expect("first");
        let dup = conn.execute(
            "INSERT INTO module_installs
                (id, project_id, module_id, module_version, install_path, status, enabled, installed_at)
             VALUES ('pp2', 'p1', 'mod-x', '0.1.0', '/pp2', 'installed', 1, ?1)",
            rusqlite::params![now],
        );
        assert!(
            dup.is_err(),
            "duplicate (project_id, module_id) pair must be rejected"
        );
    }

    /// Migration 027 is idempotent — running `apply()` twice on a fresh
    /// DB is a no-op the second time.
    #[test]
    fn migration_027_is_idempotent() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("first apply");
        apply(&conn).expect("second apply (idempotent)");
    }

    // ─── v0.2.49 Phase 6 S-4: migration 030 ──────────────────────────────

    /// Pinned schema check: after a fresh `apply()`, the projects table
    /// must carry a `folder_missing_at_last_boot` column of type INTEGER
    /// with DEFAULT 0 and NOT NULL.
    #[test]
    fn migration_030_adds_folder_missing_flag_column() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("apply migrations");

        // Use PRAGMA table_info to verify column shape.
        let mut stmt = conn
            .prepare("PRAGMA table_info(projects)")
            .unwrap();
        let cols: Vec<(String, String, i64, Option<String>, i64)> = stmt
            .query_map([], |r| {
                // table_info: cid, name, type, notnull, dflt_value, pk
                Ok((
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, i64>(3)?,
                    r.get::<_, Option<String>>(4)?,
                    r.get::<_, i64>(5)?,
                ))
            })
            .unwrap()
            .map(|r| r.unwrap())
            .collect();

        let folder_col = cols
            .iter()
            .find(|c| c.0 == "folder_missing_at_last_boot")
            .expect("folder_missing_at_last_boot column must exist after migration 030");
        assert_eq!(
            folder_col.1.to_uppercase(),
            "INTEGER",
            "column type must be INTEGER (SQLite stores BOOLEAN as INTEGER)"
        );
        assert_eq!(folder_col.2, 1, "column must be NOT NULL");
        assert_eq!(
            folder_col.3.as_deref(),
            Some("0"),
            "column DEFAULT must be 0"
        );
    }

    /// Pre-existing rows on upgrade from v29 → v30 backfill the new
    /// column to 0 (= "folder healthy"). The boot probe later flips
    /// individual rows if their folder is actually missing.
    #[test]
    fn migration_030_backfills_existing_rows_to_zero() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply_up_to(&conn, 29).expect("apply up to v29");

        // Seed two pre-030 project rows.
        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES (?1, ?2, ?3, ?4, ?5, ?5, ?6)",
            rusqlite::params!["p1", "Alpha", "/tmp/alpha", "base", now, "alpha"],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES (?1, ?2, ?3, ?4, ?5, ?5, ?6)",
            rusqlite::params!["p2", "Beta", "/tmp/beta", "base", now + 1, "beta"],
        )
        .unwrap();

        // Apply migration 030.
        apply(&conn).expect("apply remaining migrations");

        // Both rows must have the new column populated to 0.
        let mut stmt = conn
            .prepare("SELECT id, folder_missing_at_last_boot FROM projects ORDER BY id")
            .unwrap();
        let rows: Vec<(String, i64)> = stmt
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap()
            .map(|r| r.unwrap())
            .collect();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0], ("p1".to_string(), 0));
        assert_eq!(rows[1], ("p2".to_string(), 0));
    }

    /// Migration 030 is idempotent (the migration runner's version
    /// check guards against double-apply; this test pins the
    /// behaviour as a regression net in case the runner is ever
    /// refactored).
    #[test]
    fn migration_030_is_idempotent() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("first apply");
        apply(&conn).expect("second apply (idempotent)");
    }

    /// ON DELETE CASCADE still removes per-project rows when the parent
    /// project is deleted — the FK relationship was preserved through
    /// the table recreate-and-copy.
    #[test]
    fn migration_027_fk_cascade_still_works_for_per_project_rows() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("apply");

        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES ('p1', 'P1', '/tmp/p1', 'base', ?1, ?1, 'p1')",
            rusqlite::params![now],
        )
        .expect("seed project");
        conn.execute(
            "INSERT INTO module_installs
                (id, project_id, module_id, module_version, install_path, status, enabled, installed_at)
             VALUES ('pp', 'p1', 'mod-x', '0.1.0', '/pp', 'installed', 1, ?1)",
            rusqlite::params![now],
        )
        .expect("pp insert");
        conn.execute(
            "INSERT INTO module_installs
                (id, project_id, module_id, module_version, install_path, status, enabled, installed_at)
             VALUES ('g', NULL, 'mod-y', '0.1.0', '/g', 'installed', 1, ?1)",
            rusqlite::params![now],
        )
        .expect("global insert");

        conn.execute("DELETE FROM projects WHERE id = 'p1'", [])
            .expect("delete project");

        let pp_remaining: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM module_installs WHERE project_id = 'p1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(pp_remaining, 0, "per-project rows cascade-deleted");

        let global_remaining: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM module_installs WHERE project_id IS NULL",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            global_remaining, 1,
            "global rows untouched by per-project delete"
        );
    }

    // ─── Migration 031: force-upgrade shared read→write ──────────────────
    //
    // Step D / Phase 7 of the v0.2.49 access-matrix overhaul. Force-
    // upgrades every kg_collection_access row with access_level='read'
    // whose collection_name is on a project_kg_bindings row with
    // role='shared'. Per user directive 2026-06-08 ("force-update
    // everything to new default permissions"), no per-row predicate
    // gate is applied — legacy explicit downgrades on shared
    // collections are deliberately overwritten as part of the v0.2.49
    // semantic flip.

    /// Helper: seed a project + a shared kg binding + an access row
    /// with the given access_level. Returns the (project_id,
    /// collection_name) tuple so the test can assert post-migration.
    fn seed_shared_access(
        conn: &Connection,
        proj_id: &str,
        coll_name: &str,
        access_level: &str,
    ) {
        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES (?1, ?1, ?2, 'base', ?3, ?3, ?1)",
            rusqlite::params![proj_id, format!("/tmp/{}", proj_id), now],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO project_kg_bindings
                (project_id, role, collection_name, config_json, updated_at)
             VALUES (?1, 'shared', ?2, '{}', ?3)",
            rusqlite::params![proj_id, coll_name, now],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO kg_collection_access
                (project_id, collection_name, access_level, created_at, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?4)",
            rusqlite::params![proj_id, coll_name, access_level, now],
        )
        .unwrap();
    }

    /// Helper: seed a project + a PRIMARY kg binding + an access row.
    /// Used to verify the migration does NOT touch non-shared roles.
    fn seed_primary_access(
        conn: &Connection,
        proj_id: &str,
        coll_name: &str,
        access_level: &str,
    ) {
        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES (?1, ?1, ?2, 'base', ?3, ?3, ?1)",
            rusqlite::params![proj_id, format!("/tmp/{}", proj_id), now],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO project_kg_bindings
                (project_id, role, collection_name, config_json, updated_at)
             VALUES (?1, 'primary', ?2, '{}', ?3)",
            rusqlite::params![proj_id, coll_name, now],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO kg_collection_access
                (project_id, collection_name, access_level, created_at, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?4)",
            rusqlite::params![proj_id, coll_name, access_level, now],
        )
        .unwrap();
    }

    #[test]
    fn migration_031_upgrades_shared_read_rows_to_write() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        // Apply through 030 first — same shape as the launcher's
        // staged-rollout case (user on v0.2.48, hasn't yet applied 31).
        apply_up_to(&conn, 30).expect("apply through v30");

        // Seed: 1 shared-read row that should be upgraded.
        seed_shared_access(&conn, "p1", "Shared_KG", "read");

        // Pre-flight: verify state is what we think it is.
        let pre_level: String = conn
            .query_row(
                "SELECT access_level FROM kg_collection_access
                  WHERE project_id = 'p1' AND collection_name = 'Shared_KG'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(pre_level, "read");

        // Apply migration 31.
        apply(&conn).expect("apply 31");

        // Post: upgraded to write.
        let post_level: String = conn
            .query_row(
                "SELECT access_level FROM kg_collection_access
                  WHERE project_id = 'p1' AND collection_name = 'Shared_KG'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(post_level, "write");

        // Post: updated_at bumped past created_at — is_user_configured
        // flips to TRUE for the rewritten row (matches the audit-trail
        // semantic per the migration's docstring).
        let (created_at, updated_at): (i64, i64) = conn
            .query_row(
                "SELECT created_at, updated_at FROM kg_collection_access
                  WHERE project_id = 'p1' AND collection_name = 'Shared_KG'",
                [],
                |r| Ok((r.get(0).unwrap(), r.get(1).unwrap())),
            )
            .unwrap();
        assert!(
            updated_at > created_at,
            "migration must bump updated_at past created_at; got created={} updated={}",
            created_at, updated_at,
        );
    }

    #[test]
    fn migration_031_preserves_write_level_but_stamps_sentinel_updated_at() {
        // v0.2.49 Step F SF1 + Q4 semantics update: migration 031 now
        // ALSO sentinel-stamps surviving rows' `updated_at = created_at + 1`
        // (Pass 3 of the SQL) so `is_user_configured` reads FALSE for
        // them post-migration. Pre-Step-F this test asserted "migration
        // must not touch write rows" — but per the user Q3 directive
        // verbatim ("is_user_configured = FALSE for them, since those
        // were set to their default programmatically") the surviving
        // rows MUST be re-stamped to the sentinel.
        //
        // What this test still pins:
        //   - access_level stays 'write' (no privilege change)
        //   - updated_at gets sentinel-stamped (semantic alignment)
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply_up_to(&conn, 30).expect("apply through v30");

        // Pre-existing write row on a shared binding — semantic stays
        // write but updated_at gets sentinel-stamped post-migration.
        seed_shared_access(&conn, "p1", "AlreadyWrite_KG", "write");

        apply(&conn).expect("apply 31");

        // Level unchanged.
        let level: String = conn
            .query_row(
                "SELECT access_level FROM kg_collection_access
                  WHERE project_id = 'p1' AND collection_name = 'AlreadyWrite_KG'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(level, "write", "write level must be preserved (no privilege change)");

        // updated_at sentinel-stamped to created_at + 1.
        let (created_at, updated_at): (i64, i64) = conn
            .query_row(
                "SELECT created_at, updated_at FROM kg_collection_access
                  WHERE project_id = 'p1' AND collection_name = 'AlreadyWrite_KG'",
                [],
                |r| Ok((r.get(0).unwrap(), r.get(1).unwrap())),
            )
            .unwrap();
        assert_eq!(
            updated_at, created_at + 1,
            "v0.2.49 Step F: surviving rows must be sentinel-stamped \
             (updated_at = created_at + 1) per user Q3 directive — \
             is_user_configured reads FALSE downstream",
        );
    }

    #[test]
    fn migration_031_preserves_none_rows_on_shared() {
        // The migration only upgrades READ→WRITE on shared. NONE rows
        // on shared (e.g. user explicitly revoked access via the
        // CrossProjectAccessTab UI) are intentionally not upgraded.
        // The plan-level user directive was "force-update everything
        // to new DEFAULT permissions" — the default flip is read→write;
        // explicit none stays explicit none.
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply_up_to(&conn, 30).expect("apply through v30");

        seed_shared_access(&conn, "p1", "ExplicitNone_KG", "none");

        apply(&conn).expect("apply 31");

        let level: String = conn
            .query_row(
                "SELECT access_level FROM kg_collection_access
                  WHERE project_id = 'p1' AND collection_name = 'ExplicitNone_KG'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(level, "none", "migration must not touch explicit none");
    }

    #[test]
    fn migration_031_does_not_touch_primary_role_rows() {
        // Primary bindings are the project's OWN collections; access
        // for the OWNER is always write per the structural-row
        // contract (V44-C). Non-owner reads on a primary are a
        // legitimate distinct semantic (cross-project access matrix
        // grant). Migration must not conflate.
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply_up_to(&conn, 30).expect("apply through v30");

        // Seed: project p1 with a PRIMARY binding to P1Primary_KG,
        // and a cross-project read grant from p2 to that same
        // collection. The grant from p2 is on a 'primary' role from
        // p1's perspective — not shared. Migration must NOT upgrade.
        seed_primary_access(&conn, "p1", "P1Primary_KG", "write");
        // p2 has a read grant on p1's primary. Insert directly
        // (no shared binding from p2).
        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES ('p2', 'p2', '/tmp/p2', 'base', ?1, ?1, 'p2')",
            rusqlite::params![now],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO kg_collection_access
                (project_id, collection_name, access_level, created_at, updated_at)
             VALUES ('p2', 'P1Primary_KG', 'read', ?1, ?1)",
            rusqlite::params![now],
        )
        .unwrap();

        apply(&conn).expect("apply 31");

        // p2's read on p1's primary must remain read.
        let p2_level: String = conn
            .query_row(
                "SELECT access_level FROM kg_collection_access
                  WHERE project_id = 'p2' AND collection_name = 'P1Primary_KG'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            p2_level, "read",
            "cross-project read on a non-shared role must NOT be auto-upgraded",
        );
    }

    #[test]
    fn migration_031_is_idempotent_on_re_apply() {
        // Run migration 31 twice via raw SQL (simulating a defensive
        // re-application — the runner itself would only run it once
        // via _schema_migrations). The second application must be a
        // no-op (WHERE access_level='read' filter excludes the
        // already-upgraded rows).
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("apply all"); // includes 31

        seed_shared_access(&conn, "p1", "Shared_KG", "read");
        // First explicit raw re-apply (NOT through the runner, which
        // would skip).
        conn.execute_batch(MIGRATIONS[30].sql).expect("re-apply 31");
        let after_first: i64 = conn
            .query_row(
                "SELECT updated_at FROM kg_collection_access
                  WHERE project_id = 'p1' AND collection_name = 'Shared_KG'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        // Sleep enough that strftime returns a strictly larger value.
        std::thread::sleep(std::time::Duration::from_millis(1100));
        // Second explicit raw re-apply.
        conn.execute_batch(MIGRATIONS[30].sql).expect("re-apply 31 again");
        let after_second: i64 = conn
            .query_row(
                "SELECT updated_at FROM kg_collection_access
                  WHERE project_id = 'p1' AND collection_name = 'Shared_KG'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            after_second, after_first,
            "second re-apply must be a no-op (WHERE excludes upgraded rows); \
             updated_at went from {} to {}",
            after_first, after_second,
        );
    }

    // ─── Step F SF1 + Q4 — Pass 2 (DELETE cross-project peer rows) ─────
    //
    // Per user verdict 2026-06-08 (msg 234, verbatim "ok for this" to
    // option a): migration 031 ALSO drops cross-project peer rows that
    // aren't part of the v0.2.49 default keep-list (own primary + own
    // dev + own shared, plus corruption carve-out for projects with no
    // role='primary' binding). Tests below pin the Pass 2 behaviour
    // and the sentinel-updated_at semantic.

    /// Helper: seed a project + its primary AND shared bindings + the
    /// 3 default access rows shipped by populate (primary, dev, shared
    /// — all at the level passed). Matches the v0.2.49 default keep-list
    /// shape exactly. Useful as a fixture for the DELETE tests.
    fn seed_full_v0249_default_project(
        conn: &Connection,
        proj_id: &str,
        project_name: &str,
        shared_collection: &str,
    ) {
        let now: i64 = 1_700_000_000_000;
        let primary_collection = format!("{}_KnowledgeGraph", project_name);
        let dev_collection = format!("{}_Development", project_name);
        conn.execute(
            "INSERT OR IGNORE INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES (?1, ?2, ?3, 'base', ?4, ?4, ?1)",
            rusqlite::params![proj_id, project_name, format!("/tmp/{}", proj_id), now],
        ).unwrap();
        conn.execute(
            "INSERT INTO project_kg_bindings (project_id, role, collection_name, config_json, updated_at)
             VALUES (?1, 'primary', ?2, '{}', ?3)",
            rusqlite::params![proj_id, primary_collection, now],
        ).unwrap();
        conn.execute(
            "INSERT INTO project_kg_bindings (project_id, role, collection_name, config_json, updated_at)
             VALUES (?1, 'shared', ?2, '{}', ?3)",
            rusqlite::params![proj_id, shared_collection, now],
        ).unwrap();
        for (coll, lvl) in [
            (primary_collection.as_str(), "write"),
            (dev_collection.as_str(), "write"),
            (shared_collection, "write"),
        ] {
            conn.execute(
                "INSERT INTO kg_collection_access
                    (project_id, collection_name, access_level, created_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?4)",
                rusqlite::params![proj_id, coll, lvl, now],
            ).unwrap();
        }
    }

    #[test]
    fn migration_031_drops_cross_project_peer_rows() {
        // Setup: project p1 has its own default 3-row matrix, plus an
        // extra row for project p2's primary KG (cross-project peer
        // grant from a pre-v0.2.49 install). Migration 031's Pass 2
        // MUST drop the cross-project peer row.
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply_up_to(&conn, 30).expect("apply through v30");

        seed_full_v0249_default_project(&conn, "p1", "Acme", "VibeCodedOrchestrator_KnowledgeGraph");
        seed_full_v0249_default_project(&conn, "p2", "Beta", "VibeCodedOrchestrator_KnowledgeGraph");

        // p1 has a cross-project peer grant on p2's primary KG.
        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO kg_collection_access
                (project_id, collection_name, access_level, created_at, updated_at)
             VALUES ('p1', 'Beta_KnowledgeGraph', 'read', ?1, ?1)",
            rusqlite::params![now],
        ).unwrap();

        // Pre-flight: p1 has 4 rows (3 own + 1 cross-project peer).
        let pre_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM kg_collection_access WHERE project_id = 'p1'",
            [], |r| r.get(0),
        ).unwrap();
        assert_eq!(pre_count, 4, "pre-migration: p1 should have 4 rows");

        // Apply migration 31.
        apply(&conn).expect("apply 31");

        // Post: p1 retains its 3 own-rows (primary, dev, shared) but
        // the cross-project peer row on Beta_KnowledgeGraph is DROPPED.
        let post_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM kg_collection_access WHERE project_id = 'p1'",
            [], |r| r.get(0),
        ).unwrap();
        assert_eq!(post_count, 3, "post-migration: p1 should have 3 rows (the cross-project peer was dropped)");

        // Specific row check: the cross-project peer is gone.
        let peer_exists: i64 = conn.query_row(
            "SELECT COUNT(*) FROM kg_collection_access
              WHERE project_id = 'p1' AND collection_name = 'Beta_KnowledgeGraph'",
            [], |r| r.get(0),
        ).unwrap();
        assert_eq!(peer_exists, 0, "cross-project peer row must be dropped per Q4 verdict");
    }

    #[test]
    fn migration_031_preserves_own_primary_own_dev_shared() {
        // Pin the keep-list: own primary, own dev (derived via REPLACE
        // _KnowledgeGraph→_Development), own shared all survive Pass 2.
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply_up_to(&conn, 30).expect("apply through v30");

        seed_full_v0249_default_project(&conn, "p1", "Acme", "VibeCodedOrchestrator_KnowledgeGraph");

        apply(&conn).expect("apply 31");

        // All 3 own rows still present.
        for collection in &["Acme_KnowledgeGraph", "Acme_Development", "VibeCodedOrchestrator_KnowledgeGraph"] {
            let exists: i64 = conn.query_row(
                "SELECT COUNT(*) FROM kg_collection_access
                  WHERE project_id = 'p1' AND collection_name = ?1",
                rusqlite::params![collection], |r| r.get(0),
            ).unwrap();
            assert_eq!(exists, 1, "{} must survive Pass 2 (in default keep-list)", collection);
        }
    }

    #[test]
    fn migration_031_corruption_carve_out_preserves_orphan_project_rows() {
        // Corrupted state: a project has kg_collection_access rows but
        // NO role='primary' binding. Per the v0.2.49 access-matrix follow-up + the
        // migration's docstring, those rows MUST be preserved (no auto-
        // destroy user data discipline). User recovery: re-register
        // via the GUI Identity tab to heal the corrupted state.
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply_up_to(&conn, 30).expect("apply through v30");

        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
             VALUES ('corrupt-p1', 'Corrupted', '/tmp/corrupt', 'base', ?1, ?1, 'corrupt-p1')",
            rusqlite::params![now],
        ).unwrap();
        // NO project_kg_bindings rows for corrupt-p1 (the corruption).
        // 2 orphan access rows that "shouldn't exist" but do.
        conn.execute(
            "INSERT INTO kg_collection_access
                (project_id, collection_name, access_level, created_at, updated_at)
             VALUES ('corrupt-p1', 'Orphan_A', 'read', ?1, ?1),
                    ('corrupt-p1', 'Orphan_B', 'write', ?1, ?1)",
            rusqlite::params![now],
        ).unwrap();

        apply(&conn).expect("apply 31");

        // Corruption carve-out: both orphan rows are PRESERVED.
        let count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM kg_collection_access WHERE project_id = 'corrupt-p1'",
            [], |r| r.get(0),
        ).unwrap();
        assert_eq!(
            count, 2,
            "corruption carve-out: orphan project rows must be preserved \
             (the user re-registers via GUI to heal; auto-destruction \
             would surprise them with no recovery path)"
        );
    }

    #[test]
    fn migration_031_sentinel_keeps_is_user_configured_false() {
        // Pin the sentinel semantic: post-migration, surviving rows
        // have updated_at == created_at + 1, NOT wall-clock millis.
        // is_user_configured(row) := row.updated_at != row.created_at,
        // so the sentinel == TRUE for the predicate. BUT downstream
        // F-2c logic relies on distinguishing "the user explicitly
        // touched this row" from "the migration touched it". Per user
        // directive (Q3 verdict): post-migration these rows ARE
        // considered "system-defaulted" (FALSE in spirit), and the
        // canonical signal that future migrations would use is the
        // sentinel value's distinctness from wall-clock millis.
        //
        // This test pins that the sentinel is stamped (NOT wall-clock)
        // — so future-cycle code that wants to distinguish "migrated"
        // from "user-touched" can detect the sentinel by checking
        // `updated_at == created_at + 1`.
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply_up_to(&conn, 30).expect("apply through v30");

        seed_full_v0249_default_project(&conn, "p1", "Acme", "VibeCodedOrchestrator_KnowledgeGraph");

        apply(&conn).expect("apply 31");

        // All 3 surviving rows must have updated_at == created_at + 1.
        let rows = conn.prepare("SELECT collection_name, created_at, updated_at FROM kg_collection_access WHERE project_id = 'p1'")
            .unwrap()
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, i64>(2)?,
                ))
            })
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        for (coll, created_at, updated_at) in rows {
            assert_eq!(
                updated_at, created_at + 1,
                "row {}: expected sentinel updated_at = created_at + 1, got created={} updated={}",
                coll, created_at, updated_at
            );
        }
    }

    // ════════════════════════════════════════════════════════════════
    // v0.2.54 Track D: transactional migration runner.
    //
    // Pre-fix failure mode (audit "no-rollback" finding): a crash or
    // error mid-batch left the migration half-applied AND unrecorded
    // → every subsequent Db::open retried the whole batch → `duplicate
    // column name` → launcher could never boot again.
    // ════════════════════════════════════════════════════════════════

    fn bootstrap_tracking(conn: &Connection) {
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS _schema_migrations (
                version     INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at  INTEGER NOT NULL
             );",
        )
        .unwrap();
    }

    #[test]
    fn failing_migration_rolls_back_all_statements() {
        let conn = Connection::open_in_memory().unwrap();
        bootstrap_tracking(&conn);

        let bad = Migration {
            version: 9001,
            description: "synthetic: valid stmt then invalid stmt",
            sql: "CREATE TABLE trackd_t (id INTEGER PRIMARY KEY);\n\
                  THIS IS NOT SQL;",
        };
        let err = apply_one(&conn, &bad).expect_err("must fail");
        assert!(err.contains("9001"), "error names the migration: {}", err);

        // The valid first statement must have been rolled back.
        let table_exists: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='trackd_t'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(table_exists, 0, "partial batch must roll back");

        // And the version must NOT be recorded.
        let recorded: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM _schema_migrations WHERE version = 9001",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(recorded, 0, "failed migration must not be recorded");
    }

    #[test]
    fn alter_table_migration_is_retryable_after_failure() {
        // The exact production failure shape: ALTER ADD COLUMN succeeds,
        // a later statement fails. Pre-fix, the retry died on
        // `duplicate column name`; post-fix the rollback makes the
        // corrected migration apply cleanly.
        let conn = Connection::open_in_memory().unwrap();
        bootstrap_tracking(&conn);
        conn.execute_batch("CREATE TABLE trackd_base (id INTEGER PRIMARY KEY);")
            .unwrap();

        let bad = Migration {
            version: 9002,
            description: "synthetic: ALTER then failure (migration-029 shape)",
            sql: "ALTER TABLE trackd_base ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0;\n\
                  ALTER TABLE trackd_base ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0;\n\
                  ALTER TABLE nonexistent_table ADD COLUMN boom INTEGER;",
        };
        apply_one(&conn, &bad).expect_err("must fail");

        // Retry with the corrected SQL: must NOT hit duplicate column.
        let fixed = Migration {
            version: 9002,
            description: "synthetic: corrected",
            sql: "ALTER TABLE trackd_base ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0;\n\
                  ALTER TABLE trackd_base ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0;",
        };
        apply_one(&conn, &fixed)
            .expect("corrected migration must apply cleanly after rollback");
        let recorded: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM _schema_migrations WHERE version = 9002",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(recorded, 1);
    }

    #[test]
    fn version_record_is_atomic_with_content() {
        // A migration whose CONTENT succeeds gets BOTH the schema change
        // and the version row (committed together).
        let conn = Connection::open_in_memory().unwrap();
        bootstrap_tracking(&conn);
        let ok = Migration {
            version: 9003,
            description: "synthetic: ok",
            sql: "CREATE TABLE trackd_ok (id INTEGER PRIMARY KEY);",
        };
        apply_one(&conn, &ok).unwrap();
        let recorded: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM _schema_migrations WHERE version = 9003",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(recorded, 1);
        conn.execute("INSERT INTO trackd_ok (id) VALUES (1)", [])
            .unwrap();
    }

    #[test]
    fn self_transactional_list_matches_sql() {
        // The SELF_TRANSACTIONAL_MIGRATIONS allowlist must agree with
        // the .sql contents: a migration carries its own `BEGIN;` iff
        // it is on the list. A future BEGIN-containing migration that
        // isn't listed would explode inside the runner's outer
        // transaction ("cannot start a transaction within a
        // transaction"); a listed migration without BEGIN would lose
        // the atomicity guarantee silently.
        for m in MIGRATIONS {
            let has_begin = m
                .sql
                .lines()
                .map(str::trim)
                .any(|l| l.eq_ignore_ascii_case("BEGIN;") || l.eq_ignore_ascii_case("BEGIN TRANSACTION;"));
            assert_eq!(
                has_begin,
                is_self_transactional(m.version),
                "migration {} ({}): self-transactional list out of sync with sql",
                m.version,
                m.description,
            );
        }
    }

    #[test]
    fn full_apply_still_green_with_transactional_runner() {
        // End-to-end: every production migration applies on a fresh DB
        // through the new transactional path.
        let conn = Connection::open_in_memory().unwrap();
        apply(&conn).expect("full migration ladder must apply");
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM _schema_migrations", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(count as usize, MIGRATIONS.len());
    }

    // ─── Migration 035: index module_access_tokens.token_secret (N1-3) ──────

    /// After a fresh `apply()`, the `idx_module_access_tokens_secret` index
    /// exists on `module_access_tokens(token_secret)`. The hub authenticates
    /// every module request with `WHERE token_secret = ?1`; without this
    /// index that lookup is a full table scan (audit N1-3).
    #[test]
    fn migration_035_creates_token_secret_index() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("apply migrations");

        // Migration 035 must be recorded.
        let max_v: u32 = conn
            .query_row(
                "SELECT COALESCE(MAX(version), 0) FROM _schema_migrations",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(max_v >= 35, "expected at least version 35, got {}", max_v);

        // The index must exist in sqlite_master, attached to the right table.
        let on_table: String = conn
            .query_row(
                "SELECT tbl_name FROM sqlite_master \
                 WHERE type = 'index' AND name = 'idx_module_access_tokens_secret'",
                [],
                |r| r.get(0),
            )
            .expect("idx_module_access_tokens_secret must exist after migration 035");
        assert_eq!(on_table, "module_access_tokens");

        // And the index covers token_secret. PRAGMA index_info lists the
        // indexed columns; column index 2 is the column name.
        let indexed_col: String = conn
            .query_row(
                "SELECT name FROM pragma_index_info('idx_module_access_tokens_secret')",
                [],
                |r| r.get(0),
            )
            .expect("index must cover one column");
        assert_eq!(indexed_col, "token_secret");
    }

    /// Migration 035 is idempotent (the runner's version check guards
    /// double-apply; the `IF NOT EXISTS` in the .sql is belt-and-braces).
    #[test]
    fn migration_035_is_idempotent() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("first apply");
        apply(&conn).expect("second apply (idempotent)");
    }

    /// Upgrade path: a DB stopped at version 34 (pre-N1-3) gains the index
    /// when `apply()` runs the remaining migrations. Pre-existing token rows
    /// survive (the index is additive, no table rebuild).
    #[test]
    fn migration_035_adds_index_on_upgrade_preserving_rows() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply_up_to(&conn, 34).expect("apply up to v34");

        // Index must NOT exist yet at v34.
        let before: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master \
                 WHERE type = 'index' AND name = 'idx_module_access_tokens_secret'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(before, 0, "index must not exist before migration 035");

        // Seed a token row (table created by migration 019).
        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO module_access_tokens \
                (module_id, project_id, token_secret, issued_at, expires_at) \
             VALUES ('mod-x', 'p1', 'deadbeef', ?1, ?1)",
            rusqlite::params![now],
        )
        .expect("seed token row");

        // Apply remaining migrations (035).
        apply(&conn).expect("apply remaining migrations");

        // Index now exists.
        let after: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master \
                 WHERE type = 'index' AND name = 'idx_module_access_tokens_secret'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(after, 1, "index must exist after migration 035");

        // Seeded row survives the additive index.
        let secret: String = conn
            .query_row(
                "SELECT token_secret FROM module_access_tokens WHERE module_id = 'mod-x'",
                [],
                |r| r.get(0),
            )
            .expect("seeded token row must survive");
        assert_eq!(secret, "deadbeef");
    }

    // --- Migration 038: code_graph_builds.status CHECK extended with
    //     'partial' (v0.2.73 C-11 / RT-3) ---

    /// Helper: seed a project row + a code_graph_builds row so the table
    /// rebuild in 038 has data to copy. Assumes the schema is at >= v37
    /// (code_graph_builds exists with the `pid` column). `pid` is set so
    /// the rebuild's column-preservation is exercised, not just assumed.
    fn seed_project_and_build(conn: &Connection, status: &str, pid: Option<i64>) {
        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT OR IGNORE INTO projects \
                (id, name, folder_path, host, created_at, updated_at, slug) \
             VALUES ('p1', 'Alpha', '/tmp/alpha', 'base', ?1, ?1, 'alpha')",
            rusqlite::params![now],
        )
        .expect("seed project row");
        conn.execute(
            "INSERT INTO code_graph_builds \
                (project_id, status, started_at, finished_at, duration_ms, \
                 files_analyzed, languages, joern_used, error_message, log_tail, pid) \
             VALUES ('p1', ?1, ?2, ?3, 1234, 5, '[\"py\"]', 0, NULL, 'tail', ?4)",
            rusqlite::params![status, now, now + 1, pid],
        )
        .expect("seed code_graph_builds row");
    }

    /// On a fresh DB, `apply()` runs 038 and the code_graph_builds table
    /// accepts status='partial'.
    #[test]
    fn migration_038_accepts_partial_status_on_fresh_db() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("apply migrations");

        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects \
                (id, name, folder_path, host, created_at, updated_at, slug) \
             VALUES ('p1', 'Alpha', '/tmp/alpha', 'base', ?1, ?1, 'alpha')",
            rusqlite::params![now],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO code_graph_builds (project_id, status, files_analyzed) \
             VALUES ('p1', 'partial', 5)",
            [],
        )
        .expect("status='partial' must satisfy the extended CHECK");

        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM code_graph_builds WHERE status = 'partial'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    /// The extended CHECK is extended, not relaxed: a bogus status still
    /// fails after 038.
    #[test]
    fn migration_038_still_rejects_unknown_status() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("apply migrations");

        let now: i64 = 1_700_000_000_000;
        conn.execute(
            "INSERT INTO projects \
                (id, name, folder_path, host, created_at, updated_at, slug) \
             VALUES ('p1', 'Alpha', '/tmp/alpha', 'base', ?1, ?1, 'alpha')",
            rusqlite::params![now],
        )
        .unwrap();
        let err = conn.execute(
            "INSERT INTO code_graph_builds (project_id, status, files_analyzed) \
             VALUES ('p1', 'bogus', 5)",
            [],
        );
        assert!(err.is_err(), "unknown status must fail the CHECK");
        let msg = err.unwrap_err().to_string();
        assert!(
            msg.to_lowercase().contains("check"),
            "expected CHECK error, got: {}",
            msg
        );
    }

    /// Upgrade path: a DB stopped at v37 (pre-partial) rebuilds cleanly,
    /// preserving every column INCLUDING `pid`, and afterward accepts
    /// 'partial'. Pins the "carry EVERY column" requirement so a future
    /// edit that drops a column from the _new table fails loudly.
    #[test]
    fn migration_038_preserves_all_columns_including_pid_on_upgrade() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply_up_to(&conn, 37).expect("apply up to v37");

        // 'partial' must NOT be accepted yet at v37.
        seed_project_and_build(&conn, "success", Some(4242));
        let pre = conn.execute(
            "INSERT INTO code_graph_builds (project_id, status, files_analyzed) \
             VALUES ('p1', 'partial', 1)",
            [],
        );
        assert!(pre.is_err(), "'partial' must be rejected before migration 038");

        // Apply 038.
        apply(&conn).expect("apply remaining migrations");

        // The seeded row survived with pid intact (column preservation).
        let (status, files, pid): (String, i64, Option<i64>) = conn
            .query_row(
                "SELECT status, files_analyzed, pid FROM code_graph_builds WHERE project_id = 'p1'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .expect("seeded row must survive the table rebuild");
        assert_eq!(status, "success");
        assert_eq!(files, 5);
        assert_eq!(pid, Some(4242), "pid column must survive the 038 rebuild");

        // Every post-037 column is present on the rebuilt table.
        let mut stmt = conn.prepare("PRAGMA table_info(code_graph_builds)").unwrap();
        let cols: Vec<String> = stmt
            .query_map([], |r| r.get::<_, String>(1))
            .unwrap()
            .map(|r| r.unwrap())
            .collect();
        for expected in [
            "project_id",
            "status",
            "started_at",
            "finished_at",
            "duration_ms",
            "files_analyzed",
            "languages",
            "joern_used",
            "error_message",
            "log_tail",
            "pid",
        ] {
            assert!(
                cols.iter().any(|c| c == expected),
                "column '{}' must survive the 038 rebuild; got {:?}",
                expected,
                cols
            );
        }

        // And now 'partial' is accepted.
        conn.execute(
            "UPDATE code_graph_builds SET status = 'partial' WHERE project_id = 'p1'",
            [],
        )
        .expect("status='partial' must be accepted after migration 038");

        // The status index was recreated.
        let idx: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master \
                 WHERE type = 'index' AND name = 'idx_code_graph_builds_status'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(idx, 1, "idx_code_graph_builds_status must be recreated by 038");
    }

    /// Migration 038 is idempotent (runner version-gate; regression net).
    #[test]
    fn migration_038_is_idempotent() {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        apply(&conn).expect("first apply");
        apply(&conn).expect("second apply (idempotent)");
    }
}
