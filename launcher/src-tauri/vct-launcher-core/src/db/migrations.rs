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
];

/// Apply every migration whose version is greater than the current max applied.
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
        conn.execute_batch(m.sql)
            .map_err(|e| format!("apply migration {} ({}): {}", m.version, m.description, e))?;
        conn.execute(
            "INSERT INTO _schema_migrations (version, description, applied_at) VALUES (?1, ?2, ?3)",
            rusqlite::params![
                m.version,
                m.description,
                chrono::Utc::now().timestamp_millis(),
            ],
        )
        .map_err(|e| format!("record migration {}: {}", m.version, e))?;
    }

    Ok(())
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
                "kg_collection_access",
                "INSERT INTO kg_collection_access (project_id, collection_name, access_level)
                 VALUES ('grantor', 'TestCollection', 'read')",
            ),
            (
                "project_permissions",
                "INSERT INTO project_permissions (project_id, subject, kind, value, granted_at)
                 VALUES ('grantor', 'project', 'allowed_tool', 'Read', 0)",
            ),
            (
                "project_kg_bindings",
                "INSERT INTO project_kg_bindings (project_id, role, collection_name, updated_at)
                 VALUES ('grantor', 'primary', 'TestKG', 0)",
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
}
