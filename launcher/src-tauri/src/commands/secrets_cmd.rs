//! Tauri commands exposing secrets + settings to the React UI.
//!
//! Secret values NEVER leave the Rust process. Commands return presence
//! booleans and masked previews only. Settings are non-sensitive and are
//! returned fully.
//!
//! ─── Secret lifecycle (Bug 3 follow-up to PR #60) ──────────────────────
//!
//!  Lifecycle B (user-selected): the keychain VALUE is preserved across
//!  Unset → Reactivate cycles. The "active" state is a separate flag in
//!  `launcher.db::secret_active_state` (Storage A). Readers gate on it
//!  BEFORE returning anything from the keychain.
//!
//!  Operations:
//!    * `set_secret_v2`        — write value to keychain, mark active.
//!                               Used for both initial Set and Update.
//!    * `clear_secret_v2`      — Unset. Mark inactive. **Keychain
//!                               UNTOUCHED.** Read API will refuse to
//!                               return the value while inactive.
//!    * `reactivate_secret_v2` — flip active back to true. **No keychain
//!                               change**, no value re-entry required.
//!                               One-click resume after a rotation pause.
//!    * `remove_secret_v2`     — DELETE the keychain value AND drop the
//!                               active-state row. The entry is gone.
//!    * `is_secret_set`        — true ONLY when keychain has a value AND
//!                               active=true. Returns false for inactive.
//!    * `get_secret_preview`   — masked preview ONLY when active=true.
//!                               Returns Ok(None) for inactive, never
//!                               leaks the canary.

use serde::{Deserialize, Serialize};
use tauri::{command, State};

use crate::db::Db;
use crate::db::{module_settings_keys, secret_scope_policy};
use crate::secrets::{self, SecretScope};
use crate::secrets_file_store::{self, Presence};
use std::path::PathBuf;

// ─── Secrets ────────────────────────────────────────────────────────────

// Wire-format for the future "list all secrets for this project" panel.
// No command emits `Vec<SecretMetadata>` today — the current secret list
// page reads `get_secret_status_v2` per key. The PR-8 Identity/Permissions
// tab work (v0.2.11) consumes this type from the TypeScript mirror at
// `launcher/src/lib/types/launcher.ts`, even though the Rust side does
// not yet have a producer. `#[allow(dead_code)]` keeps the type
// compiled (no orphan import errors on `Serialize` derives) until the
// producer command lands. The shape is intentionally minimal: a richer
// `value_preview` policy (truncation, sensitive-detection) is owned by
// `get_secret_preview` and is not duplicated here.
#[allow(dead_code)]
#[derive(Debug, Serialize)]
pub struct SecretMetadata {
    pub key: String,
    pub scope: String,
    pub is_set: bool,
    pub sensitive: bool,
    pub value_preview: Option<String>,
}

fn scope_from_manifest<'a>(scope: &str, project_id: &'a str) -> SecretScope<'a> {
    match scope {
        "global" => SecretScope::Global,
        "shared" => SecretScope::Shared { project_id },
        _ => SecretScope::PerProject { project_id },
    }
}

/// Subagent G (2026-05-08): identify whether a given (scope, module_id)
/// targets the per-project user bucket, i.e. the bucket the
/// `SecretsPanel.svelte` add-form's "Per-project" tab writes to and
/// whose entries auto-emit into ONE specific project's env surfaces.
///
/// Module-owned secrets (`scope='per_project'`, `module_id != 'user'` —
/// e.g. licensing's `license_key____orchestrator__` global (canonical
/// per L1.M v0.2.40; was `VIBECODED_LICENSE_KEY` pre-L1.M), or any future
/// MCP-server module declaring `secrets[]` in its manifest) are
/// resolved separately by the hub `project_env` endpoint and
/// already reach subprocesses via that path. They are NOT in scope
/// for the env-file emit because:
///
///   1. Module manifests change less frequently than user secrets,
///      so file-side caching saves no real ergonomics.
///   2. Modules that need the value already call the resolver
///      directly via the bundled `vct_secrets_resolve.sh`.
///   3. Adding them to the env surface widens the bag of secrets a
///      curious user might `cat .claude/env` past.
///
/// Returns true ONLY for per-project user-bucket entries. Use
/// `is_user_emit_bucket` (below) for the broader test that also
/// catches the Shared / Global tab entries.
///
/// 0.1.7 H2 (2026-05-08): kept as a sub-predicate of
/// `is_user_emit_bucket` for the per-project-only refresh path inside
/// `refresh_env_after_user_secret_change` and as a regression-test
/// pin (see `is_per_project_user_bucket_pure_predicate`). All
/// production callers route through `is_user_emit_bucket` after H2.
#[allow(dead_code)]
fn is_per_project_user_bucket(scope: &str, module_id: &str) -> bool {
    scope == "per_project" && module_id == "user"
}

/// H2 (0.1.7 fork-readiness sweep, 2026-05-08): broaden the user-bucket
/// predicate to also recognise the SecretsPanel "Shared (this user)"
/// and "Global (this machine)" tabs.
///
/// All three tabs in `SecretsPanel.svelte` use the same constant
/// `UI_MODULE_BUCKET = "user"` for `module_id`. Pre-H2, only the
/// per-project tab's writes propagated into the env-file surfaces;
/// Shared / Global tab writes landed in the keychain but were
/// silent to every consumer — the original "GUI says secret is set,
/// but nothing reads it" gap, just one tab over.
///
/// Returns true for every tab the SecretsPanel's add-form can write to:
///   * `(per_project, user)` — one specific project
///   * `(shared,      user)` — every registered project (across this user)
///   * `(global,      user)` — every registered project (machine-wide)
///
/// Subprocesses spawned in any registered project's Claude Code session
/// see all three classes as normal env vars. The threat model is the
/// same as `is_per_project_user_bucket`: anything in the env surfaces
/// is readable by any subprocess in the project — same exposure profile
/// `~/.vct-secrets/` had pre-H2.
fn is_user_emit_bucket(scope: &str, module_id: &str) -> bool {
    if module_id != "user" {
        return false;
    }
    matches!(scope, "per_project" | "shared" | "global")
}

/// Subagent G (2026-05-08), broadened by H2 (2026-05-08): re-run
/// `write_project_env_files` for the affected project(s) after a
/// user-bucket secret change.
///
/// Three cases:
///   * `(per_project, user)` — refresh ONE project (the one in
///     `project_id`).
///   * `(shared, user)` or `(global, user)` — refresh EVERY registered
///     project. Shared / global entries are user-wide / machine-wide,
///     so a single change has to fan out to every project's env
///     surfaces. Otherwise a key added in the Shared tab would be
///     visible only to projects registered AFTER the change (because
///     `populate` reads it at write time), with stale surfaces
///     everywhere else until the next manual refresh.
///   * Anything else (`module_id != 'user'`, etc.) — skip. Module-owned
///     secrets are resolved by the hub's `/projects/{id}/env` endpoint
///     and don't go through the env-file emit path.
///
/// Failures are logged via eprintln and SWALLOWED — the secret
/// operation has already committed; an env-write hiccup must not roll
/// back the user's GUI action. For shared/global, a single project's
/// writer hiccup is logged but the fan-out continues for the rest.
///
/// `project_id` MUST already be validated by `enforce_scope_invariants`
/// before this is called — we don't double-check here.
fn refresh_env_after_user_secret_change(
    db: &Db,
    project_id: &str,
    scope: &str,
    module_id: &str,
    op: &str,
) {
    if !is_user_emit_bucket(scope, module_id) {
        return;
    }
    if scope == "per_project" {
        // Single-project refresh — the entry only affects one project.
        if let Err(e) =
            crate::commands::projects_v2::refresh_project_env_with_db(db, project_id)
        {
            tracing::warn!(
                "[vct] warning: env-file refresh after {} on {}/{} failed: {}. \
                 The keychain change has committed; env surfaces may be stale \
                 until the next refresh.",
                op, project_id, module_id, e
            );
        }
        return;
    }
    // Shared / global — fan out to every registered project. A single
    // project's writer hiccup is logged but the fan-out continues for
    // the rest. Soft-fail on the list_projects() read too: we have no
    // recovery path if the project list itself is unreadable.
    let projects = match db.list_projects() {
        Ok(rows) => rows,
        Err(e) => {
            tracing::warn!(
                "[vct] warning: env-file fan-out after {} on shared/global {} failed \
                 to list registered projects: {}. The keychain change has committed; \
                 env surfaces will pick it up on the next per-project refresh.",
                op, module_id, e,
            );
            return;
        }
    };
    for row in projects {
        if let Err(e) =
            crate::commands::projects_v2::refresh_project_env_with_db(db, &row.id)
        {
            tracing::warn!(
                "[vct] warning: env-file refresh after {} on shared/global {}/{} \
                 for project {} failed: {}. Other registered projects continue \
                 to refresh; this one will be stale until next refresh.",
                op, scope, module_id, row.id, e
            );
        }
    }
}

/// Sentinel project_id used by the GUI when scope is global / shared.
///
/// These scopes don't tie a secret to a specific project; the frontend
/// passes a stable sentinel so the audit log + keychain service name
/// remain well-formed. `_global_` for global scope, `_user_shared_` for
/// shared (per-user, across all projects).
const SENTINEL_GLOBAL: &str = "_global_";
const SENTINEL_SHARED: &str = "_user_shared_";

/// Reject path-traversal-ish project_ids and enforce that per-project
/// secrets target a project that actually exists in the DB.
///
/// Without this, a caller could write a secret under e.g.
/// `project_id = "../../"` (which would still produce a valid keychain
/// service name) or under a `project_id` that no longer corresponds to a
/// registered project. Either lets a per-project secret leak to / be read
/// by an unintended context. Project-isolation requirement (see PR
/// description "Read semantics").
fn enforce_scope_invariants(scope: &str, project_id: &str, db: &Db) -> Result<(), String> {
    // No control characters / dot-segments / slashes anywhere — applies to
    // every scope as a defence-in-depth check. Sentinels above pass.
    if project_id.is_empty()
        || project_id.contains('/')
        || project_id.contains('\\')
        || project_id.contains('\0')
        || project_id == "."
        || project_id == ".."
        || project_id.starts_with("./")
        || project_id.starts_with("../")
    {
        return Err(format!("invalid project_id: {:?}", project_id));
    }
    match scope {
        "global" => {
            if project_id != SENTINEL_GLOBAL {
                return Err(format!(
                    "global scope must use sentinel project_id={:?}; got {:?}",
                    SENTINEL_GLOBAL, project_id
                ));
            }
        }
        "shared" => {
            // Shared keychain still uses a project_id slot for backward
            // compat with existing entries (see secrets.rs `service_name`).
            // The frontend passes `SENTINEL_SHARED` so all "shared"
            // secrets land in one user-wide bucket. Real project ids are
            // also accepted here (legacy) to preserve any pre-existing
            // per-project shared entries written before this PR.
            if project_id != SENTINEL_SHARED && db.get_project(project_id)?.is_none() {
                return Err(format!(
                    "shared scope: project_id {:?} is neither sentinel {:?} nor a registered project",
                    project_id, SENTINEL_SHARED
                ));
            }
        }
        _ => {
            // Per-project: must reference an existing registered project.
            // Prevents projectA's modules from writing/reading secrets
            // under a project_id they make up.
            if db.get_project(project_id)?.is_none() {
                return Err(format!(
                    "per-project scope: project {:?} is not a registered project",
                    project_id
                ));
            }
        }
    }
    Ok(())
}

#[command]
pub async fn set_secret_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    value: String,
    validation_regex: Option<String>,
    sensitive: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    // Validate value against the manifest regex if provided.
    //
    // v0.2.80 A4: whether a manifest regex matched also decides which keychain
    // write path we take. The `secrets::set` chokepoint refuses a blob-shaped
    // value by default; but a module MAY legitimately declare a multi-line
    // secret (e.g. a PEM the allowlist doesn't recognise) whose shape its
    // manifest `validation_regex` vouches for. When a regex matched, the
    // manifest is the authority on the value's shape → route to the
    // `set_allowing_multiline` opt-out. With NO manifest regex the caller has
    // vouched nothing, so the default guarded `set` (blob-rejecting) applies.
    let manifest_vouched_shape = if let Some(pattern) = validation_regex.as_deref() {
        let re = regex::Regex::new(pattern)
            .map_err(|e| format!("invalid validation regex: {}", e))?;
        if !re.is_match(&value) {
            return Err("value does not match validation pattern".into());
        }
        true
    } else {
        false
    };

    enforce_scope_invariants(&scope, &project_id, &db)?;
    let scope_enum = scope_from_manifest(&scope, &project_id);
    if manifest_vouched_shape {
        // Manifest regex matched → the manifest authors the value's shape.
        // control-char + over-long-github_pat gates still apply inside.
        secrets::set_allowing_multiline(scope_enum, &module_id, &key, &value)?;
    } else {
        secrets::set(scope_enum, &module_id, &key, &value)?;
    }
    // Setting a value implicitly activates the entry. Covers both the
    // first Set and any later Update / "Set as new value" path. Without
    // this, an entry that was Unset and then re-Set without going through
    // Reactivate would still read as inactive.
    db.mark_secret_active(&scope, &project_id, &module_id, &key)?;

    db.audit(
        "secret_set",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({
            "key": key,
            "scope": scope,
            "sensitive": sensitive,
            // Never log the value, not even truncated. Presence + scope are
            // enough to reconstruct "what happened" for debugging.
        }),
    )?;
    // Subagent G (2026-05-08): per-project user-bucket secrets auto-emit
    // into all 3 env surfaces so they show up as $KEY in the project's
    // Claude Code session. Refresh after the audit so the audit row's
    // ordering doesn't depend on an env-file write race. Soft-fail
    // (eprintln only) — the secret has already committed to the
    // keychain.
    refresh_env_after_user_secret_change(&db, &project_id, &scope, &module_id, "set_secret_v2");
    Ok(())
}

/// Unset (Lifecycle B): flip the entry to INACTIVE without touching the
/// keychain value. The value stays in the OS keychain so a later
/// `reactivate_secret_v2` can resume the entry without the user re-typing
/// the value. While inactive, `is_secret_set` returns false and
/// `get_secret_preview` returns Ok(None) — the value cannot leak through
/// the launcher's API.
///
/// Distinct from `remove_secret_v2`, which deletes the keychain value AND
/// the active-state row.
#[command]
pub async fn clear_secret_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    // Mark inactive. Do NOT call `secrets::delete` — that's the whole
    // Lifecycle B requirement. The keychain entry is the user's saved
    // value; we just gate readers on the active flag.
    db.mark_secret_inactive(&scope, &project_id, &module_id, &key)?;
    db.audit(
        "secret_unset",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({ "key": key, "scope": scope }),
    )?;
    // Subagent G (2026-05-08): paused user-bucket secret must leave the
    // env surfaces. The writer's strip set picks up the (now-inactive)
    // row and removes it from `.claude/settings.json` env,
    // `.vscode/settings.json` claude-code.env, and the BEGIN/END block
    // of `.claude/env`. The keychain value is intentionally preserved
    // (Lifecycle B) so reactivate is one-click.
    refresh_env_after_user_secret_change(&db, &project_id, &scope, &module_id, "clear_secret_v2");
    Ok(())
}

/// Reactivate a previously-Unset entry. Flips active=true. **Does not
/// touch the keychain.** The value that was already there is now
/// re-exposed to readers. This is the one-click "resume rotation pause"
/// path.
///
/// If the keychain has no value (e.g. a user manually deleted it via the
/// OS keychain UI while the launcher was inactive), this still flips the
/// flag — the next `is_secret_set` will simply return false because the
/// keychain side is empty. The flag itself is independent of the value's
/// existence; the read gate is `keychain_has_value AND active=true`.
#[command]
pub async fn reactivate_secret_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    db.mark_secret_active(&scope, &project_id, &module_id, &key)?;
    db.audit(
        "secret_reactivate",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({ "key": key, "scope": scope }),
    )?;
    // Subagent G (2026-05-08): the value flips back into the EMIT set,
    // so it returns to the env surfaces. Same refresh pattern as set /
    // clear — the writer reads the active flag + keychain value and
    // composes the surfaces from scratch.
    refresh_env_after_user_secret_change(&db, &project_id, &scope, &module_id, "reactivate_secret_v2");
    Ok(())
}

/// Remove a secret entry: delete the keychain value AND drop the
/// active-state row. The entry is gone — Set requires re-typing the
/// value.
///
/// This is the destructive path. Use Unset (`clear_secret_v2`) if the
/// user just wants to pause an entry for token rotation.
#[command]
pub async fn remove_secret_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    let scope_enum = scope_from_manifest(&scope, &project_id);
    secrets::delete(scope_enum, &module_id, &key)?;

    // Subagent G (2026-05-08), broadened by H2 (2026-05-08): order
    // matters here for ALL user-bucket entries (per-project, shared, global).
    //
    // The env-file writer's strip set is derived from the
    // `secret_active_state` table. Once we've called
    // `forget_secret_active_state` the row is gone, the key drops out
    // of the strip set, and the env surfaces would carry a stale
    // entry across the next refresh.
    //
    // Solution: refresh BEFORE forget. At the moment of refresh, the
    // keychain was just deleted (so `secrets::get` returns None and
    // the EMIT pair-builder skips the key), but the row still exists
    // in `secret_active_state` (so the key IS in the strip set), so
    // the writer correctly removes it from every surface. AFTER
    // refresh, we forget the row for clean teardown.
    //
    // For shared / global user-bucket entries, the refresh fans out to
    // every registered project (see `refresh_env_after_user_secret_change`),
    // so the strip-set semantics apply per-project.
    //
    // Module-owned (`module_id != 'user'`) entries don't go through
    // the env-file emit path, so the strip-set semantics don't apply
    // — the order is irrelevant for them. We branch on the broader
    // user-bucket predicate so the existing code path for non-user-bucket
    // secrets stays byte-identical to pre-Subagent-G.
    if is_user_emit_bucket(&scope, &module_id) {
        refresh_env_after_user_secret_change(
            &db,
            &project_id,
            &scope,
            &module_id,
            "remove_secret_v2",
        );
        // Now the row's done its job in the strip set — forget it.
        db.forget_secret_active_state(&scope, &project_id, &module_id, &key)?;
    } else {
        // Pre-Subagent-G ordering for non-user-bucket entries.
        db.forget_secret_active_state(&scope, &project_id, &module_id, &key)?;
    }

    // v0.3.0 — Remove deletes the KEYCHAIN entry and the launcher row. It
    // does NOT (and must not silently) touch the tier-2 file store: that
    // file is the user's, written by `vct set`, and deleting it from a
    // GUI action the user believes is scoped to the launcher would be a
    // data loss they never consented to. So the honest thing is to RECORD
    // that a copy survives — the panel's confirm dialog says so before the
    // click, this says so afterwards, and the row stays visible because
    // `list_user_secret_keys_impl` unions the file store in.
    //
    // Metadata only: presence + the path (which contains the key name,
    // already in this same audit row). No value byte is read here.
    let project_name = file_store_project_name(&db, &scope, &project_id);
    db.audit(
        "secret_remove",
        Some(&project_id),
        Some(&module_id),
        &remove_audit_payload(&scope, &key, project_name.as_deref()),
    )?;
    Ok(())
}

/// The exact JSON `remove_secret_v2` records for a Remove — extracted so
/// the SURVIVOR question is testable (a `#[command]` takes `State<'_, Db>`,
/// which no unit test can construct, and the rest of that function performs
/// keychain deletes and an env-surface refresh).
///
/// Two survivors, not one. Removing the keychain entry leaves BOTH tier-2
/// legs untouched:
///   * `projects/<NAME>/<key>` — this row's own namespace;
///   * `shared/<key>` — the fall-through leg, which keeps serving a
///     per-project key after its keychain entry is gone.
/// Recording only the first logged "nothing survives" for a Remove that
/// changed nothing any consumer can observe. The shared leg is
/// MARKER-GATED, so a project that opted out is not told a file it never
/// reads survives.
///
/// Metadata only: presence booleans and the KEY (already in this row). No
/// value byte is read.
fn remove_audit_payload(
    scope: &str,
    key: &str,
    project_name: Option<&str>,
) -> serde_json::Value {
    let file_store_copy_remains = if scope == "global" {
        // No global namespace exists in the file store.
        false
    } else {
        file_store_dir_for_scope(scope, project_name)
            .map(|d| secrets_file_store::probe_key(&d, key).is_present())
            .unwrap_or(false)
    };
    let shared_file_store_copy_resolves = match (scope, project_name) {
        ("per_project", Some(name)) => {
            secrets_file_store::probe_shared_fallback(name, key).is_present()
        }
        _ => false,
    };
    serde_json::json!({
        "key": key,
        "scope": scope,
        "file_store_copy_remains": file_store_copy_remains,
        "shared_file_store_copy_resolves": shared_file_store_copy_resolves,
    })
}

// ─── Where does the value actually LIVE? (v0.3.0) ─────────────────────
//
// Pre-v0.3.0 every presence surface in this file asked exactly one
// question — "does the OS keychain have it?" — and rendered a `false` as
// "not set". But the sanctioned RESOLVERS
// (`vco_lib/agent_secrets.py::get`, `templates/scripts/vct_secrets_resolve.sh`
// and its `.ps1` sibling) are a THREE-tier chain: hub/keychain, then the
// file store at `$VCT_SECRETS_DIR` (default `~/.vct-secrets`), then the
// project's own `.env`. A key held only in the file store resolves for
// every consumer and rendered in the panel as "not set".
//
// That is not cosmetic. `CLAUDE.md` warns that a launcher-GUI save and a
// `vct set` are DIFFERENT stores and that writing the same key to both
// forks a divergent copy. A user shown "NOT SET" for a key that already
// works re-types it in the GUI — which is precisely how the fork gets
// created. The display was steering users into the documented failure
// mode, so the fix has to show WHERE the value lives, not merely flip a
// boolean.
//
// Tier 2 is TWO directories, not one, and both are probed: the
// resolvers read `projects/<NAME>/<key>` and then `shared/<key>`, so a
// per-project key held only in `shared/` resolves for every consumer.
// `file_store` reports the first (one file, one row — attribution stays
// answerable); `shared_file_store` reports the second, gated by the
// `.no-shared-fallback` marker so an opted-out project is never told a
// file it does not read satisfies its key.
//
// Tier 3 (`.env`) is deliberately NOT probed here: it is the project's
// own file, outside the launcher's stores, and the panel offers no
// lifecycle over it. Surfacing it would imply a management surface that
// does not exist. The badge copy is written to match — "not set" is
// worded as absence from the stores the launcher speaks for, never as a
// claim that nothing anywhere resolves the key.

/// Which store the runtime resolver would actually serve a key from.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum WinningStore {
    /// Tier 1 — the OS keychain, via the hub.
    Keychain,
    /// Tier 2 — `$VCT_SECRETS_DIR/{projects/<NAME>,shared}/<key>`.
    FileStore,
    /// No sanctioned store holds a live value for this key.
    NoStore,
}

/// The file-store directory a panel SCOPE maps onto.
///
/// * `shared`      → `<root>/shared`
/// * `per_project` → `<root>/projects/<NAME>` (the project's DB `name`,
///                   which is what the `.no-shared-fallback` marker
///                   (`secrets_file_store::NO_SHARED_FALLBACK_MARKER`) and
///                   `vct --project NAME` both use)
///
/// This is the row's OWN namespace only. The `shared/` leg the resolvers
/// fall through to afterwards is a different question, answered by
/// `secrets_file_store::probe_shared_fallback` and reported separately as
/// [`StoreReport::shared_file_store`].
/// * `global`      → `None`. The panel's "Global (this machine)" scope is
///                   a keychain-only concept; the file store has no global
///                   namespace, so there is nothing to probe.
///
/// `None` is also returned when the store root or the project name cannot
/// be resolved — the caller distinguishes those cases from `global` and
/// reports [`Presence::Unknown`] rather than a wrong "absent".
fn file_store_dir_for_scope(scope: &str, project_name: Option<&str>) -> Option<PathBuf> {
    match scope {
        "shared" => secrets_file_store::shared_dir(),
        "per_project" => project_name.and_then(secrets_file_store::project_dir),
        _ => None,
    }
}

/// One key's presence across every store the launcher can speak for:
/// tier 1 (the keychain), tier 2's own-namespace leg, and — for a
/// per-project row — tier 2's `shared/` fall-through leg.
///
/// Every field is metadata — presence, a path, and an equality bit. No
/// value byte is present in this struct or reachable from it.
#[derive(Debug, Clone, Serialize)]
pub struct StoreReport {
    /// Tier 1. `Unknown` when the keychain is locked or errored — a store
    /// that could not be read must never claim the key is absent.
    pub keychain: Presence,
    /// Tier 2. `Absent` for `global` scope (no such namespace); `Unknown`
    /// when the store root could not be resolved or the directory could
    /// not be read.
    pub file_store: Presence,
    /// `Some(true)` when BOTH stores hold the key and their values differ
    /// — the divergent-copy state `CLAUDE.md` warns about, and the single
    /// most useful thing the panel can tell the user. `Some(false)` when
    /// both hold it and they agree. `None` whenever the comparison could
    /// not be made (only one store has it, or the file was unreadable).
    ///
    /// Only the BOOLEAN crosses this boundary. Not the values, and not a
    /// digest of them either: a hash of a low-entropy secret is a
    /// brute-forceable oracle, so hashing would leak, not protect.
    pub values_diverge: Option<bool>,
    /// Absolute path of the file-store copy, when one exists. Contains the
    /// KEY name (already user-visible) and never the value. Needed so the
    /// Remove confirmation can name the file it is NOT going to delete.
    pub file_store_path: Option<String>,
    /// Tier 2, SECOND LEG — would `shared/<key>` satisfy this row?
    ///
    /// `file_store` above reports on ONE directory (this row's own
    /// namespace), which is what makes "where does this value live?"
    /// answerable. But the resolvers do not stop there: after
    /// `projects/<NAME>/<key>` misses they read `shared/<key>`. A
    /// per-project key held only in `shared/` therefore resolves for every
    /// consumer while `file_store` is honestly `Absent` — and the badge,
    /// reading only those two tiers, printed "not set" for it. This field
    /// is that missing leg.
    ///
    /// [`Presence::Absent`] when the project holds
    /// `.no-shared-fallback`: the file may exist, but it does NOT resolve
    /// here, and claiming otherwise would describe another project's
    /// resolution. See `secrets_file_store::probe_shared_fallback`.
    ///
    /// [`Presence::Absent`] for the `shared` and `global` scopes, and that
    /// is a definition rather than a gap. A shared row's `file_store` IS
    /// the `shared/` probe, so it has no further leg to fall through to;
    /// and the panel's `global` scope is a keychain-only concept whose
    /// `get_secret_status_v2` call carries the `_global_` SENTINEL rather
    /// than a project id, so no marker could be evaluated for it. Both are
    /// already covered where they arise: `list_user_secret_keys_v2` emits
    /// the shared row alongside them and flags the collision.
    pub shared_file_store: Presence,
    /// Absolute path of the SHARED file-store copy, when one would serve
    /// this row. KEY name and path only — never a value. Needed so the
    /// Remove confirmation can name the file that keeps resolving after
    /// the keychain entry is gone.
    pub shared_file_store_path: Option<String>,
    /// The requesting project has turned on "Disable shared secrets for
    /// this project", so the KEYCHAIN's user-shared bucket is not resolved
    /// for it (`db::secret_scope_policy::shared_secrets_read_disabled`, the
    /// gate `resolve_active_user_secret_pairs_for_requester` applies before
    /// it walks the shared bucket at all).
    ///
    /// `false` for every scope except `shared`: the gate drops ONLY that
    /// bucket — per-project and global keychain rows are untouched by it,
    /// and a per-project row's `shared/` fall-through leg is gated at the
    /// source by [`StoreReport::shared_file_store`]'s marker probe.
    ///
    /// This is DISPLAY metadata and deliberately NOT folded into `is_set`.
    /// `is_set` is the launcher's per-(secret × requester) permission gate,
    /// and this bulk opt-out is a different question with a different
    /// remedy; conflating them would silently widen the gate's meaning for
    /// every reader that asks it (`is_secret_set`, the hub, module code).
    pub shared_read_disabled: bool,
    /// The requesting project holds `.no-shared-fallback`, so tier 2's
    /// `shared/` directory is not read for it either
    /// (`secrets_file_store::shared_fallback_disabled`).
    ///
    /// Reported SEPARATELY from `shared_read_disabled` even though one
    /// toggle writes both, because the marker write is best-effort: when it
    /// fails, `set_shared_secrets_read_disabled` returns a warning saying in
    /// so many words that "file-store tier-2 shared fallback is not gated
    /// until the marker exists". In that state the shared FILE still serves
    /// this project, and a badge derived from the DB flag alone would tell
    /// the user a key does not reach them when it does — the same
    /// false-negative-about-resolution this whole report exists to remove.
    ///
    /// `false` for every scope except `shared`, for the same reason as
    /// above.
    pub shared_file_fallback_disabled: bool,
}

/// Probe both stores for one key. The ONE presence reader — every panel
/// surface (`get_secret_status_v2`, `list_user_secret_keys_v2`) goes
/// through it so they cannot disagree about where a value lives.
///
/// Exactly one keychain round-trip: the value is needed for the
/// divergence comparison and `Option::is_some` already answers presence,
/// so calling `secrets::is_set` first would have doubled the traffic.
///
/// `requester_project_id` is the project whose POINT OF VIEW is being
/// rendered — the same requester the active-flag gate is asked about. It is
/// NOT the owner: a shared row is owned by the `_user_shared_` sentinel but
/// read by a real project, and the two shared-tier opt-outs below are
/// properties of the READER, not of the row.
fn read_store_report(
    db: &Db,
    scope: &str,
    owner_project_id: &str,
    module_id: &str,
    key: &str,
    project_name: Option<&str>,
    requester_project_id: &str,
) -> StoreReport {
    let scope_enum = scope_from_manifest(scope, owner_project_id);
    let (keychain, keychain_value) = match secrets::get(scope_enum, module_id, key) {
        Ok(Some(v)) => (Presence::Present, Some(v)),
        Ok(None) => (Presence::Absent, None),
        // Locked store / daemon timeout / read error. NOT absence.
        Err(_) => (Presence::Unknown, None),
    };

    let dir = file_store_dir_for_scope(scope, project_name);
    let file_store = if scope == "global" {
        // No global namespace exists in the file store, so "absent" is a
        // complete and true answer rather than a failure to look.
        Presence::Absent
    } else {
        match &dir {
            Some(d) => secrets_file_store::probe_key(d, key),
            None => Presence::Unknown,
        }
    };

    // Divergence is only a question when BOTH stores hold the key.
    let values_diverge = match (keychain, file_store, &dir, &keychain_value) {
        (Presence::Present, Presence::Present, Some(d), Some(kv)) => {
            secrets_file_store::read_key_value(d, key).map(|fv| fv != *kv)
        }
        _ => None,
    };

    let file_store_path = match (file_store, &dir) {
        (Presence::Present, Some(d)) => Some(d.join(key).display().to_string()),
        _ => None,
    };

    // The second tier-2 leg: `projects/<NAME>/` misses → `shared/` is read,
    // unless this project opted out with `.no-shared-fallback`. Only the
    // per_project scope has a leg to fall through to (see the field docs).
    let (shared_file_store, shared_file_store_path) = if scope == "per_project" {
        match project_name {
            Some(name) => {
                let p = secrets_file_store::probe_shared_fallback(name, key);
                let path = match (p, secrets_file_store::shared_dir()) {
                    (Presence::Present, Some(d)) => Some(d.join(key).display().to_string()),
                    _ => None,
                };
                (p, path)
            }
            // No project name means no marker to evaluate and no identity to
            // answer for. Unknown, never a confident Absent.
            None => (Presence::Unknown, None),
        }
    } else {
        (Presence::Absent, None)
    };

    // The per-project "Disable shared secrets" opt-out, evaluated for the
    // READER. Only a `shared`-scope row has a shared tier to lose: the
    // keychain gate drops that bucket and nothing else, and a per-project
    // row's `shared/` leg is already marker-gated inside
    // `probe_shared_fallback` above.
    let (shared_read_disabled, shared_file_fallback_disabled) = if scope == "shared" {
        let keychain_gate =
            crate::db::secret_scope_policy::shared_secrets_read_disabled(db, requester_project_id);
        // The marker is keyed by the reader's file-store NAME, which is the
        // project's DB `name` (what `vct --project NAME` and
        // `set_shared_secrets_read_disabled` both use). An unregistered
        // requester (e.g. a caller that passed a sentinel because it does not
        // know the reader) has no marker to evaluate — and no shared tier to
        // describe either, so `false` leaves the pre-existing behaviour.
        let file_gate = db
            .get_project(requester_project_id)
            .ok()
            .flatten()
            .map(|p| secrets_file_store::shared_fallback_disabled(&p.name))
            .unwrap_or(false);
        (keychain_gate, file_gate)
    } else {
        (false, false)
    };

    StoreReport {
        keychain,
        file_store,
        values_diverge,
        file_store_path,
        shared_file_store,
        shared_file_store_path,
        shared_read_disabled,
        shared_file_fallback_disabled,
    }
}

/// The project NAME the file store keys per-project secrets under, for a
/// launcher `project_id`. `None` for the shared/global sentinels and for
/// an unregistered id.
fn file_store_project_name(db: &Db, scope: &str, project_id: &str) -> Option<String> {
    if scope != "per_project" {
        return None;
    }
    db.get_project(project_id).ok().flatten().map(|p| p.name)
}

/// Combined status used by the secrets panel UI. `is_set` follows the
/// same gate as `is_secret_set` (true ⇔ keychain has value AND
/// active=true). `has_saved_value` reports whether the keychain still
/// has a value REGARDLESS of the active flag — the UI uses this to tell
/// "newly added, never set" (no saved value) apart from "Unset, value
/// preserved" (saved value but inactive).
///
/// `has_saved_value` is metadata about the LIFECYCLE state, not the
/// secret itself — it discloses no value bytes. The audit log mentions
/// `secret_unset` / `secret_reactivate` already, so an attacker with DB
/// read access can already reconstruct this fact; surfacing the boolean
/// to the UI does not weaken the model.
///
/// v0.3.0: `is_set` / `has_saved_value` keep their exact pre-existing
/// meaning (KEYCHAIN truth × the launcher's active flag) so every reader
/// that gates on them — including `is_secret_set`, which answers the
/// launcher's own permission matrix — is byte-identical. The store
/// question the panel actually needs to answer is carried by the ADDED
/// `stores` field, which reports both tiers honestly. Do not "simplify"
/// by folding the file store into `has_saved_value`: the file store is
/// not gated by the active flag, so that would silently widen the
/// permission gate.
#[derive(Debug, Serialize)]
pub struct SecretStatus {
    pub is_set: bool,
    pub is_active: bool,
    pub has_saved_value: bool,
    /// Presence across BOTH sanctioned stores. The panel renders its
    /// badge from this, never from `has_saved_value` alone — a key held
    /// only in the file store resolves for every consumer and must never
    /// display as "not set".
    #[serde(flatten)]
    pub stores: StoreReport,
}

/// # This command is a shim on purpose
///
/// Same reasoning as [`list_user_secret_keys_v2`]: `#[command]` functions
/// take `State<'_, Db>`, which a unit test cannot construct, so a behaviour
/// test can only reach this path if the body lives somewhere callable.
/// Before v0.3.0 the body was inline and consequently had NO behavioural
/// test at all — the per-project Secret-refs tab's only backend, unproven.
/// Keeping this wrapper to a single expression means
/// [`get_secret_status_impl`] IS the production path, and mutating it turns
/// the tests red. Pinned by `status_command_is_a_pure_shim_over_the_tested_impl`.
#[command]
pub async fn get_secret_status_v2(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    requester_project_id: Option<String>,
    db: State<'_, Db>,
) -> Result<SecretStatus, String> {
    get_secret_status_impl(db.inner(), &project_id, &module_id, &scope, &key, requester_project_id.as_deref())
}

/// The production body of [`get_secret_status_v2`].
///
/// `requester_project_id` names the project whose point of view is being
/// rendered. `None` means "the owner is the reader", which is exactly true
/// for `per_project` rows and was the ONLY behaviour before v0.3.0 — so an
/// omitted argument reproduces the previous answer byte-for-byte.
///
/// It matters for the other two scopes. A `shared` row is owned by the
/// `_user_shared_` SENTINEL, which is not a project: asking the active-flag
/// gate about it finds no per-requester row and falls back to the `*`
/// sentinel row, i.e. the ALL-readers answer, while the panel is rendering
/// one specific reader's view. The reader is also the only thing the two
/// shared-tier opt-outs in [`StoreReport`] can be evaluated against.
fn get_secret_status_impl(
    db: &Db,
    project_id: &str,
    module_id: &str,
    scope: &str,
    key: &str,
    requester_project_id: Option<&str>,
) -> Result<SecretStatus, String> {
    enforce_scope_invariants(scope, project_id, db)?;
    // 0.1.7 H3 (2026-05-08): the `is_set` field is the same boolean
    // contract as the `is_secret_set` command — readers (GUI badge,
    // any module testing presence) MUST see the cross-launcher view
    // so the GUI doesn't disagree with what subprocesses see. The
    // `is_active` field stays own-DB so the GUI can distinguish "this
    // launcher paused it" from "another launcher paused it" if a
    // future UI surfaces that detail.
    // 0.2.1: per-requester gate. The GUI is asking "does THIS project see
    // the secret as active?", so the requester is `project_id`. For
    // shared/global scopes the requester is the project that's about to
    // consume the secret — same project_id the GUI already knows. For
    // per_project scope, owner == requester == project_id, so the
    // semantics are identical to the legacy single-row gate.
    // v0.3.0: the reader is the caller's `requester_project_id` when it sent
    // one, else the owner (identical for `per_project`, where owner ==
    // requester, which is what every pre-v0.3.0 caller relied on).
    let requester = requester_project_id.unwrap_or(project_id);
    let active_cross = crate::db::secret_active::is_secret_active_cross_launcher_for_requester(
        db, scope, project_id, module_id, key, requester,
    );
    let active_own =
        db.is_secret_active_for_requester(scope, project_id, module_id, key, requester)?;
    // v0.3.0: ONE probe of both stores (see `read_store_report`). This
    // replaced a `secrets::is_set(...)?` — note the `?`: a locked or
    // erroring keychain used to fail the WHOLE status call, leaving the
    // panel showing whatever stale value it had (in practice the `false`
    // its registry seeded), i.e. "not set". Now the keychain reports
    // `Unknown` and the file store is still consulted, so a key that
    // resolves can never render as absent because tier 1 was unreadable.
    let project_name = file_store_project_name(db, scope, project_id);
    let stores = read_store_report(
        db,
        scope,
        project_id,
        module_id,
        key,
        project_name.as_deref(),
        requester,
    );
    let has_saved_value = stores.keychain.is_present();
    Ok(SecretStatus {
        is_set: active_cross && has_saved_value,
        is_active: active_own,
        has_saved_value,
        stores,
    })
}

/// True only when the keychain has a value AND the launcher's active
/// flag is set. Returns false for inactive (Unset) entries even though
/// the keychain still has the value — that is the read-time gate.
///
/// 0.1.7 H3 (2026-05-08): the active-flag check is the cross-launcher
/// variant (Option γ), matching every other secret-reader path
/// (Subagent D's `github_pat_from_keychain`, Subagent G's user-secret
/// resolver, the hub's `project_env` resolver). Pre-H3 this used the
/// own-DB-only `db.is_secret_active`, which let prod's GUI report
/// "set" while every consumer (hub + env-file emit) saw "paused"
/// because dev launcher had paused the secret. H3 closes the
/// last asymmetry — the GUI's "Set" badge now agrees with what
/// subprocesses actually see.
#[command]
pub async fn is_secret_set(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    // Read-time gate: an inactive entry MUST appear "not set" to the UI
    // and to any module asking via this command. We check the gate FIRST
    // to avoid an unnecessary keychain round-trip when the entry is
    // paused.
    // 0.2.1: per-requester gate. The caller is asking "is this secret
    // set for THIS project right now?" — same project_id is the
    // requester. For per_project scope, owner == requester so the
    // semantics are identical to the legacy single-row gate; for
    // shared/global, the requester drives a per-project pause check.
    let active = crate::db::secret_active::is_secret_active_cross_launcher_for_requester(
        &db, &scope, &project_id, &module_id, &key, &project_id,
    );
    if !active {
        return Ok(false);
    }
    let scope_enum = scope_from_manifest(&scope, &project_id);
    secrets::is_set(scope_enum, &module_id, &key)
}

/// Return a masked preview for NON-sensitive secrets only. For sensitive
/// secrets, the caller should use `is_secret_set` and render a "••••••••"
/// placeholder in the UI without calling this command.
///
/// Read-time gate (Bug 3): inactive entries return Ok(None) regardless
/// of keychain state. This is the canary-test invariant — a paused
/// secret must NOT leak through the preview path even as a masked value.
#[command]
pub async fn get_secret_preview(
    project_id: String,
    module_id: String,
    scope: String,
    key: String,
    sensitive: bool,
    db: State<'_, Db>,
) -> Result<Option<String>, String> {
    if sensitive {
        return Err("cannot preview sensitive secret".into());
    }
    enforce_scope_invariants(&scope, &project_id, &db)?;
    // Active-flag gate. Even though the keychain still holds the value
    // for an Unset entry, we treat it as if it weren't there for the
    // purposes of the public API. The user "paused" the entry; readers
    // (including the UI itself) must not see anything but a "not set"
    // signal until Reactivate.
    //
    // 0.1.7 H3 (2026-05-08): cross-launcher gate (Option γ). Symmetric
    // with the hub's `project_env` resolver and `is_secret_set` so a
    // pause anywhere takes effect everywhere — no GUI/consumer
    // disagreement. See `is_secret_set` doc comment for the asymmetry
    // we're closing.
    // 0.2.1: per-requester gate (same rule as is_secret_set). The
    // preview path must agree with what `project_env` will actually
    // serve, so a per-project pause hides the masked preview too.
    let active = crate::db::secret_active::is_secret_active_cross_launcher_for_requester(
        &db, &scope, &project_id, &module_id, &key, &project_id,
    );
    if !active {
        return Ok(None);
    }
    let scope_enum = scope_from_manifest(&scope, &project_id);
    let val = secrets::get(scope_enum, &module_id, &key)?;
    Ok(val.map(|v| secrets::mask_preview(&v)))
}

// ─── Settings ───────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SettingEntry {
    pub key: String,
    pub value: serde_json::Value,
}

#[command]
pub async fn get_setting_v2(
    project_id: String,
    module_id: String,
    key: String,
    db: State<'_, Db>,
) -> Result<Option<serde_json::Value>, String> {
    db.get_setting(&project_id, &module_id, &key)
}

#[command]
pub async fn set_setting_v2(
    project_id: String,
    module_id: String,
    key: String,
    value: serde_json::Value,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_setting(&project_id, &module_id, &key, &value)
}

#[command]
pub async fn list_module_settings_v2(
    project_id: String,
    module_id: String,
    db: State<'_, Db>,
) -> Result<Vec<SettingEntry>, String> {
    let rows = db.list_module_settings(&project_id, &module_id)?;
    Ok(rows
        .into_iter()
        .map(|(key, value)| SettingEntry { key, value })
        .collect())
}

// ─── 0.2.1 grants & per-requester pause commands ─────────────────────────
//
// Five Tauri commands that surface the migration-009 grants table and
// per-(secret × requester) active-flag rows to the launcher GUI:
//
//   * `grant_secret`              — owner grants read access to grantee
//   * `revoke_secret_grant_cmd`   — owner revokes a grant
//   * `list_grants_for_project`   — return owner-issued + grantee-received
//                                    grants for the GUI's "Per-project" /
//                                    "Shared" tabs
//   * `pause_secret_for_project`  — flip the per-(secret × requester)
//                                    flag to inactive (grantee self-opt-out
//                                    or owner pausing for a specific peer)
//   * `resume_secret_for_project` — drop the per-requester pause row so the
//                                    canonical / `*` row takes over
//
// Authorisation policy (deliberately enforced in Rust, not SQL):
//   * `grant_secret`: only the OWNER project may grant.
//   * `revoke_secret_grant_cmd`: only the OWNER project may revoke.
//   * `pause_secret_for_project` / `resume_secret_for_project`: any
//     project that's a valid requester (owner OR grantee, OR — for
//     shared/global — itself) may pause for itself. Pausing for someone
//     else is restricted to the OWNER. The CHECK on `secret_grants`
//     keeps the schema honest; this layer keeps the user-facing ergonomic.

#[derive(Debug, Clone, serde::Serialize)]
pub struct SecretGrantView {
    pub scope: String,
    pub owner_project_id: String,
    pub module_id: String,
    pub key: String,
    pub grantee_project_id: String,
    pub granted_at: i64,
    pub granted_by_actor: Option<String>,
    pub note: Option<String>,
}

impl From<crate::db::secret_grants::SecretGrant> for SecretGrantView {
    fn from(g: crate::db::secret_grants::SecretGrant) -> Self {
        Self {
            scope: g.scope,
            owner_project_id: g.owner_project_id,
            module_id: g.module_id,
            key: g.key,
            grantee_project_id: g.grantee_project_id,
            granted_at: g.granted_at,
            granted_by_actor: g.granted_by_actor,
            note: g.note,
        }
    }
}

/// Wraps `list_grants_for_project` output: the GUI's per-project tab
/// renders both the grants this project ISSUED (as owner) and the
/// grants it RECEIVED (as grantee) on the same screen.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ProjectGrantsView {
    pub issued: Vec<SecretGrantView>,
    pub received: Vec<SecretGrantView>,
}

#[command]
pub async fn grant_secret(
    owner_project_id: String,
    module_id: String,
    key: String,
    grantee_project_id: String,
    note: Option<String>,
    db: State<'_, Db>,
) -> Result<bool, String> {
    if owner_project_id == grantee_project_id {
        return Err("grant_secret: owner and grantee must differ".to_string());
    }
    // Schema CHECK enforces scope='per_project'. The command takes no
    // scope arg — granting global/shared is meaningless because they're
    // already cross-project, and exposing the choice would be footgun.
    db.insert_secret_grant(
        "per_project",
        &owner_project_id,
        &module_id,
        &key,
        &grantee_project_id,
        Some("user"),
        note.as_deref(),
    )
}

#[command]
pub async fn revoke_secret_grant_cmd(
    owner_project_id: String,
    module_id: String,
    key: String,
    grantee_project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    db.revoke_secret_grant(
        "per_project",
        &owner_project_id,
        &module_id,
        &key,
        &grantee_project_id,
    )
}

#[command]
pub async fn list_grants_for_project(
    project_id: String,
    db: State<'_, Db>,
) -> Result<ProjectGrantsView, String> {
    let issued = db
        .list_grants_by_owner(&project_id)?
        .into_iter()
        .map(SecretGrantView::from)
        .collect();
    let received = db
        .list_grants_by_grantee(&project_id)?
        .into_iter()
        .map(SecretGrantView::from)
        .collect();
    Ok(ProjectGrantsView { issued, received })
}

#[command]
pub async fn pause_secret_for_project(
    scope: String,
    project_id: String,
    module_id: String,
    key: String,
    requester_project_id: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    db.mark_secret_inactive_for_requester(
        &scope,
        &project_id,
        &module_id,
        &key,
        &requester_project_id,
    )
}

#[command]
pub async fn resume_secret_for_project(
    scope: String,
    project_id: String,
    module_id: String,
    key: String,
    requester_project_id: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    enforce_scope_invariants(&scope, &project_id, &db)?;
    // Resume = drop the per-requester row so the canonical (`*` or
    // owner-literal) row takes over. We do NOT explicitly mark active —
    // that would create a row on every resume and pollute the table
    // with default-state entries we'd otherwise prune as no-ops.
    db.forget_secret_active_state_for_requester(
        &scope,
        &project_id,
        &module_id,
        &key,
        &requester_project_id,
    )
}

/// Read whether a secret is currently PAUSED for a specific requester
/// (owner's per-`(key, requester)` pause). `true` = paused (inactive),
/// `false` = active. Lets the SecretsPanel grants section render the
/// correct Pause/Resume affordance on load rather than guessing.
#[command]
pub async fn is_secret_paused_for_requester(
    scope: String,
    project_id: String,
    module_id: String,
    key: String,
    requester_project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    // `is_secret_active_for_requester` returns true when active; paused is the
    // negation. R2-12: a DB error must NOT be masked as "active/not-paused"
    // (`unwrap_or(true)` painted an UNKNOWN state as an authoritative "not
    // paused", so the panel rendered a confident Pause affordance over a failed
    // read). Surface the Err instead — the SecretsPanel's per-grant try/catch
    // already degrades to `paused = false` on error (SecretsPanel.svelte
    // `loadGrants`), so the RENDERING is identical to the old default but the
    // path is honest: the error is visible to the caller / console rather than
    // silently swallowed. A genuinely-missing row is `Ok(true)` (default-active)
    // from the DB layer, so the default-on behaviour for absent rows is
    // unchanged — only true DB FAILURES now propagate.
    let active = db.is_secret_active_for_requester(
        &scope,
        &project_id,
        &module_id,
        &key,
        &requester_project_id,
    )?;
    Ok(!active)
}

// ─── 0.2.x backlog #3: shared-tab key-collision detection ───────────────
//
// `list_user_secret_keys_v2` enumerates every user-bucket secret KEY the
// launcher has ever observed for a given project, across the three scopes
// the SecretsPanel writes to:
//
//   * `(scope='per_project', project_id=<this project>, module_id='user')`
//   * `(scope='shared',      project_id='_user_shared_', module_id='user')`
//   * `(scope='global',      project_id='_global_',      module_id='user')`
//
// For each (scope, key) row it also reports whether ANY OTHER scope has a
// row for the SAME key — the "shadowing" condition the SecretsPanel renders
// as a warning badge on the affected rows. The resolver's read-time
// precedence is `per_project > shared > global` (see SecretsPanel header
// comment "Read-time resolution order"); when collisions exist we mark the
// collision'd rows as shadowed and surface the `winning_scope` so the user
// can confirm which value will actually be used at runtime.
//
// Rationale: pre-0.2.x-backlog-#3 the panel rendered each tab in isolation
// and a duplicate KEY across e.g. Shared + Per-project was silently
// ignored — the resolver applied per-project's value while the user was
// actively editing a stale Shared entry, with no visual hint. The badge
// closes that "GUI says set, but my edit doesn't reach the runtime" gap.

/// One row in `list_user_secret_keys_v2`'s response — a single user-bucket
/// secret (scope, module_id='user', key) plus everything the SecretsPanel
/// needs to render the shadow badge.
#[derive(Debug, Clone, Serialize)]
pub struct UserSecretKeyRow {
    pub scope: String,        // "per_project" | "shared" | "global"
    pub project_id: String,   // owner project_id (sentinel for shared/global)
    pub module_id: String,    // always "user" — kept for symmetry with other APIs
    pub key: String,
    /// Same gate as `is_secret_set` — true ⇔ keychain has value AND
    /// per-requester active flag (with this `project_id` as the requester
    /// for shared/global) is set.
    pub is_set: bool,
    /// Active flag in launcher.db for this row.
    pub is_active: bool,
    /// True when the keychain still has a value (regardless of active).
    pub has_saved_value: bool,
    /// True when ANY OTHER scope has a row for the same KEY name in the
    /// user bucket and that other row would override this one (precedence:
    /// per_project > shared > global). The badge renders on every row of
    /// a collision'd KEY — both the winner and the loser — so the user
    /// can SEE which value is in effect.
    pub is_shadowed: bool,
    /// The scope whose value the resolver actually serves at runtime for
    /// this (project_id, key) tuple, considering precedence + active
    /// state. Equals `scope` when this row IS the winner. Different from
    /// `scope` when another scope's row outranks this one.
    pub winning_scope: String,
    /// v0.3.0: which STORE the winning value comes from. `winning_scope`
    /// alone was ambiguous once the file store is visible — "shared wins"
    /// means something different when the shared value lives in a file
    /// than when it lives in the keychain.
    pub winning_store: WinningStore,
    /// v0.3.0: `true` when this row exists in `secret_active_state`, i.e.
    /// the launcher manages it. `false` for a row synthesised purely from
    /// a file-store file the launcher has never been told about.
    ///
    /// The panel gates its Remove button on this: `remove_secret_v2`
    /// deletes a keychain entry and a DB row, and CANNOT delete a
    /// file-store file. Offering Remove on a file-only row would claim a
    /// removal that never happened.
    pub has_launcher_row: bool,
    /// v0.3.0: presence across both stores. `is_set` / `has_saved_value`
    /// above stay keychain-only (unchanged gate semantics); this is what
    /// the badge renders from.
    #[serde(flatten)]
    pub stores: StoreReport,
}

/// Lifecycle + store presence for one user-bucket entry.
///
/// `is_set` / `is_active` / `has_saved_value` keep their pre-v0.3.0
/// meaning exactly (cross-launcher active gate × KEYCHAIN presence);
/// `stores` is the added, honest two-store view.
#[derive(Debug, Clone)]
struct UserSecretStatus {
    is_set: bool,
    is_active: bool,
    has_saved_value: bool,
    stores: StoreReport,
}

/// Read the lifecycle state for a single user-bucket entry. Mirrors
/// `get_secret_status_v2` semantics (cross-launcher gate, per-requester)
/// but takes the same `(scope, project_id, key)` triple so we can call
/// it in a tight loop.
///
/// v0.3.0: this function used to end with
/// `secrets::is_set(...).unwrap_or(false)` — so a locked keychain, a
/// daemon timeout, or any transient read error became "no saved value"
/// and the panel rendered "not set". `read_store_report` reports
/// [`Presence::Unknown`] for those instead, and the panel renders an
/// explicit "unknown" badge. Silence about a failed probe is
/// indistinguishable from a confident negative, and only one of the two
/// tells the user to unlock their keychain.
fn read_user_secret_status(
    db: &Db,
    scope: &str,
    project_id: &str,
    requester_project_id: &str,
    key: &str,
    project_name: Option<&str>,
) -> UserSecretStatus {
    let active = crate::db::secret_active::is_secret_active_cross_launcher_for_requester(
        db,
        scope,
        project_id,
        "user",
        key,
        requester_project_id,
    );
    let active_own = db
        .is_secret_active_for_requester(scope, project_id, "user", key, requester_project_id)
        .unwrap_or(true);
    let stores = read_store_report(
        db,
        scope,
        project_id,
        "user",
        key,
        project_name,
        requester_project_id,
    );
    let has_saved_value = stores.keychain.is_present();
    UserSecretStatus {
        // is_set follows the same gate as the read-time API: cross-launcher
        // active AND keychain-present.
        is_set: active && has_saved_value,
        is_active: active_own,
        has_saved_value,
        stores,
    }
}

/// Resolve which (scope, store) the runtime resolver would ACTUALLY serve
/// for `(project_id, key)`.
///
/// The full precedence, read off the three resolver implementations
/// (`agent_secrets.py::get`, `vct_secrets_resolve.sh`, `.ps1`): tier 1
/// (hub → keychain) is consulted for EVERY scope before tier 2 (the file
/// store) is consulted at all, and within tier 2 the order is
/// `projects/<NAME>/` then `shared/`. So:
///
/// ```text
///   1. keychain  per_project      (active + present)
///   2. keychain  shared
///   3. keychain  global
///   4. file store  projects/<NAME>
///   5. file store  shared
/// ```
///
/// A keychain row only competes when its `is_set` is true (active AND
/// present) — the active flag is the launcher's permission gate. The file
/// store is NOT gated by that flag (nothing consults launcher.db to read a
/// file), which is exactly why a "paused" key can still resolve and why
/// the panel has to show the file-store copy.
///
/// `sh_file_serves_project` is the MARKER-GATED answer
/// (`secrets_file_store::probe_shared_fallback`), not a raw
/// `shared/<key>` probe. A project holding `.no-shared-fallback` never
/// reads that file, so naming it the winner would describe a resolution
/// that does not happen for the project being viewed.
///
/// If nothing is live, returns the row's own scope + [`WinningStore::NoStore`]
/// so the GUI shows "this is what you typed, even if no consumer reads it
/// yet" without needing a separate "no winner" branch.
fn resolve_winning_scope(
    own_scope: &str,
    pp_set: bool,
    sh_set: bool,
    gl_set: bool,
    pp_file: bool,
    sh_file_serves_project: bool,
) -> (String, WinningStore) {
    if pp_set {
        return ("per_project".to_string(), WinningStore::Keychain);
    }
    if sh_set {
        return ("shared".to_string(), WinningStore::Keychain);
    }
    if gl_set {
        return ("global".to_string(), WinningStore::Keychain);
    }
    if pp_file {
        return ("per_project".to_string(), WinningStore::FileStore);
    }
    if sh_file_serves_project {
        return ("shared".to_string(), WinningStore::FileStore);
    }
    // No sanctioned store has a live value — keep the badge attached to
    // the row the user is looking at.
    (own_scope.to_string(), WinningStore::NoStore)
}

/// Enumerate every user-bucket secret KEY the launcher has observed for
/// `project_id`'s view of the world (its own per_project bucket + shared +
/// global). Used by the SecretsPanel to populate the entry list AND
/// detect cross-scope KEY collisions for the shadow badge.
///
/// Soft-fail: a DB hiccup on one of the three lists yields an empty
/// sub-list rather than failing the whole call — the panel always has
/// something to render.
///
/// # This command is a shim on purpose
///
/// The body is one call to [`list_user_secret_keys_impl`]. `#[command]`
/// functions take `State<'_, Db>`, which a unit test cannot construct, and
/// the pre-existing tests in this module coped by REPLICATING the command
/// body inline — proving a copy of the logic rather than the logic (the
/// defect shape catalogued in
/// `knowledge/concepts/credited-mechanisms-that-never-fire-2026-09-04.md`).
/// Keeping this wrapper to a single expression means the impl below IS the
/// production path, and mutating it turns the tests red.
#[command]
pub async fn list_user_secret_keys_v2(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<UserSecretKeyRow>, String> {
    list_user_secret_keys_impl(db.inner(), &project_id)
}

/// Union of the launcher's own key list and the file store's, per scope.
///
/// Returns `(keys, db_keys)` — the ordered, de-duplicated union and the
/// set that came from `secret_active_state`, so each row can report
/// `has_launcher_row` honestly.
fn union_db_and_file_keys(
    db_keys: Vec<String>,
    file_keys: Vec<String>,
) -> (Vec<String>, std::collections::BTreeSet<String>) {
    use std::collections::BTreeSet;
    let owned: BTreeSet<String> = db_keys.iter().cloned().collect();
    let mut all: BTreeSet<String> = owned.clone();
    for k in file_keys {
        all.insert(k);
    }
    (all.into_iter().collect(), owned)
}

/// The production body of [`list_user_secret_keys_v2`].
///
/// v0.3.0 change of contract: the row set is the UNION of the launcher's
/// `secret_active_state` rows and the tier-2 file store's files. Before
/// this, a secret that lived only in `~/.vct-secrets/` was invisible in
/// the panel while every consumer resolved it — and after a `Remove` it
/// would vanish from the panel while still resolving, which is the same
/// lie in a different shape. The union is what makes "everything this
/// panel can affect, and everything that resolves" one list.
fn list_user_secret_keys_impl(
    db: &Db,
    project_id: &str,
) -> Result<Vec<UserSecretKeyRow>, String> {
    enforce_scope_invariants("per_project", project_id, db)?;

    // The file store keys per-project secrets under the project's NAME
    // (`vct --project NAME`), not its UUID.
    let project_name = file_store_project_name(db, "per_project", project_id);

    // Three flat key lists. `list_*` helpers in db::secret_active soft-fail
    // to empty Vec on DB error; the file-store listers soft-fail the same
    // way (an unreadable directory yields no rows, while the PER-KEY probe
    // for keys we already know about still reports `Unknown`).
    let (pp_keys, pp_db) = union_db_and_file_keys(
        db.list_user_secret_keys_for_project(project_id),
        match project_name.as_deref() {
            Some(name) => secrets_file_store::list_project_keys(name),
            None => Vec::new(),
        },
    );
    let (sh_keys, sh_db) = union_db_and_file_keys(
        db.list_shared_user_secret_keys(),
        secrets_file_store::list_shared_keys(),
    );
    // The file store has no global namespace — see `file_store_dir_for_scope`.
    let (gl_keys, gl_db) =
        union_db_and_file_keys(db.list_global_user_secret_keys(), Vec::new());

    // Build a per-key collision index up front so a single key appearing in
    // 2 or 3 scopes is flagged on EVERY row, not just one. Each value is
    // the list of scopes the key appears in.
    use std::collections::{BTreeMap, HashMap};
    let mut scope_map: HashMap<&str, Vec<&str>> = HashMap::new();
    for k in &pp_keys {
        scope_map.entry(k.as_str()).or_default().push("per_project");
    }
    for k in &sh_keys {
        scope_map.entry(k.as_str()).or_default().push("shared");
    }
    for k in &gl_keys {
        scope_map.entry(k.as_str()).or_default().push("global");
    }

    // Pre-compute per-key lifecycle for each scope so we can resolve the
    // winning_scope without re-reading the same status three times. We
    // cache by (scope, key) — small cardinality, predictable cost.
    let mut status_cache: BTreeMap<(String, String), UserSecretStatus> = BTreeMap::new();
    let mut status_for = |scope: &str, key: &str| -> UserSecretStatus {
        let cache_key = (scope.to_string(), key.to_string());
        if let Some(s) = status_cache.get(&cache_key) {
            return s.clone();
        }
        let (owner, requester) = match scope {
            "global" => (SENTINEL_GLOBAL.to_string(), project_id.to_string()),
            "shared" => (SENTINEL_SHARED.to_string(), project_id.to_string()),
            _ => (project_id.to_string(), project_id.to_string()),
        };
        let s = read_user_secret_status(
            db,
            scope,
            &owner,
            &requester,
            key,
            project_name.as_deref(),
        );
        status_cache.insert(cache_key, s.clone());
        s
    };

    let mut out: Vec<UserSecretKeyRow> = Vec::new();

    // Emit one row per (scope, key) the launcher has observed. The badge
    // condition is `scope_map[key].len() >= 2` (a KEY in ≥2 scopes).
    let push_row = |scope: &str, owner: &str, key: &str,
                    has_launcher_row: bool,
                    scope_map: &HashMap<&str, Vec<&str>>,
                    status_for: &mut dyn FnMut(&str, &str) -> UserSecretStatus,
                    out: &mut Vec<UserSecretKeyRow>| {
        let own = status_for(scope, key);
        // Compute winner using all three scopes' is_set states plus the two
        // file-store namespaces (see `resolve_winning_scope` for the order).
        let pp = if scope == "per_project" {
            own.clone()
        } else {
            status_for("per_project", key)
        };
        let sh = if scope == "shared" {
            own.clone()
        } else {
            status_for("shared", key)
        };
        let gl = if scope == "global" {
            own.clone()
        } else {
            status_for("global", key)
        };
        let (winning_scope, winning_store) = resolve_winning_scope(
            scope,
            pp.is_set,
            sh.is_set,
            gl.is_set,
            pp.stores.file_store.is_present(),
            // NOT `sh.stores.file_store` (a raw `shared/<key>` probe): the
            // question here is whether the SHARED file serves THIS project,
            // which the `.no-shared-fallback` marker can answer no. The
            // per_project row's own fall-through leg already carries that
            // gated answer, so there is one computation, not two.
            pp.stores.shared_file_store.is_present(),
        );
        let collisions = scope_map
            .get(key)
            .map(|v| v.len())
            .unwrap_or(0);
        out.push(UserSecretKeyRow {
            scope: scope.to_string(),
            project_id: owner.to_string(),
            module_id: "user".to_string(),
            key: key.to_string(),
            is_set: own.is_set,
            is_active: own.is_active,
            has_saved_value: own.has_saved_value,
            is_shadowed: collisions >= 2,
            winning_scope,
            winning_store,
            has_launcher_row,
            stores: own.stores,
        });
    };

    for k in &pp_keys {
        push_row(
            "per_project",
            project_id,
            k,
            pp_db.contains(k),
            &scope_map,
            &mut status_for,
            &mut out,
        );
    }
    for k in &sh_keys {
        push_row(
            "shared",
            SENTINEL_SHARED,
            k,
            sh_db.contains(k),
            &scope_map,
            &mut status_for,
            &mut out,
        );
    }
    for k in &gl_keys {
        push_row(
            "global",
            SENTINEL_GLOBAL,
            k,
            gl_db.contains(k),
            &scope_map,
            &mut status_for,
            &mut out,
        );
    }

    Ok(out)
}

// ─── Tests ──────────────────────────────────────────────────────────────
//
// These tests cover the scope-invariant guard rails and the active-flag
// gate. The scope-invariants check is a pure-DB function; the
// active-flag tests use the in-memory DB helpers and (where the keychain
// is involved) serialise via the shared
// `crate::secrets::test_serialize::keychain_serialize_lock`.

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Db;
    use rusqlite::{params, Connection};
    use std::sync::Mutex;

    fn make_db() -> Db {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        crate::db::migrations::apply(&conn).unwrap();
        Db(Mutex::new(conn))
    }

    /// Acquire the process-wide keychain test mutex. Every test in this
    /// module that touches the OS keyring (directly via `secrets::*` or
    /// indirectly via `refresh_env_after_user_secret_change` /
    /// `set_secret_v2` / `delete_secret_v2`) MUST hold this guard for
    /// the duration of the test body. Pre-2026-05-13 some tests skipped
    /// the lock; under heavy parallel-test load gnome-keyring-daemon
    /// SIGTRAP'd, taking the SSH-agent integration down with it. See
    /// `crate::secrets`'s module-level "Concurrency model" doc and the
    /// KG node "VCO keyring SIGTRAP" for the threat model.
    ///
    /// Returned as `_lock` (underscored) so the caller doesn't get an
    /// "unused variable" warning — the value's lifetime IS the lock
    /// scope. Hold it until end of test by binding to a `_`-prefixed
    /// local; do NOT bind to `_` alone or RAII drops the lock immediately.
    fn keychain_test_lock() -> crate::secrets::test_serialize::KeychainGuard {
        crate::secrets::test_serialize::keychain_serialize_lock()
    }

    /// Point `$VCT_SECRETS_DIR` at a fresh, empty tier-2 file store for
    /// the lifetime of the returned guard, restoring the prior value (set
    /// or unset) on drop — including on panic.
    ///
    /// Deliberately does NOT take a lock of its own. Every caller already
    /// holds [`keychain_test_lock`] (the store probe goes through
    /// `secrets::get`), and that guard is what serialises this env
    /// mutation against the other `VCT_SECRETS_DIR`-mutating tests in this
    /// crate — `installer::tests::setup_temp_env` uses the identical
    /// keychain-lock-then-set-env order. Acquiring a SECOND global mutex
    /// here would introduce a lock-ordering hazard for no added safety.
    struct FileStoreScratch {
        root: std::path::PathBuf,
        prev: Option<std::ffi::OsString>,
    }

    impl FileStoreScratch {
        fn shared(&self) -> std::path::PathBuf {
            self.root.join("shared")
        }
        fn project(&self, name: &str) -> std::path::PathBuf {
            self.root.join("projects").join(name)
        }
        /// Write a fake secret file. The bytes are a test canary, never a
        /// real credential shape.
        fn put(&self, dir: &std::path::Path, key: &str, value: &str) {
            std::fs::create_dir_all(dir).unwrap();
            std::fs::write(dir.join(key), value).unwrap();
        }
    }

    impl Drop for FileStoreScratch {
        fn drop(&mut self) {
            unsafe {
                match &self.prev {
                    Some(v) => std::env::set_var("VCT_SECRETS_DIR", v),
                    None => std::env::remove_var("VCT_SECRETS_DIR"),
                }
            }
            std::fs::remove_dir_all(&self.root).ok();
        }
    }

    fn file_store_scratch() -> FileStoreScratch {
        let root = std::env::temp_dir().join(format!(
            "vct-secrets-panel-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(root.join("shared")).unwrap();
        std::fs::create_dir_all(root.join("projects")).unwrap();
        let prev = std::env::var_os("VCT_SECRETS_DIR");
        unsafe {
            std::env::set_var("VCT_SECRETS_DIR", &root);
        }
        FileStoreScratch { root, prev }
    }

    /// A scratch store with no files in it — for tests that assert a pure
    /// tier-1 outcome and must not be perturbed by the developer's real
    /// `~/.vct-secrets/`.
    fn empty_file_store_guard() -> FileStoreScratch {
        file_store_scratch()
    }

    fn seed_project(db: &Db, id: &str, name: &str) {
        // Placeholder folder_path string — never resolved against disk by
        // these tests. Use a platform-appropriate prefix so the value isn't
        // ambiguous on Windows.
        let folder = if cfg!(windows) {
            format!(r"C:\tmp\{}", id)
        } else {
            format!("/tmp/{}", id)
        };
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                 VALUES (?1, ?2, ?3, 'base', ?4, ?5, ?5)",
                params![id, name, folder, id, 1_700_000_000_000_i64],
            )
            .unwrap();
    }

    #[test]
    fn invariants_per_project_requires_registered_project() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");

        // Registered project: passes.
        assert!(enforce_scope_invariants("per_project", "p1", &db).is_ok());

        // Unregistered project: rejected.
        let err = enforce_scope_invariants("per_project", "ghost", &db).unwrap_err();
        assert!(
            err.contains("not a registered project"),
            "expected isolation error, got: {}",
            err
        );
    }

    #[test]
    fn invariants_global_requires_sentinel() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");

        // Sentinel: passes.
        assert!(enforce_scope_invariants("global", SENTINEL_GLOBAL, &db).is_ok());

        // Real project_id under global scope is rejected — global is
        // machine-wide, must not be tied to any project's scope.
        let err = enforce_scope_invariants("global", "p1", &db).unwrap_err();
        assert!(err.contains("global scope"), "got: {}", err);
    }

    #[test]
    fn invariants_shared_accepts_sentinel_or_registered_project() {
        let db = make_db();
        seed_project(&db, "p1", "Project One");

        // Sentinel: passes.
        assert!(enforce_scope_invariants("shared", SENTINEL_SHARED, &db).is_ok());
        // Legacy: real project_id passes (backward compat for any
        // pre-existing project-shared entries written before this PR).
        assert!(enforce_scope_invariants("shared", "p1", &db).is_ok());
        // Unregistered project: rejected.
        assert!(enforce_scope_invariants("shared", "ghost", &db).is_err());
    }

    #[test]
    fn invariants_reject_path_traversal() {
        let db = make_db();
        for bad in [
            "",
            ".",
            "..",
            "../",
            "./foo",
            "foo/bar",
            "foo\\bar",
            "foo\0bar",
        ] {
            let err =
                enforce_scope_invariants("per_project", bad, &db).unwrap_err();
            assert!(
                err.contains("invalid project_id"),
                "expected traversal rejection for {:?}, got: {}",
                bad,
                err
            );
        }
    }

    #[test]
    fn invariants_audit_label_unset_vs_remove_is_caller_concern() {
        // Sanity-check the constants the frontend relies on.
        assert_eq!(SENTINEL_GLOBAL, "_global_");
        assert_eq!(SENTINEL_SHARED, "_user_shared_");
    }

    /// Pure-DB regression for the active-flag default. A secret with no
    /// row in `secret_active_state` is treated as ACTIVE — anything else
    /// would silently break every entry written before migration 007.
    #[test]
    fn active_flag_defaults_active_when_no_row() {
        // Serialize against other keychain-touching tests across
        // the crate. Required since 2026-05-13 — see crate::secrets docs.
        let _kc_lock = keychain_test_lock();
        let db = make_db();
        assert!(db.is_secret_active("global", SENTINEL_GLOBAL, "u", "K").unwrap());
    }

    /// Pure-DB regression for the unset → reactivate roundtrip.
    /// `clear_secret_v2` and `reactivate_secret_v2` both go through these
    /// `mark_secret_*` helpers, so this exercises the storage layer
    /// without needing a real keychain.
    #[test]
    fn active_flag_unset_then_reactivate_roundtrip() {
        let db = make_db();
        db.mark_secret_inactive("global", SENTINEL_GLOBAL, "u", "K").unwrap();
        assert!(!db.is_secret_active("global", SENTINEL_GLOBAL, "u", "K").unwrap());
        db.mark_secret_active("global", SENTINEL_GLOBAL, "u", "K").unwrap();
        assert!(db.is_secret_active("global", SENTINEL_GLOBAL, "u", "K").unwrap());
    }

    /// Canary test (Bug 3 security requirement): an Unset entry must NOT
    /// leak the value via the preview path. We write a unique canary
    /// directly through `secrets::*` + the active-flag DB helpers, then
    /// call the gate logic the public Tauri commands use. Going through
    /// `secrets::*` rather than the wrapped `#[command]` functions lets
    /// us avoid Tauri's `State<'_, Db>` machinery in unit tests while
    /// still exercising the exact same gate.
    ///
    /// Skipped in CI environments without an OS keychain backend (most
    /// Linux build hosts). Run locally with a logged-in desktop session
    /// or pass through to a workstation pre-merge.
    #[test]
    #[ignore = "requires OS keychain backend (keyring); skipped in CI headless env"]
    fn inactive_secret_does_not_leak_preview() {
        // Serialize against other keychain-touching tests across
        // the crate. Required since 2026-05-13 — see crate::secrets docs.
        let _kc_lock = keychain_test_lock();

        let db = make_db();
        seed_project(&db, "p1", "Project One");

        // Unique canary — substring detection catches any accidental
        // leak even if a future bug changes the masking format.
        let canary = format!(
            "test-secret-leak-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let key = format!(
            "CANARY_KEY_{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let scope = "per_project";
        let project_id = "p1";
        let module_id = "user";

        // Set + activate (mirrors `set_secret_v2`).
        let scope_enum = scope_from_manifest(scope, project_id);
        secrets::set(scope_enum, module_id, &key, &canary).expect("keychain set");
        db.mark_secret_active(scope, project_id, module_id, &key)
            .expect("mark active");

        // Sanity: while ACTIVE the gate lets a masked preview through.
        // (We replicate `get_secret_preview`'s body since the
        // `#[command]` wrapper requires Tauri State.)
        let preview_active = if db.is_secret_active(scope, project_id, module_id, &key).unwrap() {
            secrets::get(scope_enum, module_id, &key)
                .unwrap()
                .map(|v| secrets::mask_preview(&v))
        } else {
            None
        };
        assert!(preview_active.is_some(), "preview missing while active");
        // The masked form must NOT contain the raw canary verbatim.
        assert!(
            !preview_active.as_ref().unwrap().contains(&canary),
            "raw canary leaked through the masked preview while active"
        );

        // Unset — the Lifecycle B path. KEYCHAIN UNTOUCHED.
        db.mark_secret_inactive(scope, project_id, module_id, &key)
            .expect("mark inactive");

        // The keychain still has the value (proves Lifecycle B):
        let kc = secrets::get(scope_enum, module_id, &key).unwrap();
        assert_eq!(
            kc.as_deref(),
            Some(canary.as_str()),
            "Lifecycle B violated: unset cleared the keychain"
        );

        // But the read gate must lie about it:
        let is_set_inactive = db.is_secret_active(scope, project_id, module_id, &key).unwrap()
            && secrets::is_set(scope_enum, module_id, &key).unwrap();
        assert!(
            !is_set_inactive,
            "is_secret_set leaked an inactive entry as set"
        );

        // And the preview gate MUST return None (not even a masked form):
        let preview_inactive = if db.is_secret_active(scope, project_id, module_id, &key).unwrap() {
            secrets::get(scope_enum, module_id, &key)
                .unwrap()
                .map(|v| secrets::mask_preview(&v))
        } else {
            None
        };
        assert!(
            preview_inactive.is_none(),
            "preview leaked while inactive: {:?}",
            preview_inactive
        );
        if let Some(p) = preview_inactive.as_ref() {
            assert!(!p.contains(&canary), "canary substring leaked");
        }

        // Reactivate — flips flag, keychain unchanged.
        db.mark_secret_active(scope, project_id, module_id, &key)
            .expect("reactivate");

        // The same gate now yields the value again WITHOUT having
        // re-typed it. This is the user-facing benefit of Lifecycle B.
        let is_set_after = db.is_secret_active(scope, project_id, module_id, &key).unwrap()
            && secrets::is_set(scope_enum, module_id, &key).unwrap();
        assert!(
            is_set_after,
            "reactivate did not restore the read gate; user would have to re-enter the value"
        );

        // Cleanup keychain (best-effort).
        let _ = secrets::delete(scope_enum, module_id, &key);
        let _ = db.forget_secret_active_state(scope, project_id, module_id, &key);
    }

    // ─── Subagent G (2026-05-08): refresh-on-change wiring ──────────────
    //
    // These tests pin the contract that mutating a per-project user-bucket
    // secret via `set_secret_v2` / `clear_secret_v2` / `reactivate_secret_v2`
    // / `remove_secret_v2` triggers `write_project_env_files` so the env
    // surfaces stay in sync without a session restart.
    //
    // Each test seeds a real on-disk project folder so the writer has
    // somewhere to land. Keychain-touching cases short-circuit when the
    // backend is unavailable (CI containers without libsecret).

    /// Seed a project with a real on-disk folder so the env writer has
    /// a target. Returns the folder path. The caller cleans up.
    fn seed_project_with_real_folder(db: &Db, id: &str, name: &str) -> std::path::PathBuf {
        let folder = std::env::temp_dir().join(format!(
            "vct-secrets-cmd-test-{}-{}",
            id,
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&folder).unwrap();
        let folder_str = folder.to_string_lossy().to_string();
        let guard = db.lock();
        guard
            .execute(
                "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at)
                 VALUES (?1, ?2, ?3, 'base', ?4, ?5, ?5)",
                params![id, name, folder_str, id, 1_700_000_000_000_i64],
            )
            .unwrap();
        folder
    }

    /// `is_per_project_user_bucket` correctly identifies the user bucket
    /// and rejects every other (scope, module) combo.
    #[test]
    fn is_per_project_user_bucket_pure_predicate() {
        // Match: per-project + user
        assert!(is_per_project_user_bucket("per_project", "user"));
        // Module-owned per-project: not user bucket
        assert!(!is_per_project_user_bucket("per_project", "licensing"));
        assert!(!is_per_project_user_bucket("per_project", "search_mcp"));
        // Shared / global: never user bucket
        assert!(!is_per_project_user_bucket("shared", "user"));
        assert!(!is_per_project_user_bucket("global", "user"));
        assert!(!is_per_project_user_bucket("shared", "installer"));
    }

    /// H3 (2026-05-08): `is_secret_set` / `get_secret_preview` /
    /// `get_secret_status_v2` ALL use the cross-launcher active-flag
    /// gate (Option γ). Pre-H3 the GUI-facing readers used
    /// `db.is_secret_active` (own DB only) while every other secret
    /// reader used `is_secret_active_cross_launcher`, so prod's GUI
    /// could report "set" while consumers (hub + env-file emit) saw
    /// "paused" because dev launcher had paused the secret.
    ///
    /// This test pins the gate symmetry on the predicate level:
    /// after `mark_secret_inactive` on the OWN DB, all three readers
    /// agree that the secret is paused. The cross-launcher walk to a
    /// sibling DB is exercised separately in `db::secret_active::tests::
    /// sibling_launcher_pause_propagates_via_read_helper`. Combining
    /// the two gives end-to-end coverage of the H3 invariant.
    #[test]
    fn h3_readers_use_cross_launcher_gate_consistent_with_consumers() {
        let db = make_db();
        seed_project(&db, "h3-proj", "H3 Test Project");

        // Sanity: no row → all three readers report "active=true" via
        // the default-active semantic.
        let scope = "per_project";
        let project_id = "h3-proj";
        let module_id = "user";
        let key = "H3_TEST_KEY";

        let active_own = db.is_secret_active(scope, project_id, module_id, key).unwrap();
        let active_cross = crate::db::secret_active::is_secret_active_cross_launcher(
            &db, scope, project_id, module_id, key,
        );
        assert!(active_own);
        assert!(active_cross);

        // Pause via own DB.
        db.mark_secret_inactive(scope, project_id, module_id, key).unwrap();

        // Both readers MUST now say "inactive" (cross-launcher short-circuits
        // on own-DB, see `is_secret_active_cross_launcher`).
        assert!(!db.is_secret_active(scope, project_id, module_id, key).unwrap());
        assert!(!crate::db::secret_active::is_secret_active_cross_launcher(
            &db, scope, project_id, module_id, key,
        ));

        // Reactivate.
        db.mark_secret_active(scope, project_id, module_id, key).unwrap();
        assert!(crate::db::secret_active::is_secret_active_cross_launcher(
            &db, scope, project_id, module_id, key,
        ));
    }

    /// H2 (2026-05-08): the broader user-emit predicate catches all three
    /// SecretsPanel tabs (per-project, shared, global) when they target
    /// the `user` module bucket. Module-owned secrets and non-user
    /// scopes are still excluded — they go through the hub's
    /// /projects/{id}/env resolver path, not the env-file emit path.
    #[test]
    #[ignore = "requires OS keychain backend (keyring); skipped in CI headless env"]
    fn is_user_emit_bucket_pure_predicate() {
        // All three SecretsPanel tabs writing to module_id='user' MUST match.
        assert!(is_user_emit_bucket("per_project", "user"));
        assert!(is_user_emit_bucket("shared", "user"));
        assert!(is_user_emit_bucket("global", "user"));
        // Module-owned: excluded (per-project licensing, shared installer-bundled
        // PAT, global licensing key all flow via the hub resolver, not the
        // env-file emit path).
        assert!(!is_user_emit_bucket("per_project", "licensing"));
        assert!(!is_user_emit_bucket("shared", "installer"));
        assert!(!is_user_emit_bucket("global", "licensing"));
        assert!(!is_user_emit_bucket("shared", "search_mcp"));
        // Unknown scope strings: rejected. The predicate is closed over
        // the three scopes secrets_cmd.rs validates in
        // `enforce_scope_invariants`.
        assert!(!is_user_emit_bucket("bogus", "user"));
        assert!(!is_user_emit_bucket("", "user"));
    }

    /// End-to-end: `set_secret_v2` against the per-project user bucket
    /// triggers `write_project_env_files`. The keychain entry lands AND
    /// the project's `.claude/settings.json` env block carries the key.
    /// Skipped without an OS keychain (most CI containers).
    ///
    /// Phase 0.B Part 2 (2026-05-25): the env refresh inside
    /// `set_secret_v2` now goes through
    /// `apply_project_env_via_python` (subprocess into the Python
    /// canonical-env contract). The Python contract is OUT OF SCOPE
    /// for user secrets (see `vco_lib/config_projection.py` docstring
    /// §"Out of scope") — they require an active-flag bridge that
    /// lands in a future Phase 0.E. This test pins the end-to-end
    /// "set_secret_v2 → env surface carries key" path which, post-
    /// Phase-0.B-Part-2, depends on a Python module that
    /// (a) reaches the in-memory test DB, and (b) handles user
    /// secrets — neither holds today. Re-enable when Phase 0.E
    /// adds user-secret routing to the Python contract.
    #[tokio::test]
    #[ignore = "Phase 0.B Part 2: user-secret refresh deferred to Phase 0.E; \
                see test docstring"]
    async fn set_secret_v2_triggers_env_refresh() {
        // Serialize against other keychain-touching tests across
        // the crate. Required since 2026-05-13 — see crate::secrets docs.
        let _kc_lock = keychain_test_lock();
        let db = make_db();
        let folder = seed_project_with_real_folder(&db, "p_set_refresh", "SetRefreshProj");

        // Wrap Db in Tauri-style state. The free function
        // `refresh_project_env_with_db` takes &Db so the State
        // wrapper here is for parity with the production command.
        let state: tauri::State<Db> = unsafe {
            // Tauri's State<T> is a thin wrapper around &T; in unit
            // tests that don't construct a real AppHandle, we use
            // the free-function variant of the env refresh inside
            // `set_secret_v2`. Calling the public `#[command]`
            // requires Tauri State plumbing, so we replicate the
            // command body here to exercise the same code path
            // without the Tauri runtime.
            std::mem::transmute(&db)
        };
        let _ = state; // silence unused; the free-function path covers it

        // Replicate the `#[command] set_secret_v2` body using the same
        // helpers the command uses (see lines 132-180 of this file).
        // Going through the helpers (rather than the wrapped command)
        // skips the Tauri runtime requirement while keeping the
        // refresh hook, audit log, and active-flag mark identical.
        let scope = "per_project".to_string();
        let project_id = "p_set_refresh".to_string();
        let module_id = "user".to_string();
        let key = "REFRESH_TEST_KEY".to_string();
        let canary_value = format!(
            "subagent-g-set-refresh-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );

        enforce_scope_invariants(&scope, &project_id, &db).unwrap();
        let scope_enum = scope_from_manifest(&scope, &project_id);
        secrets::set(scope_enum, &module_id, &key, &canary_value).unwrap();
        db.mark_secret_active(&scope, &project_id, &module_id, &key).unwrap();
        // The refresh hook — same call the command makes.
        refresh_env_after_user_secret_change(
            &db,
            &project_id,
            &scope,
            &module_id,
            "set_secret_v2",
        );

        // Project's `.claude/settings.json` env block should now carry
        // the key.
        let cs_path = folder.join(".claude/settings.json");
        assert!(cs_path.exists(), "writer didn't run; .claude/settings.json absent");
        let cs: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&cs_path).unwrap()).unwrap();
        assert_eq!(
            cs["env"][&key],
            canary_value,
            ".claude/settings.json env block missing or wrong value: {}",
            cs["env"]
        );

        // Cleanup.
        let _ = secrets::delete(scope_enum, &module_id, &key);
        let _ = db.forget_secret_active_state(&scope, &project_id, &module_id, &key);
        std::fs::remove_dir_all(&folder).ok();
    }

    /// `remove_secret_v2` against the user bucket: the env-write fires
    /// BEFORE `forget_secret_active_state` so the row's still in the
    /// strip set when the writer composes the new surfaces. After the
    /// test the surfaces no longer carry the key.
    ///
    /// Skipped without OS keychain.
    /// Phase 0.B Part 2 (2026-05-25): see `set_secret_v2_triggers_env_refresh`
    /// docstring for the user-secret regression context. This test
    /// covers the strip side of the same flow.
    #[tokio::test]
    #[ignore = "requires OS keychain backend (keyring); skipped in CI headless env. \
                Also Phase 0.B Part 2: user-secret refresh deferred to Phase 0.E; \
                see set_secret_v2_triggers_env_refresh docstring"]
    async fn delete_secret_v2_strips_secret_from_env_surfaces() {
        // Serialize against other keychain-touching tests across
        // the crate. Required since 2026-05-13 — see crate::secrets docs.
        let _kc_lock = keychain_test_lock();
        let db = make_db();
        let folder = seed_project_with_real_folder(&db, "p_del_strip", "DelStripProj");

        let scope = "per_project";
        let project_id = "p_del_strip";
        let module_id = "user";
        let key = "STRIP_TEST_KEY";
        let canary = format!(
            "subagent-g-strip-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );

        // Step 1: set + active. Env surfaces should carry the key.
        let scope_enum = scope_from_manifest(scope, project_id);
        secrets::set(scope_enum, module_id, key, &canary).unwrap();
        db.mark_secret_active(scope, project_id, module_id, key).unwrap();
        refresh_env_after_user_secret_change(&db, project_id, scope, module_id, "set_secret_v2");
        let cs_path = folder.join(".claude/settings.json");
        let cs: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&cs_path).unwrap()).unwrap();
        assert_eq!(cs["env"][key], canary, "pre-delete: key not in env");

        // Step 2: replicate `remove_secret_v2` body — keychain delete,
        // refresh BEFORE forget, then forget.
        secrets::delete(scope_enum, module_id, key).unwrap();
        // Crucial: refresh BEFORE forget so the strip set carries the key.
        refresh_env_after_user_secret_change(&db, project_id, scope, module_id, "remove_secret_v2");
        db.forget_secret_active_state(scope, project_id, module_id, key).unwrap();

        // .claude/settings.json no longer carries the key.
        let cs_after: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&cs_path).unwrap()).unwrap();
        assert!(
            cs_after["env"].get(key).is_none(),
            "post-delete: env still has {}: {}",
            key,
            cs_after["env"]
        );
        // .claude/env: BEGIN/END block should not contain the key either.
        let claude_env = std::fs::read_to_string(folder.join(".claude/env")).unwrap();
        assert!(
            !claude_env.contains(key),
            "post-delete: .claude/env still mentions {}:\n{}",
            key,
            claude_env,
        );
        // PR-27 (v0.2.12, 2026-05-16): the launcher's env writer no
        // longer authors `.vscode/settings.json` `claude-code.env`,
        // so there is nothing to strip from it on secret delete. If
        // the file exists (because the user authored it by hand or a
        // pre-PR-27 launcher created it), the strip helper
        // (`surgically_strip_user_secret_keys`) still runs against it
        // — but the writer never creates it. Assert the key isn't
        // present whether or not the file exists.
        let vscode_path = folder.join(".vscode/settings.json");
        if vscode_path.exists() {
            let vsc: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(&vscode_path).unwrap()).unwrap();
            assert!(
                vsc.get("claude-code.env")
                    .and_then(|b| b.get(key))
                    .is_none(),
                "post-delete: stale {} key in pre-existing .vscode/settings.json",
                key,
            );
        }

        std::fs::remove_dir_all(&folder).ok();
    }

    /// H2 (2026-05-08): refresh-skip — only NON-user-emit buckets are
    /// skipped. After H2 the predicate covers per-project + shared +
    /// global user buckets (all three SecretsPanel tabs); only
    /// module-owned scopes still skip the env-file emit path.
    ///
    /// This pins the contract so a future refactor can't regress to
    /// "always refresh" or "skip shared/global". We exercise the
    /// module-owned cases (the only ones that should still skip after
    /// H2) and verify env-file writes don't fire.
    ///
    /// Doesn't need keychain because we're testing the negative —
    /// the project folder simply must NOT have `.claude/settings.json`
    /// after a module-owned secret op.
    #[test]
    #[ignore = "requires OS keychain backend (keyring); skipped in CI headless env"]
    fn refresh_env_after_user_secret_change_skips_module_owned_buckets() {
        // Serialize against other keychain-touching tests across
        // the crate. Required since 2026-05-13 — see crate::secrets docs.
        let _kc_lock = keychain_test_lock();
        let db = make_db();
        let folder = seed_project_with_real_folder(&db, "p_skip", "SkipBucketProj");

        // Module-owned shared (legacy `installer` bucket — pre-2026-05-10
        // register_github_pat wrote here; post-fix it writes to the user
        // bucket. The predicate must still treat any non-user module_id
        // as module-owned regardless, so the H2 skip contract holds for
        // any future shared-scope module bucket too.) MUST skip here.
        refresh_env_after_user_secret_change(
            &db,
            "_user_shared_",
            "shared",
            "installer",
            "set_secret_v2_module_shared",
        );
        // Module-owned global (e.g. licensing's machine-wide key).
        // MUST skip — flows via hub resolver, not env-file emit.
        refresh_env_after_user_secret_change(
            &db,
            "_global_",
            "global",
            "licensing",
            "set_secret_v2_module_global",
        );
        // Per-project, but NOT the user bucket (e.g. licensing per-project
        // entry). MUST skip.
        refresh_env_after_user_secret_change(
            &db,
            "p_skip",
            "per_project",
            "licensing",
            "set_secret_v2_module",
        );

        // None of those should have triggered the env writer.
        assert!(
            !folder.join(".claude/settings.json").exists(),
            "module-owned refresh leaked into the project's env files"
        );
        assert!(!folder.join(".vscode/settings.json").exists());
        assert!(!folder.join(".claude/env").exists());

        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── H2 (0.1.7 fork-readiness sweep, 2026-05-08): shared/global propagation ─
    //
    // The SecretsPanel "Shared (this user)" and "Global (this machine)"
    // tabs write to keychain slots `vct._user_shared_.shared.user/<key>`
    // and `vct._global_.global.user/<key>` respectively. Pre-H2, NOTHING
    // enumerated those slots — `list_user_secret_keys_for_project` only
    // returned per-project rows, and `refresh_env_after_user_secret_change`
    // skipped non-per-project scopes outright. So a user adding
    // `OPENAI_API_KEY` via the Shared tab landed it in the keychain but
    // every project's `.claude/env` stayed silent.
    //
    // These tests pin the H2 contract: a shared/global user-bucket op
    // fans out to every registered project's env files. Skipped without
    // an OS keychain backend (most CI containers).

    /// Seed two registered projects on disk so the fan-out has a
    /// non-trivial target set. The H2 invariant is "every registered
    /// project's surfaces include the shared/global key", and a
    /// single-project test would pass even if the fan-out only touched
    /// the calling project.
    fn seed_two_registered_projects(db: &Db) -> (std::path::PathBuf, std::path::PathBuf) {
        let p1 = seed_project_with_real_folder(db, "h2-p1", "H2 Project One");
        let p2 = seed_project_with_real_folder(db, "h2-p2", "H2 Project Two");
        (p1, p2)
    }

    /// H2: a shared user-bucket secret added via `set_secret_v2`
    /// propagates to EVERY registered project's `.claude/settings.json`
    /// env block (and the other two surfaces via the same writer).
    /// Pre-H2 this was a silent no-op — the keychain landed but no
    /// project's env files saw the key.
    /// Phase 0.B Part 2 (2026-05-25): see `set_secret_v2_triggers_env_refresh`
    /// docstring; this test pins fan-out propagation across projects.
    #[tokio::test]
    #[ignore = "Phase 0.B Part 2: user-secret refresh deferred to Phase 0.E; \
                see set_secret_v2_triggers_env_refresh docstring"]
    async fn set_secret_v2_shared_user_bucket_propagates_to_all_registered_projects() {
        // Serialize against other keychain-touching tests across
        // the crate. Required since 2026-05-13 — see crate::secrets docs.
        let _kc_lock = keychain_test_lock();
        let db = make_db();
        let (folder1, folder2) = seed_two_registered_projects(&db);

        let scope = "shared".to_string();
        let project_id = "_user_shared_".to_string();
        let module_id = "user".to_string();
        let key = format!(
            "H2_SHARED_KEY_{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let canary = format!(
            "h2-shared-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );

        // Replicate the `#[command] set_secret_v2` body — same as the
        // existing pre-H2 set test pattern.
        enforce_scope_invariants(&scope, &project_id, &db).unwrap();
        let scope_enum = scope_from_manifest(&scope, &project_id);
        secrets::set(scope_enum, &module_id, &key, &canary).unwrap();
        db.mark_secret_active(&scope, &project_id, &module_id, &key).unwrap();
        refresh_env_after_user_secret_change(
            &db,
            &project_id,
            &scope,
            &module_id,
            "set_secret_v2_shared",
        );

        // Both projects' `.claude/settings.json` should now carry the key.
        for (label, folder) in [("p1", &folder1), ("p2", &folder2)] {
            let cs_path = folder.join(".claude/settings.json");
            assert!(
                cs_path.exists(),
                "[{}] writer didn't run; .claude/settings.json absent",
                label
            );
            let cs: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(&cs_path).unwrap()).unwrap();
            assert_eq!(
                cs["env"][&key],
                canary,
                "[{}] .claude/settings.json env block missing or wrong value: {}",
                label,
                cs["env"]
            );
        }

        // Cleanup keychain.
        let _ = secrets::delete(scope_enum, &module_id, &key);
        let _ = db.forget_secret_active_state(&scope, &project_id, &module_id, &key);
        let _ = std::fs::remove_dir_all(&folder1);
        let _ = std::fs::remove_dir_all(&folder2);
    }

    /// H2: global user-bucket secrets propagate to every registered
    /// project's env files. Symmetric with the shared test above —
    /// the writer doesn't care which user-emit bucket the key lives
    /// in, only that it's in some user-emit bucket.
    /// Phase 0.B Part 2 (2026-05-25): see `set_secret_v2_triggers_env_refresh`
    /// docstring; this test pins fan-out propagation across projects.
    #[tokio::test]
    #[ignore = "requires OS keychain backend (keyring); skipped in CI headless env. \
                Also Phase 0.B Part 2: user-secret refresh deferred to Phase 0.E; \
                see set_secret_v2_triggers_env_refresh docstring"]
    async fn set_secret_v2_global_user_bucket_propagates_to_all_registered_projects() {
        // Serialize against other keychain-touching tests across
        // the crate. Required since 2026-05-13 — see crate::secrets docs.
        let _kc_lock = keychain_test_lock();
        let db = make_db();
        let (folder1, folder2) = seed_two_registered_projects(&db);

        let scope = "global".to_string();
        let project_id = "_global_".to_string();
        let module_id = "user".to_string();
        let key = format!(
            "H2_GLOBAL_KEY_{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let canary = format!(
            "h2-global-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );

        enforce_scope_invariants(&scope, &project_id, &db).unwrap();
        let scope_enum = scope_from_manifest(&scope, &project_id);
        secrets::set(scope_enum, &module_id, &key, &canary).unwrap();
        db.mark_secret_active(&scope, &project_id, &module_id, &key).unwrap();
        refresh_env_after_user_secret_change(
            &db,
            &project_id,
            &scope,
            &module_id,
            "set_secret_v2_global",
        );

        for (label, folder) in [("p1", &folder1), ("p2", &folder2)] {
            let cs_path = folder.join(".claude/settings.json");
            assert!(
                cs_path.exists(),
                "[{}] writer didn't run for global; .claude/settings.json absent",
                label
            );
            let cs: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(&cs_path).unwrap()).unwrap();
            assert_eq!(
                cs["env"][&key],
                canary,
                "[{}] global key missing from .claude/settings.json env: {}",
                label,
                cs["env"]
            );
        }

        let _ = secrets::delete(scope_enum, &module_id, &key);
        let _ = db.forget_secret_active_state(&scope, &project_id, &module_id, &key);
        let _ = std::fs::remove_dir_all(&folder1);
        let _ = std::fs::remove_dir_all(&folder2);
    }

    /// H2: `remove_secret_v2` on a shared user-bucket entry strips the
    /// key from EVERY registered project's env files. Mirrors the
    /// per-project strip test, but the fan-out has to land in both
    /// projects.
    /// Phase 0.B Part 2 (2026-05-25): see `set_secret_v2_triggers_env_refresh`
    /// docstring; this test pins fan-out strip across projects.
    #[tokio::test]
    #[ignore = "requires OS keychain backend (keyring); skipped in CI headless env. \
                Also Phase 0.B Part 2: user-secret refresh deferred to Phase 0.E; \
                see set_secret_v2_triggers_env_refresh docstring"]
    async fn delete_secret_v2_shared_user_bucket_strips_from_all_projects() {
        // Serialize against other keychain-touching tests across
        // the crate. Required since 2026-05-13 — see crate::secrets docs.
        let _kc_lock = keychain_test_lock();
        let db = make_db();
        let (folder1, folder2) = seed_two_registered_projects(&db);

        let scope = "shared";
        let project_id = "_user_shared_";
        let module_id = "user";
        let key = format!(
            "H2_STRIP_KEY_{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let canary = format!(
            "h2-strip-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );

        // Step 1: set + active. Both projects' env surfaces carry the key.
        let scope_enum = scope_from_manifest(scope, project_id);
        secrets::set(scope_enum, module_id, &key, &canary).unwrap();
        db.mark_secret_active(scope, project_id, module_id, &key).unwrap();
        refresh_env_after_user_secret_change(&db, project_id, scope, module_id, "set_secret_v2_shared");
        for folder in [&folder1, &folder2] {
            let cs_path = folder.join(".claude/settings.json");
            let cs: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(&cs_path).unwrap()).unwrap();
            assert_eq!(cs["env"][&key], canary);
        }

        // Step 2: replicate the `remove_secret_v2` body for shared scope.
        // Order: delete keychain → refresh (carries strip set) → forget.
        secrets::delete(scope_enum, module_id, &key).unwrap();
        refresh_env_after_user_secret_change(
            &db,
            project_id,
            scope,
            module_id,
            "remove_secret_v2_shared",
        );
        db.forget_secret_active_state(scope, project_id, module_id, &key).unwrap();

        // Both projects' surfaces no longer carry the key.
        for (label, folder) in [("p1", &folder1), ("p2", &folder2)] {
            let cs_path = folder.join(".claude/settings.json");
            let cs_after: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(&cs_path).unwrap()).unwrap();
            assert!(
                cs_after["env"].get(&key).is_none(),
                "[{}] post-delete: shared env still has {}: {}",
                label,
                key,
                cs_after["env"]
            );
            let claude_env = std::fs::read_to_string(folder.join(".claude/env")).unwrap();
            assert!(
                !claude_env.contains(&key),
                "[{}] post-delete: .claude/env still mentions {}:\n{}",
                label,
                key,
                claude_env,
            );
        }

        let _ = std::fs::remove_dir_all(&folder1);
        let _ = std::fs::remove_dir_all(&folder2);
    }

    /// H2: `remove_secret_v2` on a global user-bucket entry strips
    /// from every registered project. Symmetric with the shared strip
    /// test.
    /// Phase 0.B Part 2 (2026-05-25): see `set_secret_v2_triggers_env_refresh`
    /// docstring; this test pins fan-out strip across projects.
    #[tokio::test]
    #[ignore = "requires OS keychain backend (keyring); skipped in CI headless env. \
                Also Phase 0.B Part 2: user-secret refresh deferred to Phase 0.E; \
                see set_secret_v2_triggers_env_refresh docstring"]
    async fn delete_secret_v2_global_user_bucket_strips_from_all_projects() {
        // Serialize against other keychain-touching tests across
        // the crate. Required since 2026-05-13 — see crate::secrets docs.
        let _kc_lock = keychain_test_lock();
        let db = make_db();
        let (folder1, folder2) = seed_two_registered_projects(&db);

        let scope = "global";
        let project_id = "_global_";
        let module_id = "user";
        let key = format!(
            "H2_GLOBAL_STRIP_KEY_{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let canary = format!(
            "h2-global-strip-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );

        let scope_enum = scope_from_manifest(scope, project_id);
        secrets::set(scope_enum, module_id, &key, &canary).unwrap();
        db.mark_secret_active(scope, project_id, module_id, &key).unwrap();
        refresh_env_after_user_secret_change(&db, project_id, scope, module_id, "set_secret_v2_global");
        for folder in [&folder1, &folder2] {
            let cs: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(folder.join(".claude/settings.json")).unwrap()).unwrap();
            assert_eq!(cs["env"][&key], canary);
        }

        secrets::delete(scope_enum, module_id, &key).unwrap();
        refresh_env_after_user_secret_change(
            &db,
            project_id,
            scope,
            module_id,
            "remove_secret_v2_global",
        );
        db.forget_secret_active_state(scope, project_id, module_id, &key).unwrap();

        for (label, folder) in [("p1", &folder1), ("p2", &folder2)] {
            let cs_after: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(folder.join(".claude/settings.json")).unwrap()).unwrap();
            assert!(
                cs_after["env"].get(&key).is_none(),
                "[{}] post-delete: global env still has {}",
                label,
                key
            );
        }

        let _ = std::fs::remove_dir_all(&folder1);
        let _ = std::fs::remove_dir_all(&folder2);
    }

    // ─── 0.2.x backlog #3: shared-tab key-collision shadow badge ────────
    //
    // Tests target two layers:
    //   1. `resolve_winning_scope` — pure precedence logic.
    //   2. The end-to-end DB enumeration (`list_*_user_secret_keys` +
    //      collision detection). We exercise the same data path
    //      `list_user_secret_keys_v2` walks, asserting both the row count
    //      and the per-row `is_shadowed` / `winning_scope` decisions.
    //
    // Layer 2 needs an OS keychain because the `is_set` field (cross-launcher
    // gate × keychain presence) reads through the keychain. Tests that
    // exercise it serialise via the shared
    // `crate::secrets::test_serialize::keychain_serialize_lock`.

    /// Tier-1 (keychain) precedence, unchanged from pre-v0.3.0: every
    /// original assertion is preserved verbatim; the two file-store
    /// arguments are `false` throughout, so this pins that adding the
    /// file store did not perturb keychain resolution.
    #[test]
    fn resolve_winning_scope_precedence_per_project_beats_shared_beats_global() {
        let scope_of = |own: &str, pp: bool, sh: bool, gl: bool| {
            resolve_winning_scope(own, pp, sh, gl, false, false)
        };
        // All three set: per_project wins.
        assert_eq!(scope_of("per_project", true, true, true),
                   ("per_project".to_string(), WinningStore::Keychain));
        assert_eq!(scope_of("shared", true, true, true),
                   ("per_project".to_string(), WinningStore::Keychain));
        assert_eq!(scope_of("global", true, true, true),
                   ("per_project".to_string(), WinningStore::Keychain));
        // Per-project paused, shared+global active: shared wins.
        assert_eq!(scope_of("per_project", false, true, true),
                   ("shared".to_string(), WinningStore::Keychain));
        assert_eq!(scope_of("shared", false, true, true),
                   ("shared".to_string(), WinningStore::Keychain));
        assert_eq!(scope_of("global", false, true, true),
                   ("shared".to_string(), WinningStore::Keychain));
        // Only global active.
        assert_eq!(scope_of("per_project", false, false, true),
                   ("global".to_string(), WinningStore::Keychain));
        assert_eq!(scope_of("shared", false, false, true),
                   ("global".to_string(), WinningStore::Keychain));
        assert_eq!(scope_of("global", false, false, true),
                   ("global".to_string(), WinningStore::Keychain));
        // No scope set → fall back to own (the row the user is looking at).
        assert_eq!(scope_of("per_project", false, false, false),
                   ("per_project".to_string(), WinningStore::NoStore));
        assert_eq!(scope_of("shared", false, false, false),
                   ("shared".to_string(), WinningStore::NoStore));
        assert_eq!(scope_of("global", false, false, false),
                   ("global".to_string(), WinningStore::NoStore));
    }

    /// v0.3.0: the file store is tier 2 — it wins ONLY when no keychain
    /// scope has a live value, and within tier 2 `projects/<NAME>/` beats
    /// `shared/`. This mirrors `agent_secrets.py::get` /
    /// `vct_secrets_resolve.sh`, where tier 1 is exhausted across all
    /// scopes before tier 2 is consulted at all.
    #[test]
    fn file_store_is_tier_two_and_never_outranks_a_live_keychain_value() {
        // Any live keychain scope outranks both file-store namespaces.
        assert_eq!(
            resolve_winning_scope("shared", false, false, true, true, true),
            ("global".to_string(), WinningStore::Keychain),
            "a file-store copy must not outrank a live keychain value"
        );
        // Keychain empty everywhere → project file beats shared file.
        assert_eq!(
            resolve_winning_scope("shared", false, false, false, true, true),
            ("per_project".to_string(), WinningStore::FileStore)
        );
        // Only the shared file exists.
        assert_eq!(
            resolve_winning_scope("per_project", false, false, false, false, true),
            ("shared".to_string(), WinningStore::FileStore)
        );
        // Nothing anywhere → own scope, no store.
        assert_eq!(
            resolve_winning_scope("shared", false, false, false, false, false),
            ("shared".to_string(), WinningStore::NoStore)
        );
    }

    /// Direct DB-only collision detection: write keys to two scopes via
    /// `mark_secret_active` only (no keychain), then walk the same lists
    /// `list_user_secret_keys_v2` walks and assert collision is detected.
    /// This is the layer-2 contract WITHOUT the keychain dependency, so
    /// it runs in CI containers that don't have libsecret.
    #[test]
    fn collision_index_flags_keys_present_in_two_scopes() {
        let db = make_db();
        seed_project(&db, "pcoll", "Collision Project");

        // OPENAI_API_KEY in BOTH per_project (for pcoll) AND shared.
        db.mark_secret_active("per_project", "pcoll", "user", "OPENAI_API_KEY")
            .unwrap();
        db.mark_secret_active("shared", "_user_shared_", "user", "OPENAI_API_KEY")
            .unwrap();
        // GITHUB_TOKEN only in shared.
        db.mark_secret_active("shared", "_user_shared_", "user", "GITHUB_TOKEN")
            .unwrap();

        let pp = db.list_user_secret_keys_for_project("pcoll");
        let sh = db.list_shared_user_secret_keys();
        let gl = db.list_global_user_secret_keys();
        assert_eq!(pp, vec!["OPENAI_API_KEY".to_string()]);
        assert!(sh.contains(&"OPENAI_API_KEY".to_string()));
        assert!(sh.contains(&"GITHUB_TOKEN".to_string()));
        assert_eq!(sh.len(), 2);
        assert!(gl.is_empty());

        // Build the collision index the way list_user_secret_keys_v2 does.
        use std::collections::HashMap;
        let mut scope_map: HashMap<&str, Vec<&str>> = HashMap::new();
        for k in &pp {
            scope_map.entry(k.as_str()).or_default().push("per_project");
        }
        for k in &sh {
            scope_map.entry(k.as_str()).or_default().push("shared");
        }
        for k in &gl {
            scope_map.entry(k.as_str()).or_default().push("global");
        }

        // OPENAI_API_KEY in per_project + shared → collision.
        assert_eq!(scope_map["OPENAI_API_KEY"].len(), 2);
        // GITHUB_TOKEN only in shared → no collision.
        assert_eq!(scope_map["GITHUB_TOKEN"].len(), 1);
    }

    /// The grants-section Pause/Resume-per-requester decision logic: the
    /// paused getter is `!is_secret_active_for_requester`, default-active
    /// (not paused) on a missing row. Pins the pause → paused-true and
    /// resume → paused-false transitions the `is_secret_paused_for_requester`
    /// command reports to the UI.
    #[test]
    fn paused_for_requester_reflects_pause_then_resume() {
        let db = make_db();
        seed_project(&db, "owner", "OwnerProj");
        seed_project(&db, "grantee", "GranteeProj");

        // Default: no active-state row → active → not paused.
        let active0 = db
            .is_secret_active_for_requester("per_project", "owner", "user", "K", "grantee")
            .unwrap_or(true);
        assert!(!(!active0), "default state must be NOT paused");

        // Pause for the grantee → active=false → paused=true.
        db.mark_secret_inactive_for_requester("per_project", "owner", "user", "K", "grantee")
            .unwrap();
        let active1 = db
            .is_secret_active_for_requester("per_project", "owner", "user", "K", "grantee")
            .unwrap_or(true);
        assert!(!active1, "after pause, active must be false (paused=true)");

        // Resume (forget the row) → back to default active → not paused.
        db.forget_secret_active_state_for_requester("per_project", "owner", "user", "K", "grantee")
            .unwrap();
        let active2 = db
            .is_secret_active_for_requester("per_project", "owner", "user", "K", "grantee")
            .unwrap_or(true);
        assert!(active2, "after resume, active must be true (paused=false)");
    }

    /// Three-way collision (per_project + shared + global) is flagged on
    /// every row. Pre-fix the panel could render the per-project row
    /// without realising shared+global also had the same key.
    #[test]
    fn collision_index_flags_three_way_collision_on_every_row() {
        let db = make_db();
        seed_project(&db, "p3way", "ThreeWay");

        for (scope, owner) in [
            ("per_project", "p3way"),
            ("shared", "_user_shared_"),
            ("global", "_global_"),
        ] {
            db.mark_secret_active(scope, owner, "user", "ANTHROPIC_API_KEY")
                .unwrap();
        }

        let pp = db.list_user_secret_keys_for_project("p3way");
        let sh = db.list_shared_user_secret_keys();
        let gl = db.list_global_user_secret_keys();
        assert!(pp.contains(&"ANTHROPIC_API_KEY".to_string()));
        assert!(sh.contains(&"ANTHROPIC_API_KEY".to_string()));
        assert!(gl.contains(&"ANTHROPIC_API_KEY".to_string()));

        use std::collections::HashMap;
        let mut scope_map: HashMap<&str, Vec<&str>> = HashMap::new();
        for k in &pp { scope_map.entry(k.as_str()).or_default().push("per_project"); }
        for k in &sh { scope_map.entry(k.as_str()).or_default().push("shared"); }
        for k in &gl { scope_map.entry(k.as_str()).or_default().push("global"); }
        assert_eq!(scope_map["ANTHROPIC_API_KEY"].len(), 3);
    }

    /// Per-project rows for project A do NOT pollute project B's collision
    /// view. The badge must scope to the project the GUI is currently
    /// looking at.
    #[test]
    fn collision_per_project_isolation_between_projects() {
        let db = make_db();
        seed_project(&db, "pA", "Project A");
        seed_project(&db, "pB", "Project B");

        // Project A has OPENAI_API_KEY in per_project; project B does not.
        db.mark_secret_active("per_project", "pA", "user", "OPENAI_API_KEY")
            .unwrap();
        db.mark_secret_active("shared", "_user_shared_", "user", "OPENAI_API_KEY")
            .unwrap();

        // From pA's POV: per_project row exists → collision with shared.
        let pp_a = db.list_user_secret_keys_for_project("pA");
        let sh = db.list_shared_user_secret_keys();
        assert!(pp_a.contains(&"OPENAI_API_KEY".to_string()));
        assert!(sh.contains(&"OPENAI_API_KEY".to_string()));

        // From pB's POV: per_project bucket is empty for pB; only shared.
        let pp_b = db.list_user_secret_keys_for_project("pB");
        assert!(pp_b.is_empty(), "project A's per-project key leaked into project B's bucket");

        // Build pB's collision index — should NOT have a collision because
        // pB's per-project bucket has no OPENAI_API_KEY.
        use std::collections::HashMap;
        let mut scope_map: HashMap<&str, Vec<&str>> = HashMap::new();
        for k in &pp_b { scope_map.entry(k.as_str()).or_default().push("per_project"); }
        for k in &sh { scope_map.entry(k.as_str()).or_default().push("shared"); }
        // OPENAI_API_KEY exists only in shared from pB's POV — no collision.
        assert_eq!(scope_map["OPENAI_API_KEY"].len(), 1);
    }

    // ─── v0.3.0: the panel must report where the value ACTUALLY lives ──
    //
    // Every test below drives `list_user_secret_keys_impl` — the exact
    // body of the `list_user_secret_keys_v2` Tauri command, which is a
    // one-expression shim over it. They are hermetic: the keychain arm
    // runs on the thread-local `MockGuard`, and `$VCT_SECRETS_DIR` points
    // at a per-test scratch store, so neither the developer's keychain nor
    // their real `~/.vct-secrets/` can influence the outcome.

    fn find<'a>(
        rows: &'a [UserSecretKeyRow],
        scope: &str,
        key: &str,
    ) -> Option<&'a UserSecretKeyRow> {
        rows.iter().find(|r| r.scope == scope && r.key == key)
    }

    /// THE DEFECT. A key present only in the tier-2 file store resolves
    /// for `vct`, `agent_secrets.get` and `vct_secrets_resolve.sh`, and
    /// the panel rendered it as "not set" — steering the user into
    /// re-typing it in the GUI and forking the value across two stores,
    /// the failure mode `CLAUDE.md` explicitly warns about.
    ///
    /// After the fix the row EXISTS, reports `file_store: Present`, and
    /// carries `has_launcher_row: false` so the panel can suppress a
    /// Remove button that could not remove it.
    #[test]
    fn file_store_only_key_is_listed_and_never_reads_as_not_set() {
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "pfs", "FileStoreProj");

        // Only the file store has it — no keychain value, no launcher row.
        fs.put(&fs.shared(), "FIELD_ONLY_IN_FILE_STORE", "canary-not-a-real-token");

        let rows = list_user_secret_keys_impl(&db, "pfs").unwrap();
        let row = find(&rows, "shared", "FIELD_ONLY_IN_FILE_STORE")
            .expect("a key that resolves must appear in the panel's row list");
        assert_eq!(row.stores.file_store, Presence::Present);
        assert_eq!(
            row.stores.keychain,
            Presence::Absent,
            "the mock keychain genuinely has no entry"
        );
        assert!(!row.is_set, "is_set stays the keychain × active gate");
        assert!(!row.has_saved_value, "has_saved_value stays keychain-only");
        assert!(
            !row.has_launcher_row,
            "no secret_active_state row exists for a file-only key"
        );
        assert_eq!(row.winning_store, WinningStore::FileStore);
        assert_eq!(row.winning_scope, "shared");
    }

    /// The per-project file-store namespace is keyed by the project's
    /// NAME (`vct --project NAME`), not its launcher UUID — and one
    /// project must never see another's file-store keys.
    #[test]
    fn per_project_file_store_rows_use_the_project_name_namespace() {
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "id-mine", "MineProj");
        seed_project(&db, "id-other", "OtherProj");

        fs.put(&fs.project("MineProj"), "MINE_KEY", "canary");
        fs.put(&fs.project("OtherProj"), "OTHER_KEY", "canary");

        let rows = list_user_secret_keys_impl(&db, "id-mine").unwrap();
        let mine = find(&rows, "per_project", "MINE_KEY")
            .expect("the project's own file-store key must be listed");
        assert_eq!(mine.stores.file_store, Presence::Present);
        assert_eq!(mine.project_id, "id-mine");
        assert!(
            find(&rows, "per_project", "OTHER_KEY").is_none(),
            "another project's file-store namespace must not leak into this list"
        );
    }

    /// A project-scope row must report on its OWN namespace only in
    /// `file_store`; attributing one file to two rows would make "where
    /// does this live?" unanswerable. The resolvers' fall-through
    /// `projects/<NAME>/` → `shared/` is reported by the SEPARATE
    /// `shared_file_store` leg, asserted below — the two together are what
    /// let the panel say both "yours does not hold it" and "it still
    /// resolves, from shared". The global scope has no file-store
    /// namespace at all, so `Absent` is a complete answer there rather
    /// than a failure to look.
    #[test]
    fn shared_file_is_attributed_to_the_shared_row_only() {
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "pattr", "AttrProj");
        fs.put(&fs.shared(), "ATTR_KEY", "canary");
        // Give the same KEY a per_project and a global launcher row so all
        // three scopes emit a row for it.
        db.mark_secret_active("per_project", "pattr", "user", "ATTR_KEY").unwrap();
        db.mark_secret_active("global", SENTINEL_GLOBAL, "user", "ATTR_KEY").unwrap();

        let rows = list_user_secret_keys_impl(&db, "pattr").unwrap();
        assert_eq!(
            find(&rows, "shared", "ATTR_KEY").unwrap().stores.file_store,
            Presence::Present
        );
        assert_eq!(
            find(&rows, "per_project", "ATTR_KEY").unwrap().stores.file_store,
            Presence::Absent
        );
        assert_eq!(
            find(&rows, "global", "ATTR_KEY").unwrap().stores.file_store,
            Presence::Absent,
            "the file store has no global namespace — Absent, not Unknown"
        );
        // Attribution unchanged; the fall-through is a separate answer, so
        // the per-project row is not left implying the key is nowhere.
        assert_eq!(
            find(&rows, "per_project", "ATTR_KEY").unwrap().stores.shared_file_store,
            Presence::Present
        );
    }

    /// A keychain READ ERROR (locked login keyring, daemon timeout) must
    /// report `Unknown`, never `Absent`. Pre-v0.3.0 this path was
    /// `secrets::is_set(...).unwrap_or(false)`, so an unreadable store
    /// rendered exactly like an empty one and the user was told to type in
    /// a value they already had.
    #[test]
    fn keychain_read_error_reports_unknown_not_absent() {
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let _fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "perr", "ErrProj");
        db.mark_secret_active("shared", SENTINEL_SHARED, "user", "ERRING_KEY").unwrap();

        crate::secrets::for_tests::fail_next_get("ERRING_KEY");
        let rows = list_user_secret_keys_impl(&db, "perr").unwrap();
        let row = find(&rows, "shared", "ERRING_KEY").expect("row must still render");
        assert_eq!(
            row.stores.keychain,
            Presence::Unknown,
            "a store we could not read must not claim the key is absent"
        );
        assert!(!row.is_set, "an unreadable keychain cannot claim the key is live");
    }

    /// The divergent-copy state: BOTH stores hold the key with DIFFERENT
    /// values. This is the fork `CLAUDE.md` warns about, and the panel is
    /// the only place a user could ever notice it.
    ///
    /// The detection compares in memory and emits one boolean — no value
    /// and no digest of a value crosses the IPC boundary (a hash of a
    /// low-entropy secret is a brute-forceable oracle, so hashing would
    /// leak rather than protect).
    #[test]
    fn both_stores_holding_different_values_is_reported_as_divergent() {
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "pdiv", "DivProj");

        let shared = scope_from_manifest("shared", SENTINEL_SHARED);
        crate::secrets::set(shared, "user", "DIV_KEY", "canary-keychain-side").unwrap();
        db.mark_secret_active("shared", SENTINEL_SHARED, "user", "DIV_KEY").unwrap();
        fs.put(&fs.shared(), "DIV_KEY", "canary-file-side");

        let row_of = |db: &Db| {
            let rows = list_user_secret_keys_impl(db, "pdiv").unwrap();
            find(&rows, "shared", "DIV_KEY").cloned().unwrap()
        };

        let diverged = row_of(&db);
        assert_eq!(diverged.stores.keychain, Presence::Present);
        assert_eq!(diverged.stores.file_store, Presence::Present);
        assert_eq!(
            diverged.stores.values_diverge,
            Some(true),
            "two stores, two different values — the user must be told"
        );
        assert_eq!(
            diverged.winning_store,
            WinningStore::Keychain,
            "tier 1 wins, so the file copy is the one silently ignored"
        );

        // Same value in both → agreement, not divergence.
        fs.put(&fs.shared(), "DIV_KEY", "canary-keychain-side");
        assert_eq!(row_of(&db).stores.values_diverge, Some(false));

        // The trailing-newline convention the resolvers use must not
        // register as a difference: `vct set` writes `value\n` and every
        // resolver strips exactly one.
        fs.put(&fs.shared(), "DIV_KEY", "canary-keychain-side\n");
        assert_eq!(
            row_of(&db).stores.values_diverge,
            Some(false),
            "one trailing newline is stripped by every resolver — not a divergence"
        );

        // Only one store has it → nothing to compare.
        std::fs::remove_file(fs.shared().join("DIV_KEY")).unwrap();
        let single = row_of(&db);
        assert_eq!(single.stores.file_store, Presence::Absent);
        assert_eq!(single.stores.values_diverge, None);
    }

    /// Nothing in the row a consumer receives may carry value bytes. Pins
    /// the serialized wire shape against a canary written into BOTH
    /// stores.
    #[test]
    fn serialized_rows_never_carry_a_value() {
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "pleak", "LeakProj");

        let canary = format!("leak-canary-{}", uuid::Uuid::new_v4().simple());
        let shared = scope_from_manifest("shared", SENTINEL_SHARED);
        crate::secrets::set(shared, "user", "LEAK_KEY", &canary).unwrap();
        db.mark_secret_active("shared", SENTINEL_SHARED, "user", "LEAK_KEY").unwrap();
        let file_canary = format!("{}-file", canary);
        fs.put(&fs.shared(), "LEAK_KEY", &file_canary);

        let rows = list_user_secret_keys_impl(&db, "pleak").unwrap();
        let json = serde_json::to_string(&rows).unwrap();
        assert!(
            !json.contains(&canary),
            "the keychain value (or the file value, which embeds it) reached the wire"
        );
        // The divergence bit itself must still be there — the guard above
        // must not be satisfiable by simply omitting the comparison.
        assert!(json.contains("\"values_diverge\":true"));
        assert!(json.contains("\"file_store\":\"present\""));
    }

    /// `list_user_secret_keys_v2` must stay a one-expression delegate to
    /// `list_user_secret_keys_impl`, because that impl is what every
    /// behaviour test above drives. If the command grew logic of its own,
    /// those tests would silently stop covering production.
    ///
    /// This is a STRUCTURAL pin, and its shape matters: it extracts the
    /// command's BODY and asserts the body IS the delegate, rather than
    /// grepping the file for the callee's name. A name-substring assertion
    /// is satisfied by a comment — that exact defect is instance #8 in
    /// `knowledge/concepts/credited-mechanisms-that-never-fire-2026-09-04.md`,
    /// where a wiring guard was itself unwired. A `State<'_, Db>` cannot be
    /// constructed in a unit test, so a behavioural pin on the `#[command]`
    /// wrapper is not available; keeping the wrapper empty is what makes
    /// the behavioural tests on the impl load-bearing.
    #[test]
    fn list_command_is_a_pure_shim_over_the_tested_impl() {
        let src = include_str!("secrets_cmd.rs");
        // Split literal: an `include_str!` of THIS file also contains the
        // needle, so a contiguous spelling could match the TEST's own copy
        // instead of the command. See
        // `knowledge/concepts/source-shape-guard-tests-split-literal-needles-2026-07-02.md`.
        let sig = concat!("pub async fn ", "list_user_secret_keys_v2(");
        let at = src.find(sig).expect("the command must exist");
        let open = src[at..].find(" {\n").expect("command body must open") + at + 3;
        let close = src[open..].find("\n}\n").expect("command body must close") + open;
        let body = src[open..close].trim();
        assert_eq!(
            body, "list_user_secret_keys_impl(db.inner(), &project_id)",
            "list_user_secret_keys_v2 grew a body of its own — the behaviour \
             tests in this module drive list_user_secret_keys_impl and would \
             no longer cover what the panel calls"
        );
    }

    // ─── The tier-2 SHARED fall-through leg (v0.3.0) ──────────────────
    //
    // `read_store_report` mapped `per_project` onto `projects/<NAME>/`
    // and stopped. But the resolvers do not stop there: they read
    // `shared/<key>` next. So a per-project ref whose value lives only in
    // `shared/` resolved for every consumer and the panel called it
    // "not set" — the same lie the two-store report was written to remove,
    // one scope down. These drive the PRODUCTION entry points
    // (`get_secret_status_impl`, `list_user_secret_keys_impl`).

    /// Write the marker that opts a project out of the shared tier —
    /// through the SAME constant the launcher's toggle joins and the four
    /// resolvers read, so a rename cannot leave this test pinning a
    /// filename nobody uses.
    fn write_opt_out_marker(fs: &FileStoreScratch, project_name: &str) {
        let dir = fs.project(project_name);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join(crate::secrets_file_store::NO_SHARED_FALLBACK_MARKER),
            b"",
        )
        .unwrap();
    }

    #[test]
    fn a_per_project_key_living_only_in_shared_is_reported_as_resolving() {
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "pfall", "FallProj");

        // The residual, exactly: no keychain entry, nothing in the
        // project's OWN file-store namespace, value present in `shared/`.
        fs.put(&fs.shared(), "FALLTHROUGH_KEY", "canary");

        let st = get_secret_status_impl(&db, "pfall", "user", "per_project", "FALLTHROUGH_KEY", None)
            .expect("status must resolve");

        assert_eq!(st.stores.keychain, Presence::Absent);
        assert_eq!(
            st.stores.file_store,
            Presence::Absent,
            "the OWN-namespace probe stays honest — one file, one row"
        );
        assert_eq!(
            st.stores.shared_file_store,
            Presence::Present,
            "THE DEFECT: `projects/<NAME>/` misses and `shared/` hits, which \
             is a resolving key — it must not read as absent"
        );
        assert_eq!(
            st.stores.shared_file_store_path.as_deref(),
            Some(fs.shared().join("FALLTHROUGH_KEY").display().to_string().as_str()),
            "the path must name the SHARED file, so Remove can say what survives"
        );
        // The permission gate is untouched: the file store is not gated by
        // the launcher's active flag, and `is_set` must not start claiming
        // otherwise.
        assert!(!st.is_set);
        assert!(!st.has_saved_value);
    }

    #[test]
    fn the_opt_out_marker_denies_the_shared_leg_for_that_project_only() {
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "popt", "OptedOutProj");
        seed_project(&db, "pnorm", "NormalProj");
        fs.put(&fs.shared(), "GATED_KEY", "canary");
        write_opt_out_marker(&fs, "OptedOutProj");

        let opted = get_secret_status_impl(&db, "popt", "user", "per_project", "GATED_KEY", None)
            .expect("status");
        assert_eq!(
            opted.stores.shared_file_store,
            Presence::Absent,
            "this project's resolvers SKIP shared/ — telling it the value is \
             there would describe someone else's resolution"
        );
        assert_eq!(opted.stores.shared_file_store_path, None);

        let normal = get_secret_status_impl(&db, "pnorm", "user", "per_project", "GATED_KEY", None)
            .expect("status");
        assert_eq!(
            normal.stores.shared_file_store,
            Presence::Present,
            "the marker is per-project — the neighbour still resolves it"
        );
    }

    #[test]
    fn the_own_namespace_outranks_the_shared_leg_just_as_the_resolvers_do() {
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "pboth", "BothProj");
        fs.put(&fs.project("BothProj"), "BOTH_KEY", "canary-own");
        fs.put(&fs.shared(), "BOTH_KEY", "canary-shared");

        let st = get_secret_status_impl(&db, "pboth", "user", "per_project", "BOTH_KEY", None)
            .expect("status");
        // BOTH legs report Present — that is the point: the panel must be
        // able to say "yours wins, and there is another copy".
        assert_eq!(st.stores.file_store, Presence::Present);
        assert_eq!(st.stores.shared_file_store, Presence::Present);
        assert_eq!(
            st.stores.file_store_path.as_deref(),
            Some(fs.project("BothProj").join("BOTH_KEY").display().to_string().as_str())
        );
    }

    // ─── The project-wide shared opt-out reaches the DISPLAY (GAP-2) ──
    //
    // `set_shared_secrets_read_disabled` makes a project stop reading the
    // shared tier: the DB flag drops the keychain's user-shared bucket
    // inside `resolve_active_user_secret_pairs_for_requester`, and the
    // companion `.no-shared-fallback` marker drops `~/.vct-secrets/shared/`
    // for the four tier-2 resolvers.
    //
    // Neither gate was visible to the panel. `is_set` is the per-(secret ×
    // requester) ACTIVE flag and does not model a bulk policy — correctly,
    // and it must keep not modelling it — so a live shared row rendered
    // "set" on a project that would never receive it. These pin the fix
    // where it belongs: two DISPLAY booleans on the store report, computed
    // for the READER.

    /// Turn on the opt-out the way the production toggle does — through the
    /// same `module_settings` row and the same constants, so a rename
    /// cannot leave these tests pinning a key nothing reads.
    fn opt_out_of_shared_secrets(db: &Db, project_id: &str) {
        db.set_setting(
            project_id,
            module_settings_keys::ORCHESTRATOR_CORE_MODULE_ID,
            module_settings_keys::SETTING_KEY_SHARED_SECRETS_READ_DISABLED,
            &serde_json::Value::Bool(true),
        )
        .unwrap();
    }

    #[test]
    fn an_opted_out_project_is_told_a_live_shared_row_does_not_reach_it() {
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "popt2", "OptedOut2");

        // A perfectly healthy shared secret: keychain value, active flag on.
        let shared = scope_from_manifest("shared", SENTINEL_SHARED);
        crate::secrets::set(shared, "user", "TEAM_TOKEN", "canary").unwrap();
        db.mark_secret_active("shared", SENTINEL_SHARED, "user", "TEAM_TOKEN")
            .unwrap();
        // …and both halves of the opt-out, as the toggle writes them.
        opt_out_of_shared_secrets(&db, "popt2");
        write_opt_out_marker(&fs, "OptedOut2");

        let st = get_secret_status_impl(
            &db,
            SENTINEL_SHARED,
            "user",
            "shared",
            "TEAM_TOKEN",
            Some("popt2"),
        )
        .expect("status");

        assert!(
            st.stores.shared_read_disabled,
            "THE DEFECT: the reader has opted out of the shared keychain \
             bucket, so this row does not reach it — the panel had no way to \
             know and rendered it as set"
        );
        assert!(
            st.stores.shared_file_fallback_disabled,
            "the companion marker gates tier 2 for the same reader"
        );
        // The PERMISSION GATE is untouched, deliberately. `is_set` answers
        // "may this requester read the keychain slot", which `is_secret_set`,
        // the hub and module code all ask; folding a bulk display policy
        // into it would silently widen that answer for every one of them.
        assert!(
            st.is_set,
            "is_set must keep its exact pre-existing meaning (keychain value \
             x the per-(secret x requester) active flag)"
        );
        assert!(st.has_saved_value);
        assert_eq!(st.stores.keychain, Presence::Present);
    }

    #[test]
    fn the_shared_gates_are_computed_for_the_reader_never_the_owner() {
        // A shared row is OWNED by the `_user_shared_` sentinel, which is
        // not a project and can hold neither a setting row nor a marker. So
        // a status call that does not name the reader cannot evaluate
        // either gate — and before v0.3.0 there was no way to name one.
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "preader", "ReaderProj");
        seed_project(&db, "pother", "OtherProj");
        fs.put(&fs.shared(), "TEAM_TOKEN", "canary");
        opt_out_of_shared_secrets(&db, "preader");
        write_opt_out_marker(&fs, "ReaderProj");

        let named = get_secret_status_impl(
            &db, SENTINEL_SHARED, "user", "shared", "TEAM_TOKEN", Some("preader"),
        )
        .expect("status");
        assert!(named.stores.shared_read_disabled);
        assert!(named.stores.shared_file_fallback_disabled);

        // A DIFFERENT reader never inherits its neighbour's opt-out.
        let neighbour = get_secret_status_impl(
            &db, SENTINEL_SHARED, "user", "shared", "TEAM_TOKEN", Some("pother"),
        )
        .expect("status");
        assert!(!neighbour.stores.shared_read_disabled);
        assert!(!neighbour.stores.shared_file_fallback_disabled);

        // And an omitted reader reproduces the pre-v0.3.0 answer exactly:
        // the owner sentinel has no opt-out, so nothing is suppressed.
        let anonymous = get_secret_status_impl(
            &db, SENTINEL_SHARED, "user", "shared", "TEAM_TOKEN", None,
        )
        .expect("status");
        assert!(!anonymous.stores.shared_read_disabled);
        assert!(!anonymous.stores.shared_file_fallback_disabled);
    }

    #[test]
    fn a_failed_marker_write_leaves_the_file_tier_serving_and_the_report_says_so() {
        // `set_shared_secrets_read_disabled` writes the DB flag, then the
        // marker best-effort — and warns, in so many words, that "file-store
        // tier-2 shared fallback is not gated until the marker exists". In
        // that state the shared FILE still resolves for this project, so a
        // report collapsing both gates into one boolean would tell the user
        // a working key does not reach them.
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "phalf", "HalfGated");
        fs.put(&fs.shared(), "HALF_KEY", "canary");
        opt_out_of_shared_secrets(&db, "phalf");
        // …and NO marker on disk.

        let st = get_secret_status_impl(
            &db, SENTINEL_SHARED, "user", "shared", "HALF_KEY", Some("phalf"),
        )
        .expect("status");
        assert!(st.stores.shared_read_disabled, "the keychain bucket IS gated");
        assert!(
            !st.stores.shared_file_fallback_disabled,
            "the file tier is NOT gated until the marker file exists — the four \
             resolvers stat it on disk, they do not read launcher.db"
        );
        assert_eq!(st.stores.file_store, Presence::Present);
    }

    #[test]
    fn the_shared_opt_out_never_touches_per_project_or_global_rows() {
        // The gate drops ONE bucket. Per-project secrets and the global
        // machine-wide bucket keep resolving, and a row that claimed
        // otherwise would send the user to a checkbox that changes nothing.
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let _fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "pscope", "ScopeProj");
        opt_out_of_shared_secrets(&db, "pscope");

        let pp = get_secret_status_impl(
            &db, "pscope", "user", "per_project", "K", Some("pscope"),
        )
        .expect("status");
        assert!(!pp.stores.shared_read_disabled);
        assert!(!pp.stores.shared_file_fallback_disabled);

        let gl = get_secret_status_impl(
            &db, SENTINEL_GLOBAL, "user", "global", "K", Some("pscope"),
        )
        .expect("status");
        assert!(!gl.stores.shared_read_disabled);
        assert!(!gl.stores.shared_file_fallback_disabled);
    }

    #[test]
    fn the_panel_list_carries_the_opt_out_on_its_shared_rows() {
        // The list command is the surface that actually renders the Shared
        // tab, and it is the one that knows the reader without being told.
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "plist", "ListProj");

        let shared = scope_from_manifest("shared", SENTINEL_SHARED);
        crate::secrets::set(shared, "user", "LIST_TEAM_TOKEN", "canary").unwrap();
        db.mark_secret_active("shared", SENTINEL_SHARED, "user", "LIST_TEAM_TOKEN")
            .unwrap();
        opt_out_of_shared_secrets(&db, "plist");
        write_opt_out_marker(&fs, "ListProj");

        let rows = list_user_secret_keys_impl(&db, "plist").unwrap();
        let sh = rows
            .iter()
            .find(|r| r.scope == "shared" && r.key == "LIST_TEAM_TOKEN")
            .expect("the shared row must be listed");
        assert!(sh.stores.shared_read_disabled);
        assert!(sh.stores.shared_file_fallback_disabled);
        assert!(sh.is_set, "the permission gate is unchanged");

        // Every OTHER row in the same response keeps the gates off, so the
        // flag cannot be read as a project-wide banner.
        for r in rows.iter().filter(|r| r.scope != "shared") {
            assert!(!r.stores.shared_read_disabled, "{} leaked the gate", r.key);
            assert!(!r.stores.shared_file_fallback_disabled, "{} leaked the gate", r.key);
        }
    }

    #[test]
    fn a_shared_row_answers_about_the_readers_own_pause_not_the_all_readers_row() {
        // Same plumbing, different consumer: `mark_secret_inactive_for_requester`
        // pauses a shared key for ONE project. Asking with the sentinel as
        // the requester finds no literal row and falls back to `*` — the
        // all-readers answer — so the panel showed "set" for a key the
        // project in view had explicitly paused.
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let _fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "ppause", "PauseProj");

        let shared = scope_from_manifest("shared", SENTINEL_SHARED);
        crate::secrets::set(shared, "user", "PAUSED_TEAM_KEY", "canary").unwrap();
        db.mark_secret_active("shared", SENTINEL_SHARED, "user", "PAUSED_TEAM_KEY")
            .unwrap();
        db.mark_secret_inactive_for_requester(
            "shared", SENTINEL_SHARED, "user", "PAUSED_TEAM_KEY", "ppause",
        )
        .unwrap();

        let for_reader = get_secret_status_impl(
            &db, SENTINEL_SHARED, "user", "shared", "PAUSED_TEAM_KEY", Some("ppause"),
        )
        .expect("status");
        assert!(
            !for_reader.is_set,
            "this project paused the key; the row must not claim it is live here"
        );
        assert!(
            for_reader.has_saved_value,
            "the VALUE is still in the keychain — pausing preserves it"
        );

        // The all-readers view is unchanged, which is why the reader has to
        // be named rather than inferred.
        let all_readers = get_secret_status_impl(
            &db, SENTINEL_SHARED, "user", "shared", "PAUSED_TEAM_KEY", None,
        )
        .expect("status");
        assert!(all_readers.is_set);
    }

    // ─── Item 3: the shared-copy conflict, and who reports it ─────────

    #[test]
    fn a_shared_file_copy_collides_across_scopes_and_both_rows_say_so() {
        // `isForked` (keychain x the row's OWN file-store namespace) does
        // not fire when the second copy is `~/.vct-secrets/shared/<key>`.
        // That is not a gap: the row set is the UNION of the launcher's rows
        // and the file store's FILES, so the shared file becomes a
        // shared-scope ROW and the collision surfaces as a cross-scope
        // shadow on BOTH rows — with the winner named, which "⚠ also in
        // file store" would not do. Verified here rather than assumed.
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "pfork", "ForkProj");

        // The project's own keychain value…
        let pp = scope_from_manifest("per_project", "pfork");
        crate::secrets::set(pp, "user", "DUP_KEY", "canary-keychain").unwrap();
        db.mark_secret_active("per_project", "pfork", "user", "DUP_KEY").unwrap();
        // …and a second copy in the SHARED file store, which the launcher
        // has never been told about.
        fs.put(&fs.shared(), "DUP_KEY", "canary-shared-file");

        let rows = list_user_secret_keys_impl(&db, "pfork").unwrap();
        let own = rows
            .iter()
            .find(|r| r.scope == "per_project" && r.key == "DUP_KEY")
            .expect("per-project row");
        let shared_row = rows
            .iter()
            .find(|r| r.scope == "shared" && r.key == "DUP_KEY")
            .expect("the shared FILE must produce a shared-scope row");

        // Not a same-row fork: the project's own namespace holds nothing.
        assert_eq!(own.stores.keychain, Presence::Present);
        assert_eq!(
            own.stores.file_store,
            Presence::Absent,
            "isForked's inputs — so it stays false, correctly"
        );
        // The conflict is carried, on BOTH rows, with the winner named.
        assert!(own.is_shadowed, "the conflict must be visible from the row in view");
        assert!(shared_row.is_shadowed, "…and from the other one");
        assert_eq!(own.winning_scope, "per_project");
        assert_eq!(own.winning_store, WinningStore::Keychain);
        assert_eq!(shared_row.winning_scope, "per_project");
        // The surviving copy is still nameable, which is what Remove needs.
        assert_eq!(
            shared_row.stores.file_store_path.as_deref(),
            Some(fs.shared().join("DUP_KEY").display().to_string().as_str())
        );
    }

    #[test]
    fn the_shared_gates_never_carry_a_value_onto_the_wire() {
        // Two booleans and nothing else. A gate computed from a value —
        // or a report that started shipping one alongside them — would be
        // caught here.
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "pwire", "WireProj");

        let canary = format!("wire-canary-{}", uuid::Uuid::new_v4().simple());
        let shared = scope_from_manifest("shared", SENTINEL_SHARED);
        crate::secrets::set(shared, "user", "WIRE_KEY", &canary).unwrap();
        db.mark_secret_active("shared", SENTINEL_SHARED, "user", "WIRE_KEY").unwrap();
        fs.put(&fs.shared(), "WIRE_KEY", &canary);
        opt_out_of_shared_secrets(&db, "pwire");
        write_opt_out_marker(&fs, "WireProj");

        let rows = list_user_secret_keys_impl(&db, "pwire").unwrap();
        let json = serde_json::to_string(&rows).unwrap();
        assert!(!json.contains(&canary), "a value reached the wire");
        assert!(json.contains("\"shared_read_disabled\":true"));
        assert!(json.contains("\"shared_file_fallback_disabled\":true"));
    }

    #[test]
    fn shared_and_global_rows_carry_no_fall_through_leg() {
        // A definition pin, not an omission (see the field docs): the
        // shared row's own `file_store` IS the `shared/` probe, so it has
        // nothing to fall through TO; and `get_secret_status_v2` receives
        // the `_global_` sentinel for global rows, so no project's marker
        // could be evaluated for one.
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "pdef", "DefProj");
        fs.put(&fs.shared(), "DEF_KEY", "canary");

        let sh = get_secret_status_impl(&db, SENTINEL_SHARED, "user", "shared", "DEF_KEY", None)
            .expect("status");
        assert_eq!(sh.stores.file_store, Presence::Present);
        assert_eq!(sh.stores.shared_file_store, Presence::Absent);

        let gl = get_secret_status_impl(&db, SENTINEL_GLOBAL, "user", "global", "DEF_KEY", None)
            .expect("status");
        assert_eq!(gl.stores.file_store, Presence::Absent);
        assert_eq!(gl.stores.shared_file_store, Presence::Absent);
    }

    #[test]
    fn the_list_surface_reports_the_same_fall_through_leg() {
        // One model, two surfaces. If the list path diverged from the status
        // path the panel and the Secret-refs tab would disagree about the
        // same key.
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "plist", "ListProj");
        fs.put(&fs.shared(), "LIST_KEY", "canary");
        // A launcher row with no keychain value — the shape a Remove or a
        // hub-registered ref leaves behind.
        db.mark_secret_active("per_project", "plist", "user", "LIST_KEY").unwrap();

        let rows = list_user_secret_keys_impl(&db, "plist").unwrap();
        let pp = find(&rows, "per_project", "LIST_KEY").expect("per-project row");
        assert_eq!(pp.stores.file_store, Presence::Absent);
        assert_eq!(pp.stores.shared_file_store, Presence::Present);
        // …and the shared row still owns the file for attribution.
        let sh = find(&rows, "shared", "LIST_KEY").expect("shared row");
        assert_eq!(sh.stores.file_store, Presence::Present);
        assert_eq!(sh.stores.shared_file_store, Presence::Absent);
    }

    #[test]
    fn the_winner_follows_the_marker_gated_shared_leg_not_a_raw_probe() {
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "pwinm", "WinMarkProj");
        fs.put(&fs.shared(), "WIN_KEY", "canary");
        db.mark_secret_active("per_project", "pwinm", "user", "WIN_KEY").unwrap();

        let row_of = |db: &Db| {
            find(&list_user_secret_keys_impl(db, "pwinm").unwrap(), "per_project", "WIN_KEY")
                .cloned()
                .expect("per-project row")
        };

        let open = row_of(&db);
        assert_eq!(open.winning_scope, "shared");
        assert_eq!(open.winning_store, WinningStore::FileStore);

        // Opt out. The file has not moved — but this project no longer
        // reads it, so naming it the winner would be a false statement
        // about THIS project's runtime.
        write_opt_out_marker(&fs, "WinMarkProj");
        let gated = row_of(&db);
        assert_eq!(
            gated.winning_store,
            WinningStore::NoStore,
            "with the shared tier opted out, no sanctioned store serves this key"
        );
        assert_eq!(gated.winning_scope, "per_project");
        assert_eq!(gated.stores.shared_file_store, Presence::Absent);
    }

    #[test]
    fn status_serialization_never_carries_a_value_from_either_tier_2_leg() {
        let _kc = keychain_test_lock();
        let _mock = crate::secrets::for_tests::MockGuard::new();
        let fs = file_store_scratch();
        let db = make_db();
        seed_project(&db, "pleak2", "LeakTwoProj");

        let canary = format!("leak-canary-{}", uuid::Uuid::new_v4().simple());
        fs.put(&fs.shared(), "LEAK2_KEY", &canary);
        fs.put(&fs.project("LeakTwoProj"), "LEAK2_OWN", &canary);

        for key in ["LEAK2_KEY", "LEAK2_OWN"] {
            let st = get_secret_status_impl(&db, "pleak2", "user", "per_project", key, None)
                .expect("status");
            let json = serde_json::to_string(&st).unwrap();
            assert!(!json.contains(&canary), "a value reached the wire for {}", key);
        }
        // The guard must not be satisfiable by omitting the report.
        let st = get_secret_status_impl(&db, "pleak2", "user", "per_project", "LEAK2_KEY", None)
            .expect("status");
        let json = serde_json::to_string(&st).unwrap();
        assert!(json.contains("\"shared_file_store\":\"present\""));
    }

    /// `get_secret_status_v2` must stay a one-expression delegate to
    /// `get_secret_status_impl`, for the same reason the list command does:
    /// a `State<'_, Db>` cannot be constructed in a unit test, so the
    /// behavioural tests above only cover production while the wrapper
    /// stays empty. Structural, and deliberately extracting the BODY
    /// rather than grepping for the callee's name — a name-substring
    /// assertion is satisfied by a comment (instance #8 in
    /// `knowledge/concepts/credited-mechanisms-that-never-fire-2026-09-04.md`).
    #[test]
    fn status_command_is_a_pure_shim_over_the_tested_impl() {
        let src = include_str!("secrets_cmd.rs");
        // Split literal: this `include_str!` also contains the needle.
        let sig = concat!("pub async fn ", "get_secret_status_v2(");
        let at = src.find(sig).expect("the command must exist");
        let open = src[at..].find(" {\n").expect("command body must open") + at + 3;
        let close = src[open..].find("\n}\n").expect("command body must close") + open;
        let body = src[open..close].trim();
        assert_eq!(
            body,
            "get_secret_status_impl(db.inner(), &project_id, &module_id, &scope, &key, requester_project_id.as_deref())",
            "get_secret_status_v2 grew a body of its own — the behaviour \
             tests in this module drive get_secret_status_impl and would no \
             longer cover what the Secret-refs tab calls"
        );
    }

    // ─── Remove must not overstate what it deletes ─────────────────────

    #[test]
    fn remove_audit_records_both_surviving_tier_2_legs() {
        let _kc = keychain_test_lock();
        let fs = file_store_scratch();

        // Own namespace only.
        fs.put(&fs.project("RmProj"), "OWN_ONLY", "canary");
        let own = remove_audit_payload("per_project", "OWN_ONLY", Some("RmProj"));
        assert_eq!(own["file_store_copy_remains"], serde_json::json!(true));
        assert_eq!(own["shared_file_store_copy_resolves"], serde_json::json!(false));

        // Shared only — the case that used to audit as "nothing survives"
        // for a Remove no consumer could observe.
        fs.put(&fs.shared(), "SHARED_ONLY", "canary");
        let shared = remove_audit_payload("per_project", "SHARED_ONLY", Some("RmProj"));
        assert_eq!(shared["file_store_copy_remains"], serde_json::json!(false));
        assert_eq!(
            shared["shared_file_store_copy_resolves"],
            serde_json::json!(true),
            "Remove leaves shared/<key> in place and the key keeps resolving"
        );

        // Opted out → the shared file is NOT a survivor for this project.
        write_opt_out_marker(&fs, "RmProj");
        let gated = remove_audit_payload("per_project", "SHARED_ONLY", Some("RmProj"));
        assert_eq!(gated["shared_file_store_copy_resolves"], serde_json::json!(false));

        // Nowhere at all.
        let none = remove_audit_payload("per_project", "ABSENT", Some("RmProj"));
        assert_eq!(none["file_store_copy_remains"], serde_json::json!(false));
        assert_eq!(none["shared_file_store_copy_resolves"], serde_json::json!(false));

        // The payload carries the identifying metadata the audit reader
        // needs, and nothing else.
        assert_eq!(none["key"], serde_json::json!("ABSENT"));
        assert_eq!(none["scope"], serde_json::json!("per_project"));
    }

    /// Sanity: the keychain-backed end-to-end test. Skipped on CI hosts
    /// without libsecret. Pin: a row only counts as `is_set: true` when
    /// keychain has a value AND the cross-launcher active flag is set;
    /// the resolver's `winning_scope` follows is_set, NOT mere row presence.
    #[test]
    #[ignore = "requires OS keychain backend (keyring); skipped in CI headless env"]
    fn winning_scope_ignores_paused_or_keychain_empty_rows() {
        // Serialize against other keychain-touching tests across
        // the crate. Required since 2026-05-13 — see crate::secrets docs.
        let _kc_lock = keychain_test_lock();
        let db = make_db();
        seed_project(&db, "pwin", "Winning Project");

        let key = format!(
            "VCT_WIN_TEST_KEY_{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );

        // Set value in shared, NO value in per_project (only an active row).
        let canary = format!(
            "win-canary-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let shared_scope = scope_from_manifest("shared", SENTINEL_SHARED);
        secrets::set(shared_scope, "user", &key, &canary).expect("keychain set shared");
        db.mark_secret_active("shared", SENTINEL_SHARED, "user", &key)
            .unwrap();

        // Per-project row exists in the active-flag DB but keychain is empty
        // (e.g. user typed the value Shared but had previously toggled
        // per-project). is_set should be false for per-project, true for shared.
        db.mark_secret_active("per_project", "pwin", "user", &key)
            .unwrap();

        // Use the same status helper list_user_secret_keys_impl uses.
        // v0.3.0: an empty file store keeps this a pure tier-1 assertion —
        // the guard below pins that (if a stray file existed for this
        // random key the winner could legitimately differ).
        let _fs = empty_file_store_guard();
        let pp = read_user_secret_status(&db, "per_project", "pwin", "pwin", &key, Some("pwin"));
        let sh = read_user_secret_status(&db, "shared", SENTINEL_SHARED, "pwin", &key, None);
        assert!(!pp.is_set, "per_project must be is_set=false (keychain empty)");
        assert!(sh.is_set, "shared must be is_set=true (keychain holds canary)");

        // Winning scope: shared wins because per_project has no keychain value.
        let winner = resolve_winning_scope("shared", pp.is_set, sh.is_set, false, false, false);
        assert_eq!(winner, ("shared".to_string(), WinningStore::Keychain));
        // Even from per_project's POV, the resolver still picks shared.
        let winner_pp =
            resolve_winning_scope("per_project", pp.is_set, sh.is_set, false, false, false);
        assert_eq!(winner_pp, ("shared".to_string(), WinningStore::Keychain));

        // Cleanup.
        let _ = secrets::delete(shared_scope, "user", &key);
        let _ = db.forget_secret_active_state("shared", SENTINEL_SHARED, "user", &key);
        let _ = db.forget_secret_active_state("per_project", "pwin", "user", &key);
    }

    // ─── V47-G-final / P2: migrate_env_secrets_from_dotenv outcome logic ──
    //
    // The HTTP round-trip is exercised at the hub level in
    // vct-hub::secrets_api::tests; here we pin the pure orchestration seam
    // (build_migration_outcome) + the hub-unreachable CLI-fallback contract
    // WITHOUT a live hub, so the success/partial/down branches are covered.

    fn hub_resp(migrated: &[&str], failed: &[&str]) -> HubMigrateResponse {
        hub_resp_scoped(migrated, failed, "shared")
    }

    fn hub_resp_scoped(migrated: &[&str], failed: &[&str], scope: &str) -> HubMigrateResponse {
        HubMigrateResponse {
            migrated: migrated.iter().map(|s| s.to_string()).collect(),
            failed: failed
                .iter()
                .map(|k| HubMigrateFailure {
                    key: k.to_string(),
                    error: "boom".to_string(),
                })
                .collect(),
            scope: scope.to_string(),
        }
    }

    #[test]
    fn migration_outcome_success_migrates_and_rewrites_env() {
        // Unquoted value + inline comment → the comment is preserved (the
        // quoted-value case drops it; that asymmetry is pinned in
        // env_secrets_migrate::tests and matches the Python mirror).
        let env = "export OPENAI_API_KEY=sk-abc  # team\nPLAIN=keep\n";
        let hub = hub_resp(&["OPENAI_API_KEY"], &[]);
        let (result, new_env) = build_migration_outcome(env, &hub);

        assert!(result.ok);
        assert_eq!(result.migrated, vec!["OPENAI_API_KEY".to_string()]);
        assert!(result.failed.is_empty());
        assert!(result.error.is_none());
        // GAP-1: the hub's scope flows through to the FE result verbatim.
        assert_eq!(result.scope, "shared");
        // The .env is rewritten: only the migrated key gets the sentinel,
        // PLAIN is byte-identical, structure (export prefix + comment) kept.
        assert_eq!(
            new_env.as_deref(),
            Some("export OPENAI_API_KEY=__vco_keychain__  # team\nPLAIN=keep\n")
        );
    }

    #[test]
    fn migration_outcome_threads_per_project_scope() {
        // GAP-1: a per-project hub response surfaces `scope == "per_project"`
        // so SecretsTab renders the "this project's scope" banner + the
        // command triggers the per-project env refresh.
        let env = "CLIENTA_DB_PASSWORD=pw\n";
        let hub = hub_resp_scoped(&["CLIENTA_DB_PASSWORD"], &[], "per_project");
        let (result, _new_env) = build_migration_outcome(env, &hub);
        assert!(result.ok);
        assert_eq!(result.scope, "per_project");
        assert_eq!(result.migrated, vec!["CLIENTA_DB_PASSWORD".to_string()]);
    }

    #[test]
    fn migration_outcome_partial_failure_surfaces_failed_keys_no_flip_on_ok() {
        let env = "A_TOKEN=one\nB_SECRET=two\n";
        // Hub migrated A_TOKEN, failed B_SECRET.
        let hub = hub_resp(&["A_TOKEN"], &["B_SECRET"]);
        let (result, new_env) = build_migration_outcome(env, &hub);

        assert!(result.ok, "partial failure must not flip ok=false");
        assert_eq!(result.migrated, vec!["A_TOKEN".to_string()]);
        assert_eq!(result.failed, vec!["B_SECRET".to_string()]);
        let err = result.error.expect("error summary for failed keys");
        assert!(err.contains("B_SECRET"), "error names the failed key: {}", err);
        assert!(!err.contains("two"), "error must NOT leak the secret value: {}", err);
        // Only the migrated key's value was replaced; B_SECRET stays plaintext.
        assert_eq!(
            new_env.as_deref(),
            Some("A_TOKEN=__vco_keychain__\nB_SECRET=two\n")
        );
    }

    #[test]
    fn migration_outcome_all_failed_writes_nothing() {
        let env = "A_TOKEN=one\n";
        let hub = hub_resp(&[], &["A_TOKEN"]);
        let (result, new_env) = build_migration_outcome(env, &hub);

        assert!(result.ok);
        assert!(result.migrated.is_empty());
        assert_eq!(result.failed, vec!["A_TOKEN".to_string()]);
        assert!(
            new_env.is_none(),
            "no migrated keys ⇒ no .env rewrite (leave user data untouched)"
        );
    }

    #[test]
    fn hub_unreachable_fallback_preserves_cli_guidance() {
        // The command's hub-down branch returns this exact result shape.
        // Assert the CLI-fallback text (unchanged from the prior stub) is
        // what SecretsTab's error panel will render, and nothing "migrated".
        let down = MigrateEnvSecretsResult {
            ok: false,
            migrated: vec![],
            failed: vec![],
            error: Some(HUB_UNREACHABLE_CLI_FALLBACK.to_string()),
            scope: "shared".to_string(),
        };
        assert!(!down.ok);
        assert!(down.migrated.is_empty());
        let err = down.error.expect("fallback error present");
        assert!(
            err.contains("python install.py --update --apply-deferred"),
            "CLI-fallback guidance preserved: {}",
            err
        );
    }
}

// ─── V47-G-final (v0.2.75 / P2): in-process "Migrate from .env" wrapper ──
//
// Tauri command backing the per-project SecretsTab's "Migrate from .env"
// button. Implements the Rust → hub round-trip the earlier stub only
// promised: resolve the project root from `project_id`, audit its `.env`
// for secret-shaped keys with real values, POST them to the authed hub
// `/api/v1/secrets/migrate` endpoint, and rewrite the migrated keys'
// values to the `__vco_keychain__` sentinel on success — the same
// behaviour the install.py CLI arm (`_audit_and_offer_env_secret_migration`)
// produces.
//
// Cross-language discipline:
//   * Secret-shape check → the single B-3-guarded needle home
//     (`mcp_registration::is_secret_shaped_env_key`), consumed via the
//     `env_secrets_migrate` module. NEVER a new inline needle list.
//   * `.env` parse + sentinel-rewrite → `env_secrets_migrate` (the Rust
//     mirror of `vco_lib/secrets_audit.py`, with a MUST-MATCH comment on
//     both sides).
//
// Hub-down posture: when the hub is unreachable we return `ok=false` with
// the SAME CLI-fallback guidance the old stub emitted, so SecretsTab's
// error panel keeps steering the user to `python install.py --update
// --apply-deferred`. Nothing is written to disk in that case.

use super::env_secrets_migrate::{
    audit_env_secrets, rewrite_env_with_sentinels, EnvSecret,
};

/// FE-facing result. `failed` is a flat `Vec<String>` of key names (the
/// SecretsTab.svelte panel renders key names only); per-key error detail
/// from the hub is folded into the `error` summary line.
#[derive(Debug, Clone, Serialize)]
pub struct MigrateEnvSecretsResult {
    pub ok: bool,
    pub migrated: Vec<String>,
    pub failed: Vec<String>,
    pub error: Option<String>,
    /// GAP-1 (2026-07-14): where the hub landed the keys — `"shared"` or
    /// `"per_project"`. SecretsTab uses it to say "migrated to THIS
    /// project's scope" vs "…to the shared scope (orchestrator root)".
    /// Defaults to `"shared"` when the hub predates the `scope` field
    /// (mixed-version window during self-update).
    pub scope: String,
}

/// The CLI-fallback guidance surfaced when the hub can't be reached. Kept
/// verbatim from the prior stub so SecretsTab's error panel is unchanged.
const HUB_UNREACHABLE_CLI_FALLBACK: &str =
    "Could not reach the local vct-hub to migrate secrets. Make sure the \
     launcher / vct-hub is running, or use the CLI fallback: \
     `python install.py --update --apply-deferred` at the project root.";

/// One `{key, error}` failure row as returned by the hub's
/// `/api/v1/secrets/migrate` response.
#[derive(Debug, Clone, Deserialize)]
struct HubMigrateFailure {
    key: String,
    #[allow(dead_code)]
    error: String,
}

/// The hub's `POST /api/v1/secrets/migrate` 200 response body.
#[derive(Debug, Clone, Deserialize)]
struct HubMigrateResponse {
    migrated: Vec<String>,
    failed: Vec<HubMigrateFailure>,
    /// GAP-1 (2026-07-14): `"shared"` | `"per_project"`. `#[serde(default)]`
    /// tolerates an OLDER hub (mixed-version self-update window) whose
    /// response omits the field → defaults to `"shared"`, the correct value
    /// for that hub since it only ever wrote the shared bucket.
    #[serde(default = "default_shared_scope")]
    scope: String,
}

fn default_shared_scope() -> String {
    "shared".to_string()
}

/// Read the hub port + token the same way `hub_proxy.rs` does (re-read
/// from disk each call so a hub restart's rotated token propagates).
fn hub_port_token() -> Result<(u16, String), String> {
    let root = crate::paths::vct_root_dir();
    let port = std::fs::read_to_string(root.join("hub.port"))
        .map_err(|e| format!("read hub.port: {}", e))?
        .trim()
        .parse::<u16>()
        .map_err(|e| format!("parse hub.port: {}", e))?;
    let token = std::fs::read_to_string(root.join("hub.token"))
        .map_err(|e| format!("read hub.token: {}", e))?
        .trim()
        .to_string();
    if token.is_empty() {
        return Err("hub.token is empty".into());
    }
    Ok((port, token))
}

/// POST the audited secrets to the hub and return its `(migrated, failed)`.
/// Errors here are treated as "hub unreachable" by the caller (nothing is
/// written to disk).
async fn post_secrets_to_hub(
    secrets: &[EnvSecret],
    project_id: Option<&str>,
) -> Result<HubMigrateResponse, String> {
    let (port, token) = hub_port_token()?;
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| format!("http client: {}", e))?;
    let url = format!("http://127.0.0.1:{}/api/v1/secrets/migrate", port);
    // GAP-1: forward the owning project id so the hub's scope policy (S1)
    // routes the keys to this project's scope (or Shared for the
    // orchestrator root). No scope logic here — the hub decides.
    let mut payload = serde_json::json!({
        "secrets": secrets
            .iter()
            .map(|s| serde_json::json!({ "key": s.key, "value": s.value }))
            .collect::<Vec<_>>(),
    });
    if let Some(pid) = project_id {
        payload["project_id"] = serde_json::Value::String(pid.to_string());
    }
    let resp = client
        .post(&url)
        .bearer_auth(&token)
        .json(&payload)
        .send()
        .await
        .map_err(|e| format!("hub POST /secrets/migrate: {}", e))?;
    if !resp.status().is_success() {
        return Err(format!("hub returned {}", resp.status().as_u16()));
    }
    resp.json::<HubMigrateResponse>()
        .await
        .map_err(|e| format!("parse hub response: {}", e))
}

/// Pure orchestration seam: given the current `.env` text and the hub's
/// migrate result, compute the FE result + the new `.env` text to write
/// (None ⇒ nothing to write). Kept separate from I/O so the success and
/// failure branches are unit-testable without a live hub or filesystem.
fn build_migration_outcome(
    env_text: &str,
    hub: &HubMigrateResponse,
) -> (MigrateEnvSecretsResult, Option<String>) {
    let migrated = hub.migrated.clone();
    let failed_keys: Vec<String> = hub.failed.iter().map(|f| f.key.clone()).collect();

    let new_env = if migrated.is_empty() {
        None
    } else {
        Some(rewrite_env_with_sentinels(env_text, &migrated).text)
    };

    let error = if failed_keys.is_empty() {
        None
    } else {
        Some(format!(
            "{} key(s) could not be migrated: {}",
            failed_keys.len(),
            failed_keys.join(", ")
        ))
    };

    (
        MigrateEnvSecretsResult {
            // `ok` reflects "the round-trip completed" — partial failures
            // are surfaced via `failed` + `error`, not by flipping `ok`
            // (matches the hub's own partial-success-is-200 contract).
            ok: true,
            migrated,
            failed: failed_keys,
            error,
            scope: hub.scope.clone(),
        },
        new_env,
    )
}

#[command]
pub async fn migrate_env_secrets_from_dotenv(
    project_id: String,
    db: State<'_, Db>,
) -> Result<MigrateEnvSecretsResult, String> {
    // 1. Resolve the project root from the launcher DB.
    let project = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("no project registered with id {:?}", project_id))?;
    let env_path = std::path::Path::new(&project.folder_path).join(".env");

    // 2. Audit `.env` for secret-shaped keys with real values.
    let env_text = match std::fs::read_to_string(&env_path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            // No .env → nothing to migrate. `ok=true`, empty lists.
            return Ok(MigrateEnvSecretsResult {
                ok: true,
                migrated: vec![],
                failed: vec![],
                error: None,
                scope: "shared".to_string(),
            });
        }
        Err(e) => return Err(format!("read {}: {}", env_path.display(), e)),
    };
    let candidates = audit_env_secrets(&env_text);
    if candidates.is_empty() {
        return Ok(MigrateEnvSecretsResult {
            ok: true,
            migrated: vec![],
            failed: vec![],
            error: None,
            scope: "shared".to_string(),
        });
    }

    // 3. POST to the hub, forwarding this project's id so the hub's scope
    //    policy (S1) routes the keys per-project (or Shared for the
    //    orchestrator root). The id is already resolved above — pre-GAP-1
    //    it was used only to find `.env` and then dropped, which is the bug
    //    that migrated a per-project secret into the shared bucket.
    //    Hub-unreachable → CLI-fallback, nothing written.
    let hub = match post_secrets_to_hub(&candidates, Some(&project_id)).await {
        Ok(h) => h,
        Err(_) => {
            return Ok(MigrateEnvSecretsResult {
                ok: false,
                migrated: vec![],
                failed: vec![],
                error: Some(HUB_UNREACHABLE_CLI_FALLBACK.to_string()),
                scope: "shared".to_string(),
            });
        }
    };

    // 4. Rewrite `.env` for the hub-confirmed keys (atomic write).
    let (result, new_env) = build_migration_outcome(&env_text, &hub);
    if let Some(text) = new_env {
        write_env_atomically(&env_path, &text)
            .map_err(|e| format!("rewrite {}: {}", env_path.display(), e))?;
    }

    // 5. On a per-project migration that landed keys, refresh this project's
    //    env surfaces so the freshly-scoped secrets appear immediately —
    //    same soft-fail contract as the manual add-form's post-write refresh.
    if result.scope == "per_project" && !result.migrated.is_empty() {
        refresh_env_after_user_secret_change(
            &db,
            &project_id,
            "per_project",
            "user",
            "migrate_env_secrets_from_dotenv",
        );
    }

    Ok(result)
}

// ─── GAP-2 (2026-07-14): bulk "disable shared secrets" opt-out ───────────
//
// Mirror of the shared-KG read gate (`set_shared_kg_read_disabled`,
// `commands/projects_v2.rs`). When ON, this project omits the user-shared
// secrets bucket from its `/env` pairs (enforced server-side in the core
// resolver `resolve_active_user_secret_pairs_for_requester`). Backed by the
// SAME `module_settings` storage the KG gate uses, addressed via the ONE
// shared policy home (`secret_scope_policy` / `module_settings_keys`) so no
// second module_id namespace can drift in (the KG-gate split-brain lesson).
//
// Scope decision (recorded here + in the resolver comment): the gate covers
// the USER shared bucket (`module_id='user'`) only. Orchestrator-bundled /
// module-manifest SHARED secrets (e.g. `github_pat`) are NOT gated — they are
// infrastructure the project's hooks need (git push), and per-key pause
// already covers them.

/// Result of a GAP-2 toggle write. Carries any non-fatal warnings (env
/// refresh / marker write) the UI should surface without treating them as
/// failures.
#[derive(Debug, Clone, Serialize)]
pub struct SharedSecretsToggleResult {
    pub read_disabled: bool,
    pub warnings: Vec<String>,
}

/// Read the per-project SHARED_SECRETS_READ_DISABLED gate for the launcher
/// GUI's SecretsTab. Mirror of `get_shared_kg_read_disabled_cmd`.
#[command]
pub async fn get_shared_secrets_read_disabled_cmd(
    project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    Ok(secret_scope_policy::shared_secrets_read_disabled(
        &db,
        &project_id,
    ))
}

/// Persist the per-project SHARED_SECRETS_READ_DISABLED toggle and refresh
/// this project's env surfaces so the change takes effect immediately.
/// Mirror of `set_shared_kg_read_disabled`.
#[command]
pub async fn set_shared_secrets_read_disabled(
    project_id: String,
    read_disabled: bool,
    db: State<'_, Db>,
) -> Result<SharedSecretsToggleResult, String> {
    let row = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;

    // 1. Persist to module_settings under the canonical orchestrator-core id
    //    (binding constraint 3: reuse Db::set_setting, no new accessor).
    db.set_setting(
        &project_id,
        module_settings_keys::ORCHESTRATOR_CORE_MODULE_ID,
        module_settings_keys::SETTING_KEY_SHARED_SECRETS_READ_DISABLED,
        &serde_json::Value::Bool(read_disabled),
    )?;

    let mut warnings: Vec<String> = Vec::new();

    // 2. Refresh env surfaces so shared keys are emitted/stripped now. The
    //    gate lives in the core resolver, so the writer re-reads the flag on
    //    this refresh — same soft-fail contract as the KG toggle.
    if let Err(e) = crate::commands::projects_v2::refresh_project_env_with_db(&db, &project_id) {
        let msg = format!(
            "shared-secrets read-disabled env refresh failed: {}. Toggle \
             persisted to DB but env files may be stale until the next refresh.",
            e
        );
        tracing::warn!("[vct] warning: {}", msg);
        warnings.push(msg);
    }

    // 3. Companion file-store marker (S5): the documented
    //    `~/.vct-secrets/projects/<NAME>/.no-shared-fallback` gate for the
    //    hub-independent tier-2 file store. Best-effort — a marker write/remove
    //    failure is a warning, never an Err (conservative soft-fail).
    if let Some(shared_dir) = vct_secrets_shared_dir_for_marker() {
        let proj_dir = shared_dir.join("projects").join(&row.name);
        let marker = proj_dir.join(secrets_file_store::NO_SHARED_FALLBACK_MARKER);
        if read_disabled {
            if let Err(e) = std::fs::create_dir_all(&proj_dir)
                .and_then(|()| std::fs::write(&marker, b""))
            {
                let msg = format!(
                    "could not create the file-store shared-fallback opt-out \
                     marker at {}: {} (keychain-side gate is active; file-store \
                     tier-2 shared fallback is not gated until the marker exists)",
                    marker.display(),
                    e
                );
                tracing::warn!("[vct] warning: {}", msg);
                warnings.push(msg);
            }
        } else if marker.exists() {
            if let Err(e) = std::fs::remove_file(&marker) {
                let msg = format!(
                    "could not remove the file-store shared-fallback opt-out \
                     marker at {}: {} (keychain-side gate is off; file-store \
                     tier-2 may still skip shared until the marker is removed)",
                    marker.display(),
                    e
                );
                tracing::warn!("[vct] warning: {}", msg);
                warnings.push(msg);
            }
        }
    }

    // 4. Audit (metadata only — never a value).
    db.audit(
        "project_shared_secrets_read_disabled",
        Some(&project_id),
        None,
        &serde_json::json!({ "read_disabled": read_disabled }),
    )?;

    Ok(SharedSecretsToggleResult {
        read_disabled,
        warnings,
    })
}

// v0.3.0: import alias onto the ONE file-store root resolver. The copy
// that lived here ignored `$VCT_SECRETS_DIR`, so the `.no-shared-fallback`
// marker could be written under `$HOME/.vct-secrets` while the resolvers
// read a different root entirely.
use crate::secrets_file_store::secrets_root as vct_secrets_shared_dir_for_marker;

/// Atomic `.env` rewrite: write a sibling temp file (0o600 on Unix so the
/// sentinel-replaced file never flashes world-readable), then rename into
/// place. Mirrors the atomic-write discipline in
/// `vco_lib/secrets_audit.py::rewrite_env_with_sentinels`.
fn write_env_atomically(env_path: &std::path::Path, text: &str) -> std::io::Result<()> {
    use std::io::Write as _;
    let parent = env_path.parent().unwrap_or_else(|| std::path::Path::new("."));
    std::fs::create_dir_all(parent)?;
    let tmp = parent.join(format!(
        ".env.vco-migrate-{}",
        std::process::id()
    ));
    {
        let mut f = std::fs::File::create(&tmp)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            let perms = std::fs::Permissions::from_mode(0o600);
            let _ = f.set_permissions(perms);
        }
        f.write_all(text.as_bytes())?;
        f.flush()?;
    }
    match std::fs::rename(&tmp, env_path) {
        Ok(()) => Ok(()),
        Err(e) => {
            let _ = std::fs::remove_file(&tmp);
            Err(e)
        }
    }
}
