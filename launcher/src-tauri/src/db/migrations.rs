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
