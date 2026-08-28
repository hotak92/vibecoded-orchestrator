//! Module settings — non-sensitive per-project config values stored as JSON.
//! Secrets use the OS keychain (see `crate::secrets`), not this table.

use rusqlite::{params, OptionalExtension};
use serde_json::Value;

use super::Db;

/// Canonical key for the per-project enable flag of a (potentially
/// global-scope) module. v0.2.49 Stream B — closes the gap where a
/// module installed at global scope (e.g. `vct-rl-reranker` shared
/// across the host's projects) had no way to be silenced per-project.
///
/// Value semantics: stored as a JSON boolean (`true` / `false`).
/// Default when no row exists: `true` (enabled). The reader
/// (`module_is_enabled_for_project`) treats both "row absent" and
/// "row present but malformed" as enabled, so a corrupted setting
/// can never silently turn off a module the user expects to work.
///
/// Coordination with the per-project enable toggle for *project-scope*
/// modules:
///   * The legacy `module_installs.enabled` column already gates
///     per-project-installed modules — that surface stays unchanged.
///   * This key is the *additional* gate for global-scope modules
///     where a single install row exists (or none, when the module
///     binds to the orchestrator-root project) but per-project routing
///     decisions still need a yes/no flag.
///
/// See the v0.2.49 global-install-per-project-routing plan for the
/// design rationale.
pub const MODULE_ENABLED_FOR_PROJECT_KEY: &str = "enabled_for_project";

impl Db {
    /// Read the per-project enable flag for a module. Returns `true`
    /// when the row is absent, present with `true`, or present but
    /// malformed (fail-open: a corrupted setting never silently
    /// disables a module the user expects to work).
    ///
    /// Returns `false` only when the row exists AND its value is the
    /// JSON literal `false`.
    pub fn module_is_enabled_for_project(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<bool, String> {
        match self.get_setting(project_id, module_id, MODULE_ENABLED_FOR_PROJECT_KEY)? {
            None => Ok(true),
            Some(Value::Bool(b)) => Ok(b),
            // Malformed (string, number, null, object, array) — fail open.
            // The setter only ever writes Bool, so a non-Bool here means
            // either a hand-edited row or a schema-version mismatch.
            Some(_) => Ok(true),
        }
    }

    /// Write the per-project enable flag for a module. Idempotent
    /// upsert. Always writes a JSON boolean so the reader's strict
    /// `Bool` match path is the fast path.
    ///
    /// Thin wrapper over [`Db::module_write_enabled_for_project`] — the
    /// seeding paths (project create / module install) only ever write an
    /// explicit value, so they keep this two-argument shape while the
    /// tri-state GUI setter uses the `Option` form. ONE writer underneath.
    pub fn module_set_enabled_for_project(
        &self,
        project_id: &str,
        module_id: &str,
        enabled: bool,
    ) -> Result<(), String> {
        self.module_write_enabled_for_project(project_id, module_id, Some(enabled))
    }

    /// Write — or CLEAR — the per-project enable flag for a module.
    ///
    /// `value = None` DELETES the row, returning the project to inheriting
    /// the host-wide default. Without a clear, the per-project control is a
    /// one-way door: once a user has clicked either side there is no way
    /// back to "no override set" (the F-4 dead end, at the per-project
    /// layer). Same shape as WP-L's `Db::set_dual_flag_for_project`.
    ///
    /// Idempotent in both directions (an upsert; a delete of an absent row).
    pub fn module_write_enabled_for_project(
        &self,
        project_id: &str,
        module_id: &str,
        value: Option<bool>,
    ) -> Result<(), String> {
        match value {
            Some(enabled) => self.set_setting(
                project_id,
                module_id,
                MODULE_ENABLED_FOR_PROJECT_KEY,
                &Value::Bool(enabled),
            ),
            None => {
                self.delete_setting(project_id, module_id, MODULE_ENABLED_FOR_PROJECT_KEY)
            }
        }
    }

    /// Delete the per-project enable flag row for a module across
    /// *every* project. Called from the uninstall path of a global-
    /// scope module so the seeded rows don't outlive the module.
    /// Returns the number of rows actually removed. Idempotent.
    pub fn module_clear_enabled_for_project_all(
        &self,
        module_id: &str,
    ) -> Result<usize, String> {
        let guard = self.lock();
        let removed = guard
            .execute(
                "DELETE FROM module_settings
                  WHERE module_id = ?1 AND setting_key = ?2",
                params![module_id, MODULE_ENABLED_FOR_PROJECT_KEY],
            )
            .map_err(|e| format!("module_clear_enabled_for_project_all: {}", e))?;
        Ok(removed)
    }

    // ─── v0.2.52 V52-AD — host-wide GLOBAL toggle ───────────────────────
    //
    // Stored as a row in `module_settings` with `project_id IS NULL` —
    // see migration 034's docstring. The reader cascade is:
    //
    //     effective_enabled =
    //         per_project_setting (project_id = $project)
    //             .unwrap_or(global_default (project_id IS NULL))
    //             .unwrap_or(true)   -- fail-open
    //
    // The `enabled_for_project` setting_key is reused (NOT a new key) so
    // a single uninstall-time DELETE WHERE setting_key = ... still cleans
    // both per-project AND global rows.

    /// Read the GLOBAL (host-wide) enable flag for a module. Returns
    /// `None` when no global row exists (caller should fall back to the
    /// system default — typically `true`). Returns `Some(false)` only
    /// when the row exists AND its value is the JSON literal `false`.
    /// Malformed values fail open to `Some(true)` per the same contract
    /// as the per-project reader.
    pub fn module_global_enabled(
        &self,
        module_id: &str,
    ) -> Result<Option<bool>, String> {
        let guard = self.lock();
        let row: Option<String> = guard
            .query_row(
                "SELECT setting_value FROM module_settings
                  WHERE project_id IS NULL
                    AND module_id = ?1
                    AND setting_key = ?2",
                params![module_id, MODULE_ENABLED_FOR_PROJECT_KEY],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| format!("module_global_enabled read: {}", e))?;

        match row {
            None => Ok(None),
            Some(s) => match serde_json::from_str::<Value>(&s) {
                Ok(Value::Bool(b)) => Ok(Some(b)),
                // Malformed → fail open to enabled (matches per-project
                // contract: a corrupted setting never silently disables).
                Ok(_) | Err(_) => Ok(Some(true)),
            },
        }
    }

    /// Write the GLOBAL (host-wide) enable flag for a module. Idempotent
    /// upsert. Always writes a JSON boolean. The conflict target is the
    /// partial unique index `idx_ms_unique_global` (migration 034) so
    /// the standard `ON CONFLICT(project_id, module_id, setting_key)`
    /// shape used by `set_setting` would NOT trigger here — we use an
    /// explicit DELETE + INSERT to keep the upsert semantics clear and
    /// independent of the partial-index conflict target.
    pub fn module_set_global_enabled(
        &self,
        module_id: &str,
        enabled: bool,
    ) -> Result<(), String> {
        let encoded = serde_json::to_string(&Value::Bool(enabled))
            .map_err(|e| format!("module_set_global_enabled encode: {}", e))?;
        let guard = self.lock();
        // Single transaction: delete any pre-existing global row, then
        // insert the new one. Cheaper than reasoning about partial-
        // index ON CONFLICT semantics, and the partial unique index
        // still enforces correctness if a concurrent writer races.
        let tx = guard
            .unchecked_transaction()
            .map_err(|e| format!("module_set_global_enabled txn: {}", e))?;
        tx.execute(
            "DELETE FROM module_settings
              WHERE project_id IS NULL
                AND module_id = ?1
                AND setting_key = ?2",
            params![module_id, MODULE_ENABLED_FOR_PROJECT_KEY],
        )
        .map_err(|e| format!("module_set_global_enabled delete: {}", e))?;
        tx.execute(
            "INSERT INTO module_settings (project_id, module_id, setting_key, setting_value)
             VALUES (NULL, ?1, ?2, ?3)",
            params![module_id, MODULE_ENABLED_FOR_PROJECT_KEY, encoded],
        )
        .map_err(|e| format!("module_set_global_enabled insert: {}", e))?;
        tx.commit()
            .map_err(|e| format!("module_set_global_enabled commit: {}", e))?;
        Ok(())
    }

    /// Delete the GLOBAL (host-wide) enable row for a module, returning the
    /// host to the system default (fail-open `true`). Returns the number of
    /// rows removed; idempotent.
    ///
    /// v0.2.91 decision #23 (F-4). [`Db::module_set_global_enabled`] is
    /// DELETE+INSERT — it can never leave the row ABSENT — and the only
    /// method that removed it was the uninstall-time
    /// [`Db::module_clear_enabled_for_project_all`], which wipes EVERY
    /// project's row as well and was never exposed as a command. So `None`
    /// (a real, persistent state on every fresh install: "no host-wide
    /// override set") became unreachable the moment a user clicked either
    /// button. This is the way back, and it touches ONLY the
    /// `project_id IS NULL` row — per-project overrides survive.
    pub fn module_clear_global_enabled(&self, module_id: &str) -> Result<usize, String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM module_settings
                  WHERE project_id IS NULL
                    AND module_id = ?1
                    AND setting_key = ?2",
                params![module_id, MODULE_ENABLED_FOR_PROJECT_KEY],
            )
            .map_err(|e| format!("module_clear_global_enabled: {}", e))
    }

    /// Effective enable flag for a (project, module) pair using the
    /// v0.2.52 V52-AD cascade. Reader contract:
    ///
    /// 1. If a per-project row exists for `(project_id, module_id,
    ///    enabled_for_project)`, return its boolean value (fail-open
    ///    on malformed values).
    /// 2. Else if a GLOBAL row exists (`project_id IS NULL`), return
    ///    its boolean value (same fail-open contract).
    /// 3. Else return `true` (system default — fail-open).
    ///
    /// This is the function the hub resolver should call when deciding
    /// `rl_reranker_enabled_for_project`. The legacy
    /// `module_is_enabled_for_project` still works (collapses the
    /// cascade after step 1 → step 3) and is preserved for any caller
    /// that doesn't want the global default factored in.
    pub fn module_effective_enabled(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<bool, String> {
        // Step 1: per-project row (explicit override).
        if let Some(value) =
            self.get_setting(project_id, module_id, MODULE_ENABLED_FOR_PROJECT_KEY)?
        {
            return match value {
                Value::Bool(b) => Ok(b),
                // Malformed → fail open (matches per-project contract).
                _ => Ok(true),
            };
        }
        // Step 2: global fallback (host-wide default).
        if let Some(b) = self.module_global_enabled(module_id)? {
            return Ok(b);
        }
        // Step 3: system default.
        Ok(true)
    }
}

// ─── v0.2.91 decision #23 — the module-enable cascade, WITH PROVENANCE ───
//
// `module_effective_enabled` above answers "on or off?". That is all the hub
// resolver needs, and it is NOT enough for a control: a project inheriting a
// host-wide `false` and a project that explicitly chose `false` render
// identically from the boolean alone, so a two-state control rendered from it
// cannot say which one the user is looking at — and cannot offer the way back
// to inheriting. That is the same lying-toggle shape WP-L's `DualFlagState`
// exists to prevent, so this mirrors it deliberately: same three-variant
// source enum, same `explicit: Option<bool>` field, same "resolved state is
// returned by the setter so the GUI re-renders from truth" contract.
//
// Deliberate differences from `DualFlagState`, both forced by this cascade:
//
//   * `install_default` is `Option<bool>` here, not `bool`. A dual flag's
//     absent host-wide row means `false`; an absent module-enable row means
//     "no host-wide choice", which resolves to the fail-open `true`. Folding
//     that into a bare bool would erase the very state F-4 could not re-enter.
//   * there is no `clamped` field — module-enable has no cross-flag
//     dependency to clamp.
//
// WHAT THIS GATES (plan §F #25, USER standing constraint): reranking only.
// Nothing in this cascade is read by any telemetry-collection path — the hub's
// `POST /api/v1/rl/events` ingest and `Db::insert_rl_event` never consult
// `module_settings`, `module_installs`, or any enable flag, and must not start.
// A project with the RL module DISABLED, or with no install row at all, keeps
// collecting events. Do not add a module-presence check to a collection path.

/// Where a resolved module-enable value came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ModuleEnableSource {
    /// An explicit per-project row (either value).
    Project,
    /// No per-project row; the host-wide default row supplied it.
    GlobalDefault,
    /// Neither tier had a row; fail-open `true` by system default.
    SystemDefault,
}

/// Resolved enable state of one module for one project.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub struct ModuleEnableState {
    /// The explicit per-project row, if one exists. `None` = inheriting.
    pub explicit: Option<bool>,
    /// The host-wide default row, if one exists. `None` = none set (which
    /// resolves to the fail-open system default, NOT to `false`).
    pub global_default: Option<bool>,
    /// What the hub resolver serves — identical to
    /// [`Db::module_effective_enabled`] by construction (see the test).
    pub effective: bool,
    /// Which tier supplied `effective`.
    pub source: ModuleEnableSource,
}

impl Db {
    /// Resolve one (project, module) enable state with provenance.
    ///
    /// Same three tiers, same fail-open behaviour, same malformed-value
    /// handling as [`Db::module_effective_enabled`] — this reports WHICH tier
    /// answered in addition to the answer. The two must never disagree; the
    /// `resolve_module_enable_matches_effective` test pins that.
    pub fn resolve_module_enable(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<ModuleEnableState, String> {
        // Malformed values fail open to `true` in BOTH tiers, exactly as the
        // legacy readers do — a corrupted row must never silently disable a
        // module the user expects to work.
        let explicit = self
            .get_setting(project_id, module_id, MODULE_ENABLED_FOR_PROJECT_KEY)?
            .map(|v| match v {
                Value::Bool(b) => b,
                _ => true,
            });
        let global_default = self.module_global_enabled(module_id)?;
        let (effective, source) = match (explicit, global_default) {
            (Some(b), _) => (b, ModuleEnableSource::Project),
            (None, Some(b)) => (b, ModuleEnableSource::GlobalDefault),
            (None, None) => (true, ModuleEnableSource::SystemDefault),
        };
        Ok(ModuleEnableState {
            explicit,
            global_default,
            effective,
            source,
        })
    }
}

// ─── v0.2.91 WP-L — dual-embedding / RL-log flags: host-wide defaults ────
//
// The three "dual" flags (`dual_embedding_write_all_slots`,
// `dual_rl_log_enabled`, `dual_embedding_arctic_secondary`) gained an
// install-wide default tier in v0.2.91 (plan decision #22). Before that they
// were per-project-only, read everywhere as
// `get_setting(...).as_bool().unwrap_or(false)` — a shape that collapses "no
// row" into `false` and therefore CANNOT express the new middle tier.
//
// Three tiers, resolved per flag:
//
//   1. explicit per-project `module_settings` row → wins IN BOTH DIRECTIONS
//      (an explicit `false` beats a host-wide default of `true`);
//   2. host-wide default from `app_state` (`embedding.dual_*_default`);
//   3. `false` (system default).
//
// ## The cross-tier clamp (the part decision #22's wording does not cover)
//
// RL dual-logging requires dual-write: you cannot log into a secondary slot
// that is not being populated. The per-project setters have always enforced
// that WITHIN one tier (turning the log on force-enables write; turning write
// off force-disables the log), so the DB could never hold the incoherent
// `(log = true, write = false)` pair. A second tier reopens it ACROSS tiers:
//
//     host-wide `dual_rl_log_default = true`
//   + project explicitly sets `write = false`
//   = resolved (log = true, write = false)  ← incoherent, and reachable
//
// So the cascade is re-derived on the RESOLVED values, in ONE place — here:
//
//   * the dependent flag CLAMPS DOWN: `resolved_log &&= resolved_write`;
//   * an inherited `log = true` NEVER forces `write = true` (clamping up
//     would let a host-wide default silently overrule an explicit
//     per-project `write = false`, which is exactly what tier 1 forbids).
//
// ## Cross-surface lockstep (must match, byte-for-byte)
//
// This resolver is the ONE home. Two other surfaces read the same rows in
// other processes and MUST agree with it:
//
//   * the hub `/config` resolver — `vct-hub/src/config_api.rs` (calls
//     `resolve_dual_flags` directly; same crate, so no mirror);
//   * the Python env projection —
//     `vco_lib/config_projection.py::_resolve_dual_flags_cascade` (a mirror:
//     different language, no interpreter on the projection hot path — tier C
//     of the A>B>C rule, locked by
//     `tests/test_dual_flags_cascade_parity_v0291.py`).
//
// Drift between them is the Defect-D class of GUI-write-vs-hub-read
// disagreement. Change one, change all three, and update the parity test's
// shared truth table.

/// Host-wide (`app_state`) default key for `dual_embedding_write_all_slots`.
/// Absent row ⇒ `false`. Written only by
/// [`Db::set_dual_flag_global_default`], which applies the global-tier cascade.
pub const APP_STATE_KEY_DUAL_WRITE_DEFAULT: &str = "embedding.dual_write_default";

/// Host-wide (`app_state`) default key for `dual_rl_log_enabled`.
pub const APP_STATE_KEY_DUAL_RL_LOG_DEFAULT: &str = "embedding.dual_rl_log_default";

/// Host-wide (`app_state`) default key for `dual_embedding_arctic_secondary`.
pub const APP_STATE_KEY_DUAL_ARCTIC_DEFAULT: &str = "embedding.dual_arctic_default";

/// `module_settings.module_id` the two embedding-scope dual flags live under.
/// Same value as [`super::module_settings_keys::ORCHESTRATOR_CORE_MODULE_ID`];
/// re-stated here so the parity test has ONE Rust file to parse for the whole
/// dual-flag addressing table.
pub const DUAL_FLAG_MODULE_ID_ORCHESTRATOR_CORE: &str = "orchestrator-core";

/// `module_settings.module_id` the RL-scope dual flag lives under.
pub const DUAL_FLAG_MODULE_ID_RL_RERANKER: &str = "vct-rl-reranker";

/// Per-project setting key: write embeddings to ALL named-vector slots.
pub const SETTING_KEY_DUAL_WRITE_ALL_SLOTS: &str = "dual_embedding_write_all_slots";

/// Per-project setting key: also log RL events under the secondary slot.
pub const SETTING_KEY_DUAL_RL_LOG: &str = "dual_rl_log_enabled";

/// Per-project setting key: also write a secondary arctic embedding slot.
pub const SETTING_KEY_DUAL_ARCTIC_SECONDARY: &str = "dual_embedding_arctic_secondary";

/// One of the three dual flags. The wire name is what the Tauri commands
/// accept from the GUI; unknown names are REJECTED rather than silently
/// no-op'd (a command that quietly ignores a value is the same shipped lie as
/// a toggle with no consumer).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DualFlag {
    WriteAllSlots,
    RlLog,
    ArcticSecondary,
}

impl DualFlag {
    /// Every flag, in the order the GUI renders them.
    pub const ALL: [DualFlag; 3] = [
        DualFlag::WriteAllSlots,
        DualFlag::RlLog,
        DualFlag::ArcticSecondary,
    ];

    /// Parse the wire name the GUI sends. Unknown ⇒ explicit error.
    pub fn from_wire(name: &str) -> Result<Self, String> {
        match name {
            "write_all_slots" => Ok(DualFlag::WriteAllSlots),
            "rl_log" => Ok(DualFlag::RlLog),
            "arctic_secondary" => Ok(DualFlag::ArcticSecondary),
            other => Err(format!(
                "unknown dual flag '{}' (expected one of: write_all_slots, \
                 rl_log, arctic_secondary)",
                other
            )),
        }
    }

    /// The wire name. Stable — the GUI and the Tauri commands agree on it.
    pub fn wire_name(self) -> &'static str {
        match self {
            DualFlag::WriteAllSlots => "write_all_slots",
            DualFlag::RlLog => "rl_log",
            DualFlag::ArcticSecondary => "arctic_secondary",
        }
    }

    /// `module_settings.module_id` this flag's per-project row lives under.
    pub fn module_id(self) -> &'static str {
        match self {
            DualFlag::RlLog => DUAL_FLAG_MODULE_ID_RL_RERANKER,
            _ => DUAL_FLAG_MODULE_ID_ORCHESTRATOR_CORE,
        }
    }

    /// `module_settings.setting_key` for the per-project row.
    pub fn setting_key(self) -> &'static str {
        match self {
            DualFlag::WriteAllSlots => SETTING_KEY_DUAL_WRITE_ALL_SLOTS,
            DualFlag::RlLog => SETTING_KEY_DUAL_RL_LOG,
            DualFlag::ArcticSecondary => SETTING_KEY_DUAL_ARCTIC_SECONDARY,
        }
    }

    /// `app_state` key for the host-wide default.
    pub fn app_state_key(self) -> &'static str {
        match self {
            DualFlag::WriteAllSlots => APP_STATE_KEY_DUAL_WRITE_DEFAULT,
            DualFlag::RlLog => APP_STATE_KEY_DUAL_RL_LOG_DEFAULT,
            DualFlag::ArcticSecondary => APP_STATE_KEY_DUAL_ARCTIC_DEFAULT,
        }
    }
}

/// Where a resolved dual flag's stored intent came from.
///
/// Deliberately three variants, not two: unlike `ACTIVE_EMBEDDING` (whose
/// per-project row is either a sticky pick or absent) a dual flag has a
/// meaningful explicit `false` that differs from "unset", and the GUI must be
/// able to say which of the two it is showing.
///
/// Note the split of responsibilities with [`DualFlagState::clamped`]:
/// `source` describes where the flag's OWN stored intent came from;
/// `clamped` says whether the log⟹write dependency overrode that intent when
/// computing `effective`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DualFlagSource {
    /// An explicit per-project `module_settings` row (either value).
    Project,
    /// No per-project row; the host-wide `app_state` default supplied it.
    InstallDefault,
    /// Neither tier had a row; `false` by system default.
    SystemDefault,
}

/// Resolved state of one dual flag for one project.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub struct DualFlagState {
    /// The explicit per-project row, if one exists. `None` = inheriting.
    pub explicit: Option<bool>,
    /// The host-wide default (`false` when no `app_state` row exists).
    pub install_default: bool,
    /// What consumers actually see, AFTER the log⟹write clamp.
    pub effective: bool,
    /// Provenance of the stored intent (NOT of the clamp — see `clamped`).
    pub source: DualFlagSource,
    /// `true` when the log⟹write dependency forced `effective` below the
    /// value `explicit`/`install_default` would otherwise have produced.
    /// Only ever `true` for [`DualFlag::RlLog`]. The GUI renders a distinct
    /// line for this case, because "Off — host default" would misattribute
    /// the cause.
    pub clamped: bool,
}

/// All three flags for one project, resolved in one pass.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub struct DualFlagsState {
    pub write_all_slots: DualFlagState,
    pub rl_log: DualFlagState,
    pub arctic_secondary: DualFlagState,
}

impl DualFlagsState {
    /// Borrow one flag's state by enum.
    pub fn get(&self, flag: DualFlag) -> &DualFlagState {
        match flag {
            DualFlag::WriteAllSlots => &self.write_all_slots,
            DualFlag::RlLog => &self.rl_log,
            DualFlag::ArcticSecondary => &self.arctic_secondary,
        }
    }
}

/// The three host-wide defaults, for the global panel.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub struct DualFlagGlobalDefaults {
    pub write_all_slots: bool,
    pub rl_log: bool,
    pub arctic_secondary: bool,
}

impl Db {
    /// Read the explicit per-project row for one dual flag.
    ///
    /// `None` = no row (inheriting). A malformed (non-bool) value is treated
    /// as **absent** rather than fail-open-to-true: unlike
    /// `enabled_for_project` (where fail-open keeps a module working), these
    /// flags are opt-in cost multipliers — a corrupt row must not silently
    /// turn on extra embedding calls. Soft-fail on DB errors.
    pub fn dual_flag_explicit(
        &self,
        project_id: &str,
        flag: DualFlag,
    ) -> Option<bool> {
        self.get_setting(project_id, flag.module_id(), flag.setting_key())
            .ok()
            .flatten()
            .and_then(|v| v.as_bool())
    }

    /// Read the host-wide default for one dual flag. Absent row ⇒ `false`.
    pub fn dual_flag_install_default(&self, flag: DualFlag) -> bool {
        self.app_state_get_bool(flag.app_state_key())
            .ok()
            .flatten()
            .unwrap_or(false)
    }

    /// The three host-wide defaults in one read.
    pub fn dual_flag_global_defaults(&self) -> DualFlagGlobalDefaults {
        DualFlagGlobalDefaults {
            write_all_slots: self.dual_flag_install_default(DualFlag::WriteAllSlots),
            rl_log: self.dual_flag_install_default(DualFlag::RlLog),
            arctic_secondary: self
                .dual_flag_install_default(DualFlag::ArcticSecondary),
        }
    }

    /// Resolve all three dual flags for a project — THE cascade.
    ///
    /// See the module-level block above for the tier order, the cross-tier
    /// clamp, and the two other surfaces that must stay in lockstep with it.
    pub fn resolve_dual_flags(&self, project_id: &str) -> DualFlagsState {
        // Tier 1 (explicit row) + tier 2 (host-wide default) + tier 3
        // (`false`), independently per flag. `pre_clamp` is the value BEFORE
        // the dependency is applied.
        //
        // NOTE (plan §F #25 — telemetry collection is module-independent):
        // `DualFlag::RlLog` addresses a `module_settings` row under module_id
        // `vct-rl-reranker`, but NOTHING here consults `module_installs` or
        // any enabled-flag. The paid module gates RERANKING, never
        // COLLECTION — a project with the module absent resolves the log
        // flag exactly the same way. Do not add a module-presence check.
        let read = |flag: DualFlag| -> (Option<bool>, bool, bool) {
            let explicit = self.dual_flag_explicit(project_id, flag);
            let install_default = self.dual_flag_install_default(flag);
            (explicit, install_default, explicit.unwrap_or(install_default))
        };
        let write = read(DualFlag::WriteAllSlots);
        let log = read(DualFlag::RlLog);
        let arctic = read(DualFlag::ArcticSecondary);

        // Cross-tier clamp: the dependent flag can only be forced DOWN.
        let write_effective = write.2;
        let log_effective = log.2 && write_effective;
        let arctic_effective = arctic.2;

        let build =
            |(explicit, install_default, pre_clamp): (Option<bool>, bool, bool),
             effective: bool| DualFlagState {
                explicit,
                install_default,
                effective,
                source: match explicit {
                    Some(_) => DualFlagSource::Project,
                    None if install_default => DualFlagSource::InstallDefault,
                    None => DualFlagSource::SystemDefault,
                },
                clamped: effective != pre_clamp,
            };

        DualFlagsState {
            write_all_slots: build(write, write_effective),
            rl_log: build(log, log_effective),
            arctic_secondary: build(arctic, arctic_effective),
        }
    }

    /// Set (or CLEAR) one dual flag for one project.
    ///
    /// `value = None` DELETES the per-project row, returning the project to
    /// inheriting the host-wide default. Without this the tri-state control
    /// would be a one-way door — the dead end `GlobalModuleTogglesPanel`
    /// shipped with.
    ///
    /// The within-tier coherence cascade is preserved, but is now written
    /// against the RESOLVED values so it does not create rows it does not
    /// need:
    ///
    ///   * turning the log ON force-enables dual-write ONLY when dual-write
    ///     does not already RESOLVE to on (so a project inheriting a
    ///     host-wide `write = true` keeps inheriting instead of being pinned);
    ///   * turning dual-write OFF writes an explicit `log = false` row ONLY
    ///     when the log currently RESOLVES to on (an explicit row, not a
    ///     delete, because a host-wide `log = true` would otherwise revive it).
    ///
    /// Arctic-secondary is independent — no cascade in either direction.
    pub fn set_dual_flag_for_project(
        &self,
        project_id: &str,
        flag: DualFlag,
        value: Option<bool>,
    ) -> Result<(), String> {
        if project_id.is_empty() {
            return Err("set_dual_flag_for_project: project_id required".into());
        }

        // Cascade FIRST for the log-on case, so an observer can never read
        // (log = true, write = false) between the two writes.
        if flag == DualFlag::RlLog
            && value == Some(true)
            && !self.resolve_dual_flags(project_id).write_all_slots.effective
        {
            self.write_dual_flag_row(project_id, DualFlag::WriteAllSlots, Some(true))?;
        }

        self.write_dual_flag_row(project_id, flag, value)?;

        // Cascade AFTER for the write-off case: the dependent cannot outlive
        // its prerequisite. `value == None` can also drop write to off (when
        // the host-wide default is off), so re-resolve rather than testing
        // the requested value.
        if flag == DualFlag::WriteAllSlots {
            let resolved = self.resolve_dual_flags(project_id);
            // Pre-clamp log value: what the log WOULD resolve to if the
            // dependency were not applied. Only that case needs a row —
            // an explicit `false` (not a delete) so a host-wide log default
            // cannot revive it behind the user's back.
            let log_pre_clamp = resolved
                .rl_log
                .explicit
                .unwrap_or(resolved.rl_log.install_default);
            if !resolved.write_all_slots.effective && log_pre_clamp {
                self.write_dual_flag_row(project_id, DualFlag::RlLog, Some(false))?;
            }
        }
        Ok(())
    }

    /// Write-or-delete one per-project dual-flag row. No cascade.
    fn write_dual_flag_row(
        &self,
        project_id: &str,
        flag: DualFlag,
        value: Option<bool>,
    ) -> Result<(), String> {
        match value {
            Some(v) => self.set_setting(
                project_id,
                flag.module_id(),
                flag.setting_key(),
                &Value::Bool(v),
            ),
            None => self.delete_setting(project_id, flag.module_id(), flag.setting_key()),
        }
    }

    /// Set one host-wide dual-flag default, applying the log⟹write cascade
    /// AT THE GLOBAL TIER (decision #22: "the cascade holds at the global
    /// tier too"). Same shape as the per-project setter:
    ///
    ///   * log default ON ⇒ write default ON;
    ///   * write default OFF ⇒ log default OFF.
    ///
    /// Callers are responsible for re-projecting env afterwards — this
    /// function only owns the DB truth.
    pub fn set_dual_flag_global_default(
        &self,
        flag: DualFlag,
        value: bool,
    ) -> Result<(), String> {
        if flag == DualFlag::RlLog && value {
            // Prerequisite first, so no observer sees the incoherent pair.
            self.app_state_set_bool(DualFlag::WriteAllSlots.app_state_key(), true)?;
        }
        self.app_state_set_bool(flag.app_state_key(), value)?;
        if flag == DualFlag::WriteAllSlots && !value {
            self.app_state_set_bool(DualFlag::RlLog.app_state_key(), false)?;
        }
        Ok(())
    }
}

impl Db {
    pub fn get_setting(
        &self,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<Option<Value>, String> {
        let guard = self.lock();
        let row: Option<String> = guard
            .query_row(
                "SELECT setting_value FROM module_settings
                  WHERE project_id = ?1 AND module_id = ?2 AND setting_key = ?3",
                params![project_id, module_id, key],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| format!("get setting: {}", e))?;

        match row {
            None => Ok(None),
            Some(s) => serde_json::from_str(&s)
                .map(Some)
                .map_err(|e| format!("parse setting json: {}", e)),
        }
    }

    pub fn set_setting(
        &self,
        project_id: &str,
        module_id: &str,
        key: &str,
        value: &Value,
    ) -> Result<(), String> {
        let encoded = serde_json::to_string(value)
            .map_err(|e| format!("encode setting: {}", e))?;
        let guard = self.lock();
        // v0.2.52 V52-AD — the table-level UNIQUE(project_id, module_id,
        // setting_key) was dropped by migration 034 (partial-index
        // replacement; see migration docstring). For SQLite's upsert
        // semantics to target the surviving partial index
        // `idx_ms_unique_per_project`, the ON CONFLICT clause must
        // include the partial index's WHERE predicate. Pre-034 callers
        // (passing a non-NULL project_id) behave identically — the
        // partial-index WHERE matches every such row.
        guard
            .execute(
                "INSERT INTO module_settings (project_id, module_id, setting_key, setting_value)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(project_id, module_id, setting_key)
                   WHERE project_id IS NOT NULL
                 DO UPDATE SET setting_value = excluded.setting_value",
                params![project_id, module_id, key, encoded],
            )
            .map_err(|e| format!("set setting: {}", e))?;
        Ok(())
    }

    pub fn list_module_settings(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<Vec<(String, Value)>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT setting_key, setting_value FROM module_settings
                  WHERE project_id = ?1 AND module_id = ?2
               ORDER BY setting_key ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![project_id, module_id], |r| {
                let key: String = r.get(0)?;
                let raw: String = r.get(1)?;
                Ok((key, raw))
            })
            .map_err(|e| format!("query: {}", e))?;

        let mut out = Vec::new();
        for row in rows {
            let (key, raw) = row.map_err(|e| format!("row: {}", e))?;
            let val: Value =
                serde_json::from_str(&raw).map_err(|e| format!("parse '{}': {}", key, e))?;
            out.push((key, val));
        }
        Ok(out)
    }

    /// v0.2.91 WP-F2 — read ONE setting key across EVERY project, as booleans.
    ///
    /// Exists for the tray-preference re-homing migration: `tray_close_to_tray`
    /// and `tray_start_minimized` are launcher-GLOBAL window behaviours but the
    /// Preferences page persisted them per selected project, so the legacy
    /// value could be sitting under any project id. The migration adopts them
    /// into `app_state` once (see `quit_dialog::adopt_legacy_pref`).
    ///
    /// Non-boolean rows are skipped rather than coerced — a value the launcher
    /// never wrote is not evidence of a user's choice. Returns rows in
    /// `project_id` order for deterministic logging; the adoption decision
    /// itself is order-independent.
    pub fn find_all_project_settings_bool(
        &self,
        module_id: &str,
        key: &str,
    ) -> Result<Vec<bool>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT setting_value FROM module_settings
                  WHERE module_id = ?1 AND setting_key = ?2 AND project_id IS NOT NULL
               ORDER BY project_id ASC",
            )
            .map_err(|e| format!("prepare: {}", e))?;
        let rows = stmt
            .query_map(params![module_id, key], |r| r.get::<_, String>(0))
            .map_err(|e| format!("query: {}", e))?;

        let mut out = Vec::new();
        for row in rows {
            let raw = row.map_err(|e| format!("row: {}", e))?;
            if let Ok(Value::Bool(b)) = serde_json::from_str::<Value>(&raw) {
                out.push(b);
            }
        }
        Ok(out)
    }

    pub fn clear_module_settings(
        &self,
        project_id: &str,
        module_id: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM module_settings WHERE project_id = ?1 AND module_id = ?2",
                params![project_id, module_id],
            )
            .map_err(|e| format!("clear settings: {}", e))?;
        Ok(())
    }

    /// Delete a single setting row. Idempotent: returns Ok(()) whether or
    /// not the row existed. Used by the SHARED_KG_OPT_OUT → SHARED_KG_WRITE_DISABLED
    /// migration helper to retire the legacy key after copying its value.
    pub fn delete_setting(
        &self,
        project_id: &str,
        module_id: &str,
        key: &str,
    ) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM module_settings
                  WHERE project_id = ?1 AND module_id = ?2 AND setting_key = ?3",
                params![project_id, module_id, key],
            )
            .map_err(|e| format!("delete setting: {}", e))?;
        Ok(())
    }
}

// ─── v0.2.49 Stream B — per-project enable toggle tests ──────────────────
#[cfg(test)]
mod enable_toggle_tests {
    use super::*;
    use crate::db::models::ProjectHost;

    fn db_with_project(slug: &str) -> (Db, String) {
        let db = Db::open_in_memory().expect("in-memory db");
        let id = format!("proj-{}", slug);
        db.insert_project(
            &id,
            &format!("Test {}", slug),
            &format!("/tmp/{}", slug),
            ProjectHost::Base,
            slug,
        )
        .expect("insert project");
        (db, id)
    }

    /// Default (no row): module reads as enabled. This is the
    /// fail-open contract: a fresh install must never start in a
    /// disabled state just because the seeding step skipped this
    /// (project, module) pair.
    #[test]
    fn module_is_enabled_for_project_default_true_when_row_absent() {
        let (db, pid) = db_with_project("a");
        let enabled = db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .expect("read");
        assert!(
            enabled,
            "absent row must read as enabled (fail-open default)"
        );
    }

    /// Set true → reads true. Set false → reads false. The setter
    /// is the canonical writer for the toggle.
    #[test]
    fn module_set_enabled_for_project_roundtrip() {
        let (db, pid) = db_with_project("b");

        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", true)
            .expect("write true");
        assert!(db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .unwrap());

        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", false)
            .expect("write false");
        assert!(!db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .unwrap());

        // Idempotent re-write of the same value.
        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", false)
            .expect("write false again");
        assert!(!db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .unwrap());

        // Flip back.
        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", true)
            .expect("flip back");
        assert!(db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .unwrap());
    }

    /// Toggles for distinct (project, module) pairs do not interfere.
    /// Guards against a regression where the SQL WHERE clause drops
    /// a discriminator and one project's disable silently affects
    /// another's enable.
    #[test]
    fn module_enabled_isolation_between_projects_and_modules() {
        let (db, pid_a) = db_with_project("iso-a");
        let pid_b = "proj-iso-b".to_string();
        db.insert_project(
            &pid_b,
            "Test iso-b",
            "/tmp/iso-b",
            ProjectHost::Base,
            "iso-b",
        )
        .unwrap();

        db.module_set_enabled_for_project(&pid_a, "vct-rl-reranker", false)
            .unwrap();
        db.module_set_enabled_for_project(&pid_a, "vct-coordination", true)
            .unwrap();
        db.module_set_enabled_for_project(&pid_b, "vct-rl-reranker", true)
            .unwrap();

        assert!(!db
            .module_is_enabled_for_project(&pid_a, "vct-rl-reranker")
            .unwrap());
        assert!(db
            .module_is_enabled_for_project(&pid_a, "vct-coordination")
            .unwrap());
        assert!(db
            .module_is_enabled_for_project(&pid_b, "vct-rl-reranker")
            .unwrap());
        // Unrelated (project_b, vct-coordination) untouched → default
        // true.
        assert!(db
            .module_is_enabled_for_project(&pid_b, "vct-coordination")
            .unwrap());
    }

    /// `module_clear_enabled_for_project_all` removes every row for a
    /// given module across all projects, leaving other modules' rows
    /// alone. This is the uninstall path's cleanup hook for global-
    /// scope modules.
    #[test]
    fn module_clear_enabled_for_project_all_removes_only_target_module() {
        let (db, pid_a) = db_with_project("clr-a");
        let pid_b = "proj-clr-b".to_string();
        db.insert_project(&pid_b, "B", "/tmp/clr-b", ProjectHost::Base, "clr-b")
            .unwrap();

        // Seed 2 projects × 2 modules so we can prove cross-row safety.
        db.module_set_enabled_for_project(&pid_a, "vct-rl-reranker", true)
            .unwrap();
        db.module_set_enabled_for_project(&pid_b, "vct-rl-reranker", false)
            .unwrap();
        db.module_set_enabled_for_project(&pid_a, "vct-coordination", false)
            .unwrap();
        db.module_set_enabled_for_project(&pid_b, "vct-coordination", true)
            .unwrap();

        let removed = db
            .module_clear_enabled_for_project_all("vct-rl-reranker")
            .expect("clear");
        assert_eq!(removed, 2, "two rl-reranker rows removed");

        // RL rows gone → default reads true everywhere.
        assert!(db
            .module_is_enabled_for_project(&pid_a, "vct-rl-reranker")
            .unwrap());
        assert!(db
            .module_is_enabled_for_project(&pid_b, "vct-rl-reranker")
            .unwrap());

        // Coordination rows untouched (still explicit values, not defaults).
        assert!(!db
            .module_is_enabled_for_project(&pid_a, "vct-coordination")
            .unwrap());
        assert!(db
            .module_is_enabled_for_project(&pid_b, "vct-coordination")
            .unwrap());

        // Re-clearing is a no-op (idempotent).
        let removed_again = db
            .module_clear_enabled_for_project_all("vct-rl-reranker")
            .expect("clear again");
        assert_eq!(removed_again, 0);
    }

    /// Malformed value (non-bool JSON) must read as enabled — fail-open
    /// per the docstring. The setter would never write this, but a
    /// hand-edited row or a stale schema version could.
    #[test]
    fn module_is_enabled_for_project_fail_open_on_malformed_value() {
        let (db, pid) = db_with_project("mal");

        // Inject a non-bool JSON value directly via the generic setter.
        db.set_setting(
            &pid,
            "vct-rl-reranker",
            MODULE_ENABLED_FOR_PROJECT_KEY,
            &serde_json::json!("disabled"),
        )
        .expect("inject string");

        let enabled = db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .expect("read");
        assert!(
            enabled,
            "malformed value (string) must fail open to enabled — \
             matches the docstring contract"
        );

        // Same for null.
        db.set_setting(
            &pid,
            "vct-rl-reranker",
            MODULE_ENABLED_FOR_PROJECT_KEY,
            &serde_json::Value::Null,
        )
        .unwrap();
        assert!(db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .unwrap());
    }

    // ─── v0.2.52 V52-AD — global toggle tests ────────────────────────────

    /// Global default reads as `None` when no row exists. The hub
    /// resolver translates that to the system default (`true`).
    #[test]
    fn module_global_enabled_returns_none_when_row_absent() {
        let db = Db::open_in_memory().expect("in-memory db");
        let result = db.module_global_enabled("vct-rl-reranker").expect("read");
        assert!(
            result.is_none(),
            "absent global row must read as None (caller picks default)"
        );
    }

    /// Set false at global level → reads as Some(false). Set true →
    /// Some(true). Roundtrip pins the setter/reader contract for the
    /// `project_id IS NULL` rows added by migration 034.
    #[test]
    fn module_set_global_enabled_roundtrip() {
        let db = Db::open_in_memory().expect("in-memory db");

        db.module_set_global_enabled("vct-rl-reranker", false)
            .expect("write false");
        assert_eq!(
            db.module_global_enabled("vct-rl-reranker").unwrap(),
            Some(false),
            "explicit global disable must surface"
        );

        db.module_set_global_enabled("vct-rl-reranker", true)
            .expect("write true");
        assert_eq!(
            db.module_global_enabled("vct-rl-reranker").unwrap(),
            Some(true),
        );

        // Idempotent re-write of the same value (delete+insert path).
        db.module_set_global_enabled("vct-rl-reranker", true)
            .expect("write true again");
        assert_eq!(
            db.module_global_enabled("vct-rl-reranker").unwrap(),
            Some(true),
        );
    }

    /// `module_effective_enabled` cascade:
    ///   no per-project row, no global row → true (fail-open default)
    ///   no per-project row, global=false → false (global wins)
    ///   no per-project row, global=true  → true
    ///   per-project=true, global=false   → true (per-project overrides)
    ///   per-project=false, global=true   → false (per-project overrides)
    #[test]
    fn module_effective_enabled_cascade() {
        let (db, pid) = db_with_project("eff");

        // (a) Both absent → default true.
        assert!(
            db.module_effective_enabled(&pid, "vct-rl-reranker").unwrap(),
            "no rows → fail-open default true"
        );

        // (b) Global=false, no per-project → false (global default applies).
        db.module_set_global_enabled("vct-rl-reranker", false)
            .unwrap();
        assert!(
            !db.module_effective_enabled(&pid, "vct-rl-reranker").unwrap(),
            "global default false must propagate when no per-project row"
        );

        // (c) Per-project=true, global=false → true (per-project overrides).
        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", true)
            .unwrap();
        assert!(
            db.module_effective_enabled(&pid, "vct-rl-reranker").unwrap(),
            "per-project enable must override global disable"
        );

        // (d) Per-project=false, global=false → false (both agree).
        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", false)
            .unwrap();
        assert!(
            !db.module_effective_enabled(&pid, "vct-rl-reranker").unwrap(),
            "both false → false"
        );

        // (e) Per-project=false, global=true → false (per-project overrides).
        db.module_set_global_enabled("vct-rl-reranker", true)
            .unwrap();
        assert!(
            !db.module_effective_enabled(&pid, "vct-rl-reranker").unwrap(),
            "per-project disable must override global enable"
        );
    }

    /// Global rows are independent across distinct module_ids.
    #[test]
    fn module_global_enabled_isolation_between_modules() {
        let db = Db::open_in_memory().expect("in-memory db");

        db.module_set_global_enabled("vct-rl-reranker", false)
            .unwrap();
        db.module_set_global_enabled("vct-coordination", true)
            .unwrap();

        assert_eq!(
            db.module_global_enabled("vct-rl-reranker").unwrap(),
            Some(false),
        );
        assert_eq!(
            db.module_global_enabled("vct-coordination").unwrap(),
            Some(true),
        );
        // Third unrelated module reads None.
        assert!(db.module_global_enabled("vct-other").unwrap().is_none());
    }

    /// Global row survives a project deletion (FK cascade only fires
    /// for non-NULL project_id rows). Critical correctness property:
    /// dropping a project must NOT silently re-enable a globally-
    /// disabled module across the rest of the host.
    #[test]
    fn module_global_enabled_survives_project_delete() {
        let (db, pid) = db_with_project("survive");
        db.module_set_global_enabled("vct-rl-reranker", false)
            .unwrap();
        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", true)
            .unwrap();

        // Sanity: per-project row exists pre-delete.
        assert!(db
            .module_is_enabled_for_project(&pid, "vct-rl-reranker")
            .unwrap());

        // Delete the project (FK ON DELETE CASCADE wipes per-project
        // rows for that project_id, leaves NULL rows intact).
        db.lock()
            .execute(
                "DELETE FROM projects WHERE id = ?1",
                rusqlite::params![&pid],
            )
            .expect("delete project");

        // Global row still alive.
        assert_eq!(
            db.module_global_enabled("vct-rl-reranker").unwrap(),
            Some(false),
            "global row (project_id IS NULL) must NOT cascade-delete with a project"
        );
    }

    /// `module_clear_enabled_for_project_all` (the uninstall-time
    /// cleanup helper) ALSO clears the global row, because it uses
    /// `WHERE module_id = ? AND setting_key = ?` with no project_id
    /// filter. This is correct behaviour: when a module is uninstalled
    /// host-wide, all its enable rows (per-project + global) should go.
    #[test]
    fn module_clear_enabled_for_project_all_also_clears_global_row() {
        let db = Db::open_in_memory().expect("in-memory db");
        db.module_set_global_enabled("vct-rl-reranker", false)
            .unwrap();

        let removed = db
            .module_clear_enabled_for_project_all("vct-rl-reranker")
            .expect("clear");
        assert_eq!(removed, 1, "global row counted in the cleanup");
        assert!(
            db.module_global_enabled("vct-rl-reranker")
                .unwrap()
                .is_none(),
            "global row was cleared"
        );
    }

    /// Setter always writes a JSON boolean, regardless of caller
    /// hygiene. This is the contract that lets the reader's strict
    /// `Bool` match be the fast path. Verified by reading the raw
    /// stored value through `list_module_settings`.
    #[test]
    fn module_set_enabled_for_project_always_writes_boolean() {
        let (db, pid) = db_with_project("bool");
        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", true)
            .unwrap();
        let settings = db
            .list_module_settings(&pid, "vct-rl-reranker")
            .expect("list");
        let row = settings
            .iter()
            .find(|(k, _)| k == MODULE_ENABLED_FOR_PROJECT_KEY)
            .expect("row exists");
        assert!(matches!(row.1, Value::Bool(true)));

        db.module_set_enabled_for_project(&pid, "vct-rl-reranker", false)
            .unwrap();
        let settings = db
            .list_module_settings(&pid, "vct-rl-reranker")
            .expect("list");
        let row = settings
            .iter()
            .find(|(k, _)| k == MODULE_ENABLED_FOR_PROJECT_KEY)
            .expect("row exists");
        assert!(matches!(row.1, Value::Bool(false)));
    }

    // ─── v0.2.91 decision #23 — tri-state (write / clear) + provenance ───

    /// The three positions of the per-project control, and the way BACK.
    /// `Some(false)` → `None` is the F-4 fix at the per-project layer: after
    /// the clear the project inherits again, and `explicit` is `None` — not
    /// a second explicit value dressed up as inheriting.
    #[test]
    fn per_project_write_then_clear_returns_to_inheriting() {
        let (db, pid) = db_with_project("tri");
        db.module_set_global_enabled("vct-rl-reranker", true).unwrap();

        db.module_write_enabled_for_project(&pid, "vct-rl-reranker", Some(false))
            .unwrap();
        let st = db.resolve_module_enable(&pid, "vct-rl-reranker").unwrap();
        assert_eq!(st.explicit, Some(false));
        assert!(!st.effective, "an explicit off beats a host-wide on");
        assert_eq!(st.source, ModuleEnableSource::Project);

        db.module_write_enabled_for_project(&pid, "vct-rl-reranker", None)
            .unwrap();
        let st = db.resolve_module_enable(&pid, "vct-rl-reranker").unwrap();
        assert_eq!(st.explicit, None, "the row is GONE, not set to true");
        assert_eq!(st.global_default, Some(true));
        assert!(st.effective);
        assert_eq!(st.source, ModuleEnableSource::GlobalDefault);
    }

    /// Clearing an absent row is a no-op, not an error (idempotent).
    #[test]
    fn per_project_clear_is_idempotent() {
        let (db, pid) = db_with_project("tri-idem");
        db.module_write_enabled_for_project(&pid, "vct-rl-reranker", None)
            .expect("clearing an absent row must succeed");
        let st = db.resolve_module_enable(&pid, "vct-rl-reranker").unwrap();
        assert_eq!(st.explicit, None);
        assert_eq!(st.source, ModuleEnableSource::SystemDefault);
    }

    /// F-4 at the GLOBAL layer: `module_set_global_enabled` is DELETE+INSERT
    /// and can never leave the row absent, so before decision #23 the `None`
    /// state was unreachable after the first click. The clear restores it —
    /// and must NOT take per-project rows with it (the uninstall-time
    /// `module_clear_enabled_for_project_all` is the one that does that).
    #[test]
    fn global_clear_restores_none_and_spares_per_project_rows() {
        let (db, pid) = db_with_project("gclear");
        db.module_set_global_enabled("vct-rl-reranker", false).unwrap();
        db.module_write_enabled_for_project(&pid, "vct-rl-reranker", Some(true))
            .unwrap();

        let removed = db.module_clear_global_enabled("vct-rl-reranker").unwrap();
        assert_eq!(removed, 1);
        assert_eq!(db.module_global_enabled("vct-rl-reranker").unwrap(), None);

        // Leave-alone half: the per-project row survived.
        let st = db.resolve_module_enable(&pid, "vct-rl-reranker").unwrap();
        assert_eq!(st.explicit, Some(true));
        assert_eq!(st.global_default, None);

        // Idempotent: a second clear removes nothing and does not error.
        assert_eq!(db.module_clear_global_enabled("vct-rl-reranker").unwrap(), 0);
    }

    /// The global clear is scoped to ONE module id.
    #[test]
    fn global_clear_scopes_to_module_id() {
        let (db, _pid) = db_with_project("gscope");
        db.module_set_global_enabled("vct-rl-reranker", false).unwrap();
        db.module_set_global_enabled("vct-coordination", false).unwrap();

        assert_eq!(db.module_clear_global_enabled("vct-rl-reranker").unwrap(), 1);
        assert_eq!(
            db.module_global_enabled("vct-coordination").unwrap(),
            Some(false),
            "the other module's host-wide row is untouched",
        );
    }

    /// The provenance resolver and the boolean resolver the hub calls must
    /// agree in EVERY cell of the cascade — including the malformed-row
    /// fail-open path, which is the one a hand-edited DB can reach.
    #[test]
    fn resolve_module_enable_matches_effective() {
        let (db, pid) = db_with_project("agree");
        let m = "vct-rl-reranker";
        let cases: [(Option<bool>, Option<bool>); 9] = [
            (None, None),
            (None, Some(true)),
            (None, Some(false)),
            (Some(true), None),
            (Some(true), Some(true)),
            (Some(true), Some(false)),
            (Some(false), None),
            (Some(false), Some(true)),
            (Some(false), Some(false)),
        ];
        for (explicit, global) in cases {
            db.module_write_enabled_for_project(&pid, m, explicit).unwrap();
            match global {
                Some(g) => db.module_set_global_enabled(m, g).unwrap(),
                None => {
                    db.module_clear_global_enabled(m).unwrap();
                }
            }
            let st = db.resolve_module_enable(&pid, m).unwrap();
            assert_eq!(
                st.effective,
                db.module_effective_enabled(&pid, m).unwrap(),
                "provenance resolver disagreed with the hub's resolver at \
                 (explicit={explicit:?}, global={global:?})",
            );
            assert_eq!(st.explicit, explicit);
            assert_eq!(st.global_default, global);
        }

        // Malformed per-project row: both readers fail OPEN to true, and the
        // provenance says the project tier answered (a row IS present).
        db.set_setting(&pid, m, MODULE_ENABLED_FOR_PROJECT_KEY, &Value::String("yes".into()))
            .unwrap();
        db.module_set_global_enabled(m, false).unwrap();
        let st = db.resolve_module_enable(&pid, m).unwrap();
        assert!(st.effective, "malformed must fail OPEN, not disable");
        assert_eq!(st.effective, db.module_effective_enabled(&pid, m).unwrap());
        assert_eq!(st.source, ModuleEnableSource::Project);
    }

    /// Plan §F #25 (USER standing constraint) — TELEMETRY IS MODULE-INDEPENDENT.
    ///
    /// The enable mechanism gates reranking. It must not gate COLLECTION. Both
    /// sides, at the layer where a regression would be introduced:
    ///   * with the module DISABLED (per-project AND host-wide) events insert;
    ///   * with the module ABSENT (no `module_installs` row at all) they
    ///     insert too — and `insert_rl_event` never consults either table.
    /// The dual-flag row that DOES drive collection (`dual_rl_log_enabled`,
    /// stored under the same module_id) must survive every enable write.
    #[test]
    fn enable_flag_never_gates_rl_event_collection() {
        let (db, pid) = db_with_project("telemetry");
        let m = "vct-rl-reranker";

        // The project has NO module_installs row for the RL module: absent.
        assert!(
            db.list_module_installs_for_project(&pid)
                .unwrap()
                .iter()
                .all(|r| r.module_id != m),
            "precondition: module is ABSENT for this project",
        );
        db.insert_rl_event(
            "retrieval", 1, 1_000, Some(&pid), Some("Test telemetry"), "task-absent",
            None, None, None, None, "{}",
        )
        .expect("collection must work with the module ABSENT");

        // Now DISABLE it at both tiers via the mechanism this WP ships.
        db.module_write_enabled_for_project(&pid, m, Some(false)).unwrap();
        db.module_set_global_enabled(m, false).unwrap();
        assert!(
            !db.resolve_module_enable(&pid, m).unwrap().effective,
            "precondition: reranking is gated off",
        );
        db.insert_rl_event(
            "retrieval", 1, 2_000, Some(&pid), Some("Test telemetry"), "task-disabled",
            None, None, None, None, "{}",
        )
        .expect("collection must work with the module DISABLED");

        assert_eq!(
            db.count_rl_events(Some(&pid), None, None, None).unwrap(),
            2,
            "both events landed — the enable flag gates reranking, never collection",
        );

        // The collection flag lives under the SAME module_id but a different
        // setting_key; no enable write may disturb it.
        db.set_dual_flag_for_project(&pid, DualFlag::RlLog, Some(true))
            .unwrap();
        db.module_write_enabled_for_project(&pid, m, Some(false)).unwrap();
        db.module_write_enabled_for_project(&pid, m, None).unwrap();
        assert!(
            db.resolve_dual_flags(&pid).rl_log.effective,
            "writing and clearing the enable flag must not touch dual_rl_log_enabled",
        );
    }
}

// ─── v0.2.91 WP-L — dual-flag host-wide default cascade tests ────────────
//
// The five-case matrix per flag (plan §F #22 + the WP-L brief), the two
// global-tier cascade directions, the CROSS-TIER clamp, and the
// module-independence leave-alone case.
#[cfg(test)]
mod dual_flag_cascade_tests {
    use super::*;
    use crate::db::models::ProjectHost;

    fn db_with_project() -> (Db, String) {
        let db = Db::open_in_memory().expect("in-memory db");
        let id = "proj-dual".to_string();
        db.insert_project(&id, "Dual", "/tmp/dual", ProjectHost::Base, "dual")
            .expect("insert project");
        (db, id)
    }

    /// Raw app_state write — bypasses `set_dual_flag_global_default`'s cascade
    /// on purpose so the resolver can be driven into states the setter alone
    /// cannot produce (that is how the cross-tier clamp gets exercised).
    fn raw_global(db: &Db, flag: DualFlag, value: bool) {
        db.app_state_set_bool(flag.app_state_key(), value).unwrap();
    }

    // ── Case 1: no project row, no global row ───────────────────────────
    #[test]
    fn case1_no_rows_anywhere_resolves_false_system_default() {
        let (db, p) = db_with_project();
        let s = db.resolve_dual_flags(&p);
        for st in [s.write_all_slots, s.rl_log, s.arctic_secondary] {
            assert_eq!(st.explicit, None);
            assert!(!st.install_default);
            assert!(!st.effective);
            assert_eq!(st.source, DualFlagSource::SystemDefault);
            assert!(!st.clamped);
        }
    }

    // ── Case 2: no project row, host-wide default true ──────────────────
    #[test]
    fn case2_no_project_row_inherits_install_default_true() {
        let (db, p) = db_with_project();
        // Set every global default on (via the cascading setter for log, so
        // its prerequisite comes along — that IS the shipped behaviour).
        db.set_dual_flag_global_default(DualFlag::WriteAllSlots, true)
            .unwrap();
        db.set_dual_flag_global_default(DualFlag::RlLog, true).unwrap();
        db.set_dual_flag_global_default(DualFlag::ArcticSecondary, true)
            .unwrap();

        let s = db.resolve_dual_flags(&p);
        for st in [s.write_all_slots, s.rl_log, s.arctic_secondary] {
            assert_eq!(st.explicit, None, "no per-project row was written");
            assert!(st.install_default);
            assert!(st.effective, "an inheriting project must see the host default");
            assert_eq!(st.source, DualFlagSource::InstallDefault);
            assert!(!st.clamped);
        }
    }

    // ── Case 3: explicit project true, global false ─────────────────────
    #[test]
    fn case3_explicit_project_true_beats_global_false() {
        let (db, p) = db_with_project();
        for flag in DualFlag::ALL {
            db.set_dual_flag_for_project(&p, flag, Some(true)).unwrap();
        }
        let s = db.resolve_dual_flags(&p);
        for st in [s.write_all_slots, s.rl_log, s.arctic_secondary] {
            assert_eq!(st.explicit, Some(true));
            assert!(!st.install_default);
            assert!(st.effective);
            assert_eq!(st.source, DualFlagSource::Project);
        }
    }

    // ── Case 4: explicit project FALSE, global TRUE ─────────────────────
    //
    // The "wins in both directions" case. A resolver written as
    // `project.unwrap_or(global)` on a `bool` (rather than an `Option<bool>`)
    // silently fails HERE and nowhere else.
    #[test]
    fn case4_explicit_project_false_beats_global_true() {
        let (db, p) = db_with_project();
        for flag in DualFlag::ALL {
            raw_global(&db, flag, true);
            db.set_dual_flag_for_project(&p, flag, Some(false)).unwrap();
        }
        let s = db.resolve_dual_flags(&p);
        for st in [s.write_all_slots, s.rl_log, s.arctic_secondary] {
            assert_eq!(st.explicit, Some(false));
            assert!(st.install_default, "the host default really is on");
            assert!(
                !st.effective,
                "an explicit per-project OFF must beat a host-wide ON",
            );
            assert_eq!(st.source, DualFlagSource::Project);
        }
    }

    // ── Case 5: the way back — clearing the row restores inheritance ────
    #[test]
    fn case5_clearing_the_row_returns_to_inheriting() {
        let (db, p) = db_with_project();
        raw_global(&db, DualFlag::ArcticSecondary, true);
        db.set_dual_flag_for_project(&p, DualFlag::ArcticSecondary, Some(false))
            .unwrap();
        assert!(!db.resolve_dual_flags(&p).arctic_secondary.effective);

        db.set_dual_flag_for_project(&p, DualFlag::ArcticSecondary, None)
            .unwrap();
        let st = db.resolve_dual_flags(&p).arctic_secondary;
        assert_eq!(st.explicit, None, "the row must be DELETED, not set to false");
        assert!(st.effective, "back to inheriting the host-wide ON");
        assert_eq!(st.source, DualFlagSource::InstallDefault);

        // And with no host default either, it lands back on case 1.
        raw_global(&db, DualFlag::ArcticSecondary, false);
        let st = db.resolve_dual_flags(&p).arctic_secondary;
        assert!(!st.effective);
        assert_eq!(st.source, DualFlagSource::SystemDefault);
    }

    /// Case 5 for EVERY flag, not just arctic: setting an explicit value and
    /// then clearing it must land each flag back on the inherited state, with
    /// no row left behind. Run per-flag on its own DB so the log⟹write
    /// cascade cannot mask a missing delete on one of them.
    #[test]
    fn case5_clearing_returns_every_flag_to_inheriting() {
        for flag in DualFlag::ALL {
            let (db, p) = db_with_project();
            // Host-wide ON for this flag (and its prerequisite, so an
            // inherited log is actually reachable after the clear).
            raw_global(&db, DualFlag::WriteAllSlots, true);
            raw_global(&db, flag, true);

            db.set_dual_flag_for_project(&p, flag, Some(false)).unwrap();
            assert_eq!(
                db.resolve_dual_flags(&p).get(flag).explicit,
                Some(false),
                "{:?}: explicit row not written",
                flag,
            );

            db.set_dual_flag_for_project(&p, flag, None).unwrap();
            let st = *db.resolve_dual_flags(&p).get(flag);
            assert_eq!(st.explicit, None, "{:?}: row was not deleted", flag);
            assert_eq!(
                st.source,
                DualFlagSource::InstallDefault,
                "{:?}: did not return to inheriting",
                flag,
            );
            assert!(st.effective, "{:?}: inherited value not in force", flag);
        }
    }

    /// The delete really removes the row (not "writes false"): a fresh
    /// project and a cleared project must be indistinguishable.
    #[test]
    fn clearing_leaves_no_module_settings_row_behind() {
        let (db, p) = db_with_project();
        db.set_dual_flag_for_project(&p, DualFlag::ArcticSecondary, Some(true))
            .unwrap();
        assert!(db
            .get_setting(
                &p,
                DualFlag::ArcticSecondary.module_id(),
                DualFlag::ArcticSecondary.setting_key()
            )
            .unwrap()
            .is_some());

        db.set_dual_flag_for_project(&p, DualFlag::ArcticSecondary, None)
            .unwrap();
        assert!(
            db.get_setting(
                &p,
                DualFlag::ArcticSecondary.module_id(),
                DualFlag::ArcticSecondary.setting_key()
            )
            .unwrap()
            .is_none(),
            "'use host-wide default' must DELETE the row",
        );
    }

    // ── Case 6/7: the cascade at the GLOBAL tier ────────────────────────
    #[test]
    fn case6_global_log_default_on_forces_global_write_default_on() {
        let (db, _p) = db_with_project();
        db.set_dual_flag_global_default(DualFlag::RlLog, true).unwrap();
        let g = db.dual_flag_global_defaults();
        assert!(g.rl_log);
        assert!(
            g.write_all_slots,
            "turning the host-wide log default ON must turn the write default ON",
        );
    }

    #[test]
    fn case7_global_write_default_off_forces_global_log_default_off() {
        let (db, _p) = db_with_project();
        db.set_dual_flag_global_default(DualFlag::RlLog, true).unwrap();
        db.set_dual_flag_global_default(DualFlag::WriteAllSlots, false)
            .unwrap();
        let g = db.dual_flag_global_defaults();
        assert!(!g.write_all_slots);
        assert!(
            !g.rl_log,
            "turning the host-wide write default OFF must turn the log default OFF",
        );
    }

    #[test]
    fn global_arctic_default_is_independent_of_the_cascade() {
        let (db, _p) = db_with_project();
        db.set_dual_flag_global_default(DualFlag::ArcticSecondary, true)
            .unwrap();
        let g = db.dual_flag_global_defaults();
        assert!(g.arctic_secondary);
        assert!(!g.write_all_slots, "arctic must not drag write along");
        assert!(!g.rl_log);
    }

    // ── Case 8: THE CROSS-TIER CLAMP ────────────────────────────────────
    //
    // The incoherent pair decision #22's wording does not cover: a host-wide
    // log default of ON meeting an explicit per-project write of OFF.
    #[test]
    fn case8_cross_tier_clamp_global_log_on_project_write_off() {
        let (db, p) = db_with_project();
        // Raw writes so the global tier really holds (log=true, write=true)
        // and the project tier really holds write=false.
        raw_global(&db, DualFlag::RlLog, true);
        raw_global(&db, DualFlag::WriteAllSlots, true);
        db.write_dual_flag_row(&p, DualFlag::WriteAllSlots, Some(false))
            .unwrap();

        let s = db.resolve_dual_flags(&p);
        assert!(!s.write_all_slots.effective, "explicit project OFF wins");
        assert!(
            !s.rl_log.effective,
            "RL dual-logging must clamp DOWN to its prerequisite — resolved \
             (log=true, write=false) is the incoherent state the whole \
             cascade exists to prevent",
        );
        assert!(
            s.rl_log.clamped,
            "the clamp must be visible to the GUI so it can explain WHY it is off",
        );
        // Provenance is still honestly reported: the log flag's own stored
        // intent came from the host-wide default.
        assert_eq!(s.rl_log.source, DualFlagSource::InstallDefault);
        assert_eq!(s.rl_log.explicit, None);
    }

    /// The clamp is one-directional: an INHERITED log=true must never force
    /// write=true. (Clamping "up" would let a host-wide default overrule an
    /// explicit per-project OFF — exactly what tier 1 forbids.)
    #[test]
    fn case8b_inherited_log_true_never_forces_write_true() {
        let (db, p) = db_with_project();
        raw_global(&db, DualFlag::RlLog, true);
        // Global write default stays OFF, project has no rows at all.
        let s = db.resolve_dual_flags(&p);
        assert!(!s.write_all_slots.effective, "write must stay off");
        assert!(!s.rl_log.effective, "and the dependent clamps down with it");
        assert!(s.rl_log.clamped);

        // Same with an EXPLICIT project write=false under a global write=true.
        raw_global(&db, DualFlag::WriteAllSlots, true);
        db.write_dual_flag_row(&p, DualFlag::WriteAllSlots, Some(false))
            .unwrap();
        let s = db.resolve_dual_flags(&p);
        assert_eq!(s.write_all_slots.explicit, Some(false));
        assert!(
            !s.write_all_slots.effective,
            "the dependent's ON must never promote the prerequisite",
        );
    }

    // ── Case 9: the per-project cascade still holds ─────────────────────
    #[test]
    fn case9_project_log_on_forces_project_write_on() {
        let (db, p) = db_with_project();
        db.set_dual_flag_for_project(&p, DualFlag::RlLog, Some(true))
            .unwrap();
        let s = db.resolve_dual_flags(&p);
        assert!(s.rl_log.effective);
        assert!(
            s.write_all_slots.effective,
            "enabling the log must enable its prerequisite",
        );
        assert_eq!(s.write_all_slots.explicit, Some(true));
    }

    #[test]
    fn case9_project_write_off_forces_project_log_off() {
        let (db, p) = db_with_project();
        db.set_dual_flag_for_project(&p, DualFlag::RlLog, Some(true))
            .unwrap();
        db.set_dual_flag_for_project(&p, DualFlag::WriteAllSlots, Some(false))
            .unwrap();
        let s = db.resolve_dual_flags(&p);
        assert!(!s.write_all_slots.effective);
        assert!(!s.rl_log.effective);
        assert_eq!(
            s.rl_log.explicit,
            Some(false),
            "an EXPLICIT false, so a host-wide log default cannot revive it",
        );
    }

    /// Turning the log ON while dual-write already RESOLVES on (inherited)
    /// must NOT pin an explicit write row — the project keeps inheriting.
    #[test]
    fn project_log_on_does_not_pin_an_already_inherited_write() {
        let (db, p) = db_with_project();
        raw_global(&db, DualFlag::WriteAllSlots, true);
        db.set_dual_flag_for_project(&p, DualFlag::RlLog, Some(true))
            .unwrap();
        let s = db.resolve_dual_flags(&p);
        assert!(s.rl_log.effective);
        assert_eq!(
            s.write_all_slots.explicit, None,
            "the prerequisite already resolved ON — do not convert an \
             inheriting project into an explicit one as a side effect",
        );
        assert_eq!(s.write_all_slots.source, DualFlagSource::InstallDefault);
    }

    /// Clearing the write row (back to inherit) when the host default is OFF
    /// drops the prerequisite — the dependent must be written OFF too.
    #[test]
    fn clearing_write_row_down_to_off_cascades_the_log_off() {
        let (db, p) = db_with_project();
        db.set_dual_flag_for_project(&p, DualFlag::RlLog, Some(true))
            .unwrap();
        assert_eq!(
            db.resolve_dual_flags(&p).write_all_slots.explicit,
            Some(true)
        );

        db.set_dual_flag_for_project(&p, DualFlag::WriteAllSlots, None)
            .unwrap();
        let s = db.resolve_dual_flags(&p);
        assert!(!s.write_all_slots.effective, "host default is off");
        assert_eq!(
            s.rl_log.explicit,
            Some(false),
            "dropping the prerequisite must write the dependent explicitly off",
        );
    }

    // ── Arctic stays independent through everything ─────────────────────
    #[test]
    fn arctic_is_untouched_by_either_cascade_direction() {
        let (db, p) = db_with_project();
        db.set_dual_flag_for_project(&p, DualFlag::ArcticSecondary, Some(true))
            .unwrap();
        db.set_dual_flag_for_project(&p, DualFlag::RlLog, Some(true))
            .unwrap();
        db.set_dual_flag_for_project(&p, DualFlag::WriteAllSlots, Some(false))
            .unwrap();
        let s = db.resolve_dual_flags(&p);
        assert!(s.arctic_secondary.effective);
        assert_eq!(s.arctic_secondary.explicit, Some(true));
        assert!(!s.arctic_secondary.clamped);
    }

    // ── Plan §F #25: telemetry collection is MODULE-INDEPENDENT ─────────
    //
    // `dual_rl_log_enabled` is addressed under module_id "vct-rl-reranker",
    // but the paid module gates RERANKING, never COLLECTION. Resolution must
    // be byte-identical whether or not the module is installed/enabled — the
    // act/leave-alone pair for a check that must NOT exist.
    #[test]
    fn rl_log_resolves_identically_with_and_without_the_rl_module() {
        // (a) No module_installs row for vct-rl-reranker at all.
        let (db_absent, p_absent) = db_with_project();
        db_absent
            .set_dual_flag_for_project(&p_absent, DualFlag::RlLog, Some(true))
            .unwrap();
        let absent = db_absent.resolve_dual_flags(&p_absent);

        // (b) The module present AND explicitly disabled for the project —
        // the harshest "module is not available here" shape the DB can hold.
        let (db_present, p_present) = db_with_project();
        db_present
            .module_set_enabled_for_project(&p_present, DUAL_FLAG_MODULE_ID_RL_RERANKER, false)
            .unwrap();
        db_present
            .set_dual_flag_for_project(&p_present, DualFlag::RlLog, Some(true))
            .unwrap();
        let present = db_present.resolve_dual_flags(&p_present);

        assert_eq!(
            absent.rl_log, present.rl_log,
            "collection must never depend on the RL module being installed \
             or enabled — the module gates reranking only",
        );
        assert!(absent.rl_log.effective);
        assert!(present.rl_log.effective);

        // Same for the inherited tier.
        raw_global(&db_absent, DualFlag::RlLog, true);
        raw_global(&db_absent, DualFlag::WriteAllSlots, true);
        raw_global(&db_present, DualFlag::RlLog, true);
        raw_global(&db_present, DualFlag::WriteAllSlots, true);
        let (db_fresh_a, p_fresh_a) = db_with_project();
        raw_global(&db_fresh_a, DualFlag::RlLog, true);
        raw_global(&db_fresh_a, DualFlag::WriteAllSlots, true);
        db_fresh_a
            .module_set_enabled_for_project(&p_fresh_a, DUAL_FLAG_MODULE_ID_RL_RERANKER, false)
            .unwrap();
        assert!(
            db_fresh_a.resolve_dual_flags(&p_fresh_a).rl_log.effective,
            "a disabled RL module must not suppress an inherited log default",
        );
    }

    // ── Wire-name validation ────────────────────────────────────────────
    #[test]
    fn wire_names_round_trip_and_unknown_names_are_rejected() {
        for flag in DualFlag::ALL {
            assert_eq!(DualFlag::from_wire(flag.wire_name()).unwrap(), flag);
        }
        let err = DualFlag::from_wire("dual_write").unwrap_err();
        assert!(
            err.contains("unknown dual flag"),
            "an unknown flag name must error explicitly, not silently no-op: {err}",
        );
    }

    /// The addressing table is what the hub resolver and the Python
    /// projection mirror. Pin the strings so a rename surfaces here first.
    #[test]
    fn addressing_table_is_stable() {
        assert_eq!(
            DualFlag::WriteAllSlots.module_id(),
            "orchestrator-core"
        );
        assert_eq!(
            DualFlag::WriteAllSlots.setting_key(),
            "dual_embedding_write_all_slots"
        );
        assert_eq!(DualFlag::RlLog.module_id(), "vct-rl-reranker");
        assert_eq!(DualFlag::RlLog.setting_key(), "dual_rl_log_enabled");
        assert_eq!(
            DualFlag::ArcticSecondary.module_id(),
            "orchestrator-core"
        );
        assert_eq!(
            DualFlag::ArcticSecondary.setting_key(),
            "dual_embedding_arctic_secondary"
        );
        assert_eq!(
            DualFlag::WriteAllSlots.app_state_key(),
            "embedding.dual_write_default"
        );
        assert_eq!(
            DualFlag::RlLog.app_state_key(),
            "embedding.dual_rl_log_default"
        );
        assert_eq!(
            DualFlag::ArcticSecondary.app_state_key(),
            "embedding.dual_arctic_default"
        );
    }

    /// A corrupt (non-bool) per-project row must be treated as ABSENT, not as
    /// fail-open-true: these flags are opt-in cost multipliers.
    #[test]
    fn malformed_project_row_falls_back_to_the_host_default() {
        let (db, p) = db_with_project();
        db.set_setting(
            &p,
            DualFlag::ArcticSecondary.module_id(),
            DualFlag::ArcticSecondary.setting_key(),
            &Value::String("yes".into()),
        )
        .unwrap();
        let st = db.resolve_dual_flags(&p).arctic_secondary;
        assert_eq!(st.explicit, None);
        assert!(!st.effective, "a corrupt row must not turn extra embeds ON");
        assert_eq!(st.source, DualFlagSource::SystemDefault);
    }

    /// An empty project id is a caller bug, not a soft-fail.
    #[test]
    fn empty_project_id_is_rejected_by_the_setter() {
        let db = Db::open_in_memory().expect("in-memory db");
        assert!(db
            .set_dual_flag_for_project("", DualFlag::RlLog, Some(true))
            .is_err());
    }
}
