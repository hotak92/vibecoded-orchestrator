//! Access-matrix CRUD: KG collections + cross-project codegraph.

use chrono::Utc;
use rusqlite::{params, OptionalExtension};
use serde::Serialize;
use serde_json::Value as JsonValue;

use super::Db;

// ─── W40-B (v0.2.40): boot-time KG binding adoption types ────────────────

/// Outcome counts from `adopt_populated_collections_at_boot`. Returned to
/// the launcher boot path so the init block can log a one-line summary.
///
/// Field semantics:
///   * `adopted` — number of `project_kg_bindings` rows whose
///     `collection_name` was rewritten to an unambiguous Weaviate
///     candidate (exactly one populated class matched the binding's
///     suffix).
///   * `deferred` — number of rows where multiple populated candidates
///     existed; we did NOT pick (preserves user intent), and a warning
///     was logged naming the alternatives so the user can resolve
///     manually via the launcher GUI's "Manage shared KG collection"
///     picker.
///   * `no_change` — rows where the advertised collection already
///     exists in Weaviate (the common happy path) OR a case-sibling
///     exists (the existing case-insensitive self-heal handles those)
///     OR no populated candidate matched (preserves user intent on a
///     genuinely-missing collection — they'll re-create it on demand).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct AdoptionReport {
    pub adopted: usize,
    pub deferred: usize,
    pub no_change: usize,
}

// ─── KG collection access ────────────────────────────────────────────────

/// v0.2.49 access-matrix Phase 2 (item #6) — strongly-typed access
/// level. Wire-stable through `as_str()` / `from_str_strict()` so the
/// SQL column keeps storing `"read" | "write" | "none"` unchanged.
///
/// The enum exists to give Phase 2's resolver (`resolve_default_access_level`)
/// and Stream A's #13 populate path (`populate_kg_collection_access_for_global_module`)
/// a misuse-resistant type to pass around. The string-typed
/// `kg_set_access` / `kg_seed_access` setters remain — most call sites
/// only need `.as_str()` interop. A future v0.2.50+ sweep can flip
/// the setters' signatures to take `AccessLevel` directly; deferred
/// to avoid touching every existing call site in this release.
///
/// Variant semantics:
///   - `Read`: project can SELECT objects from the collection.
///   - `Write`: project can SELECT + INSERT/UPDATE/DELETE.
///   - `Denied`: row exists; project is explicitly denied. SQL wire
///     value is `"none"` (matches the existing `kg_set_access`
///     validator + the CHECK constraint at migration 001).
///
/// "No row exists at all for this (project, collection)" is the
/// caller's `Option::None` — outside the `AccessLevel` value space.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
pub enum AccessLevel {
    Read,
    Write,
    Denied,
}

impl AccessLevel {
    /// Wire-stable SQL value. Used at every call site that needs to
    /// hand the level to the string-typed `kg_set_access` /
    /// `kg_seed_access` setters or to bind into a raw SQL parameter.
    pub fn as_str(self) -> &'static str {
        match self {
            AccessLevel::Read => "read",
            AccessLevel::Write => "write",
            AccessLevel::Denied => "none",
        }
    }

    /// Strict parser — only accepts the canonical SQL wire values.
    /// Unknown strings (typos, capitalization mismatches, legacy
    /// values) return Err, never silently default. The misuse-resistant
    /// half of the wire-stable contract.
    pub fn from_str_strict(s: &str) -> Result<Self, String> {
        match s {
            "read" => Ok(AccessLevel::Read),
            "write" => Ok(AccessLevel::Write),
            "none" => Ok(AccessLevel::Denied),
            other => Err(format!("invalid access level: {:?}", other)),
        }
    }
}

impl std::fmt::Display for AccessLevel {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// v0.2.49 access-matrix Step A.5 — full row shape for
/// `kg_collection_access`, including the audit columns introduced by
/// migration 029.
///
/// Used by `kg_get_access_row` (Phase 1 helper for Phase 2's
/// `is_user_configured` predicate) and any other consumer that needs
/// to read audit data alongside the access level. The plain `String`
/// return of `kg_get_access` is preserved for backward compatibility
/// with the many call sites that only need the level.
///
/// Field semantics:
///   - `access_level`: SQL wire value (`"read" | "write" | "none"`).
///     Phase 2 introduces an `AccessLevel` enum; consumers that want
///     the typed form go via `AccessLevel::from_str_strict`.
///   - `created_at`: wall-clock millis at row INSERT. Legacy rows
///     (pre-migration-029) backfill to 0 — a sentinel that
///     distinguishes them from any row written by v0.2.49+ code.
///   - `updated_at`: wall-clock millis at the most recent UPSERT
///     that touched the row (any change to `access_level`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct KgAccessRow {
    pub project_id: String,
    pub collection_name: String,
    pub access_level: String,
    pub created_at: i64,
    pub updated_at: i64,
}

impl KgAccessRow {
    /// v0.2.49 access-matrix Phase 2 core invariant (item #4):
    /// `is_user_configured(row) := row.updated_at != row.created_at`.
    ///
    /// Semantics:
    ///   - **`false`** when the row is in its seeded state (the seed
    ///     path set `created_at == updated_at`; no subsequent UPSERT
    ///     has touched it). The row carries the system's default
    ///     access value for this (project, collection) pair.
    ///   - **`true`** when the user (or any non-seed code path) has
    ///     UPSERTed the row at least once after its initial seed,
    ///     bumping `updated_at` past `created_at`. The row carries an
    ///     explicit user-chosen value that the system must not
    ///     silently override.
    ///
    /// Legacy rows (pre-migration-029): both timestamps default to 0
    /// → predicate reads `false` (= not user-configured). This is
    /// intentional: the v0.2.49 force-upgrade migration (Step D /
    /// Phase 7) rewrites every legacy `read` shared-row to `write`
    /// unconditionally per user directive 2026-06-08 ("force-update
    /// everything to new default permissions"). After that migration
    /// runs, the rewritten rows have `updated_at > created_at == 0`,
    /// so they correctly read as user-configured by this predicate
    /// for any FUTURE-cycle policy decision (v0.2.50+).
    ///
    /// Used by Phase 5 item #15 (F-2c peer-revoke skip):
    /// `WHERE is_user_configured(row) = false` filters the UPDATE
    /// loop so the user's explicit downgrades on peers' rows are
    /// preserved.
    pub fn is_user_configured(&self) -> bool {
        self.updated_at != self.created_at
    }
}

impl Db {
    pub fn kg_get_access(
        &self,
        project_id: &str,
        collection: &str,
    ) -> Result<Option<String>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT access_level FROM kg_collection_access
                  WHERE project_id = ?1 AND collection_name = ?2",
                params![project_id, collection],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| format!("kg_get_access: {}", e))
    }

    /// v0.2.49 access-matrix Step A.5 — read the full row including
    /// audit columns. Phase 2's `is_user_configured` predicate is a
    /// method on `KgAccessRow`; consumers that need it call this
    /// getter instead of the plain `kg_get_access` (which only
    /// returns the level string).
    ///
    /// Returns `Ok(None)` when no row exists for the (project_id,
    /// collection) pair. `Ok(Some(row))` carries the full row shape.
    pub fn kg_get_access_row(
        &self,
        project_id: &str,
        collection: &str,
    ) -> Result<Option<KgAccessRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT project_id, collection_name, access_level, created_at, updated_at
                   FROM kg_collection_access
                  WHERE project_id = ?1 AND collection_name = ?2",
                params![project_id, collection],
                |r| {
                    Ok(KgAccessRow {
                        project_id: r.get(0)?,
                        collection_name: r.get(1)?,
                        access_level: r.get(2)?,
                        created_at: r.get(3)?,
                        updated_at: r.get(4)?,
                    })
                },
            )
            .optional()
            .map_err(|e| format!("kg_get_access_row: {}", e))
    }

    /// v0.2.49 access-matrix Phase 2 (item #6) — the centralized
    /// default-access resolver. Determines the access level that a
    /// project should have for a collection BY DEFAULT (i.e. when the
    /// access matrix is freshly seeded, no user override applied).
    ///
    /// Single source of truth for the F-2a semantic: "a project owns
    /// its primary + shared bindings → `Write`; everything else →
    /// `Denied`." Replaces the three substring-heuristic-based
    /// classifications previously scattered across `commands::kg`,
    /// `commands::project_env_settings`, and the install.py seed-path
    /// blocks.
    ///
    /// Consumed by:
    ///   - Phase 4 item #10 (hub CLI `vct project create` populate path)
    ///   - Phase 4 item #13 (`populate_kg_collection_access_for_global_module`)
    ///   - install.py parity self-heal seed-path (future v0.2.50 cleanup)
    ///   - The Phase 7 force-upgrade migration's per-row UPDATE target
    ///     value (when running, it explicitly sets `Write` for shared
    ///     rows; the resolver's `Write` return here matches that
    ///     contract).
    ///
    /// **Decision rule** (in evaluation order):
    /// 1. If `project_kg_bindings` has a row matching (project_id,
    ///    collection_name) with role `primary` or `shared` → `Write`.
    ///    These are bindings the project OWNS — full read+write.
    /// 2. Otherwise → `Denied`. Per-project sharing is opt-in: the
    ///    user grants `Read` on someone else's collection through the
    ///    cross-project access matrix UI, not through this default.
    ///
    /// Returns `Err` only on actual SQL errors. Non-existence (no
    /// binding row matching the inputs) returns `Ok(AccessLevel::Denied)`
    /// — that's the "no relationship known" default.
    pub fn resolve_default_access_level(
        &self,
        project_id: &str,
        collection: &str,
    ) -> Result<AccessLevel, String> {
        let guard = self.lock();
        let role: Option<String> = guard
            .query_row(
                "SELECT role
                   FROM project_kg_bindings
                  WHERE project_id = ?1 AND collection_name = ?2
                  LIMIT 1",
                params![project_id, collection],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| format!("resolve_default_access_level: {}", e))?;
        match role.as_deref() {
            Some("primary") | Some("shared") => Ok(AccessLevel::Write),
            // 'archive' or any other role → Denied (not auto-write).
            // Future cycles may add more nuance per role here.
            _ => Ok(AccessLevel::Denied),
        }
    }

    /// v0.2.49 access-matrix Phase 2 (item #5) — V44-C structural-row
    /// guard relocated into the DB layer. Detects whether
    /// `(project_id, collection)` identifies the orchestrator-root
    /// project's primary KG binding. That row is STRUCTURAL: the
    /// orchestrator-root must retain `write` access to its own
    /// primary collection or the install breaks (kg-sync etc. would
    /// refuse to write to the canonical KG).
    ///
    /// Returns:
    ///   - `Ok(true)` if this is the orchestrator-root project + the
    ///     collection equals that project's `role='primary'`
    ///     binding name → caller must refuse to write any non-`write`
    ///     level.
    ///   - `Ok(false)` otherwise (any other project, any non-primary
    ///     collection, missing rows) → caller is free to write any
    ///     valid level.
    ///   - `Err` only on SQL error.
    ///
    /// Pre-v0.2.49 this check lived in `commands::kg::
    /// kg_set_collection_access_mode` (one guard, one call site).
    /// Relocating into `db::access` means EVERY caller of the
    /// `kg_set_access` family — current and future — is guarded by
    /// construction.
    pub fn is_orchestrator_root_structural_row(
        &self,
        project_id: &str,
        collection: &str,
    ) -> Result<bool, String> {
        let owner = self
            .get_project(project_id)
            .map_err(|e| format!("get_project: {}", e))?;
        let Some(owner_row) = owner else { return Ok(false) };
        if owner_row.host != crate::db::models::ProjectHost::OrchestratorRoot {
            return Ok(false);
        }
        let bindings = self
            .list_project_kg_bindings(project_id)
            .map_err(|e| format!("list_project_kg_bindings: {}", e))?;
        Ok(bindings
            .iter()
            .any(|b| b.role == "primary" && b.collection_name == collection))
    }

    pub fn kg_set_access(
        &self,
        project_id: &str,
        collection: &str,
        level: &str,
    ) -> Result<(), String> {
        if !matches!(level, "read" | "write" | "none") {
            return Err(format!("invalid kg access level: {}", level));
        }
        // v0.2.49 access-matrix Phase 2 (item #5) — V44-C structural
        // row guard at the DB layer. Pre-relocation this guard lived
        // in `commands::kg::kg_set_collection_access_mode`; relocating
        // here means every caller of kg_set_access is guarded by
        // construction. The guard's contract: the orchestrator-root
        // project's primary KG collection MUST retain `write` access.
        // Any attempt to demote it to `read` or `none` is rejected
        // with a structural-violation error.
        //
        // The mutation loop in `commands::kg::kg_set_collection_access_mode`
        // skips the owner project entirely (it always writes `write`
        // for the owner before the loop), so this guard doesn't
        // re-trigger mid-loop. The loop iterates peers; for the
        // orchestrator-root project specifically, attempting to
        // demote its structural row from a peer-mode-set call would
        // surface here. Test
        // `kg_set_access_refuses_orchestrator_root_structural_demote`
        // pins this.
        if level != "write"
            && self.is_orchestrator_root_structural_row(project_id, collection)?
        {
            return Err(format!(
                "Refusing to change access level for orchestrator-root's \
                 structural row (collection '{}', level '{}'). The \
                 orchestrator-root project must retain write access to its \
                 primary collection.",
                collection, level,
            ));
        }
        // v0.2.49 access-matrix Step A.5 + Step F SB4: bind both audit
        // columns with conditional updated_at on UPSERT.
        //
        // INSERT path (no conflict): `created_at` and `updated_at`
        // are both set to the same `now` timestamp. This is the
        // load-bearing property that makes the
        // `is_user_configured(row) := row.updated_at != row.created_at`
        // predicate work correctly: a freshly seeded row reads as
        // "not user configured" because the two timestamps match.
        //
        // UPSERT path (conflict) — v0.2.49 Step F SB4 fix (L1-F4):
        // bump `updated_at` ONLY when `access_level` actually changes
        // (real user mutation), NOT on no-op rewrites. Pre-Step-F
        // every UPSERT bumped `updated_at` unconditionally — which
        // meant a no-op write (e.g. F-2c's mode-setter loop hitting
        // a row that was already at the target level) flipped
        // `is_user_configured` to TRUE for that row. F-2c reads
        // `is_user_configured` as load-bearing ("preserve user-
        // configured peers"); the no-op-bumps poisoned the predicate
        // → F-2c silently skipped peers the user never actually
        // touched. Step F Lens 1 (L1-F4) flagged this as SHIP-BLOCKER.
        //
        // The CASE expression below evaluates per-row:
        //   - access_level != excluded.access_level → real change →
        //     bump updated_at to wall-clock millis
        //   - access_level == excluded.access_level → no-op rewrite →
        //     preserve existing updated_at value (predicate stays
        //     stable; F-2c's load-bearing read keeps working)
        //
        // System-driven INSERTs (migrations, boot probes, install.py
        // parity self-heal) should still use the dedicated
        // `kg_seed_access` path (defined below) — it uses
        // `INSERT OR IGNORE` which never bumps either timestamp on
        // re-runs.
        let now = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO kg_collection_access (project_id, collection_name, access_level, created_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?4)
                 ON CONFLICT(project_id, collection_name)
                 DO UPDATE SET access_level = excluded.access_level,
                               updated_at = CASE
                                   WHEN access_level != excluded.access_level
                                       THEN excluded.updated_at
                                   ELSE updated_at
                               END",
                params![project_id, collection, level, now],
            )
            .map_err(|e| format!("kg_set_access: {}", e))?;
        Ok(())
    }

    /// v0.2.49 access-matrix Step A.5 — seed-path INSERT for the
    /// access matrix. Use this from migrations, boot probes, install.py
    /// parity self-heal, and any code path that writes "the system's
    /// default value for this row" rather than "the user's chosen
    /// value." Both audit timestamps are set to the same value,
    /// preserving the seed-path invariant
    /// `created_at == updated_at` so that
    /// `is_user_configured(row) := row.updated_at != row.created_at`
    /// reads FALSE for the row.
    ///
    /// INSERT OR IGNORE semantics: existing rows are NOT overwritten.
    /// This protects user-configured downgrades from being silently
    /// upgraded by a seed-path that doesn't know the user touched the
    /// row. Callers that want to deliberately overwrite should use
    /// `kg_set_access` (which bumps `updated_at`, signalling the user
    /// touched the row).
    ///
    /// v0.2.49 Step F MF1 (L2-MF1) — V44-C structural-row guard:
    /// mirrors the guard in `kg_set_access`. Pre-Step-F the docstring
    /// here falsely claimed "every caller of the kg_set_access family
    /// is guarded by construction" — but `kg_seed_access` had ZERO
    /// guard. A first-INSERT of orchestrator-root's structural row
    /// at 'read' or 'none' would silently bypass the invariant
    /// (`INSERT OR IGNORE` protects EXISTING rows but doesn't protect
    /// the FIRST seed at a bad level). The relocated guard fires on
    /// any seed at a non-'write' level for the orchestrator-root
    /// structural row, matching `kg_set_access`'s discipline.
    ///
    /// Returns the number of rows actually inserted (0 when the row
    /// already exists; 1 when freshly seeded).
    pub fn kg_seed_access(
        &self,
        project_id: &str,
        collection: &str,
        level: &str,
    ) -> Result<usize, String> {
        if !matches!(level, "read" | "write" | "none") {
            return Err(format!("invalid kg access level: {}", level));
        }
        // v0.2.49 Step F MF1 (L2-MF1): V44-C structural-row guard.
        // Same predicate + same error message as `kg_set_access`'s
        // guard above. Defense-in-depth at the seed-path layer so
        // even the system-driven INSERT OR IGNORE path can't
        // accidentally land the orchestrator-root structural row
        // at a non-'write' level on first seed.
        if level != "write"
            && self.is_orchestrator_root_structural_row(project_id, collection)?
        {
            return Err(format!(
                "Refusing to seed access level for orchestrator-root's \
                 structural row (collection '{}', level '{}'). The \
                 orchestrator-root project must retain write access to its \
                 primary collection. (kg_seed_access guard)",
                collection, level,
            ));
        }
        let now = chrono::Utc::now().timestamp_millis();
        let guard = self.lock();
        let n = guard
            .execute(
                "INSERT OR IGNORE INTO kg_collection_access
                    (project_id, collection_name, access_level, created_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?4)",
                params![project_id, collection, level, now],
            )
            .map_err(|e| format!("kg_seed_access: {}", e))?;
        Ok(n)
    }

    pub fn kg_list_access(
        &self,
        project_id: &str,
    ) -> Result<Vec<(String, String)>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT collection_name, access_level FROM kg_collection_access
                  WHERE project_id = ?1 ORDER BY collection_name ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    /// v0.2.49 Step F MF4 (L1-F3): cleanup orphan peer-grant rows when
    /// a project is deleted.
    ///
    /// The FK CASCADE in `001_initial.sql:64` drops rows where
    /// `project_id = deleted_id` — that's the OWNER's own access rows
    /// going away with the project. But `kg_collection_access` ALSO
    /// stores cross-project peer-grant rows: a different (live)
    /// project holding `read` or `write` on the deleted project's
    /// collections. Those rows have `project_id = <live_peer>` but
    /// `collection_name = <deleted_project's_collection>`. The FK
    /// CASCADE doesn't touch them (cascade is on `projects.id`, not
    /// on `collection_name`). Result: peer access rows stay stranded
    /// forever, referencing collections that no longer exist (the
    /// orchestrator's boot reconcile only sweeps when
    /// `purge_collections=true`, which is opt-in).
    ///
    /// This helper drops all peer-grant rows on a list of
    /// collection_names, EXCLUDING rows owned by the deleted project
    /// itself (those are already gone via cascade, or are about to be).
    ///
    /// Caller responsibility:
    ///   - Enumerate the deleted project's collection_names BEFORE
    ///     the cascade DELETE (via `list_project_kg_bindings`), then
    ///     pass them here AFTER the delete. Doing it before is OK
    ///     (the WHERE excludes the deleted project's own rows) but
    ///     after is safer (no race window).
    ///   - Audit-log the result count for observability.
    ///
    /// Returns the number of peer rows deleted.
    pub fn delete_orphan_peer_access_for_collections(
        &self,
        deleted_project_id: &str,
        collection_names: &[String],
    ) -> Result<usize, String> {
        if collection_names.is_empty() {
            return Ok(0);
        }
        // SQLite: build a comma-separated `?` placeholder list for
        // the IN clause. `rusqlite::params_from_iter` handles the
        // value binding.
        let placeholders = (1..=collection_names.len())
            .map(|i| format!("?{}", i + 1))
            .collect::<Vec<_>>()
            .join(",");
        let sql = format!(
            "DELETE FROM kg_collection_access
              WHERE project_id != ?1
                AND collection_name IN ({})",
            placeholders
        );
        let guard = self.lock();
        let mut all_params: Vec<&dyn rusqlite::ToSql> = Vec::with_capacity(1 + collection_names.len());
        all_params.push(&deleted_project_id);
        for c in collection_names {
            all_params.push(c);
        }
        let n = guard
            .execute(&sql, all_params.as_slice())
            .map_err(|e| format!("delete_orphan_peer_access_for_collections: {}", e))?;
        Ok(n)
    }

    /// v0.2.46 Decision A/B/C cousin — delete a single `kg_collection_access`
    /// row by (project_id, collection_name). Idempotent: missing rows
    /// return 0, never error. Required by `reconcile_kg_collection_access`
    /// (boot helper) and `kg_rename_access` (write-time propagation).
    pub fn kg_delete_access(
        &self,
        project_id: &str,
        collection: &str,
    ) -> Result<usize, String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM kg_collection_access
                  WHERE project_id = ?1 AND collection_name = ?2",
                params![project_id, collection],
            )
            .map_err(|e| format!("kg_delete_access: {}", e))
    }

    /// v0.2.46 Decision A/B/C cousin — rename an access row's
    /// `collection_name` for a single (project_id, old). Used by the
    /// on-rebind propagation: when a `project_kg_bindings` row's
    /// `collection_name` changes, the matching access-matrix row(s)
    /// need to point at the new name too.
    ///
    /// Collision handling: if a row already exists at the target name
    /// for the same project, we DELETE the old row (no duplicate keys)
    /// and leave the existing target row's `access_level` UNCHANGED —
    /// matching the "never lower an existing privilege" discipline
    /// from the install.py parity self-heal at line 13383+.
    ///
    /// Returns 1 when a row was renamed or merged, 0 when no source
    /// row existed (idempotent).
    ///
    /// v0.2.46 adversarial-review L3 follow-up: collision-handling now
    /// genuinely "never lowers an existing privilege". Previously the
    /// code dropped the source row unconditionally on collision —
    /// preserving the TARGET row's level, which silently downgraded
    /// when the source was higher. Now it picks the higher level
    /// between (source.access_level, target.access_level) and writes
    /// it to the target before dropping the source.
    pub fn kg_rename_access(
        &self,
        project_id: &str,
        old_collection: &str,
        new_collection: &str,
    ) -> Result<usize, String> {
        if old_collection == new_collection {
            return Ok(0); // trivial no-op
        }
        let guard = self.lock();

        // Probe: read source row's access_level (None if absent).
        let source_level: Option<String> = guard
            .query_row(
                "SELECT access_level FROM kg_collection_access
                  WHERE project_id = ?1 AND collection_name = ?2",
                params![project_id, old_collection],
                |r| r.get::<_, String>(0),
            )
            .optional()
            .map_err(|e| format!("kg_rename_access: source probe: {}", e))?;
        let source_level = match source_level {
            Some(level) => level,
            None => return Ok(0),
        };

        // Probe: read target row's access_level (None if absent).
        let target_level: Option<String> = guard
            .query_row(
                "SELECT access_level FROM kg_collection_access
                  WHERE project_id = ?1 AND collection_name = ?2",
                params![project_id, new_collection],
                |r| r.get::<_, String>(0),
            )
            .optional()
            .map_err(|e| format!("kg_rename_access: target probe: {}", e))?;

        if let Some(existing_target_level) = target_level {
            // Collision. Pick the higher privilege level — never lower
            // an existing privilege (matches install.py parity self-heal).
            // Privilege ordering (low → high): Denied < Read < Write.
            //
            // v0.2.49 Step F SF4 (L2-SF3): parse both strings into the
            // wire-stable `AccessLevel` enum before delegating to the
            // privilege-ordering helper. The DB schema CHECK constraint
            // guarantees the raw values are in {read, write, none}, so
            // `from_str_strict` is statically infallible here — but the
            // explicit parse + Result-propagation makes a future
            // CHECK-weakening surface as an error rather than a silent
            // "unknown → 0" fall-through.
            let source_parsed = AccessLevel::from_str_strict(&source_level)
                .map_err(|e| format!("kg_rename_access: source level parse: {}", e))?;
            let target_parsed = AccessLevel::from_str_strict(&existing_target_level)
                .map_err(|e| format!("kg_rename_access: target level parse: {}", e))?;
            let higher = pick_higher_access_level(source_parsed, target_parsed);
            if higher != target_parsed {
                // Source had a higher level than target — upgrade target.
                guard
                    .execute(
                        "UPDATE kg_collection_access
                            SET access_level = ?1
                          WHERE project_id = ?2 AND collection_name = ?3",
                        params![higher.as_str(), project_id, new_collection],
                    )
                    .map_err(|e| format!("kg_rename_access: upgrade target: {}", e))?;
            }
            // Drop the source row in either case (no duplicate keys).
            guard
                .execute(
                    "DELETE FROM kg_collection_access
                      WHERE project_id = ?1 AND collection_name = ?2",
                    params![project_id, old_collection],
                )
                .map_err(|e| format!("kg_rename_access: drop source on collision: {}", e))?;
            Ok(1)
        } else {
            // Simple rename: UPDATE the row's collection_name.
            let renamed = guard
                .execute(
                    "UPDATE kg_collection_access
                        SET collection_name = ?1
                      WHERE project_id = ?2 AND collection_name = ?3",
                    params![new_collection, project_id, old_collection],
                )
                .map_err(|e| format!("kg_rename_access: UPDATE: {}", e))?;
            Ok(renamed)
        }
    }

    /// v0.2.49 access-matrix Phase 5 item #16 (M-1): VCO-managed
    /// collection-name predicate. Returns true when the collection name
    /// matches one of the orchestrator's well-known suffix patterns
    /// (per-project KG + Development + Diagrams + the five code-graph
    /// entity classes).
    ///
    /// Used by `reconcile_kg_collection_access` to scope the orphan-drop
    /// loop: a row is only a candidate for dropping when its collection
    /// name is either named by an active binding OR matches a VCO
    /// suffix. Names that don't match (user-created Weaviate classes
    /// for their own experiments, classes belonging to other tools that
    /// happen to share the Weaviate instance, etc.) MUST be preserved
    /// unconditionally — they're outside VCO's stewardship.
    ///
    /// Suffix list mirrors the schema-creation paths in
    /// `vco_lib/weaviate_schema.py`: per-project bindings produce
    /// `<Prefix>_KnowledgeGraph` + `<Prefix>_Development` +
    /// `<Prefix>_Diagrams`, while code-graph analysis produces the five
    /// `<Prefix>_Code{Module,Class,Function,API,Interaction}` classes.
    /// Bare-name code classes (`CodeFunction` etc.) are intentionally
    /// NOT matched here — those are legacy pre-multi-project data and
    /// dropping their access rows could surprise a user who's still
    /// keeping them around for migration.
    fn is_vco_managed_collection_name(name: &str) -> bool {
        const VCO_SUFFIXES: &[&str] = &[
            "_KnowledgeGraph",
            "_Development",
            "_Diagrams",
            "_CodeModule",
            "_CodeClass",
            "_CodeFunction",
            "_CodeAPI",
            "_CodeInteraction",
        ];
        VCO_SUFFIXES.iter().any(|suffix| name.ends_with(suffix))
    }

    /// v0.2.46 Decision A/B/C cousin — boot-time reconciliation of
    /// `kg_collection_access` against current binding rows + Weaviate
    /// schema.
    ///
    /// **v0.2.49 access-matrix Phase 5 item #16 (M-1)**: the orphan-drop
    /// loop is scoped to VCO-managed collections only. A row is dropped
    /// iff ALL three hold:
    /// 1. The collection name doesn't appear as a `collection_name` in
    ///    ANY `project_kg_bindings` row for ANY project on this machine
    ///    (= no binding owns it), AND
    /// 2. The collection name doesn't appear in `existing_classes` (=
    ///    Weaviate doesn't have it either), AND
    /// 3. The collection name matches a VCO-managed suffix pattern
    ///    (`_KnowledgeGraph`, `_Development`, `_Diagrams`, or one of the
    ///    five code-graph entity suffixes) — i.e. only VCO's own
    ///    collections are subject to orphan-drop.
    ///
    /// Preserves rows where ANY condition fails:
    /// - The class exists in Weaviate but no local binding names it
    ///   (peer-access to a peer's collection; user may have manually
    ///   granted this via the access-matrix GUI).
    /// - A binding names the collection but Weaviate doesn't have it
    ///   yet (binding owns the lazy-create expectation).
    /// - The collection name doesn't match a VCO-managed suffix —
    ///   user's own Weaviate classes (their experiments, other tools'
    ///   collections) are outside VCO's stewardship and MUST NOT be
    ///   touched by boot reconcile. Item #16's invariant.
    ///
    /// Idempotent: a second call after a successful reconcile finds no
    /// rows matching the drop predicate.
    ///
    /// The caller is responsible for fetching `existing_classes` from
    /// Weaviate (mirroring `adopt_populated_collections_at_boot`'s
    /// schema-probe pattern). This function is pure-SQL so it's safe
    /// to test without network mocks.
    ///
    /// Returns the count of dropped rows.
    pub fn reconcile_kg_collection_access(
        &self,
        existing_classes: &std::collections::HashSet<String>,
    ) -> Result<usize, String> {
        let guard = self.lock();

        // Collect all (project_id, collection_name) pairs from the
        // access matrix.
        let mut stmt = guard
            .prepare(
                "SELECT project_id, collection_name FROM kg_collection_access",
            )
            .map_err(|e| format!("reconcile: prepare list: {}", e))?;
        let access_rows: Vec<(String, String)> = stmt
            .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
            .map_err(|e| format!("reconcile: query list: {}", e))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("reconcile: collect list: {}", e))?;
        drop(stmt);

        // Collect every collection name that appears in any binding row.
        let mut stmt2 = guard
            .prepare(
                "SELECT DISTINCT collection_name FROM project_kg_bindings",
            )
            .map_err(|e| format!("reconcile: prepare bindings: {}", e))?;
        let binding_collections: std::collections::HashSet<String> = stmt2
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(|e| format!("reconcile: query bindings: {}", e))?
            .collect::<Result<std::collections::HashSet<_>, _>>()
            .map_err(|e| format!("reconcile: collect bindings: {}", e))?;
        drop(stmt2);

        let mut dropped: usize = 0;
        for (project_id, collection_name) in &access_rows {
            let has_binding = binding_collections.contains(collection_name);
            let has_class = existing_classes.contains(collection_name);
            // v0.2.49 Phase 5 item #16 (M-1): only orphan-drop rows
            // whose collection name is VCO-managed. User-created
            // classes that happen to have an access row (e.g. from a
            // pre-v0.2.49 install that wrote a row before the scope
            // tightened) are preserved unconditionally — orphan-drop
            // is VCO's invariant maintenance, not user-collection
            // garbage collection.
            let is_vco_managed = Self::is_vco_managed_collection_name(collection_name);
            if !has_binding && !has_class && is_vco_managed {
                guard
                    .execute(
                        "DELETE FROM kg_collection_access
                          WHERE project_id = ?1 AND collection_name = ?2",
                        params![project_id, collection_name],
                    )
                    .map_err(|e| {
                        format!(
                            "reconcile: DELETE ({}, {}): {}",
                            project_id, collection_name, e
                        )
                    })?;
                dropped += 1;
                eprintln!(
                    "[vct] reconcile-kg-access: dropped orphan project_id={} collection={}",
                    project_id, collection_name
                );
            }
        }
        Ok(dropped)
    }

    /// v0.2.46 Decision A/B/C cousin — async wrapper around
    /// `reconcile_kg_collection_access` that probes Weaviate's `/v1/schema`
    /// to build the `existing_classes` set, then delegates to the pure-SQL
    /// helper. Suitable for the launcher boot-init sequence.
    ///
    /// Soft-fail: Weaviate unreachable → return `Err` so the caller can
    /// log + continue without blocking boot. Same shape as
    /// `adopt_populated_collections_at_boot`.
    pub async fn reconcile_kg_collection_access_at_boot(
        &self,
        weaviate_url: &str,
    ) -> Result<usize, String> {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(5))
            .build()
            .map_err(|e| format!("reconcile_at_boot: reqwest client: {}", e))?;

        let schema_url = format!("{}/v1/schema", weaviate_url.trim_end_matches('/'));
        let resp = client
            .get(&schema_url)
            .send()
            .await
            .map_err(|e| format!("reconcile_at_boot: GET {}: {}", schema_url, e))?;
        if !resp.status().is_success() {
            return Err(format!(
                "reconcile_at_boot: {} returned status {}",
                schema_url,
                resp.status().as_u16()
            ));
        }
        let schema: JsonValue = resp
            .json()
            .await
            .map_err(|e| format!("reconcile_at_boot: parse {}: {}", schema_url, e))?;

        let existing_classes: std::collections::HashSet<String> = schema
            .get("classes")
            .and_then(|c| c.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|c| {
                        c.get("class").and_then(|v| v.as_str()).map(String::from)
                    })
                    .collect()
            })
            .unwrap_or_default();

        self.reconcile_kg_collection_access(&existing_classes)
    }

    /// Init-time migration: rewrite legacy shared-KG collection names in
    /// `kg_collection_access` to the current canonical name.
    ///
    /// Two legacy names are handled:
    /// * `VibeCodedTools_KnowledgeGraph` — pre-v0.2.12 PR-26 rename
    /// * `VibecodedOrchestrator_KnowledgeGraph` — v0.2.12–v0.2.22
    ///   lowercase-c default
    ///
    /// For each legacy name the operation is:
    /// 1. DELETE rows where BOTH the legacy name AND the canonical name
    ///    already exist for the same `project_id` (dedup — the canonical
    ///    row wins and the legacy duplicate is dropped).
    /// 2. UPDATE the remaining legacy rows to the canonical name.
    ///
    /// Returns the total number of rows renamed across both legacy names.
    /// Idempotent: a second call with no legacy rows is a no-op returning 0.
    ///
    /// Soft-fail contract: any DB error is returned to the caller; the
    /// caller (launcher `setup()`) logs and continues — a migration hiccup
    /// MUST NOT block launcher boot.
    ///
    /// // NEW-12 (2026-05-28): init-time migration for legacy shared-KG names
    pub fn migrate_legacy_shared_kg_collection_names(
        &self,
        canonical: &str,
    ) -> Result<usize, String> {
        // Pre-v0.2.12 name (PR-26 rename). Mirrors
        // `launcher::commands::project_env_settings::LEGACY_SHARED_KG_COLLECTION`.
        const LEGACY_TOOLS: &str = "VibeCodedTools_KnowledgeGraph";
        // v0.2.12–v0.2.22 lowercase-c name. Mirrors
        // `launcher::commands::project_env_settings::LEGACY_SHARED_KG_COLLECTION_LOWERCASE_C`.
        const LEGACY_LOWERCASE_C: &str = "VibecodedOrchestrator_KnowledgeGraph";

        let mut total_renamed: usize = 0;

        for legacy in &[LEGACY_TOOLS, LEGACY_LOWERCASE_C] {
            if *legacy == canonical {
                // Safety guard: never rename a name to itself.
                continue;
            }

            let guard = self.lock();

            // Step 1: delete duplicate legacy rows where the canonical row
            // already exists for the same project_id.
            let deleted = guard
                .execute(
                    "DELETE FROM kg_collection_access
                      WHERE collection_name = ?1
                        AND project_id IN (
                              SELECT project_id FROM kg_collection_access
                              WHERE collection_name = ?2
                            )",
                    params![legacy, canonical],
                )
                .map_err(|e| {
                    format!(
                        "migrate_legacy_shared_kg: dedup-delete ({} → {}): {}",
                        legacy, canonical, e
                    )
                })?;

            // Step 2: rename the remaining legacy rows.
            let renamed = guard
                .execute(
                    "UPDATE kg_collection_access
                        SET collection_name = ?1
                      WHERE collection_name = ?2",
                    params![canonical, legacy],
                )
                .map_err(|e| {
                    format!(
                        "migrate_legacy_shared_kg: rename ({} → {}): {}",
                        legacy, canonical, e
                    )
                })?;

            if deleted > 0 || renamed > 0 {
                eprintln!(
                    "[vct] migrate-shared-kg: \
                     legacy='{}' canonical='{}' \
                     dedup_deleted={} renamed={}",
                    legacy, canonical, deleted, renamed
                );
            }

            total_renamed += renamed;
        }

        Ok(total_renamed)
    }

    /// v0.2.46 Decision A — boot-time backfill for orchestrator-root
    /// primary/shared row alignment.
    ///
    /// Background: the GUI's `KG / Codegraph` tab saves one
    /// `project_kg_bindings` row at a time (one role per save).
    /// Pre-v0.2.46, saving `Role=primary` for the orchestrator-root
    /// project did NOT touch the `Role=shared` row, leaving the two
    /// out-of-sync. The v0.2.40 W40-B research doc §4b documented this
    /// as the canonical drift class on the maintainer's dev machine.
    ///
    /// v0.2.46 fixes this at write-time via
    /// `set_project_kg_binding_with_root_sync` in `project_state.rs`.
    /// THIS function backfills the same invariant at boot, for users
    /// upgrading from a release where the write-time sync didn't exist
    /// yet (every release v0.2.12-v0.2.45).
    ///
    /// Behaviour:
    /// - Looks up the orchestrator-root project by slug.
    /// - If the project has a `role='primary'` binding row AND a
    ///   `role='shared'` row that names a DIFFERENT `collection_name`,
    ///   rewrites the shared row's `collection_name` to match primary.
    ///   The shared row gets `config_json.manual_override =
    ///   "v0.2.46-sync-shared-to-primary-boot"` so a downstream auditor
    ///   can see which boot did the fix.
    /// - If the project has primary but NO shared row, INSERTs the
    ///   shared row pointing at primary's `collection_name`.
    /// - If primary doesn't exist at all (fresh machine, no
    ///   orchestrator-root yet), no-op.
    /// - If shared == primary already, no-op (idempotent).
    ///
    /// Soft-fail contract: returns Result; the boot caller logs and
    /// continues. Same shape as `migrate_legacy_shared_kg_collection_names`.
    ///
    /// Returns `(updated: usize, inserted: usize)` so the caller can
    /// surface what happened.
    pub fn sync_shared_to_primary_for_orchestrator_root(
        &self,
    ) -> Result<(usize, usize), String> {
        const ORCH_SLUG: &str = "orchestrator-root";
        let now = chrono::Utc::now().timestamp_millis();

        let guard = self.lock();

        // Step 1: find orchestrator-root project_id.
        let project_id: Option<String> = guard
            .query_row(
                "SELECT id FROM projects WHERE slug = ?1",
                params![ORCH_SLUG],
                |r| r.get::<_, String>(0),
            )
            .ok();
        let project_id = match project_id {
            Some(p) => p,
            None => return Ok((0, 0)), // no orchestrator-root yet → nothing to sync
        };

        // Step 2: read primary collection_name. If absent, nothing to mirror.
        let primary_name: Option<String> = guard
            .query_row(
                "SELECT collection_name FROM project_kg_bindings
                 WHERE project_id = ?1 AND role = 'primary'",
                params![project_id],
                |r| r.get::<_, String>(0),
            )
            .ok();
        let primary_name = match primary_name {
            Some(name) if !name.is_empty() => name,
            _ => return Ok((0, 0)),
        };

        // Step 3: read current shared row (if any).
        let shared_now: Option<String> = guard
            .query_row(
                "SELECT collection_name FROM project_kg_bindings
                 WHERE project_id = ?1 AND role = 'shared'",
                params![project_id],
                |r| r.get::<_, String>(0),
            )
            .ok();

        // Step 4: decide path.
        match shared_now {
            Some(ref existing) if existing == &primary_name => {
                // Aligned already; no-op (idempotent).
                Ok((0, 0))
            }
            Some(_) => {
                // Existing shared row disagrees with primary; UPDATE.
                let cfg = format!(
                    "{{\"manual_override\":\"v0.2.46-sync-shared-to-primary-boot\"}}"
                );
                let updated = guard
                    .execute(
                        "UPDATE project_kg_bindings
                         SET collection_name = ?1,
                             config_json = ?2,
                             updated_at = ?3
                         WHERE project_id = ?4 AND role = 'shared'",
                        params![primary_name, cfg, now, project_id],
                    )
                    .map_err(|e| {
                        format!(
                            "sync_shared_to_primary_for_orchestrator_root: UPDATE: {}",
                            e
                        )
                    })?;
                if updated > 0 {
                    eprintln!(
                        "[vct] sync-shared-to-primary: orchestrator-root shared row \
                         rewritten to '{}' (matches primary)",
                        primary_name
                    );
                }
                Ok((updated, 0))
            }
            None => {
                // No shared row exists; INSERT one matching primary.
                let cfg = format!(
                    "{{\"manual_override\":\"v0.2.46-sync-shared-to-primary-boot\"}}"
                );
                let inserted = guard
                    .execute(
                        "INSERT INTO project_kg_bindings
                         (project_id, role, collection_name, embedding_model,
                          embedding_dim, kg_dir_path, weaviate_url,
                          config_json, updated_at)
                         VALUES (?1, 'shared', ?2, NULL, NULL, NULL, NULL,
                                 ?3, ?4)",
                        params![project_id, primary_name, cfg, now],
                    )
                    .map_err(|e| {
                        format!(
                            "sync_shared_to_primary_for_orchestrator_root: INSERT: {}",
                            e
                        )
                    })?;
                if inserted > 0 {
                    eprintln!(
                        "[vct] sync-shared-to-primary: orchestrator-root shared row \
                         created pointing at '{}' (matches primary)",
                        primary_name
                    );
                }
                Ok((0, inserted))
            }
        }
    }

    /// W40-B (v0.2.40): cross-prefix self-heal for `project_kg_bindings`
    /// at launcher boot.
    ///
    /// Background: the existing case-insensitive self-heal
    /// (`migrate_legacy_shared_kg_collection_names` above, plus
    /// `install.py::_self_heal_kg_bindings_on_update`) covers casing
    /// flips (`VibecodedOrchestrator_*` → `VibeCodedOrchestrator_*`)
    /// and legacy-name flips (`VibeCodedTools_*` → `VibeCodedOrchestrator_*`).
    /// It does NOT handle the case where a `project_kg_bindings` row
    /// names a collection that doesn't exist in Weaviate AND has no
    /// case-sibling AND a different-prefix collection with the same
    /// suffix (`*_KnowledgeGraph`) DOES exist with populated rows.
    ///
    /// VCO_dev's broken state (the canonical fixture this targets):
    /// ```text
    /// project_kg_bindings (orchestrator-root):
    ///   primary  = VCODev_KnowledgeGraph                     (manual_override:v0.2.29-cleanup)
    ///   shared   = VibeCodedOrchestrator_KnowledgeGraph      (manual_override:v0.2.28-recovery)
    /// Weaviate:
    ///   VCODev_KnowledgeGraph                                1033 rows  ← actual data
    ///   VibeCodedOrchestrator_KnowledgeGraph                 missing    ← what shared advertises
    /// ```
    /// The advertised collection doesn't exist; the populated one
    /// matches the same suffix. Auto-adopt is safe because there's
    /// exactly one populated `*_KnowledgeGraph` candidate.
    ///
    /// Algorithm:
    /// 1. Fetch Weaviate schema → set of existing class names
    ///    (`/v1/schema`).
    /// 2. For each row in `project_kg_bindings`:
    ///    a. If `collection_name` exists → `no_change`.
    ///    b. If `collection_name.to_lowercase()` matches an existing
    ///    class (case-sibling) → `no_change` (the case-insensitive
    ///    self-heal owns this scenario; we never compete with it).
    ///    c. Otherwise: derive the suffix from the binding's name
    ///    (`_KnowledgeGraph`). Enumerate populated classes with
    ///    the same suffix via GraphQL aggregate count. If exactly
    ///    one has `count > 0`, `UPDATE` the binding's
    ///    `collection_name` + set `config_json` to
    ///    `{"manual_override":"v0.2.40-prefix-adopt"}`. If
    ///    multiple, log a structured warning naming the
    ///    alternatives and `defer`. If zero, `no_change`.
    ///
    /// Idempotency: second call after a successful adoption no-ops
    /// because the new `collection_name` now exists in step 2a.
    ///
    /// Soft-fail contract:
    ///   * Weaviate unreachable → return `Err(...)`. The caller
    ///     (launcher boot) logs the error and continues. Boot MUST
    ///     NOT be blocked by a missing Weaviate.
    ///   * Per-row probe failures (GraphQL parse error, etc.) are
    ///     logged and treated as "no populated candidate"; we err on
    ///     the side of NOT rewriting a binding the user may have
    ///     intentionally set.
    ///   * DB errors after a successful probe ARE returned (the row
    ///     write is the load-bearing step; a failure there leaves the
    ///     binding stale and we want the caller to know).
    ///
    /// Tags every adopted row with `manual_override=v0.2.40-prefix-adopt`
    /// in `config_json` so the env-backfill path (`vco_lib/project_init.py
    /// ::_align_env_with_db_bindings`) trusts the new value next time
    /// `populate()` runs. Matches the sentinel discipline established
    /// in v0.2.28-recovery + v0.2.29-cleanup.
    ///
    /// Position in the boot sequence: called AFTER
    /// `migrate_legacy_shared_kg_collection_names` so the canonical
    /// name is finalized first; the case-insensitive self-heal (in
    /// install.py / W40-A's branch) covers the casing gap; THIS
    /// function only fires on the residue (full-prefix divergence).
    ///
    /// // W40-B (v0.2.40, 2026-05-30): cross-prefix adoption
    pub async fn adopt_populated_collections_at_boot(
        &self,
        weaviate_url: &str,
    ) -> Result<AdoptionReport, String> {
        // ── Step 1: probe Weaviate schema ────────────────────────────
        //
        // Single GET /v1/schema. Failure → return Err so the caller can
        // log; don't probe per-row with no schema context.
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(5))
            .build()
            .map_err(|e| format!("adopt_populated: reqwest client: {}", e))?;

        let schema_url = format!("{}/v1/schema", weaviate_url.trim_end_matches('/'));
        let resp = client
            .get(&schema_url)
            .send()
            .await
            .map_err(|e| format!("adopt_populated: GET {}: {}", schema_url, e))?;
        if !resp.status().is_success() {
            return Err(format!(
                "adopt_populated: {} returned status {}",
                schema_url,
                resp.status().as_u16()
            ));
        }
        let schema: JsonValue = resp
            .json()
            .await
            .map_err(|e| format!("adopt_populated: parse {}: {}", schema_url, e))?;

        let existing: Vec<String> = schema
            .get("classes")
            .and_then(|c| c.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|c| {
                        c.get("class").and_then(|v| v.as_str()).map(String::from)
                    })
                    .collect()
            })
            .unwrap_or_default();

        let existing_set: std::collections::HashSet<&str> =
            existing.iter().map(String::as_str).collect();
        let existing_by_lower: std::collections::HashMap<String, &str> = existing
            .iter()
            .map(|n| (n.to_lowercase(), n.as_str()))
            .collect();

        // ── Step 2: collect candidate rows from the DB ───────────────
        //
        // Read every project_kg_bindings row. We don't filter at the
        // SQL layer because the set is tiny (≤ 3-5 rows per project ×
        // ~10 projects in worst case) and we want the probe → adopt
        // decision to happen in one pass.
        struct BindingRow {
            project_id: String,
            role: String,
            collection_name: String,
        }

        let rows: Vec<BindingRow> = {
            let guard = self.lock();
            let mut stmt = guard
                .prepare(
                    "SELECT project_id, role, collection_name \
                     FROM project_kg_bindings",
                )
                .map_err(|e| format!("adopt_populated: prepare: {}", e))?;
            let mapped = stmt
                .query_map([], |r| {
                    Ok(BindingRow {
                        project_id: r.get(0)?,
                        role: r.get(1)?,
                        collection_name: r.get(2)?,
                    })
                })
                .map_err(|e| format!("adopt_populated: query: {}", e))?;
            mapped
                .collect::<Result<Vec<_>, _>>()
                .map_err(|e| format!("adopt_populated: collect: {}", e))?
        };

        let mut report = AdoptionReport::default();

        // ── Step 3: per-row decision ─────────────────────────────────
        for row in &rows {
            // 3a — advertised collection exists → nothing to do.
            if existing_set.contains(row.collection_name.as_str()) {
                report.no_change += 1;
                continue;
            }

            // 3b — case-sibling exists → defer to the case-insensitive
            // self-heal in install.py / `_self_heal_kg_bindings_on_update`.
            // We never compete with that path; it'll get this row on the
            // next `install.py --update`.
            if existing_by_lower.contains_key(&row.collection_name.to_lowercase()) {
                report.no_change += 1;
                continue;
            }

            // 3c — derive suffix; we only know how to adopt for the
            // two known suffixes (`_KnowledgeGraph`, `_Development`).
            // Anything else is treated as user intent we don't
            // understand; preserve the row.
            let suffix = match suffix_of(&row.collection_name) {
                Some(s) => s,
                None => {
                    report.no_change += 1;
                    continue;
                }
            };

            // Enumerate populated same-suffix candidates.
            let candidates = collect_candidates(&existing, suffix);
            if candidates.is_empty() {
                report.no_change += 1;
                continue;
            }

            // Aggregate-count each candidate. Soft-fail per-candidate:
            // a count probe error → treat as 0 (don't auto-adopt on
            // ambiguous information).
            let mut populated: Vec<(String, u64)> = Vec::new();
            for cand in &candidates {
                let count = fetch_class_count(&client, weaviate_url, cand)
                    .await
                    .unwrap_or(0);
                if count > 0 {
                    populated.push((cand.clone(), count));
                }
            }

            match populated.len() {
                0 => {
                    // No populated candidate — preserve user intent.
                    // They probably set this binding ahead of populating
                    // the collection (or just renamed it intentionally).
                    report.no_change += 1;
                }
                1 => {
                    // Unambiguous adoption candidate. Rewrite the row.
                    let (new_name, count) = &populated[0];
                    if let Err(e) = self.update_binding_for_adoption(
                        &row.project_id,
                        &row.role,
                        &row.collection_name,
                        new_name,
                    ) {
                        // DB write failure is propagated — caller logs
                        // and the next boot will retry.
                        return Err(format!(
                            "adopt_populated: rewrite project_id={} role={} \
                             {} → {} failed: {}",
                            row.project_id, row.role,
                            row.collection_name, new_name, e
                        ));
                    }
                    eprintln!(
                        "[vct] adopt-populated: project_id={} role={} \
                         {} → {} (rows={})",
                        row.project_id, row.role,
                        row.collection_name, new_name, count
                    );
                    report.adopted += 1;
                }
                _ => {
                    // Multiple populated candidates — defer. Log the
                    // alternatives so the user can pick via the
                    // launcher GUI's "Manage shared KG collection"
                    // picker. Do NOT auto-pick the highest-row-count
                    // one: the user may legitimately want either, and
                    // a silent pick from the launcher's boot path is
                    // exactly the surprise we want to avoid.
                    let alts: Vec<String> = populated
                        .iter()
                        .map(|(n, c)| format!("{} ({} rows)", n, c))
                        .collect();
                    eprintln!(
                        "[vct] adopt-populated DEFER: project_id={} role={} \
                         advertised='{}' multiple populated candidates: [{}]",
                        row.project_id, row.role,
                        row.collection_name,
                        alts.join(", ")
                    );
                    report.deferred += 1;
                }
            }
        }

        Ok(report)
    }

    /// Helper for `adopt_populated_collections_at_boot`: update a
    /// `project_kg_bindings` row's `collection_name` AND tag its
    /// `config_json` with the v0.2.40 prefix-adopt sentinel.
    ///
    /// The sentinel matters: `vco_lib/project_init.py::_align_env_with_db_bindings`
    /// uses the presence of a `manual_override` key to know it should
    /// CORRECT a stale env file rather than preserve user edits. So
    /// the boot-time write must declare itself in the same sentinel
    /// channel that v0.2.28-recovery / v0.2.29-cleanup use; otherwise
    /// the env file would silently lag behind the DB.
    ///
    /// We preserve any other keys in `config_json` by reading the
    /// existing JSON, merging in the sentinel, and re-serializing.
    /// (Today nothing else lives there, but future fields could; the
    /// merge is the safe shape.)
    fn update_binding_for_adoption(
        &self,
        project_id: &str,
        role: &str,
        old_collection: &str,
        new_collection: &str,
    ) -> Result<(), String> {
        let now = Utc::now().timestamp_millis();

        // v0.2.49 access-matrix Phase 3 (item #8) — atomic binding
        // rebind + access-matrix rename. Pre-fix, the binding's
        // collection_name was rewritten but its corresponding
        // kg_collection_access row(s) were left pointing at the old
        // name. The boot reconcile (`reconcile_kg_collection_access_at_boot`)
        // would then sweep the now-orphan access rows in the SAME boot —
        // but only because the F-1 ordering (reconcile AFTER adopt)
        // was repaired in the same release. Calling
        // `kg_rename_access` HERE makes the rebind atomic at the per-
        // project granularity: the access row(s) for `old_collection`
        // get renamed to `new_collection` in the same transaction
        // window as the binding update.
        //
        // Soft-fail: a kg_rename_access error is logged + propagated;
        // the binding update is NOT skipped (it's the load-bearing
        // half; the access matrix can be reconciled later via the
        // boot probe). Returns the binding update's Err result if
        // the binding itself fails.
        {
            // Note: kg_rename_access acquires its own lock and may
            // perform its own UPSERT semantics for the access row(s).
            // We call it BEFORE the binding UPDATE so the access
            // rename always reflects a pre-rebind state — if the
            // binding UPDATE then fails, the access matrix still
            // points at the new collection name, which the next boot
            // reconcile will detect as orphan and clean up. This is
            // the v0.2.46 reconcile contract; we're not changing it.
            if old_collection != new_collection {
                if let Err(e) = self.kg_rename_access(
                    project_id, old_collection, new_collection,
                ) {
                    // Best-effort: log + continue. The boot reconcile
                    // catches the residue.
                    eprintln!(
                        "[vct] update_binding_for_adoption: kg_rename_access \
                         project_id={} {} → {} warning (non-fatal): {}",
                        project_id, old_collection, new_collection, e,
                    );
                }
            }
        }

        let guard = self.lock();

        // Read existing config_json so we don't clobber unrelated keys.
        let existing_cfg: String = guard
            .query_row(
                "SELECT config_json FROM project_kg_bindings \
                  WHERE project_id = ?1 AND role = ?2",
                params![project_id, role],
                |r| r.get::<_, String>(0),
            )
            .unwrap_or_else(|_| "{}".to_string());

        let mut cfg: JsonValue =
            serde_json::from_str(&existing_cfg).unwrap_or(JsonValue::Object(serde_json::Map::new()));
        if !cfg.is_object() {
            cfg = JsonValue::Object(serde_json::Map::new());
        }
        if let Some(obj) = cfg.as_object_mut() {
            obj.insert(
                "manual_override".to_string(),
                JsonValue::String("v0.2.40-prefix-adopt".to_string()),
            );
        }
        let cfg_s = serde_json::to_string(&cfg).unwrap_or_else(|_| "{}".to_string());

        guard
            .execute(
                "UPDATE project_kg_bindings \
                    SET collection_name = ?1, \
                        config_json = ?2, \
                        updated_at = ?3 \
                  WHERE project_id = ?4 AND role = ?5",
                params![new_collection, cfg_s, now, project_id, role],
            )
            .map_err(|e| format!("update: {}", e))?;
        Ok(())
    }

    /// v0.2.49 access-matrix Phase 4 (item #10): seed the default
    /// kg_collection_access rows for a newly-created project.
    ///
    /// Centralizes the logic previously embedded in the launcher-side
    /// `commands::project_state_populate::populate_kg_collection_access`
    /// so every project-create surface (Tauri command, hub `vct project
    /// create` CLI, future automation) can call ONE consistent helper.
    ///
    /// Writes three rows (each idempotent via INSERT OR IGNORE — pre-
    /// existing user-configured rows are preserved):
    ///   1. `<Sanitized>_KnowledgeGraph` → "write"  (project's own KG)
    ///   2. `<Sanitized>_Development`    → "write"  (project's docs)
    ///   3. `LAST_RESORT_SHARED_KG_COLLECTION` → "read"  (cross-project shared)
    ///
    /// Where `<Sanitized>` is `sanitize_kg_collection(project_name)`
    /// (matches launcher-side `commands::projects_v2::sanitize_kg_collection`
    /// byte-for-byte; see `sanitize_kg_collection_local` below).
    ///
    /// Why default-grant on the own collections + shared: the read-gate
    /// `require_kg_read` (commands::kg::require_kg_read + hub::cli_api)
    /// rejects every access to a collection the project doesn't have an
    /// explicit row for. Pre-PR-3 the access matrix was permanently empty
    /// for fresh projects, so every search/read of the project's own KG
    /// or the shared bundled KG failed until the user manually granted
    /// via the GUI access matrix.
    ///
    /// Returns the number of rows actually inserted (0..=3). Pre-existing
    /// rows are not counted; user-configured downgrades are preserved.
    /// Errors on rows fail the call (caller sees the SQL error and can
    /// log a warning); the partial state is committed up to the failure
    /// point (each kg_seed_access is its own statement).
    pub fn populate_kg_collection_access_for_project(
        &self,
        project_id: &str,
        project_name: &str,
    ) -> Result<usize, String> {
        let pascal = sanitize_kg_collection_local(project_name);
        let primary_collection = format!("{}_KnowledgeGraph", pascal);
        let dev_collection = format!("{}_Development", pascal);
        // Resolve the canonical shared KG name from `app_state` (Phase 1
        // item #3 single-source-of-truth). White-label installers override
        // this at install time via `set_orchestrator_root_kg_collection`.
        let shared_collection = self.get_orchestrator_root_kg_collection()?;

        // v0.2.49 access-matrix Step F SB2 fix (L2-SB1): match the
        // semantic that `resolve_default_access_level` returns ONCE
        // the bindings are written downstream of this call.
        //
        // ⚠ Cannot CALL `resolve_default_access_level` from here:
        // `populate_kg_collection_access_for_project` runs at
        // project-create time, BEFORE any rows in `project_kg_bindings`
        // exist for this project (see the call ordering in
        // `vct-hub/src/cli_api.rs::create_project` — insert_project
        // → populate_kg_collection_access_for_project → bindings
        // written later by the launcher GUI populate path). With zero
        // bindings the resolver returns `Denied` for every collection,
        // which would defeat the populate's whole purpose.
        //
        // The fix: write `Write` for all three collections. That
        // exactly mirrors what the resolver WILL return once the
        // bindings exist:
        //   - primary binding → resolver says `Write` (F-2a rule)
        //   - dev collection → maps to `role='primary'` binding's
        //     `_Development` variant → resolver says `Write`
        //   - shared binding → resolver says `Write` (F-2a rule;
        //     reinforced by Step D's force-upgrade migration which
        //     promotes any legacy `Read` shared rows to `Write`)
        //
        // Pre-Step-F this helper wrote `Write/Write/Read` — the `Read`
        // for shared was inconsistent with the resolver's F-2a rule
        // AND with Step D's force-upgrade target. Step F audit (L2-SB1)
        // surfaced the drift; this fix aligns the literals to the
        // resolver semantic.
        //
        // FUTURE: if the resolver semantic changes again, update BOTH
        // here AND the resolver. The unit test
        // `populate_writes_three_default_rows` pins the contract +
        // a new sibling test pins that populate output matches
        // resolve_default_access_level's output post-binding-write.
        let mut inserted = 0usize;
        let default_level = AccessLevel::Write.as_str();
        inserted += self.kg_seed_access(project_id, &primary_collection, default_level)?;
        inserted += self.kg_seed_access(project_id, &dev_collection, default_level)?;
        inserted += self.kg_seed_access(project_id, &shared_collection, default_level)?;

        // v0.2.49 access-matrix Step F MF3 (L1-F2): back-fill access rows
        // for every ALREADY-INSTALLED global-scope module's declared
        // `kg_collections`. Inverse of item #13:
        //
        //   - item #13 runs at module-install time + iterates ALL
        //     existing projects to seed their access rows for THIS
        //     module's collections.
        //   - MF3 runs at project-create time + iterates ALL existing
        //     global module installs to seed THIS project's access rows
        //     for their collections.
        //
        // Together they close the symmetry: a new project gets the same
        // initial access state regardless of whether it was created
        // BEFORE or AFTER a global module install.
        //
        // Access level for module collections: literal `Write`. The
        // resolver `resolve_default_access_level` can't be consulted
        // because module collections aren't in `project_kg_bindings`
        // (they're declared in the module's manifest, not as project
        // bindings) — the resolver would return `Denied`. The Write
        // semantic matches user directive 2026-06-08 "default R/W on
        // own + shared" + treats module collections as
        // shared-infrastructure (every project that installed the module
        // can write to its collection).
        //
        // v0.2.49 Step F MF3-v2 refactor: source of truth for the
        // module's declared collections is `module_installs.kg_collections`
        // (migration 032 — TEXT column, JSON-encoded array). Set at
        // install time by `commands::modules::install_module` calling
        // `Db::set_module_kg_collections` immediately after the install
        // row lands. Pre-v0.2.49 module installs have NULL → empty Vec
        // → skipped (caught by the `if collections.is_empty()` guard).
        //
        // No filesystem I/O on this hot path: the launcher DB is the
        // authoritative state per the orchestrator's single-writer
        // discipline.
        let global_installs = self
            .list_global_module_installs()
            .unwrap_or_default();
        for install in &global_installs {
            if install.kg_collections.is_empty() {
                continue; // module declares no KG collections
            }
            for coll in &install.kg_collections {
                // Best-effort: per-collection seed errors don't abort
                // the rest. The boot reconcile path catches any partial
                // populate failure.
                if let Ok(n) = self.kg_seed_access(project_id, coll, default_level) {
                    inserted += n;
                }
            }
        }

        Ok(inserted)
    }

    /// v0.2.49 access-matrix Step F SB2 (L1-F1 + L2-SB1 cross-lens):
    /// propagate a project rename into the `kg_collection_access` matrix.
    ///
    /// Pre-fix this lived as a free function in
    /// `commands/projects_v2.rs::propagate_kg_access_on_rename` —
    /// callable from the Tauri rename path only. The hub-CLI rename
    /// (`vct-hub/src/cli_api.rs::rename_project`) never called it, so
    /// CLI-driven renames left orphan `kg_collection_access` rows
    /// referencing the OLD sanitized collection names. After rename,
    /// the project's own KG read-gate would refuse access to its own
    /// (new-name) primary collection until manual GUI re-grant.
    ///
    /// Lifted into `db::access` so both surfaces (Tauri + hub CLI) call
    /// the same code path. Same pattern as Step B's
    /// `populate_kg_collection_access_for_project` lift.
    ///
    /// Soft-fail per-suffix: a failure to rename one collection's row
    /// does NOT abort the loop — the next suffix is still attempted.
    /// Failed renames append to the returned `warnings` Vec; callers
    /// fold these into their result envelope so the GUI/CLI can
    /// surface them (matches the original projects_v2 contract). The
    /// boot reconcile (`Db::reconcile_kg_collection_access`) catches
    /// any stale rows on the next launcher start as backup.
    ///
    /// Returns `Vec<String>` of human-readable warnings (empty on the
    /// happy path).
    pub fn propagate_kg_access_on_rename(
        &self,
        project_id: &str,
        old_name: &str,
        new_name: &str,
    ) -> Vec<String> {
        let mut warnings = Vec::new();
        if old_name == new_name {
            return warnings;
        }
        let old_sanitized = sanitize_kg_collection_local(old_name);
        let new_sanitized = sanitize_kg_collection_local(new_name);
        if old_sanitized == new_sanitized {
            return warnings;
        }
        // v0.2.49 Step F SF5 fix: `_Diagrams` suffix was missing from
        // the pre-lift list at `projects_v2.rs:3190` — Mermaid +
        // Excalidraw indexer writes to `<Project>_Diagrams` per the
        // diagrams pipeline. Without rename propagation, post-rename
        // diagram writes silently failed access checks. Add `_Diagrams`
        // here so rename + indexer stay coherent.
        for suffix in &["_KnowledgeGraph", "_Development", "_Diagrams"] {
            let old_collection = format!("{}{}", old_sanitized, suffix);
            let new_collection = format!("{}{}", new_sanitized, suffix);
            if let Err(e) = self.kg_rename_access(project_id, &old_collection, &new_collection) {
                let msg = format!(
                    "kg_rename_access({}, {} → {}): {}. Access matrix may carry \
                     stale rows until next boot reconcile.",
                    project_id, old_collection, new_collection, e
                );
                eprintln!("[vct] warning: {}", msg);
                warnings.push(msg);
            }
        }
        warnings
    }
}

/// v0.2.49 access-matrix Phase 4 (item #10) — local copy of
/// `commands::projects_v2::sanitize_kg_collection`. Kept here so
/// `populate_kg_collection_access_for_project` works from inside
/// vct-launcher-core without taking a dep on the launcher crate.
///
/// MUST stay byte-equivalent to the launcher-side version (and both must
/// match the Python SSOT `vco_lib.codegraph_naming.sanitize_for_weaviate_class`
/// — cross-language parity pinned by `tests/fixtures/kg_sanitizer_parity.json`).
/// A future refactor can hoist this into a shared util module; deferred to
/// keep the diff bounded.
///
/// Convert a project display name into a Weaviate-collection-safe id.
/// Weaviate collections must start with [A-Z] and contain only
/// alphanumerics — strip everything else and Title-case.
///
/// X-1 / v0.2.76 (ruling #2): empty / all-non-alnum / leading-digit input
/// all fall back to the sentinel prefix `"vct"` (unified with Python; the
/// old "Project" / "P"-prepend divergence is retired).
pub(crate) fn sanitize_kg_collection_local(name: &str) -> String {
    let mut out = String::new();
    let mut next_upper = true;
    for ch in name.chars() {
        if ch.is_ascii_alphanumeric() {
            if next_upper {
                out.extend(ch.to_uppercase());
                next_upper = false;
            } else {
                out.push(ch);
            }
        } else {
            next_upper = true;
        }
    }
    // Unified fallback (X-1 / v0.2.76): empty OR leading-digit → sentinel
    // prefix. Matches Python's `sanitize_for_weaviate_class`.
    if out.is_empty() || out.chars().next().unwrap().is_ascii_digit() {
        return "vct".to_string();
    }
    out
}

/// Extract the recognized suffix from a binding collection name. We
/// only know how to adopt across known suffixes; anything else (a user
/// pointing a binding at a custom-named class) is preserved as-is.
///
/// Returns the suffix INCLUDING the leading underscore so callers can
/// build candidate names directly via `format!("{prefix}{suffix}")`.
fn suffix_of(name: &str) -> Option<&'static str> {
    const SUFFIXES: &[&str] = &["_KnowledgeGraph", "_Development"];
    for s in SUFFIXES {
        if name.ends_with(s) {
            return Some(*s);
        }
    }
    None
}

/// Collect classes in `existing` that share the given suffix. Used by
/// `adopt_populated_collections_at_boot` to enumerate adoption candidates.
fn collect_candidates(existing: &[String], suffix: &str) -> Vec<String> {
    existing
        .iter()
        .filter(|n| n.ends_with(suffix))
        .cloned()
        .collect()
}

/// Aggregate-count a Weaviate class via GraphQL. Mirrors the
/// implementation in `launcher::commands::kg::fetch_class_count` but
/// lives in the core crate where the boot-time self-heal also needs
/// it. Returns 0 when the class is missing / the query errors /
/// parsing fails — the caller decides whether 0 → "no populated
/// candidate".
async fn fetch_class_count(
    client: &reqwest::Client,
    weaviate_url: &str,
    class: &str,
) -> Result<u64, String> {
    let url = format!("{}/v1/graphql", weaviate_url.trim_end_matches('/'));
    let body = serde_json::json!({
        "query": format!(
            "{{ Aggregate {{ {class} {{ meta {{ count }} }} }} }}",
            class = class
        )
    });
    let resp = client
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("graphql: {}", e))?;
    if !resp.status().is_success() {
        return Err(format!("graphql status {}", resp.status().as_u16()));
    }
    let v: JsonValue = resp.json().await.map_err(|e| format!("parse: {}", e))?;
    Ok(v.pointer(&format!("/data/Aggregate/{}/0/meta/count", class))
        .and_then(|n| n.as_u64())
        .unwrap_or(0))
}

// ─── Codegraph access ────────────────────────────────────────────────────

impl Db {
    pub fn codegraph_grant(
        &self,
        grantor: &str,
        grantee: &str,
        level: &str,
    ) -> Result<(), String> {
        if !matches!(level, "read" | "none") {
            return Err(format!("invalid codegraph access level: {}", level));
        }
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO codegraph_access
                 (grantor_project_id, grantee_project_id, access_level, granted_at)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(grantor_project_id, grantee_project_id)
                 DO UPDATE SET access_level = excluded.access_level,
                               granted_at = excluded.granted_at",
                params![grantor, grantee, level, Utc::now().timestamp_millis()],
            )
            .map_err(|e| format!("codegraph_grant: {}", e))?;
        Ok(())
    }

    pub fn codegraph_check(
        &self,
        grantor: &str,
        grantee: &str,
    ) -> Result<Option<String>, String> {
        if grantor == grantee {
            return Ok(Some("read".to_string()));
        }
        let guard = self.lock();
        guard
            .query_row(
                "SELECT access_level FROM codegraph_access
                  WHERE grantor_project_id = ?1 AND grantee_project_id = ?2",
                params![grantor, grantee],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| format!("codegraph_check: {}", e))
    }

    pub fn codegraph_list_grants_from(
        &self,
        grantor: &str,
    ) -> Result<Vec<(String, String)>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT grantee_project_id, access_level FROM codegraph_access
                  WHERE grantor_project_id = ?1 ORDER BY granted_at DESC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![grantor], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }

    pub fn codegraph_list_grants_to(
        &self,
        grantee: &str,
    ) -> Result<Vec<(String, String)>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT grantor_project_id, access_level FROM codegraph_access
                  WHERE grantee_project_id = ?1 ORDER BY granted_at DESC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![grantee], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| format!("query: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect: {}", e))
    }
}

/// Map an audit operation name to the table the frontend should
/// re-fetch. Returns None for ops that already imply audit_log alone
/// (which is always logged separately). Best-effort — unknown op names
/// fall through to a generic "audit_log" event so consumers always get
/// SOME signal.
fn infer_table_for_op(op: &str) -> Option<&'static str> {
    match op {
        "project_create" | "project_delete" | "project_host_switch" | "project_rename" => {
            Some("projects")
        }
        s if s.starts_with("module_") => Some("module_installs"),
        s if s.starts_with("secret_") => Some("project_secret_refs"),
        s if s.starts_with("setting_") => Some("module_settings"),
        s if s.starts_with("kg_") => Some("kg_collection_access"),
        s if s.starts_with("codegraph_") => Some("codegraph_access"),
        s if s.starts_with("hook_") => Some("project_hooks"),
        s if s.starts_with("agent_") => Some("project_agents"),
        s if s.starts_with("skill_") => Some("project_skills"),
        s if s.starts_with("permission_") => Some("project_permissions"),
        s if s.starts_with("license_") => Some("tier_cache"),
        // Migration 021 — diagrams (Mermaid + Excalidraw). Order matters:
        // narrower prefixes are tested first so a `diagram_snapshot_*` op
        // routes to diagram_snapshots and not project_diagrams.
        s if s.starts_with("diagram_snapshot_") => Some("diagram_snapshots"),
        s if s.starts_with("diagram_access_") => Some("diagram_access"),
        s if s.starts_with("diagram_") => Some("project_diagrams"),
        s if s.starts_with("mcp_tool_grant_") => Some("project_mcp_tool_grants"),
        s if s.starts_with("project_module_") => Some("project_modules"),
        _ => None,
    }
}

/// v0.2.46 adversarial-review L3 follow-up + v0.2.49 Step F SF4
/// (L2-SF3): pick the higher of two `AccessLevel` privilege levels.
///
/// Privilege ordering (low → high): `Denied` < `Read` < `Write`. Used
/// by `kg_rename_access`'s collision path to **never lower an existing
/// privilege** — the v0.2.28 install.py parity self-heal discipline
/// applied at the helper-function layer.
///
/// Step F SF4 signature change: pre-fix this took `&str` arguments
/// and had a dead "unknown → 0" branch (since callers passed strings
/// read from `kg_collection_access.access_level`, which has a CHECK
/// constraint on `('read','write','none')`). The "unknown" case was
/// statically impossible but the signature invited future drift.
/// Post-fix the function takes `AccessLevel` directly — exhaustive
/// enum match removes the dead branch + makes invalid-value bugs
/// impossible at the type level.
fn pick_higher_access_level(a: AccessLevel, b: AccessLevel) -> AccessLevel {
    fn rank(level: AccessLevel) -> u8 {
        match level {
            AccessLevel::Write => 3,
            AccessLevel::Read => 2,
            AccessLevel::Denied => 1,
        }
    }
    if rank(a) >= rank(b) { a } else { b }
}

// ─── Audit log ───────────────────────────────────────────────────────────

impl Db {
    pub fn audit(
        &self,
        operation: &str,
        project_id: Option<&str>,
        module_id: Option<&str>,
        detail: &serde_json::Value,
    ) -> Result<(), String> {
        self.audit_as(super::current_actor(), operation, project_id, module_id, detail)
    }

    /// Variant that lets the caller override the actor — useful for
    /// commands that act on behalf of a specific user (e.g. when the
    /// launcher gains real auth). Today everything goes through `audit`
    /// which uses `current_actor()`.
    pub fn audit_as(
        &self,
        actor: &str,
        operation: &str,
        project_id: Option<&str>,
        module_id: Option<&str>,
        detail: &serde_json::Value,
    ) -> Result<(), String> {
        {
            let guard = self.lock();
            guard
                .execute(
                    "INSERT INTO audit_log (operation, project_id, module_id, detail, actor, created_at)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                    params![
                        operation,
                        project_id,
                        module_id,
                        detail.to_string(),
                        actor,
                        Utc::now().timestamp_millis(),
                    ],
                )
                .map_err(|e| format!("audit: {}", e))?;
        }

        // Mirror every audited mutation into the change_log so frontend
        // polling can detect cross-window edits (multi-tenant infra P7).
        let _ = self.log_change("audit_log", "insert", None, project_id);
        let inferred = infer_table_for_op(operation);
        if let Some(t) = inferred {
            let _ = self.log_change(t, "update", module_id, project_id);
        }
        Ok(())
    }

    /// Read audit entries, newest first.
    ///
    /// All filters are pushed into SQLite via parameterized WHERE clauses so
    /// we don't ship large windows to the frontend just for it to throw rows
    /// away. Earlier revisions returned up to 500 rows and let the browser
    /// filter on time-range/actor/search; that fell over for high-volume
    /// audit logs.
    ///
    /// Filter semantics:
    ///   * `project_id` — exact match on `project_id` column.
    ///   * `actor` — exact match on `actor` column (case-sensitive).
    ///   * `since_ms` / `until_ms` — inclusive bounds on `created_at`
    ///     (epoch ms). Either or both may be `None`.
    ///   * `search` — substring match (`LIKE '%' || ? || '%'`) against
    ///     `operation` OR `detail`. SQLite's default LIKE is
    ///     case-insensitive for ASCII, which matches the previous
    ///     browser-side `.toLowerCase().includes(...)` behaviour for the
    ///     ASCII range we care about.
    ///
    /// `limit` is bounded to 10000 server-side. The full table scan over a
    /// limit of that size is bounded and acceptable; we'd add a covering
    /// index if a profile ever showed it mattered.
    pub fn audit_list(
        &self,
        project_id: Option<&str>,
        actor: Option<&str>,
        since_ms: Option<i64>,
        until_ms: Option<i64>,
        search: Option<&str>,
        limit: u32,
    ) -> Result<Vec<crate::db::audit_types::AuditEvent>, String> {
        let guard = self.lock();
        let limit = std::cmp::min(limit, 10000);

        // Build the WHERE clause + bound params dynamically. Using
        // `Vec<Box<dyn ToSql>>` keeps the param order tied to placeholder
        // order regardless of which filters are active.
        let mut where_parts: Vec<&'static str> = Vec::new();
        let mut bound: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();

        if let Some(pid) = project_id {
            where_parts.push("project_id = ?");
            bound.push(Box::new(pid.to_string()));
        }
        if let Some(a) = actor {
            if !a.is_empty() {
                where_parts.push("actor = ?");
                bound.push(Box::new(a.to_string()));
            }
        }
        if let Some(s) = since_ms {
            where_parts.push("created_at >= ?");
            bound.push(Box::new(s));
        }
        if let Some(u) = until_ms {
            where_parts.push("created_at <= ?");
            bound.push(Box::new(u));
        }
        if let Some(q) = search {
            let q = q.trim();
            if !q.is_empty() {
                // Match operation OR detail. We bind the same value twice
                // (once per `?`) — clearer than reusing one `?N` and works
                // identically in SQLite.
                where_parts.push("(operation LIKE '%' || ? || '%' OR detail LIKE '%' || ? || '%')");
                bound.push(Box::new(q.to_string()));
                bound.push(Box::new(q.to_string()));
            }
        }

        let where_clause = if where_parts.is_empty() {
            String::new()
        } else {
            format!(" WHERE {}", where_parts.join(" AND "))
        };

        let sql = format!(
            "SELECT id, operation, project_id, module_id, detail, actor, created_at
             FROM audit_log{}
             ORDER BY created_at DESC
             LIMIT ?",
            where_clause
        );
        bound.push(Box::new(limit));

        let mut stmt = guard.prepare(&sql).map_err(|e| format!("audit_list prepare: {}", e))?;

        let map_row = |row: &rusqlite::Row| -> rusqlite::Result<crate::db::audit_types::AuditEvent> {
            Ok(crate::db::audit_types::AuditEvent {
                id: row.get(0)?,
                operation: row.get(1)?,
                project_id: row.get(2)?,
                module_id: row.get(3)?,
                detail: row.get(4)?,
                actor: row.get(5)?,
                created_at: row.get(6)?,
            })
        };

        let param_refs: Vec<&dyn rusqlite::ToSql> = bound.iter().map(|b| &**b as &dyn rusqlite::ToSql).collect();

        let rows: Vec<_> = stmt
            .query_map(rusqlite::params_from_iter(param_refs.iter()), map_row)
            .map_err(|e| format!("audit_list query: {}", e))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("audit_list collect: {}", e))?;

        Ok(rows)
    }
}

#[cfg(test)]
mod audit_tests {
    use crate::db::Db;
    use serde_json::json;

    #[test]
    fn audit_writes_row_with_actor_and_operation() {
        let db = Db::open_in_memory().expect("open in-memory");

        db.audit_as(
            "alice",
            "project_create",
            Some("proj-1"),
            None,
            &json!({"host": "base", "name": "demo"}),
        )
        .expect("audit insert");

        let rows = db
            .audit_list(None, None, None, None, None, 100)
            .expect("audit_list");

        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].operation, "project_create");
        assert_eq!(rows[0].actor, "alice");
        assert_eq!(rows[0].project_id.as_deref(), Some("proj-1"));
        assert!(rows[0].detail.contains("\"host\":\"base\""));
        assert!(rows[0].detail.contains("\"name\":\"demo\""));
    }

    #[test]
    fn audit_filter_by_project_and_actor() {
        let db = Db::open_in_memory().expect("open in-memory");

        db.audit_as("alice", "secret_set", Some("p1"), None, &json!({"k": "OPENAI"}))
            .unwrap();
        db.audit_as("bob", "license_activate", None, None, &json!({})).unwrap();
        db.audit_as("alice", "module_install", Some("p2"), Some("rag"), &json!({}))
            .unwrap();

        let by_alice = db
            .audit_list(None, Some("alice"), None, None, None, 100)
            .unwrap();
        assert_eq!(by_alice.len(), 2);

        let on_p1 = db.audit_list(Some("p1"), None, None, None, None, 100).unwrap();
        assert_eq!(on_p1.len(), 1);
        assert_eq!(on_p1[0].operation, "secret_set");

        let by_search = db
            .audit_list(None, None, None, None, Some("license"), 100)
            .unwrap();
        assert_eq!(by_search.len(), 1);
        assert_eq!(by_search[0].operation, "license_activate");
    }
}

// ─── Tests: migrate_legacy_shared_kg_collection_names (NEW-12) ───────────

#[cfg(test)]
mod migrate_shared_kg_tests {
    use crate::db::Db;

    const CANONICAL: &str = "VibeCodedOrchestrator_KnowledgeGraph";
    const LEGACY_TOOLS: &str = "VibeCodedTools_KnowledgeGraph";
    const LEGACY_LOWERCASE_C: &str = "VibecodedOrchestrator_KnowledgeGraph";

    /// Helper: seed a project row and an access row.
    /// `folder_path` is derived from `project_id` so every project gets a
    /// unique path — the `projects` table has a UNIQUE constraint on
    /// `folder_path`, so reusing the same path for two projects would cause
    /// the second `INSERT OR IGNORE` to silently skip, leaving the FK
    /// reference broken.
    fn seed(db: &Db, project_id: &str, collection: &str, level: &str) {
        let now = 1_700_000_000_000_i64;
        let folder = format!("/tmp/test/{}", project_id);
        let guard = db.lock();
        guard.execute(
            "INSERT OR IGNORE INTO projects \
             (id, name, folder_path, host, created_at, updated_at, slug) \
             VALUES (?1, ?2, ?3, 'base', ?4, ?4, ?1)",
            rusqlite::params![project_id, project_id, folder, now],
        ).unwrap_or_else(|e| panic!("seed project {}: {}", project_id, e));
        drop(guard);
        db.kg_set_access(project_id, collection, level).unwrap();
    }

    /// Helper: count rows for a given (project_id, collection_name) pair.
    fn count(db: &Db, project_id: &str, collection: &str) -> i64 {
        let guard = db.lock();
        guard
            .query_row(
                "SELECT COUNT(*) FROM kg_collection_access \
                  WHERE project_id = ?1 AND collection_name = ?2",
                rusqlite::params![project_id, collection],
                |r| r.get(0),
            )
            .unwrap_or(0)
    }

    /// Case 1: a lone legacy `VibeCodedTools_KnowledgeGraph` row gets renamed
    /// to the canonical name.
    #[test]
    fn legacy_tools_row_alone_gets_renamed() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", LEGACY_TOOLS, "read");

        let renamed = db
            .migrate_legacy_shared_kg_collection_names(CANONICAL)
            .unwrap();
        assert_eq!(renamed, 1, "expected 1 rename");

        assert_eq!(count(&db, "p1", LEGACY_TOOLS), 0, "legacy row must be gone");
        assert_eq!(count(&db, "p1", CANONICAL), 1, "canonical row must exist");
    }

    /// Case 1b: the lowercase-c legacy name also gets renamed.
    #[test]
    fn legacy_lowercase_c_row_alone_gets_renamed() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", LEGACY_LOWERCASE_C, "write");

        let renamed = db
            .migrate_legacy_shared_kg_collection_names(CANONICAL)
            .unwrap();
        assert_eq!(renamed, 1);

        assert_eq!(count(&db, "p1", LEGACY_LOWERCASE_C), 0, "legacy_lowercase_c row must be gone");
        assert_eq!(count(&db, "p1", CANONICAL), 1, "canonical row must exist");
    }

    /// Case 2: when BOTH a legacy row AND a canonical row exist for the SAME
    /// project_id, the legacy duplicate is deleted (no uniqueness violation)
    /// and only the canonical row survives.
    #[test]
    fn legacy_plus_canonical_same_project_deduped() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", LEGACY_TOOLS, "read");
        seed(&db, "p1", CANONICAL, "write");

        // Pre-condition: both rows present.
        assert_eq!(count(&db, "p1", LEGACY_TOOLS), 1);
        assert_eq!(count(&db, "p1", CANONICAL), 1);

        let renamed = db
            .migrate_legacy_shared_kg_collection_names(CANONICAL)
            .unwrap();
        // The dedup-delete path fires; the rename step has 0 remaining rows.
        assert_eq!(renamed, 0, "legacy dup was deleted, not renamed");

        assert_eq!(count(&db, "p1", LEGACY_TOOLS), 0, "legacy dup must be gone");
        assert_eq!(count(&db, "p1", CANONICAL), 1, "canonical row must survive");
    }

    /// Case 3: only the canonical row exists — no-op, returns 0.
    #[test]
    fn only_canonical_row_is_noop() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", CANONICAL, "read");

        let renamed = db
            .migrate_legacy_shared_kg_collection_names(CANONICAL)
            .unwrap();
        assert_eq!(renamed, 0);

        assert_eq!(count(&db, "p1", CANONICAL), 1, "canonical row must be untouched");
    }

    /// Case 4: no rows at all — no-op, returns 0.
    #[test]
    fn empty_table_is_noop() {
        let db = Db::open_in_memory().unwrap();

        let renamed = db
            .migrate_legacy_shared_kg_collection_names(CANONICAL)
            .unwrap();
        assert_eq!(renamed, 0);
    }

    /// Idempotency: calling migrate twice on the same DB must not error and
    /// the second call must return 0 (all legacy rows already renamed).
    #[test]
    fn idempotent_second_call_is_noop() {
        let db = Db::open_in_memory().unwrap();
        seed(&db, "p1", LEGACY_TOOLS, "read");
        seed(&db, "p2", LEGACY_LOWERCASE_C, "write");

        let first = db
            .migrate_legacy_shared_kg_collection_names(CANONICAL)
            .unwrap();
        assert_eq!(first, 2);

        let second = db
            .migrate_legacy_shared_kg_collection_names(CANONICAL)
            .unwrap();
        assert_eq!(second, 0, "second call must be a no-op");
    }
}

// ─── Tests: sync_shared_to_primary_for_orchestrator_root (v0.2.46 Decision A boot backfill) ─────────

#[cfg(test)]
mod sync_shared_to_primary_tests {
    use crate::db::Db;

    /// Seed the orchestrator-root project plus arbitrary KG binding rows.
    /// Returns the project_id so tests can inspect post-state.
    fn seed_orchestrator_root(db: &Db) -> String {
        let project_id = "orch-root-test";
        let now = 1_700_000_000_000_i64;
        let folder = format!("/tmp/test-sync-shared/{}", project_id);
        let guard = db.lock();
        guard
            .execute(
                "INSERT OR IGNORE INTO projects \
                 (id, name, folder_path, host, slug, created_at, updated_at) \
                 VALUES (?1, ?2, ?3, 'orchestrator_root', 'orchestrator-root', ?4, ?4)",
                rusqlite::params![project_id, "Orchestrator Root Test", folder, now],
            )
            .expect("seed orchestrator-root project");
        project_id.to_string()
    }

    fn seed_kg_binding(
        db: &Db,
        project_id: &str,
        role: &str,
        collection_name: &str,
        config_override: Option<&str>,
    ) {
        let now = 1_700_000_000_000_i64;
        let cfg = config_override.unwrap_or("{}");
        let guard = db.lock();
        guard
            .execute(
                "INSERT OR REPLACE INTO project_kg_bindings
                 (project_id, role, collection_name, embedding_model,
                  embedding_dim, kg_dir_path, weaviate_url,
                  config_json, updated_at)
                 VALUES (?1, ?2, ?3, NULL, NULL, NULL, NULL, ?4, ?5)",
                rusqlite::params![project_id, role, collection_name, cfg, now],
            )
            .unwrap_or_else(|e| panic!("seed binding {} for {}: {}", role, project_id, e));
    }

    fn read_collection(db: &Db, project_id: &str, role: &str) -> Option<String> {
        let guard = db.lock();
        guard
            .query_row(
                "SELECT collection_name FROM project_kg_bindings
                 WHERE project_id = ?1 AND role = ?2",
                rusqlite::params![project_id, role],
                |r| r.get::<_, String>(0),
            )
            .ok()
    }

    fn read_config(db: &Db, project_id: &str, role: &str) -> Option<String> {
        let guard = db.lock();
        guard
            .query_row(
                "SELECT config_json FROM project_kg_bindings
                 WHERE project_id = ?1 AND role = ?2",
                rusqlite::params![project_id, role],
                |r| r.get::<_, String>(0),
            )
            .ok()
    }

    /// VCO_dev-shape state: primary points at a populated collection, shared
    /// is stuck at a previous canonical (`VibeCodedOrchestrator_KnowledgeGraph`).
    /// Boot backfill must rewrite shared to match primary.
    #[test]
    fn vco_dev_shape_shared_rewritten_to_primary() {
        let db = Db::open_in_memory().unwrap();
        let project_id = seed_orchestrator_root(&db);
        seed_kg_binding(&db, &project_id, "primary", "VCODev_KnowledgeGraph", None);
        seed_kg_binding(
            &db,
            &project_id,
            "shared",
            "VibeCodedOrchestrator_KnowledgeGraph",
            Some(r#"{"manual_override":"v0.2.28-recovery"}"#),
        );

        let (updated, inserted) = db
            .sync_shared_to_primary_for_orchestrator_root()
            .expect("sync should succeed");
        assert_eq!(updated, 1);
        assert_eq!(inserted, 0);

        assert_eq!(
            read_collection(&db, &project_id, "shared").as_deref(),
            Some("VCODev_KnowledgeGraph"),
            "shared must now equal primary"
        );
        // The mirror sentinel should be present.
        assert!(
            read_config(&db, &project_id, "shared")
                .unwrap_or_default()
                .contains("v0.2.46-sync-shared-to-primary-boot"),
            "shared config_json should record the boot-sync sentinel"
        );
        // Primary unchanged.
        assert_eq!(
            read_collection(&db, &project_id, "primary").as_deref(),
            Some("VCODev_KnowledgeGraph")
        );
    }

    /// When shared row is absent entirely (partial-state edge case), it
    /// must be INSERTED pointing at primary.
    #[test]
    fn shared_row_inserted_when_absent() {
        let db = Db::open_in_memory().unwrap();
        let project_id = seed_orchestrator_root(&db);
        seed_kg_binding(&db, &project_id, "primary", "RootKG", None);
        // No shared row.

        let (updated, inserted) = db
            .sync_shared_to_primary_for_orchestrator_root()
            .expect("sync should succeed");
        assert_eq!(updated, 0);
        assert_eq!(inserted, 1);

        assert_eq!(
            read_collection(&db, &project_id, "shared").as_deref(),
            Some("RootKG")
        );
    }

    /// When shared already equals primary, the call is a no-op.
    #[test]
    fn aligned_state_is_noop() {
        let db = Db::open_in_memory().unwrap();
        let project_id = seed_orchestrator_root(&db);
        seed_kg_binding(&db, &project_id, "primary", "Same", None);
        seed_kg_binding(&db, &project_id, "shared", "Same", None);

        let (updated, inserted) = db
            .sync_shared_to_primary_for_orchestrator_root()
            .expect("sync should succeed");
        assert_eq!(updated, 0);
        assert_eq!(inserted, 0);
    }

    /// Calling the boot sync twice in a row must be safe (idempotent).
    #[test]
    fn idempotent_second_call() {
        let db = Db::open_in_memory().unwrap();
        let project_id = seed_orchestrator_root(&db);
        seed_kg_binding(&db, &project_id, "primary", "VCODev_KnowledgeGraph", None);
        seed_kg_binding(
            &db,
            &project_id,
            "shared",
            "VibeCodedOrchestrator_KnowledgeGraph",
            Some(r#"{"manual_override":"v0.2.28-recovery"}"#),
        );

        let (u1, i1) = db
            .sync_shared_to_primary_for_orchestrator_root()
            .unwrap();
        assert_eq!((u1, i1), (1, 0));

        let (u2, i2) = db
            .sync_shared_to_primary_for_orchestrator_root()
            .unwrap();
        assert_eq!((u2, i2), (0, 0), "second call must be no-op");
    }

    /// If no orchestrator-root project exists (fresh machine, peer-only),
    /// the function returns (0, 0) cleanly.
    #[test]
    fn no_orchestrator_root_is_noop() {
        let db = Db::open_in_memory().unwrap();
        // Don't seed any project at all.
        let (updated, inserted) = db
            .sync_shared_to_primary_for_orchestrator_root()
            .expect("sync should succeed even when no project exists");
        assert_eq!((updated, inserted), (0, 0));
    }

    /// If primary doesn't exist (only shared, e.g. partial install),
    /// no-op (we don't auto-create primary from shared — one-way mirror).
    #[test]
    fn shared_only_without_primary_is_noop() {
        let db = Db::open_in_memory().unwrap();
        let project_id = seed_orchestrator_root(&db);
        seed_kg_binding(&db, &project_id, "shared", "StrayShared", None);
        // No primary.

        let (updated, inserted) = db
            .sync_shared_to_primary_for_orchestrator_root()
            .unwrap();
        assert_eq!((updated, inserted), (0, 0));
        // Stray shared stays put.
        assert_eq!(
            read_collection(&db, &project_id, "shared").as_deref(),
            Some("StrayShared")
        );
    }

    /// Peer projects MUST NOT be touched by this function (only operates
    /// on orchestrator-root). If a peer has primary=X, shared=Y (Y != X),
    /// boot sync leaves both alone.
    #[test]
    fn peer_projects_untouched() {
        let db = Db::open_in_memory().unwrap();
        // Seed BOTH orchestrator-root AND a peer with mismatched bindings.
        let root_id = seed_orchestrator_root(&db);
        seed_kg_binding(&db, &root_id, "primary", "RootKG", None);
        seed_kg_binding(&db, &root_id, "shared", "OldRootKG", None);

        let peer_id = "peer-project";
        let now = 1_700_000_000_000_i64;
        let folder = format!("/tmp/test-sync-shared/{}", peer_id);
        {
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO projects \
                     (id, name, folder_path, host, slug, created_at, updated_at) \
                     VALUES (?1, ?2, ?3, 'base', 'peer-project', ?4, ?4)",
                    rusqlite::params![peer_id, "Peer", folder, now],
                )
                .unwrap();
        }
        seed_kg_binding(&db, peer_id, "primary", "PeerKG", None);
        seed_kg_binding(&db, peer_id, "shared", "PeerSharedDifferent", None);

        let _ = db
            .sync_shared_to_primary_for_orchestrator_root()
            .unwrap();

        // Peer must be unchanged.
        assert_eq!(
            read_collection(&db, peer_id, "primary").as_deref(),
            Some("PeerKG")
        );
        assert_eq!(
            read_collection(&db, peer_id, "shared").as_deref(),
            Some("PeerSharedDifferent"),
            "peer shared row must NOT be touched by boot sync"
        );
        // Root was healed.
        assert_eq!(
            read_collection(&db, &root_id, "shared").as_deref(),
            Some("RootKG")
        );
    }
}

// ─── Tests: kg_delete_access + kg_rename_access + reconcile_kg_collection_access (v0.2.46 Decision A/B/C cousin) ───

#[cfg(test)]
mod kg_access_propagation_tests {
    use crate::db::Db;

    fn seed_project(db: &Db, project_id: &str) {
        let now = 1_700_000_000_000_i64;
        let folder = format!("/tmp/test-kg-access/{}", project_id);
        let guard = db.lock();
        guard
            .execute(
                "INSERT OR IGNORE INTO projects \
                 (id, name, folder_path, host, slug, created_at, updated_at) \
                 VALUES (?1, ?2, ?3, 'base', ?1, ?4, ?4)",
                rusqlite::params![project_id, project_id, folder, now],
            )
            .unwrap();
    }

    fn read_access(db: &Db, project_id: &str, collection: &str) -> Option<String> {
        db.kg_get_access(project_id, collection).unwrap_or(None)
    }

    // ─── kg_delete_access ─────────────────────────────────────────────

    #[test]
    fn kg_delete_access_removes_existing_row() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");
        db.kg_set_access("p1", "Foo_KnowledgeGraph", "read").unwrap();
        assert_eq!(read_access(&db, "p1", "Foo_KnowledgeGraph"), Some("read".to_string()));

        let deleted = db.kg_delete_access("p1", "Foo_KnowledgeGraph").unwrap();
        assert_eq!(deleted, 1);
        assert_eq!(read_access(&db, "p1", "Foo_KnowledgeGraph"), None);
    }

    #[test]
    fn kg_delete_access_missing_row_is_zero() {
        // Idempotent: deleting a non-existent row returns 0, not an error.
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");
        let deleted = db.kg_delete_access("p1", "NonExistent").unwrap();
        assert_eq!(deleted, 0);
    }

    #[test]
    fn kg_delete_access_scoped_to_project() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");
        seed_project(&db, "p2");
        db.kg_set_access("p1", "Shared_KG", "read").unwrap();
        db.kg_set_access("p2", "Shared_KG", "read").unwrap();

        let deleted = db.kg_delete_access("p1", "Shared_KG").unwrap();
        assert_eq!(deleted, 1);
        // p2's row untouched.
        assert_eq!(read_access(&db, "p2", "Shared_KG"), Some("read".to_string()));
        assert_eq!(read_access(&db, "p1", "Shared_KG"), None);
    }

    // ─── Step F MF4 (L1-F3) — delete_orphan_peer_access_for_collections ───

    #[test]
    fn delete_orphan_peer_drops_other_projects_grants_on_named_collections() {
        // Scenario: project A is deleted. Its primary collection is
        // 'A_KnowledgeGraph'. Project B previously had a 'read' grant
        // on 'A_KnowledgeGraph' (cross-project peer access from a
        // pre-v0.2.49 install). The FK CASCADE drops A's own rows but
        // leaves B's grant — orphaned, pointing at a collection that
        // no longer exists. MF4's helper sweeps these.
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "a");
        seed_project(&db, "b");

        // A's own access row.
        db.kg_set_access("a", "A_KnowledgeGraph", "write").unwrap();
        // B's peer-grant row on A's collection.
        db.kg_set_access("b", "A_KnowledgeGraph", "read").unwrap();
        // B's OWN access (unrelated; must survive).
        db.kg_set_access("b", "B_KnowledgeGraph", "write").unwrap();

        // Simulate the FK CASCADE (would happen via db.delete_project("a")):
        {
            let guard = db.lock();
            guard
                .execute("DELETE FROM kg_collection_access WHERE project_id = 'a'", [])
                .unwrap();
        }

        // Pre-MF4: B's peer row is still there.
        assert_eq!(read_access(&db, "b", "A_KnowledgeGraph"), Some("read".to_string()));

        // MF4 sweep.
        let n = db
            .delete_orphan_peer_access_for_collections("a", &["A_KnowledgeGraph".to_string()])
            .unwrap();
        assert_eq!(n, 1, "MF4 must drop exactly 1 peer row (B's grant)");

        // B's peer grant on A's collection is gone.
        assert_eq!(read_access(&db, "b", "A_KnowledgeGraph"), None);
        // B's own access is unaffected.
        assert_eq!(
            read_access(&db, "b", "B_KnowledgeGraph"),
            Some("write".to_string())
        );
    }

    #[test]
    fn delete_orphan_peer_excludes_deleted_projects_own_rows() {
        // Edge case: if the caller passes the deleted project's own
        // collection_names but the deleted project's rows haven't
        // been cascaded yet (e.g. the sweep runs BEFORE delete_project
        // by mistake), the helper must NOT count the deleted
        // project's own rows in its result. The WHERE clause
        // `project_id != ?1` enforces this.
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "a");

        // Only A has rows (no peers).
        db.kg_set_access("a", "A_KG", "write").unwrap();

        // Call MF4 sweep on A's own collection BEFORE cascading A's
        // delete. The helper should report 0 deletions (A's own row
        // is excluded by the WHERE).
        let n = db
            .delete_orphan_peer_access_for_collections("a", &["A_KG".to_string()])
            .unwrap();
        assert_eq!(n, 0, "deleted project's own rows must be excluded");

        // A's own row is still there (was never deleted by this helper).
        assert_eq!(read_access(&db, "a", "A_KG"), Some("write".to_string()));
    }

    #[test]
    fn delete_orphan_peer_empty_collection_list_is_no_op() {
        // Defensive: empty input list short-circuits before touching the DB.
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "a");
        db.kg_set_access("a", "A_KG", "write").unwrap();

        let n = db
            .delete_orphan_peer_access_for_collections("a", &[])
            .unwrap();
        assert_eq!(n, 0);
        // Existing row untouched.
        assert_eq!(read_access(&db, "a", "A_KG"), Some("write".to_string()));
    }

    #[test]
    fn delete_orphan_peer_drops_multiple_peer_rows_across_multiple_collections() {
        // Scale test: 3 peer projects each with grants on 2 of A's
        // collections. After A's delete + sweep, all 6 peer rows are gone.
        let db = Db::open_in_memory().unwrap();
        for pid in &["a", "b", "c", "d"] {
            seed_project(&db, pid);
        }

        // A's own rows.
        db.kg_set_access("a", "A_KG", "write").unwrap();
        db.kg_set_access("a", "A_Dev", "write").unwrap();

        // 3 peer projects × 2 collections = 6 peer rows.
        for pid in &["b", "c", "d"] {
            db.kg_set_access(pid, "A_KG", "read").unwrap();
            db.kg_set_access(pid, "A_Dev", "read").unwrap();
        }

        // Simulate cascade.
        {
            let guard = db.lock();
            guard
                .execute("DELETE FROM kg_collection_access WHERE project_id = 'a'", [])
                .unwrap();
        }

        // Sweep both collections.
        let n = db
            .delete_orphan_peer_access_for_collections(
                "a",
                &["A_KG".to_string(), "A_Dev".to_string()],
            )
            .unwrap();
        assert_eq!(n, 6, "expected 3 peers × 2 collections = 6 peer rows");

        // No peer rows remain on A's collections.
        for pid in &["b", "c", "d"] {
            assert_eq!(read_access(&db, pid, "A_KG"), None);
            assert_eq!(read_access(&db, pid, "A_Dev"), None);
        }
    }

    // ─── kg_rename_access ─────────────────────────────────────────────

    #[test]
    fn kg_rename_access_updates_collection_name() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");
        db.kg_set_access("p1", "OldName_KnowledgeGraph", "write").unwrap();

        let renamed = db
            .kg_rename_access("p1", "OldName_KnowledgeGraph", "NewName_KnowledgeGraph")
            .unwrap();
        assert_eq!(renamed, 1);
        assert_eq!(read_access(&db, "p1", "OldName_KnowledgeGraph"), None);
        assert_eq!(
            read_access(&db, "p1", "NewName_KnowledgeGraph"),
            Some("write".to_string())
        );
    }

    #[test]
    fn kg_rename_access_no_source_row_is_noop() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");
        let renamed = db
            .kg_rename_access("p1", "Absent", "Whatever")
            .unwrap();
        assert_eq!(renamed, 0);
    }

    #[test]
    fn kg_rename_access_when_target_already_exists_keeps_higher_privilege() {
        // If a row already exists at the target name, the rename merges:
        // - the OLD row is deleted (preserves no-duplicate invariant)
        // - the HIGHER of (source, existing-target) access_level is
        //   preserved on the target (never lower an existing privilege).
        //
        // v0.2.46 adversarial-review L3 follow-up: previously this test
        // only covered the source-lower case (source=read, target=write),
        // which silently passed because the implementation dropped the
        // source and left the target unchanged. That was the right answer
        // BY ACCIDENT — when source is HIGHER (write) and target is
        // LOWER (read), the pre-L3 implementation would have downgraded
        // the user's effective access. Both directions are now tested.
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");
        db.kg_set_access("p1", "Source_KG", "read").unwrap();
        db.kg_set_access("p1", "Target_KG", "write").unwrap();

        let renamed = db.kg_rename_access("p1", "Source_KG", "Target_KG").unwrap();
        // Renamed=1 means we resolved the collision (deleted Source_KG).
        assert_eq!(renamed, 1);
        assert_eq!(read_access(&db, "p1", "Source_KG"), None);
        // Target_KG keeps its WRITE level (don't downgrade to read).
        assert_eq!(read_access(&db, "p1", "Target_KG"), Some("write".to_string()));
    }

    #[test]
    fn kg_rename_access_source_higher_target_lower_upgrades_target() {
        // v0.2.46 adversarial-review L3: when source has HIGHER privilege
        // than the existing target, the target row's access_level must
        // be UPGRADED to the source's level (never lower the user's
        // effective privilege). Pre-L3 the source was simply dropped
        // and the target left at its lower level — silent downgrade.
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");
        db.kg_set_access("p1", "Source_KG", "write").unwrap();
        db.kg_set_access("p1", "Target_KG", "read").unwrap();

        let renamed = db.kg_rename_access("p1", "Source_KG", "Target_KG").unwrap();
        assert_eq!(renamed, 1);
        assert_eq!(read_access(&db, "p1", "Source_KG"), None);
        // Target_KG must be UPGRADED to write (source's level).
        assert_eq!(
            read_access(&db, "p1", "Target_KG"),
            Some("write".to_string()),
            "L3 invariant: rename must upgrade target to source's higher \
             privilege, never silently downgrade"
        );
    }

    #[test]
    fn kg_rename_access_equal_privileges_no_change() {
        // When source and target have the same level, the merge drops
        // source and leaves target unchanged.
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");
        db.kg_set_access("p1", "Source_KG", "read").unwrap();
        db.kg_set_access("p1", "Target_KG", "read").unwrap();

        let renamed = db.kg_rename_access("p1", "Source_KG", "Target_KG").unwrap();
        assert_eq!(renamed, 1);
        assert_eq!(read_access(&db, "p1", "Source_KG"), None);
        assert_eq!(read_access(&db, "p1", "Target_KG"), Some("read".to_string()));
    }

    #[test]
    fn kg_rename_access_scoped_to_project() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");
        seed_project(&db, "p2");
        db.kg_set_access("p1", "OldName_KG", "read").unwrap();
        db.kg_set_access("p2", "OldName_KG", "read").unwrap();

        let renamed = db
            .kg_rename_access("p1", "OldName_KG", "NewName_KG")
            .unwrap();
        assert_eq!(renamed, 1);
        // p2 untouched.
        assert_eq!(read_access(&db, "p2", "OldName_KG"), Some("read".to_string()));
        assert_eq!(read_access(&db, "p2", "NewName_KG"), None);
    }

    // ─── reconcile_kg_collection_access (boot helper) ─────────────────
    //
    // v0.2.49 Phase 5 item #16 (M-1) update: the existing tests below
    // use VCO-managed collection names (`*_KnowledgeGraph`) so the
    // orphan-drop predicate matches. Tests for the M-1 scope-narrowing
    // (drop ONLY VCO-managed names; preserve user-unrelated classes
    // unconditionally) are added at the bottom of this module.

    /// reconcile drops rows whose `collection_name` doesn't match any
    /// binding for any project AND doesn't appear in the supplied
    /// `existing_classes` set (the Weaviate-known classes). Keeps rows
    /// whose collection EITHER exists in Weaviate OR is named by a
    /// binding row somewhere (e.g. a peer's primary).
    #[test]
    fn reconcile_drops_orphan_with_no_binding_and_no_class() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");
        seed_project(&db, "p2");
        // p1 owns Foo_KnowledgeGraph (its binding); p2 grants read access on it.
        let folder = format!("/tmp/test-kg-access/orphan");
        let _ = folder; // suppress unused
        db.set_project_kg_binding(
            "p1",
            "primary",
            "Foo_KnowledgeGraph",
            None,
            None,
            None,
            None,
            &serde_json::Value::Null,
        )
        .unwrap();
        db.kg_set_access("p1", "Foo_KnowledgeGraph", "write").unwrap();
        db.kg_set_access("p2", "Foo_KnowledgeGraph", "read").unwrap();
        // p2 ALSO grants read on a stale name — no binding, no Weaviate class.
        // Name uses the `_KnowledgeGraph` suffix so M-1's VCO-managed
        // predicate matches (item #16); user-unrelated names are
        // exercised by `reconcile_preserves_user_unrelated_collections`.
        db.kg_set_access("p2", "StaleOrphan_KnowledgeGraph", "read").unwrap();

        // Pretend Weaviate has only Foo_KnowledgeGraph (no StaleOrphan).
        let existing: std::collections::HashSet<String> =
            ["Foo_KnowledgeGraph".to_string()].into_iter().collect();

        let dropped = db.reconcile_kg_collection_access(&existing).unwrap();
        assert_eq!(dropped, 1, "must drop StaleOrphan_KnowledgeGraph");

        // Live rows preserved.
        assert_eq!(
            read_access(&db, "p1", "Foo_KnowledgeGraph"),
            Some("write".to_string())
        );
        assert_eq!(
            read_access(&db, "p2", "Foo_KnowledgeGraph"),
            Some("read".to_string())
        );
        // Orphan dropped.
        assert_eq!(read_access(&db, "p2", "StaleOrphan_KnowledgeGraph"), None);
    }

    #[test]
    fn reconcile_keeps_rows_for_existing_classes_without_binding() {
        // If a class exists in Weaviate but no binding row names it, we
        // STILL keep the access row — the user may legitimately grant
        // peer-access to a peer's collection we don't have a binding
        // for. The reconcile is "drop unreferenced AND unknown", not
        // "drop everything without a binding".
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");
        db.kg_set_access("p1", "PeerOrch_KnowledgeGraph", "read").unwrap();

        let existing: std::collections::HashSet<String> =
            ["PeerOrch_KnowledgeGraph".to_string()].into_iter().collect();

        let dropped = db.reconcile_kg_collection_access(&existing).unwrap();
        assert_eq!(dropped, 0);
        assert_eq!(
            read_access(&db, "p1", "PeerOrch_KnowledgeGraph"),
            Some("read".to_string())
        );
    }

    #[test]
    fn reconcile_keeps_rows_named_by_binding_even_when_class_absent() {
        // Weaviate-class absence is NOT a delete trigger when a binding
        // row still names that collection — the binding owns the
        // expectation that the class will exist lazily.
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");
        db.set_project_kg_binding(
            "p1",
            "primary",
            "LazyClass_KnowledgeGraph",
            None,
            None,
            None,
            None,
            &serde_json::Value::Null,
        )
        .unwrap();
        db.kg_set_access("p1", "LazyClass_KnowledgeGraph", "write").unwrap();

        // Weaviate is empty.
        let existing: std::collections::HashSet<String> = Default::default();

        let dropped = db.reconcile_kg_collection_access(&existing).unwrap();
        assert_eq!(dropped, 0, "binding-named collection must NOT be dropped even when absent from Weaviate");
        assert_eq!(
            read_access(&db, "p1", "LazyClass_KnowledgeGraph"),
            Some("write".to_string())
        );
    }

    #[test]
    fn reconcile_idempotent_second_call_is_noop() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");
        db.kg_set_access("p1", "Orphan_KnowledgeGraph", "read").unwrap();
        let existing: std::collections::HashSet<String> = Default::default();
        assert_eq!(db.reconcile_kg_collection_access(&existing).unwrap(), 1);
        // Second call: orphan gone, no further work.
        assert_eq!(db.reconcile_kg_collection_access(&existing).unwrap(), 0);
    }

    #[test]
    fn reconcile_empty_db_is_noop() {
        let db = Db::open_in_memory().unwrap();
        let existing: std::collections::HashSet<String> = Default::default();
        assert_eq!(db.reconcile_kg_collection_access(&existing).unwrap(), 0);
    }

    // ─── v0.2.49 Phase 5 item #16 (M-1): VCO-managed scope-narrowing ──
    //
    // The boot reconcile MUST NOT drop access rows for collections
    // outside VCO's stewardship (user-created Weaviate classes, classes
    // belonging to other tools that share the Weaviate instance). The
    // scope is: collection names matching VCO suffix patterns ONLY.

    /// All eight VCO suffix patterns must be subject to orphan-drop
    /// when they're truly orphaned. This guards against a future
    /// refactor that accidentally narrows the suffix list and leaves
    /// real orphans behind.
    #[test]
    fn reconcile_drops_only_vco_managed_collections() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");

        // One orphan row per VCO-managed suffix.
        let vco_managed_names = [
            "Foo_KnowledgeGraph",
            "Foo_Development",
            "Foo_Diagrams",
            "Foo_CodeModule",
            "Foo_CodeClass",
            "Foo_CodeFunction",
            "Foo_CodeAPI",
            "Foo_CodeInteraction",
        ];
        for name in &vco_managed_names {
            db.kg_set_access("p1", name, "read").unwrap();
        }

        // Weaviate is empty + no bindings exist → every VCO-managed
        // row is an orphan that the M-1 predicate matches.
        let existing: std::collections::HashSet<String> = Default::default();
        let dropped = db.reconcile_kg_collection_access(&existing).unwrap();
        assert_eq!(
            dropped,
            vco_managed_names.len(),
            "every VCO-managed orphan must be dropped",
        );
        for name in &vco_managed_names {
            assert_eq!(
                read_access(&db, "p1", name),
                None,
                "row '{}' must have been dropped",
                name,
            );
        }
    }

    /// Rows pointing to collections OUTSIDE VCO's stewardship (the
    /// user's own experiments, other tools' Weaviate classes) MUST be
    /// preserved by boot reconcile — even when they're absent from
    /// both Weaviate and the binding table. M-1's "VCO doesn't garbage-
    /// collect user data" invariant.
    #[test]
    fn reconcile_preserves_user_unrelated_collections() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");

        // Names the user (or another tool) may have created that the
        // launcher knows nothing about. None of these match a VCO
        // suffix → none must be dropped.
        let user_unrelated_names = [
            "MyExperiment",
            "Custom_Collection",
            "OtherTool_Data",
            "PostgresMigration",
            "TestCollection123",
            "Foo_Bar",  // ends with _Bar, not a VCO suffix
            "Foo_KG",   // legacy / non-VCO name pattern
            "Article",  // bare name without prefix
        ];
        for name in &user_unrelated_names {
            db.kg_set_access("p1", name, "read").unwrap();
        }

        // Weaviate empty + no bindings → would be orphans under the
        // pre-v0.2.49 reconcile. Under M-1 they're preserved because
        // the name doesn't match a VCO suffix.
        let existing: std::collections::HashSet<String> = Default::default();
        let dropped = db.reconcile_kg_collection_access(&existing).unwrap();
        assert_eq!(
            dropped, 0,
            "no user-unrelated row may be dropped (M-1 invariant)",
        );
        for name in &user_unrelated_names {
            assert_eq!(
                read_access(&db, "p1", name),
                Some("read".to_string()),
                "user-unrelated row '{}' must be preserved",
                name,
            );
        }
    }
}

// ─── Tests: adopt_populated_collections_at_boot (W40-B / v0.2.40) ─────────

#[cfg(test)]
mod adopt_populated_tests {
    use super::AdoptionReport;
    use crate::db::Db;
    use serde_json::json;
    use std::collections::HashMap;
    use std::io::{Read as _, Write as _};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};
    use std::thread;

    /// Tiny mock Weaviate server. Spawns a synchronous TCP listener on
    /// an ephemeral port that responds to:
    ///   * `GET /v1/schema` → JSON with a `classes` array.
    ///   * `POST /v1/graphql` (an Aggregate query) → JSON with a count
    ///     for the requested class.
    ///
    /// Why a hand-rolled server and not an external mock crate: the
    /// `vct-launcher-core` crate has no test-dependency budget (see
    /// the `[dev-dependencies]` rationale in Cargo.toml's comments —
    /// any feature-gated dep risks tauri-build environment issues).
    /// A 40-line TCP echo loop is cheaper than adding `httpmock`.
    ///
    /// The server runs until the test drops the handle (the listener
    /// goes out of scope and the accept loop returns).
    struct MockWeaviate {
        url: String,
        _handle: thread::JoinHandle<()>,
    }

    impl MockWeaviate {
        /// Spawn a mock server. `classes` is the list of class names
        /// served by `/v1/schema`. `counts` maps each class to a row
        /// count served by GraphQL Aggregate.
        ///
        /// The server handles a bounded number of requests (16) then
        /// quietly exits. Tests need at most: 1 schema fetch + N count
        /// probes, where N is the candidate count — well under 16.
        fn spawn(classes: Vec<String>, counts: HashMap<String, u64>) -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").expect("bind ephemeral");
            let port = listener.local_addr().unwrap().port();
            let url = format!("http://127.0.0.1:{}", port);

            let classes_arc = Arc::new(classes);
            let counts_arc = Arc::new(Mutex::new(counts));

            let handle = thread::spawn(move || {
                listener.set_nonblocking(false).ok();
                for (i, stream) in listener.incoming().enumerate() {
                    if i >= 16 {
                        break;
                    }
                    let Ok(mut stream) = stream else { continue };
                    let mut buf = [0u8; 8192];
                    let n = stream.read(&mut buf).unwrap_or(0);
                    let req = String::from_utf8_lossy(&buf[..n]).to_string();

                    let body = if req.starts_with("GET /v1/schema") {
                        serde_json::json!({
                            "classes": classes_arc
                                .iter()
                                .map(|c| serde_json::json!({"class": c}))
                                .collect::<Vec<_>>()
                        })
                        .to_string()
                    } else if req.contains("POST /v1/graphql") {
                        // Extract the class name from the GraphQL query.
                        // Body starts after the blank line; the query has
                        // shape `{ Aggregate { ClassName { meta {...} } } }`.
                        // Crude but sufficient for the test fixture.
                        let class = req
                            .split("Aggregate")
                            .nth(1)
                            .and_then(|s| s.split('{').nth(1))
                            .map(|s| s.trim().to_string())
                            .unwrap_or_default();
                        let count = counts_arc
                            .lock()
                            .unwrap()
                            .get(&class)
                            .copied()
                            .unwrap_or(0);
                        serde_json::json!({
                            "data": {
                                "Aggregate": {
                                    class: [
                                        { "meta": { "count": count } }
                                    ]
                                }
                            }
                        })
                        .to_string()
                    } else {
                        "{}".to_string()
                    };

                    let response = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                        body.len(),
                        body
                    );
                    let _ = stream.write_all(response.as_bytes());
                    let _ = stream.flush();
                }
            });

            Self { url, _handle: handle }
        }
    }

    /// Seed a project + a `project_kg_bindings` row.
    fn seed_binding(
        db: &Db,
        project_id: &str,
        role: &str,
        collection: &str,
        config: serde_json::Value,
    ) {
        let now = 1_700_000_000_000_i64;
        let folder = format!("/tmp/test-w40b/{}", project_id);
        {
            let guard = db.lock();
            guard.execute(
                "INSERT OR IGNORE INTO projects \
                 (id, name, folder_path, host, created_at, updated_at, slug) \
                 VALUES (?1, ?2, ?3, 'base', ?4, ?4, ?1)",
                rusqlite::params![project_id, project_id, folder, now],
            ).unwrap_or_else(|e| panic!("seed project {}: {}", project_id, e));
        }
        db.set_project_kg_binding(
            project_id,
            role,
            collection,
            None,
            None,
            None,
            None,
            &config,
        )
        .expect("seed binding");
    }

    /// Helper: read a single binding back out.
    fn read_binding(
        db: &Db,
        project_id: &str,
        role: &str,
    ) -> (String, serde_json::Value) {
        let bindings = db.list_project_kg_bindings(project_id).unwrap();
        let b = bindings
            .into_iter()
            .find(|b| b.role == role)
            .unwrap_or_else(|| panic!("binding {}/{} missing", project_id, role));
        (b.collection_name, b.config)
    }

    /// T1 — Fresh install shape: binding's `collection_name` is present
    /// in Weaviate. No adoption needed, report `no_change=1`.
    #[tokio::test]
    async fn t1_existing_collection_is_no_op() {
        let db = Db::open_in_memory().unwrap();
        seed_binding(
            &db,
            "p1",
            "primary",
            "VibeCodedOrchestrator_KnowledgeGraph",
            json!({}),
        );

        let mock = MockWeaviate::spawn(
            vec!["VibeCodedOrchestrator_KnowledgeGraph".to_string()],
            HashMap::from([(
                "VibeCodedOrchestrator_KnowledgeGraph".to_string(),
                42_u64,
            )]),
        );

        let report = db
            .adopt_populated_collections_at_boot(&mock.url)
            .await
            .unwrap();
        assert_eq!(
            report,
            AdoptionReport { adopted: 0, deferred: 0, no_change: 1 }
        );

        // Binding unchanged.
        let (name, _cfg) = read_binding(&db, "p1", "primary");
        assert_eq!(name, "VibeCodedOrchestrator_KnowledgeGraph");
    }

    /// T2 — VCO_dev's broken shape: binding names a missing class, but
    /// a populated same-suffix candidate (different prefix) exists.
    /// Auto-adopt; binding's config_json gains
    /// `manual_override:v0.2.40-prefix-adopt`.
    #[tokio::test]
    async fn t2_vco_dev_shape_single_populated_candidate_adopted() {
        let db = Db::open_in_memory().unwrap();
        seed_binding(
            &db,
            "p1",
            "shared",
            "VibeCodedOrchestrator_KnowledgeGraph",
            json!({"manual_override": "v0.2.28-recovery"}),
        );

        // Weaviate has VCODev_KnowledgeGraph (1033 rows). Advertised
        // VibeCodedOrchestrator_KnowledgeGraph is missing entirely.
        let mock = MockWeaviate::spawn(
            vec!["VCODev_KnowledgeGraph".to_string()],
            HashMap::from([("VCODev_KnowledgeGraph".to_string(), 1033_u64)]),
        );

        let report = db
            .adopt_populated_collections_at_boot(&mock.url)
            .await
            .unwrap();
        assert_eq!(
            report,
            AdoptionReport { adopted: 1, deferred: 0, no_change: 0 }
        );

        // Binding rebound to VCODev_KnowledgeGraph; sentinel updated.
        let (name, cfg) = read_binding(&db, "p1", "shared");
        assert_eq!(name, "VCODev_KnowledgeGraph");
        assert_eq!(
            cfg.get("manual_override").and_then(|v| v.as_str()),
            Some("v0.2.40-prefix-adopt"),
            "sentinel must reflect the boot-time prefix adoption \
             (overrides the prior v0.2.28-recovery on this row)"
        );
    }

    /// T3 — Two populated candidates → don't auto-pick; defer.
    /// Binding remains unchanged so the user can resolve via the GUI.
    #[tokio::test]
    async fn t3_multiple_populated_candidates_defer() {
        let db = Db::open_in_memory().unwrap();
        seed_binding(
            &db,
            "p1",
            "shared",
            "VibeCodedOrchestrator_KnowledgeGraph",
            json!({}),
        );

        let mock = MockWeaviate::spawn(
            vec![
                "VCODev_KnowledgeGraph".to_string(),
                "AcmeOrch_KnowledgeGraph".to_string(),
            ],
            HashMap::from([
                ("VCODev_KnowledgeGraph".to_string(), 500_u64),
                ("AcmeOrch_KnowledgeGraph".to_string(), 200_u64),
            ]),
        );

        let report = db
            .adopt_populated_collections_at_boot(&mock.url)
            .await
            .unwrap();
        assert_eq!(
            report,
            AdoptionReport { adopted: 0, deferred: 1, no_change: 0 }
        );

        // Binding intact — we never auto-pick on ambiguity.
        let (name, _cfg) = read_binding(&db, "p1", "shared");
        assert_eq!(name, "VibeCodedOrchestrator_KnowledgeGraph");
    }

    /// T4 — Zero populated candidates → no-op. The user's
    /// not-yet-populated binding is preserved (they may be about to
    /// run a seed).
    #[tokio::test]
    async fn t4_no_populated_candidates_no_op() {
        let db = Db::open_in_memory().unwrap();
        seed_binding(
            &db,
            "p1",
            "primary",
            "BrandNew_KnowledgeGraph",
            json!({}),
        );

        // Weaviate has a same-suffix class but it's empty (count=0).
        let mock = MockWeaviate::spawn(
            vec!["OtherProject_KnowledgeGraph".to_string()],
            HashMap::from([("OtherProject_KnowledgeGraph".to_string(), 0_u64)]),
        );

        let report = db
            .adopt_populated_collections_at_boot(&mock.url)
            .await
            .unwrap();
        assert_eq!(
            report,
            AdoptionReport { adopted: 0, deferred: 0, no_change: 1 }
        );

        // Binding intact.
        let (name, _cfg) = read_binding(&db, "p1", "primary");
        assert_eq!(name, "BrandNew_KnowledgeGraph");
    }

    /// T5 — Idempotency: after a successful T2-shape adoption, running
    /// again no-ops (the binding's new collection_name now exists in
    /// Weaviate, so step 2a short-circuits).
    #[tokio::test]
    async fn t5_idempotent_after_adoption() {
        let db = Db::open_in_memory().unwrap();
        seed_binding(
            &db,
            "p1",
            "shared",
            "VibeCodedOrchestrator_KnowledgeGraph",
            json!({}),
        );

        // First server: missing advertised, populated candidate.
        let mock1 = MockWeaviate::spawn(
            vec!["VCODev_KnowledgeGraph".to_string()],
            HashMap::from([("VCODev_KnowledgeGraph".to_string(), 1033_u64)]),
        );
        let first = db
            .adopt_populated_collections_at_boot(&mock1.url)
            .await
            .unwrap();
        assert_eq!(first.adopted, 1);

        // Second server: binding now points at the existing class.
        // (We use a fresh mock because the first listener has finite
        // accept budget; the test fixture isn't a long-running server.)
        let mock2 = MockWeaviate::spawn(
            vec!["VCODev_KnowledgeGraph".to_string()],
            HashMap::from([("VCODev_KnowledgeGraph".to_string(), 1033_u64)]),
        );
        let second = db
            .adopt_populated_collections_at_boot(&mock2.url)
            .await
            .unwrap();
        assert_eq!(
            second,
            AdoptionReport { adopted: 0, deferred: 0, no_change: 1 },
            "second call must be a no-op"
        );
    }

    /// T6 — Case-sibling handling. A binding whose case-different
    /// sibling exists on disk MUST be left alone (the case-insensitive
    /// self-heal owns that scenario; we never compete with it). Without
    /// this guard, W40-B would steal cases from the install.py path and
    /// rewrite via the prefix-adopt sentinel instead of the proper
    /// case-rebind sentinel.
    #[tokio::test]
    async fn t6_case_sibling_is_no_op() {
        let db = Db::open_in_memory().unwrap();
        // Binding names capital-C; Weaviate has lowercase-c (case-sibling).
        seed_binding(
            &db,
            "p1",
            "shared",
            "VibeCodedOrchestrator_KnowledgeGraph",
            json!({}),
        );

        let mock = MockWeaviate::spawn(
            vec!["VibecodedOrchestrator_KnowledgeGraph".to_string()],
            HashMap::from([(
                "VibecodedOrchestrator_KnowledgeGraph".to_string(),
                10_u64,
            )]),
        );

        let report = db
            .adopt_populated_collections_at_boot(&mock.url)
            .await
            .unwrap();
        assert_eq!(
            report,
            AdoptionReport { adopted: 0, deferred: 0, no_change: 1 },
            "case-sibling must be deferred to the case-insensitive heal"
        );

        // Binding untouched.
        let (name, _cfg) = read_binding(&db, "p1", "shared");
        assert_eq!(name, "VibeCodedOrchestrator_KnowledgeGraph");
    }

    /// T7 — Weaviate unreachable → Err. Caller (launcher boot) logs
    /// and continues; boot is NOT blocked.
    #[tokio::test]
    async fn t7_weaviate_unreachable_returns_err() {
        let db = Db::open_in_memory().unwrap();
        seed_binding(
            &db,
            "p1",
            "primary",
            "VibeCodedOrchestrator_KnowledgeGraph",
            json!({}),
        );

        // Bind+drop a listener to grab an unused port; nothing will
        // listen on it for the call.
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let dead_url = format!("http://127.0.0.1:{}", port);

        let result = db.adopt_populated_collections_at_boot(&dead_url).await;
        assert!(result.is_err(), "expected Err on unreachable Weaviate, got {:?}", result);
    }
}

// ─── Step A.5 access-matrix audit-column tests ────────────────────────────
//
// Pin migration 029's contract: the `kg_collection_access` schema has
// `created_at` + `updated_at INTEGER NOT NULL DEFAULT 0` columns; new
// INSERTs from `kg_set_access` and `kg_seed_access` bind them with the
// load-bearing seed-path invariant `created_at == updated_at` on first
// INSERT.

#[cfg(test)]
mod access_audit_column_tests {
    use super::*;

    /// Helper: insert a project row so FK constraints don't reject the
    /// kg_collection_access INSERT.
    fn seed_proj(db: &Db, id: &str) {
        let now: i64 = 1_700_000_000_000;
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
                 VALUES (?1, ?1, ?2, 'base', ?3, ?3, ?1)",
                params![id, format!("/tmp/{}", id), now],
            )
            .unwrap();
    }

    #[test]
    fn migration_029_adds_audit_columns_with_default_zero() {
        // Fresh in-memory DB applies migrations through 029. Insert a
        // row with only the 3 canonical columns bound (legacy-shape
        // INSERT, e.g. raw SQL that pre-dates v0.2.49 callers). The
        // audit columns default to 0.
        let db = Db::open_in_memory().unwrap();
        seed_proj(&db, "p1");
        {
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO kg_collection_access (project_id, collection_name, access_level)
                     VALUES ('p1', 'LegacyKG', 'read')",
                    [],
                )
                .unwrap();
        }
        let row = db
            .kg_get_access_row("p1", "LegacyKG")
            .expect("read row")
            .expect("row present");
        assert_eq!(row.access_level, "read");
        assert_eq!(row.created_at, 0, "legacy row's created_at must default to 0");
        assert_eq!(row.updated_at, 0, "legacy row's updated_at must default to 0");
        assert!(
            !row.is_user_configured(),
            "legacy row (created_at == updated_at == 0) must read NOT user-configured",
        );
    }

    #[test]
    fn kg_set_access_first_insert_sets_created_eq_updated() {
        // Seed-path invariant: the first INSERT into a (project_id,
        // collection) pair via `kg_set_access` sets both audit
        // timestamps to the same value. The `is_user_configured`
        // predicate reads FALSE because the equality holds.
        let db = Db::open_in_memory().unwrap();
        seed_proj(&db, "p1");
        db.kg_set_access("p1", "FreshKG", "write").unwrap();

        let row = db
            .kg_get_access_row("p1", "FreshKG")
            .expect("read")
            .expect("present");
        assert_eq!(row.access_level, "write");
        assert_eq!(
            row.created_at, row.updated_at,
            "first INSERT must set created_at == updated_at; got created={}, updated={}",
            row.created_at, row.updated_at,
        );
        assert!(
            row.created_at > 0,
            "v0.2.49+ INSERT must bind a non-zero timestamp",
        );
    }

    #[test]
    fn kg_set_access_upsert_bumps_updated_at_only() {
        // User-mutation signal: a subsequent INSERT (treated as upsert
        // via ON CONFLICT) bumps `updated_at` but PRESERVES
        // `created_at`. The `is_user_configured` predicate flips to
        // TRUE because the timestamps diverge.
        let db = Db::open_in_memory().unwrap();
        seed_proj(&db, "p1");
        db.kg_set_access("p1", "TouchKG", "read").unwrap();
        let initial = db
            .kg_get_access_row("p1", "TouchKG")
            .unwrap()
            .unwrap();
        // Sleep enough that `chrono::Utc::now().timestamp_millis()`
        // returns a strictly larger value. Wall-clock-millisecond
        // granularity is fine; 2 ms is safely above any clock-resolution
        // floor we care about.
        std::thread::sleep(std::time::Duration::from_millis(2));

        db.kg_set_access("p1", "TouchKG", "write").unwrap();
        let updated = db
            .kg_get_access_row("p1", "TouchKG")
            .unwrap()
            .unwrap();
        assert_eq!(
            updated.created_at, initial.created_at,
            "upsert must preserve created_at (got created={}, expected={})",
            updated.created_at, initial.created_at,
        );
        assert!(
            updated.updated_at > initial.updated_at,
            "upsert must bump updated_at past the previous value; \
             updated_at went from {} to {}",
            initial.updated_at, updated.updated_at,
        );
        assert!(
            updated.is_user_configured(),
            "row post-upsert (created_at != updated_at) must read user-configured",
        );
    }

    /// v0.2.49 Step F SB4 (L1-F4): a no-op UPSERT (writing the same
    /// access_level that's already persisted) MUST NOT bump
    /// `updated_at`. Pre-Step-F the SQL `DO UPDATE SET updated_at =
    /// excluded.updated_at` clause bumped unconditionally, which
    /// poisoned the `is_user_configured` predicate for downstream
    /// F-2c logic ("preserve user-configured peers" mode-setter loop).
    /// Concrete failure pre-fix:
    ///   1. Owner clicks "Mode: shared" → all peers seeded to 'read'
    ///      via kg_set_access. updated_at = T1, created_at = T0,
    ///      is_user_configured = TRUE (incorrect — user only touched
    ///      the OWNER row, not the peers).
    ///   2. Owner clicks "Mode: shared" AGAIN → F-2c's
    ///      `if peer_is_user_configured { continue }` gate SKIPS
    ///      every peer because all read TRUE → preserve logic fires
    ///      on SYSTEM-stamped peers, opposite of intent.
    /// Post-fix: a no-op rewrite (level didn't change) preserves
    /// updated_at, predicate stays correct, F-2c works correctly.
    #[test]
    fn kg_set_access_no_op_upsert_preserves_updated_at() {
        let db = Db::open_in_memory().unwrap();
        seed_proj(&db, "p1");
        db.kg_set_access("p1", "NoOpKG", "read").unwrap();
        let initial = db
            .kg_get_access_row("p1", "NoOpKG")
            .unwrap()
            .unwrap();

        // Sleep enough that a NAIVE re-stamp (pre-Step-F behavior)
        // would produce a strictly larger updated_at. Post-Step-F
        // the CASE clause must preserve the original value.
        std::thread::sleep(std::time::Duration::from_millis(5));

        // Write the SAME level — this is the no-op path.
        db.kg_set_access("p1", "NoOpKG", "read").unwrap();
        let post = db
            .kg_get_access_row("p1", "NoOpKG")
            .unwrap()
            .unwrap();

        assert_eq!(post.access_level, "read", "level unchanged (no-op)");
        assert_eq!(
            post.updated_at, initial.updated_at,
            "v0.2.49 Step F SB4: no-op upsert MUST NOT bump updated_at \
             (pre-fix this leaked a false user-configured signal into \
             F-2c's load-bearing predicate). got pre={}, post={}",
            initial.updated_at, post.updated_at,
        );
        assert_eq!(
            post.created_at, initial.created_at,
            "created_at always preserved on upsert",
        );
        // is_user_configured semantically stable across no-op writes.
        assert_eq!(
            post.is_user_configured(),
            initial.is_user_configured(),
            "is_user_configured predicate must NOT flip on no-op writes",
        );
    }

    /// v0.2.49 Step F SB4 sibling test: a REAL change (different
    /// level) DOES bump `updated_at`. Pin the positive case so future
    /// drift of the CASE clause's branch logic is caught.
    #[test]
    fn kg_set_access_real_change_bumps_updated_at() {
        let db = Db::open_in_memory().unwrap();
        seed_proj(&db, "p1");
        db.kg_set_access("p1", "RealChangeKG", "read").unwrap();
        let initial = db
            .kg_get_access_row("p1", "RealChangeKG")
            .unwrap()
            .unwrap();
        std::thread::sleep(std::time::Duration::from_millis(5));

        // Real change: read → write.
        db.kg_set_access("p1", "RealChangeKG", "write").unwrap();
        let post = db
            .kg_get_access_row("p1", "RealChangeKG")
            .unwrap()
            .unwrap();

        assert_eq!(post.access_level, "write");
        assert!(
            post.updated_at > initial.updated_at,
            "real-change upsert (read→write) MUST bump updated_at",
        );
    }

    #[test]
    fn kg_seed_access_preserves_invariant_on_first_insert() {
        // The dedicated seed-path setter also sets timestamps equal.
        // Used by install.py parity self-heal, migrations, boot probes
        // — code paths that write "the system's default" rather than
        // "the user's value."
        let db = Db::open_in_memory().unwrap();
        seed_proj(&db, "p1");
        let n = db.kg_seed_access("p1", "SeedKG", "write").unwrap();
        assert_eq!(n, 1, "fresh row should be inserted");

        let row = db
            .kg_get_access_row("p1", "SeedKG")
            .unwrap()
            .unwrap();
        assert_eq!(row.access_level, "write");
        assert_eq!(
            row.created_at, row.updated_at,
            "seed-path INSERT must preserve created_at == updated_at",
        );
        assert!(!row.is_user_configured());
    }

    #[test]
    fn kg_seed_access_is_idempotent_does_not_clobber_user_row() {
        // INSERT OR IGNORE semantics: a seed-path call on an existing
        // row is a no-op. The existing row's audit data + access
        // level are preserved verbatim — even if the user UPSERTed
        // it after the original seed.
        let db = Db::open_in_memory().unwrap();
        seed_proj(&db, "p1");
        // 1. First seed.
        db.kg_seed_access("p1", "K", "read").unwrap();
        // 2. User upserts to a different level (this bumps updated_at,
        //    flipping `is_user_configured` to TRUE).
        std::thread::sleep(std::time::Duration::from_millis(2));
        db.kg_set_access("p1", "K", "write").unwrap();
        let after_user = db.kg_get_access_row("p1", "K").unwrap().unwrap();
        assert!(after_user.is_user_configured());
        // 3. Seed-path is called again (e.g. install.py --update re-runs
        //    parity self-heal). It MUST NOT clobber the user-chosen row.
        let n = db.kg_seed_access("p1", "K", "read").unwrap();
        assert_eq!(n, 0, "INSERT OR IGNORE on existing row must be a no-op");
        let after_reseed = db.kg_get_access_row("p1", "K").unwrap().unwrap();
        assert_eq!(after_reseed.access_level, "write",
            "seed-path must NOT downgrade the user's explicit write to read");
        assert_eq!(
            after_reseed, after_user,
            "row must be byte-identical after no-op seed call",
        );
    }

    // ─── Phase 2 (Step B) — AccessLevel + resolver + V44-C guard ───────────

    #[test]
    fn access_level_as_str_wire_stable() {
        // The SQL column stores "read" / "write" / "none". Enum
        // variants must round-trip via `as_str()` / `from_str_strict()`
        // without altering the wire values.
        assert_eq!(AccessLevel::Read.as_str(), "read");
        assert_eq!(AccessLevel::Write.as_str(), "write");
        assert_eq!(AccessLevel::Denied.as_str(), "none");
        assert_eq!(format!("{}", AccessLevel::Read), "read");
        assert_eq!(format!("{}", AccessLevel::Write), "write");
        assert_eq!(format!("{}", AccessLevel::Denied), "none");

        assert_eq!(AccessLevel::from_str_strict("read").unwrap(), AccessLevel::Read);
        assert_eq!(AccessLevel::from_str_strict("write").unwrap(), AccessLevel::Write);
        assert_eq!(AccessLevel::from_str_strict("none").unwrap(), AccessLevel::Denied);
    }

    #[test]
    fn access_level_from_str_strict_rejects_unknown() {
        // Misuse-resistance: no silent default for typos, capitalization
        // mismatches, or legacy values. Each is an Err.
        for bad in &["READ", "Write", "denied", "rw", "", "yes"] {
            assert!(
                AccessLevel::from_str_strict(bad).is_err(),
                "expected Err for {:?}",
                bad,
            );
        }
    }

    #[test]
    fn resolve_default_access_level_write_on_primary() {
        // Item #6 contract: when (project_id, collection) IS the
        // project's primary binding, default access is Write.
        let db = Db::open_in_memory().unwrap();
        seed_proj(&db, "p1");
        let now: i64 = 1_700_000_000_000;
        {
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO project_kg_bindings
                        (project_id, role, collection_name, config_json, updated_at)
                     VALUES ('p1', 'primary', 'P1Primary_KG', '{}', ?1)",
                    params![now],
                )
                .unwrap();
        }
        let level = db.resolve_default_access_level("p1", "P1Primary_KG").unwrap();
        assert_eq!(level, AccessLevel::Write);
    }

    #[test]
    fn resolve_default_access_level_write_on_shared() {
        let db = Db::open_in_memory().unwrap();
        seed_proj(&db, "p1");
        let now: i64 = 1_700_000_000_000;
        {
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO project_kg_bindings
                        (project_id, role, collection_name, config_json, updated_at)
                     VALUES ('p1', 'shared', 'Org_KG', '{}', ?1)",
                    params![now],
                )
                .unwrap();
        }
        let level = db.resolve_default_access_level("p1", "Org_KG").unwrap();
        assert_eq!(level, AccessLevel::Write);
    }

    #[test]
    fn resolve_default_access_level_denied_when_no_binding() {
        // No binding row → no implicit grant. Default is Denied
        // ("none" on the wire). Cross-project access must be opt-in
        // by the user via the kg_set_collection_access_mode command.
        let db = Db::open_in_memory().unwrap();
        seed_proj(&db, "p1");
        // No binding row inserted for (p1, SomeOtherProjectsKG).
        let level = db
            .resolve_default_access_level("p1", "SomeOtherProjectsKG")
            .unwrap();
        assert_eq!(level, AccessLevel::Denied);
    }

    #[test]
    fn resolve_default_access_level_denied_on_archive_role() {
        // Future-proofing: archive bindings (and any other non-
        // primary/shared role) default to Denied. Plan's F-2a rule
        // says only own-primary + shared get auto-write.
        let db = Db::open_in_memory().unwrap();
        seed_proj(&db, "p1");
        let now: i64 = 1_700_000_000_000;
        {
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO project_kg_bindings
                        (project_id, role, collection_name, config_json, updated_at)
                     VALUES ('p1', 'archive', 'P1Archive_KG', '{}', ?1)",
                    params![now],
                )
                .unwrap();
        }
        let level = db.resolve_default_access_level("p1", "P1Archive_KG").unwrap();
        assert_eq!(level, AccessLevel::Denied);
    }

    #[test]
    fn kg_set_access_refuses_orchestrator_root_structural_demote() {
        // Item #5 — V44-C guard relocated into kg_set_access. Any
        // attempt to demote the orchestrator-root's primary structural
        // row from "write" must be refused at the DB layer, regardless
        // of caller. Defense in depth for any future caller that
        // bypasses the command-layer guard.
        let db = Db::open_in_memory().unwrap();
        // Seed an orchestrator-root project + its primary binding.
        let now: i64 = 1_700_000_000_000;
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
                 VALUES ('root', 'Root', '/tmp/root', 'orchestrator_root', ?1, ?1, 'root')",
                params![now],
            )
            .unwrap();
        guard
            .execute(
                "INSERT INTO project_kg_bindings
                    (project_id, role, collection_name, config_json, updated_at)
                 VALUES ('root', 'primary', 'RootStructural_KG', '{}', ?1)",
                params![now],
            )
            .unwrap();
        drop(guard);

        // Sanity: writing "write" succeeds.
        db.kg_set_access("root", "RootStructural_KG", "write").unwrap();

        // Attempts to demote to "read" or "none" must Err with a
        // structural-violation message.
        let err = db
            .kg_set_access("root", "RootStructural_KG", "read")
            .unwrap_err();
        assert!(
            err.contains("structural"),
            "expected structural-violation message, got: {}",
            err,
        );
        let err = db
            .kg_set_access("root", "RootStructural_KG", "none")
            .unwrap_err();
        assert!(err.contains("structural"));

        // Non-structural rows on the orchestrator-root project are
        // unaffected (e.g. setting access to OTHER collections).
        db.kg_set_access("root", "OtherCollection", "read").unwrap();
        db.kg_set_access("root", "OtherCollection", "none").unwrap();
    }

    /// v0.2.49 Step F MF1 (L2-MF1): the V44-C structural-row guard
    /// MUST also fire from `kg_seed_access`. Pre-Step-F the seed
    /// path had ZERO guard — a first-INSERT of orchestrator-root's
    /// structural row at 'read' would silently bypass the invariant.
    /// (`INSERT OR IGNORE` protects EXISTING rows but not the FIRST
    /// seed at a bad level.)
    #[test]
    fn kg_seed_access_refuses_orchestrator_root_structural_seed_below_write() {
        let db = Db::open_in_memory().unwrap();
        // Seed orchestrator-root project + its primary binding (the
        // structural-row identifier).
        let now: i64 = 1_700_000_000_000;
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
                 VALUES ('root', 'Root', '/tmp/root', 'orchestrator_root', ?1, ?1, 'root')",
                params![now],
            )
            .unwrap();
        guard
            .execute(
                "INSERT INTO project_kg_bindings
                    (project_id, role, collection_name, config_json, updated_at)
                 VALUES ('root', 'primary', 'RootStructural_KG', '{}', ?1)",
                params![now],
            )
            .unwrap();
        drop(guard);

        // Seeding write is allowed (matches the invariant).
        db.kg_seed_access("root", "RootStructural_KG", "write").unwrap();

        // Wipe the row (simulating a fresh first-seed) + re-attempt
        // with read or none. Both must Err with the seed-path guard's
        // message.
        {
            let g = db.lock();
            g.execute(
                "DELETE FROM kg_collection_access WHERE project_id = 'root' AND collection_name = 'RootStructural_KG'",
                [],
            ).unwrap();
        }
        let err = db
            .kg_seed_access("root", "RootStructural_KG", "read")
            .unwrap_err();
        assert!(
            err.contains("structural") && err.contains("kg_seed_access"),
            "expected seed-path structural-violation message, got: {}",
            err,
        );
        let err = db
            .kg_seed_access("root", "RootStructural_KG", "none")
            .unwrap_err();
        assert!(
            err.contains("structural") && err.contains("kg_seed_access"),
            "expected seed-path structural-violation message, got: {}",
            err,
        );

        // Non-structural rows on the orchestrator-root project are
        // unaffected by the seed-path guard (parity with kg_set_access).
        db.kg_seed_access("root", "OtherCollection", "read").unwrap();
        db.kg_seed_access("root", "OtherCollection", "none").unwrap();
    }

    #[test]
    fn kg_set_access_does_not_guard_non_root_projects() {
        // The V44-C guard fires ONLY for orchestrator-root host
        // projects. A regular base project's primary binding can be
        // demoted freely (user is in control of their own access).
        let db = Db::open_in_memory().unwrap();
        let now: i64 = 1_700_000_000_000;
        {
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
                     VALUES ('p1', 'P1', '/tmp/p1', 'base', ?1, ?1, 'p1')",
                    params![now],
                )
                .unwrap();
            guard
                .execute(
                    "INSERT INTO project_kg_bindings
                        (project_id, role, collection_name, config_json, updated_at)
                     VALUES ('p1', 'primary', 'P1Primary_KG', '{}', ?1)",
                    params![now],
                )
                .unwrap();
        }
        // All three levels succeed on a non-orchestrator-root project.
        db.kg_set_access("p1", "P1Primary_KG", "write").unwrap();
        db.kg_set_access("p1", "P1Primary_KG", "read").unwrap();
        db.kg_set_access("p1", "P1Primary_KG", "none").unwrap();
    }

    /// v0.2.49 Step F SF2 (L2-SF1) — symmetric pair test.
    ///
    /// The existing test `kg_set_access_does_not_guard_non_root_projects`
    /// (above) pins that the V44-C guard ignores non-orchestrator-root
    /// PRIMARY rows. This test pins the SYMMETRIC case: a NON-
    /// orchestrator-root project iterating in a peer-mode-setter loop
    /// that tries to demote the ORCHESTRATOR-ROOT'S primary structural
    /// row → guard must STILL refuse the demote, regardless of who's
    /// calling.
    ///
    /// Why this matters: the V44-C guard's predicate
    /// (`is_orchestrator_root_structural_row`) checks the TARGET row's
    /// project_id + role + collection_name — NOT the caller's
    /// project_id. A future refactor that misreads the guard as
    /// "owner-only" (i.e. "only the orchestrator-root project itself
    /// can be blocked from demoting its own row") would let peer
    /// projects bypass it. This test pins the correct semantic.
    #[test]
    fn kg_set_access_peer_loop_cannot_demote_orchestrator_root_structural() {
        let db = Db::open_in_memory().unwrap();
        let now: i64 = 1_700_000_000_000;

        // Setup: orchestrator-root project + its primary binding (the
        // structural row that must NEVER be demoted).
        {
            let guard = db.lock();
            guard
                .execute(
                    "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
                     VALUES ('root', 'Root', '/tmp/root', 'orchestrator_root', ?1, ?1, 'root')",
                    params![now],
                )
                .unwrap();
            guard
                .execute(
                    "INSERT INTO project_kg_bindings
                        (project_id, role, collection_name, config_json, updated_at)
                     VALUES ('root', 'primary', 'RootStructural_KG', '{}', ?1)",
                    params![now],
                )
                .unwrap();

            // Peer project — a regular base project (NOT orchestrator-root).
            guard
                .execute(
                    "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
                     VALUES ('peer', 'Peer', '/tmp/peer', 'base', ?1, ?1, 'peer')",
                    params![now],
                )
                .unwrap();
        }

        // Seed peer's read access on the orchestrator-root's structural
        // collection (the pre-state for the demote attempt below).
        db.kg_set_access("peer", "RootStructural_KG", "read").unwrap();

        // The guard's predicate checks the (project_id, collection_name)
        // pair against `is_orchestrator_root_structural_row`. The PEER's
        // access row is (project_id='peer', collection_name=
        // 'RootStructural_KG') — NOT the structural row (which is
        // (project_id='root', collection_name='RootStructural_KG')).
        // So writing 'none' from the peer's mode-setter loop targeting
        // peer's OWN row at that collection succeeds (it's NOT the
        // structural row). This is symmetric to the existing
        // `does_not_guard_non_root_projects` test but reverses the
        // confusion vector.
        db.kg_set_access("peer", "RootStructural_KG", "none").unwrap();

        // Now the critical assertion: a programmatic attempt to demote
        // the STRUCTURAL row directly (project_id='root',
        // collection_name='RootStructural_KG') MUST be refused,
        // REGARDLESS of which code path invokes kg_set_access. The
        // guard's predicate doesn't know who's calling — it knows
        // ONLY about the target row.
        let err = db.kg_set_access("root", "RootStructural_KG", "read").unwrap_err();
        assert!(
            err.contains("structural"),
            "Step F SF2: V44-C guard must reject demotion of \
             orchestrator-root's structural row regardless of caller. \
             A future refactor reading the guard as 'owner-only' would \
             let peer code paths bypass it. Got: {}",
            err
        );
        let err = db.kg_set_access("root", "RootStructural_KG", "none").unwrap_err();
        assert!(err.contains("structural"), "same for 'none' level: {}", err);
    }

    // ─── Step C / Phase 3 — F-1 reorder via tokio::sync::oneshot ──────

    /// v0.2.49 Phase 3 F-1 ship-blocker — boot-order assertion test.
    ///
    /// The launcher's `setup()` spawns adopt + reconcile as concurrent
    /// `tokio::async_runtime::spawn` tasks, with a `oneshot` channel
    /// between them so reconcile WAITS for adopt before its sweep.
    /// This test pins the pattern by mirroring the spawn shape on a
    /// shared `Arc<Mutex<Vec<&'static str>>>` order log: adopt pushes
    /// `"adopt"`, reconcile pushes `"reconcile"`. After both join, the
    /// log MUST read `["adopt", "reconcile"]`.
    ///
    /// Regression sentinel: if a future refactor accidentally removes
    /// the `await` on the oneshot in reconcile, the order becomes
    /// non-deterministic (Tokio may schedule reconcile first → log
    /// reads `["reconcile", "adopt"]` → test fails). The test catches
    /// the regression deterministically, not probabilistically.
    ///
    /// We deliberately delay the adopt task with a brief sleep so
    /// reconcile would have a chance to run first if the await were
    /// removed — exercises the worst-case scheduling.
    #[tokio::test]
    async fn boot_reconcile_runs_strictly_after_adopt() {
        use std::sync::{Arc, Mutex};
        use std::time::Duration;

        let order_log: Arc<Mutex<Vec<&'static str>>> =
            Arc::new(Mutex::new(Vec::new()));
        let (adopt_done_tx, adopt_done_rx) =
            tokio::sync::oneshot::channel::<()>();

        let adopt_log = order_log.clone();
        let adopt_task = tokio::spawn(async move {
            // Simulate adopt's wall-clock cost (HTTP probes to
            // Weaviate). If reconcile is scheduled in the meantime
            // WITHOUT the oneshot await, it would push "reconcile"
            // first → assertion fails.
            tokio::time::sleep(Duration::from_millis(20)).await;
            adopt_log.lock().unwrap().push("adopt");
            // Signal regardless of success — matches lib.rs Phase 3
            // contract (reconcile runs after adopt COMPLETES, not
            // after adopt SUCCEEDS).
            let _ = adopt_done_tx.send(());
        });

        let reconcile_log = order_log.clone();
        let reconcile_task = tokio::spawn(async move {
            // Gate: must await the oneshot before running. The
            // `.ok()` discard mirrors lib.rs: if the sender was
            // dropped (adopt task didn't spawn), we still proceed —
            // not stopping reconcile is safer than running it
            // out-of-order.
            let _ = adopt_done_rx.await;
            reconcile_log.lock().unwrap().push("reconcile");
        });

        // Spawn order is "reconcile first" textually, on purpose,
        // to verify the await actually blocks. In lib.rs the adopt
        // spawn happens before reconcile spawn, but the order log
        // here is independent of spawn order — it depends ONLY on
        // when each task pushes onto the log.

        adopt_task.await.unwrap();
        reconcile_task.await.unwrap();

        let log = order_log.lock().unwrap();
        assert_eq!(
            *log,
            vec!["adopt", "reconcile"],
            "Phase 3 F-1 ordering violated: reconcile must run AFTER \
             adopt completes. If this fails the oneshot await in \
             lib.rs's reconcile task may have been removed or the \
             send on completion broken.",
        );
    }

    /// Negative-case test: if the oneshot receiver was dropped
    /// instead of awaited (e.g. someone accidentally removed the
    /// `await` line in lib.rs), the reconcile task could push before
    /// adopt. This test verifies our ASSERTION is sensitive enough
    /// to catch that — runs the SAME shape but without awaiting.
    /// MUST FAIL (so we invert the assertion).
    #[tokio::test]
    async fn negative_case_no_await_loses_ordering_guarantee() {
        use std::sync::{Arc, Mutex};
        use std::time::Duration;

        let order_log: Arc<Mutex<Vec<&'static str>>> =
            Arc::new(Mutex::new(Vec::new()));
        let (adopt_done_tx, _adopt_done_rx) =
            tokio::sync::oneshot::channel::<()>();

        let adopt_log = order_log.clone();
        let adopt_task = tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(20)).await;
            adopt_log.lock().unwrap().push("adopt");
            let _ = adopt_done_tx.send(());
        });

        let reconcile_log = order_log.clone();
        let reconcile_task = tokio::spawn(async move {
            // BUG SIMULATION: no await on _adopt_done_rx. Reconcile
            // races against adopt.
            reconcile_log.lock().unwrap().push("reconcile");
        });

        adopt_task.await.unwrap();
        reconcile_task.await.unwrap();

        let log = order_log.lock().unwrap();
        // Expected to see reconcile run first (adopt sleeps 20ms;
        // reconcile starts immediately). If the test ever observes
        // ["adopt", "reconcile"] here, the negative-case sentinel is
        // broken — sleep timing changed; bump the duration.
        assert_eq!(
            *log,
            vec!["reconcile", "adopt"],
            "regression sentinel: without the await, reconcile must \
             race ahead of adopt (sleep timing in this test ensures \
             it). If THIS test fails with [adopt, reconcile], the \
             sleep duration is no longer enough to expose the race; \
             bump it.",
        );
    }

    #[test]
    fn is_orchestrator_root_structural_row_distinguishes_correctly() {
        // Helper-level test for the predicate. Covers the 3 axes:
        //   - host: orchestrator_root vs base
        //   - role: primary vs shared/archive
        //   - collection name match vs miss
        let db = Db::open_in_memory().unwrap();
        let now: i64 = 1_700_000_000_000;
        {
            let guard = db.lock();
            // Orchestrator root with a primary + shared binding.
            guard.execute(
                "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
                 VALUES ('root', 'Root', '/tmp/root', 'orchestrator_root', ?1, ?1, 'root')",
                params![now],
            ).unwrap();
            guard.execute(
                "INSERT INTO project_kg_bindings (project_id, role, collection_name, config_json, updated_at)
                 VALUES ('root', 'primary', 'Structural_KG', '{}', ?1),
                        ('root', 'shared',  'Shared_KG',     '{}', ?1)",
                params![now],
            ).unwrap();
            // Base project with a primary binding (NOT structural in V44-C sense).
            guard.execute(
                "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug)
                 VALUES ('p1', 'P1', '/tmp/p1', 'base', ?1, ?1, 'p1')",
                params![now],
            ).unwrap();
            guard.execute(
                "INSERT INTO project_kg_bindings (project_id, role, collection_name, config_json, updated_at)
                 VALUES ('p1', 'primary', 'P1Primary_KG', '{}', ?1)",
                params![now],
            ).unwrap();
        }

        // Orchestrator-root + primary collection name match → true.
        assert!(db.is_orchestrator_root_structural_row("root", "Structural_KG").unwrap());
        // Orchestrator-root + shared role → false (not the structural primary).
        assert!(!db.is_orchestrator_root_structural_row("root", "Shared_KG").unwrap());
        // Orchestrator-root + unknown collection → false.
        assert!(!db.is_orchestrator_root_structural_row("root", "Unknown_KG").unwrap());
        // Base project + primary → false (only orchestrator-root has structural rows).
        assert!(!db.is_orchestrator_root_structural_row("p1", "P1Primary_KG").unwrap());
        // Non-existent project → false.
        assert!(!db.is_orchestrator_root_structural_row("nope", "Whatever").unwrap());
    }

    #[test]
    fn is_user_configured_predicate_legacy_row_reads_false() {
        // Plan core invariant. A row whose `updated_at == created_at`
        // (including the legacy default of both being 0) reads as
        // NOT user-configured. The Phase 7 force-upgrade migration
        // is therefore safe to rewrite EVERY existing row regardless
        // of the predicate, because all legacy rows match the
        // not-configured semantic.
        let legacy = KgAccessRow {
            project_id: "p1".into(),
            collection_name: "K".into(),
            access_level: "read".into(),
            created_at: 0,
            updated_at: 0,
        };
        assert!(!legacy.is_user_configured());

        let seeded_today = KgAccessRow {
            created_at: 1_700_000_000_000,
            updated_at: 1_700_000_000_000,
            ..legacy.clone()
        };
        assert!(!seeded_today.is_user_configured());

        let user_touched = KgAccessRow {
            created_at: 1_700_000_000_000,
            updated_at: 1_700_000_000_001,
            ..legacy
        };
        assert!(user_touched.is_user_configured());
    }
}

// ─── Tests: populate_kg_collection_access_for_project (Phase 4 #10) ───────
//
// Centralized seed helper used by both the launcher-side `create_project_v2`
// path (via `populate_kg_collection_access` delegation) and the hub-side
// `vct project create` CLI path. These tests pin the contract independent
// of either caller.

#[cfg(test)]
mod populate_access_for_project_tests {
    use super::sanitize_kg_collection_local;
    use crate::db::Db;

    fn seed_project(db: &Db, project_id: &str) {
        let now = 1_700_000_000_000_i64;
        let folder = format!("/tmp/test-populate-access/{}", project_id);
        let guard = db.lock();
        guard
            .execute(
                "INSERT OR IGNORE INTO projects \
                 (id, name, folder_path, host, slug, created_at, updated_at) \
                 VALUES (?1, ?2, ?3, 'base', ?1, ?4, ?4)",
                rusqlite::params![project_id, project_id, folder, now],
            )
            .unwrap();
    }

    #[test]
    fn populate_writes_three_default_rows() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");

        let inserted = db
            .populate_kg_collection_access_for_project("p1", "Acme")
            .unwrap();
        // 3 default rows: own primary (write), own dev (write), shared (WRITE).
        // v0.2.49 Step F SB2 fix (L2-SB1): the shared row's default
        // flipped from "read" to "write" to align with
        // `resolve_default_access_level`'s F-2a rule (role='shared' →
        // Write) AND with Step D's force-upgrade migration target.
        // Pre-Step-F this asserted "read"; the drift between populate
        // and resolver was the SHIP-BLOCKER closure.
        assert_eq!(inserted, 3);

        let access = db.kg_list_access("p1").unwrap();
        let by_collection: std::collections::HashMap<&str, &str> = access
            .iter()
            .map(|(c, l)| (c.as_str(), l.as_str()))
            .collect();
        assert_eq!(by_collection.get("Acme_KnowledgeGraph"), Some(&"write"));
        assert_eq!(by_collection.get("Acme_Development"), Some(&"write"));
        assert_eq!(
            by_collection.get("VibeCodedOrchestrator_KnowledgeGraph"),
            Some(&"write"),
            "v0.2.49 Step F SB2: shared default is 'write' (was 'read' pre-fix; \
             drift vs resolver's F-2a output for role='shared' bindings)"
        );
    }

    #[test]
    fn populate_is_idempotent_preserves_user_configured_levels() {
        // After the first populate seeds the defaults, the user
        // explicitly downgrades the shared collection to "none". A
        // second populate call MUST NOT clobber the user-configured row.
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");

        let first = db
            .populate_kg_collection_access_for_project("p1", "Acme")
            .unwrap();
        assert_eq!(first, 3);

        // User downgrades shared.
        db.kg_set_access("p1", "VibeCodedOrchestrator_KnowledgeGraph", "none")
            .unwrap();

        let second = db
            .populate_kg_collection_access_for_project("p1", "Acme")
            .unwrap();
        // Zero rows inserted: every default already exists (INSERT OR
        // IGNORE no-op). User's downgrade survives.
        assert_eq!(second, 0);

        let access = db.kg_list_access("p1").unwrap();
        let by_collection: std::collections::HashMap<&str, &str> = access
            .iter()
            .map(|(c, l)| (c.as_str(), l.as_str()))
            .collect();
        assert_eq!(
            by_collection.get("VibeCodedOrchestrator_KnowledgeGraph"),
            Some(&"none"),
            "user-set 'none' must NOT be reset to default 'read'"
        );
    }

    #[test]
    fn populate_handles_sanitization_of_project_name() {
        // Project names with spaces / punctuation must be sanitized to
        // a Weaviate-safe collection prefix matching the launcher-side
        // `sanitize_kg_collection`.
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");

        db.populate_kg_collection_access_for_project("p1", "my project name")
            .unwrap();

        let access = db.kg_list_access("p1").unwrap();
        let collections: std::collections::HashSet<&str> =
            access.iter().map(|(c, _)| c.as_str()).collect();
        assert!(collections.contains("MyProjectName_KnowledgeGraph"));
        assert!(collections.contains("MyProjectName_Development"));
    }

    #[test]
    fn populate_writes_per_project_scoped_rows() {
        // Two distinct projects get distinct sets of rows; calling
        // populate for p1 must not write rows under p2's project_id.
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");
        seed_project(&db, "p2");

        db.populate_kg_collection_access_for_project("p1", "Acme")
            .unwrap();
        assert_eq!(db.kg_list_access("p1").unwrap().len(), 3);
        assert_eq!(
            db.kg_list_access("p2").unwrap().len(),
            0,
            "p1's populate must not touch p2"
        );

        db.populate_kg_collection_access_for_project("p2", "Beta")
            .unwrap();
        assert_eq!(db.kg_list_access("p2").unwrap().len(), 3);
    }

    #[test]
    fn populate_respects_orchestrator_root_kg_collection_override() {
        // White-label scenario: install.py persists a branded shared
        // collection name. populate must use the persisted name as the
        // shared row's collection, not the bundled default.
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");
        db.set_orchestrator_root_kg_collection("WhiteLabel_KnowledgeGraph")
            .unwrap();

        db.populate_kg_collection_access_for_project("p1", "Acme")
            .unwrap();

        let access = db.kg_list_access("p1").unwrap();
        let by_collection: std::collections::HashMap<&str, &str> = access
            .iter()
            .map(|(c, l)| (c.as_str(), l.as_str()))
            .collect();
        // Shared row points at the branded name + at write level
        // (v0.2.49 Step F SB2 alignment with resolver's F-2a output).
        assert_eq!(
            by_collection.get("WhiteLabel_KnowledgeGraph"),
            Some(&"write")
        );
        // …NOT the bundled default.
        assert_eq!(
            by_collection.get("VibeCodedOrchestrator_KnowledgeGraph"),
            None
        );
    }

    /// Item #10 integration test target: hub-side `vct project create`
    /// populates access. Asserts the core-helper contract on the same
    /// surface a hub caller would observe (insert_project → populate).
    /// The actual hub HTTP endpoint test lives in
    /// `vct-hub::cli_api::cli_project_create_tests` (see the
    /// `vct_project_create_via_cli_populates_access` test there).
    #[test]
    fn vct_project_create_via_cli_populates_access() {
        let db = Db::open_in_memory().unwrap();
        // Mirror the hub's create_project flow exactly: insert_project
        // + populate. This is the call pair the cli_api endpoint
        // performs (with audit between them).
        let pid = "test-cli-create";
        db.insert_project(
            pid,
            "CliCreated",
            "/tmp/cli-created",
            crate::db::models::ProjectHost::Base,
            "cli-created",
        )
        .unwrap();
        let inserted = db
            .populate_kg_collection_access_for_project(pid, "CliCreated")
            .unwrap();
        assert_eq!(inserted, 3);

        let access = db.kg_list_access(pid).unwrap();
        let by_collection: std::collections::HashMap<&str, &str> = access
            .iter()
            .map(|(c, l)| (c.as_str(), l.as_str()))
            .collect();
        assert_eq!(by_collection.get("CliCreated_KnowledgeGraph"), Some(&"write"));
        assert_eq!(by_collection.get("CliCreated_Development"), Some(&"write"));
        // v0.2.49 Step F SB2 (L2-SB1): shared default is now "write"
        // to align with resolver's F-2a output + Step D's force-upgrade
        // migration target.
        assert_eq!(
            by_collection.get("VibeCodedOrchestrator_KnowledgeGraph"),
            Some(&"write")
        );
    }

    #[test]
    fn sanitize_local_matches_launcher_side_canonical_cases() {
        // Pin sanitize_kg_collection_local's output for the cases the
        // launcher-side test covers, ensuring we don't drift from the
        // launcher's `commands::projects_v2::sanitize_kg_collection`.
        assert_eq!(sanitize_kg_collection_local("Acme"), "Acme");
        assert_eq!(sanitize_kg_collection_local("my project"), "MyProject");
        assert_eq!(sanitize_kg_collection_local("foo-bar_baz"), "FooBarBaz");
        // X-1 / v0.2.76: out-of-domain input unifies to "vct" (matches the
        // launcher-side `sanitize_kg_collection` + the Python SSOT).
        assert_eq!(sanitize_kg_collection_local(""), "vct");
        assert_eq!(sanitize_kg_collection_local("123abc"), "vct");
    }

    /// v0.2.49 Step F SB2 drift sentinel (L2-SB1 follow-up).
    ///
    /// Pins that the levels written by
    /// `populate_kg_collection_access_for_project` exactly match
    /// what `resolve_default_access_level` returns ONCE the project's
    /// `project_kg_bindings` rows exist for those collections. The
    /// populate runs BEFORE bindings are written (see hub
    /// `create_project` flow at `vct-hub/src/cli_api.rs:148-172`),
    /// so it can't CALL the resolver directly — but the values it
    /// writes must mirror the resolver's downstream output, else a
    /// future tweak to either side silently diverges.
    ///
    /// Workflow this test pins:
    ///   1. seed a project (no bindings yet)
    ///   2. call populate → writes Write/Write/Write defaults
    ///   3. NOW write the bindings (post-populate, mirroring the
    ///      real-world order)
    ///   4. ask the resolver for each collection's default level
    ///   5. assert resolver's answer matches what populate wrote
    ///
    /// If this test fails, EITHER the populate's literal defaults
    /// drifted from the resolver's F-2a rule OR the resolver's
    /// decision tree changed without updating the populate. Both
    /// require synchronized fixes.
    #[test]
    fn populate_output_matches_resolver_output_post_binding_write() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");

        // Step 1+2: populate runs before bindings exist.
        db.populate_kg_collection_access_for_project("p1", "Acme")
            .unwrap();

        // Step 3: NOW write the bindings (the real-world post-populate
        // step). The launcher GUI's add-project flow does this via the
        // populate_project_state_from_filesystem path; the hub CLI does
        // it via the binding-write Tauri command. Both surfaces land
        // primary + shared bindings at the SAME collection_name the
        // populate wrote rows for.
        let shared_collection = db.get_orchestrator_root_kg_collection().unwrap();
        db.set_project_kg_binding(
            "p1",
            "primary",
            "Acme_KnowledgeGraph",
            None,
            None,
            None,
            None,
            &serde_json::Value::Null,
        )
        .unwrap();
        db.set_project_kg_binding(
            "p1",
            "shared",
            &shared_collection,
            None,
            None,
            None,
            None,
            &serde_json::Value::Null,
        )
        .unwrap();

        // Step 4+5: resolver's output must match populate's output for
        // EVERY collection populate wrote.
        let populate_access = db.kg_list_access("p1").unwrap();
        for (collection, populated_level) in &populate_access {
            let resolver_level = db
                .resolve_default_access_level("p1", collection)
                .unwrap();
            // Special case: Acme_Development was populated as Write,
            // but the resolver sees no `role='primary'` binding at
            // that EXACT collection_name (the binding's at
            // Acme_KnowledgeGraph). Per the F-2a rule the resolver
            // returns Denied for dev — but populate writes Write per
            // schema convention (dev is the `_Development` variant of
            // the primary binding's collection). The dev collection
            // is the documented exception to the literal-mirror; skip
            // it in this sentinel so the sentinel's intent (catching
            // primary + shared drift) stays load-bearing without
            // false-positive failures on the documented dev exception.
            if collection == "Acme_Development" {
                continue;
            }
            assert_eq!(
                populated_level,
                resolver_level.as_str(),
                "drift: populate wrote {} for {}, but resolver returns {} \
                 post-binding-write. Either populate's literal defaults \
                 drifted from the resolver's F-2a rule OR the resolver's \
                 decision tree changed. Re-synchronize both sides.",
                populated_level,
                collection,
                resolver_level.as_str()
            );
        }
    }

    /// v0.2.49 Step F MF3-v2 refactor (L1-F2): populate back-fills
    /// access rows for every already-installed global-scope module's
    /// declared `kg_collections` (now sourced from the launcher DB
    /// column `module_installs.kg_collections`, migration 032).
    ///
    /// Inverse of item #13's "install seeds all projects" — this is
    /// "new project gets rows for already-installed globals."
    ///
    /// v1 → v2 refactor (user directive 2026-06-08): instead of reading
    /// `vct-module.json` from disk at populate time, the module's
    /// declared collections are denormalized into the launcher DB at
    /// install time via `Db::set_module_kg_collections`. The launcher
    /// DB is the single source of truth per the orchestrator's
    /// single-writer discipline. No filesystem I/O on the hot path.
    ///
    /// Flow:
    ///   1. Insert a global module install (sets `kg_collections=NULL`).
    ///   2. Set kg_collections via `db.set_module_kg_collections(install_id, Some(&["RLMeta_KG", "RLMeta_Telemetry"]))`.
    ///   3. Insert a new project.
    ///   4. Call populate → 3 own (primary/dev/shared) + 2 module = 5.
    #[test]
    fn populate_backfills_global_module_kg_collections() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");

        // Insert a global module install. `kg_collections` defaults to
        // empty Vec (column NULL → decoded as empty per row_to_install_row).
        let install_row = db
            .insert_global_module_install(
                "install-1",
                "vct-rl-meta",
                "0.1.0",
                "/tmp/does-not-matter-for-v2",
            )
            .unwrap();
        // Now persist kg_collections via the dedicated setter.
        db.set_module_kg_collections(
            &install_row.id,
            Some(&[
                "RLMeta_KG".to_string(),
                "RLMeta_Telemetry".to_string(),
            ]),
        )
        .unwrap();

        // Populate for p1.
        let inserted = db
            .populate_kg_collection_access_for_project("p1", "Acme")
            .unwrap();
        // 3 own rows + 2 module rows = 5.
        assert_eq!(
            inserted, 5,
            "expected 3 own (primary/dev/shared) + 2 from rl-meta global module"
        );

        let access = db.kg_list_access("p1").unwrap();
        let by_collection: std::collections::HashMap<&str, &str> = access
            .iter()
            .map(|(c, l)| (c.as_str(), l.as_str()))
            .collect();
        assert_eq!(by_collection.get("Acme_KnowledgeGraph"), Some(&"write"));
        assert_eq!(by_collection.get("Acme_Development"), Some(&"write"));
        assert_eq!(
            by_collection.get("VibeCodedOrchestrator_KnowledgeGraph"),
            Some(&"write")
        );
        // The 2 module collections were back-filled at "write".
        assert_eq!(by_collection.get("RLMeta_KG"), Some(&"write"));
        assert_eq!(by_collection.get("RLMeta_Telemetry"), Some(&"write"));
    }

    /// Pre-v0.2.49 global module installs have `kg_collections = NULL`
    /// in the DB column (migration 032 default). The row decoder
    /// resolves NULL to an empty `Vec<String>`, and the populate loop's
    /// `if install.kg_collections.is_empty() { continue }` guard skips
    /// them. Asserts no module rows seeded for an install whose
    /// kg_collections is the default (never set via the setter).
    #[test]
    fn populate_skips_global_module_with_null_kg_collections() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");

        // Insert a global install but DON'T call set_module_kg_collections.
        // Column stays NULL → decoded as empty Vec → populate loop skips.
        db.insert_global_module_install(
            "install-no-kg",
            "vct-rl-reranker",
            "0.2.10",
            "/tmp/does-not-matter",
        )
        .unwrap();

        let inserted = db
            .populate_kg_collection_access_for_project("p1", "Acme")
            .unwrap();
        assert_eq!(inserted, 3); // only own-project rows
    }

    /// `set_module_kg_collections(install_id, Some(&[]))` sets the
    /// column to JSON `"[]"`. The populate loop's empty-Vec guard
    /// treats this identically to NULL — no module rows seeded.
    /// Pins that the empty-array case is semantically equivalent to
    /// "module declares no collections" (NOT a "wipe" signal).
    #[test]
    fn populate_skips_global_module_with_empty_kg_collections_array() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");

        let install_row = db
            .insert_global_module_install(
                "install-empty",
                "vct-rl-reranker",
                "0.2.10",
                "/tmp/does-not-matter",
            )
            .unwrap();
        // Explicitly set to empty array.
        db.set_module_kg_collections(&install_row.id, Some(&[]))
            .unwrap();

        let inserted = db
            .populate_kg_collection_access_for_project("p1", "Acme")
            .unwrap();
        assert_eq!(inserted, 3); // only own-project rows
    }

    /// User has already explicitly denied access to one of the
    /// module's declared kg_collections (pre-existing access row at
    /// 'none'). A re-run of populate (e.g. on next project create
    /// path or via some re-onboarding flow) MUST NOT clobber the
    /// user's choice. The `kg_seed_access` INSERT OR IGNORE semantic
    /// is the load-bearing primitive here.
    #[test]
    fn populate_preserves_user_configured_module_collection() {
        let db = Db::open_in_memory().unwrap();
        seed_project(&db, "p1");

        let install_row = db
            .insert_global_module_install(
                "install-1",
                "vct-rl-meta",
                "0.1.0",
                "/tmp/does-not-matter",
            )
            .unwrap();
        db.set_module_kg_collections(
            &install_row.id,
            Some(&["RLMeta_KG".to_string()]),
        )
        .unwrap();

        // First populate seeds RLMeta_KG at "write".
        db.populate_kg_collection_access_for_project("p1", "Acme")
            .unwrap();
        assert_eq!(
            db.kg_get_access("p1", "RLMeta_KG").unwrap(),
            Some("write".into())
        );

        // User downgrades to "none".
        db.kg_set_access("p1", "RLMeta_KG", "none").unwrap();

        // Re-run populate. INSERT OR IGNORE preserves the user's row.
        let inserted = db
            .populate_kg_collection_access_for_project("p1", "Acme")
            .unwrap();
        assert_eq!(inserted, 0, "all rows existed; no re-seed");
        assert_eq!(
            db.kg_get_access("p1", "RLMeta_KG").unwrap(),
            Some("none".into())
        );
    }

    /// Cross-cutting test: `set_module_kg_collections` followed by
    /// `list_global_module_installs` correctly round-trips the
    /// collections list through the JSON column. Pins the row-decoder
    /// + setter contract directly (not through populate).
    #[test]
    fn set_module_kg_collections_roundtrips_through_list() {
        let db = Db::open_in_memory().unwrap();
        let install_row = db
            .insert_global_module_install(
                "install-roundtrip",
                "vct-test",
                "0.1.0",
                "/tmp/test",
            )
            .unwrap();
        // Fresh row: kg_collections is empty Vec (column NULL).
        assert!(install_row.kg_collections.is_empty());

        // Set it.
        let collections = vec!["Foo_KG".to_string(), "Bar_KG".to_string()];
        db.set_module_kg_collections(&install_row.id, Some(&collections))
            .unwrap();

        // Re-read via list_global_module_installs.
        let listed = db.list_global_module_installs().unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].kg_collections, collections);

        // Setter is idempotent: re-applying same collections is a no-op
        // semantically (UPDATE writes same JSON).
        db.set_module_kg_collections(&install_row.id, Some(&collections))
            .unwrap();
        let listed2 = db.list_global_module_installs().unwrap();
        assert_eq!(listed2[0].kg_collections, collections);
    }
}
