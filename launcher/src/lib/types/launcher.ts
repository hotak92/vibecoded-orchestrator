// Type mirrors of Rust command return values. Mirrors live in:
//   launcher/src-tauri/src/commands/projects_v2.rs   ProjectView, SwitchHostResult
//   launcher/src-tauri/src/commands/secrets_cmd.rs   SecretMetadata, SettingEntry
//   launcher/src-tauri/src/commands/licensing.rs     TierCacheView
//   launcher/src-tauri/src/commands/modules.rs       ModuleCatalogEntry, ModuleStatusView
//   launcher/src-tauri/src/db/models.rs              ModuleInstallRow, ProjectHost, ModuleStatus
//
// Keep these in sync. Field naming follows serde defaults (snake_case).

export type ProjectHost = 'base' | 'mao' | 'orchestrator_root';

// v0.2.35 (Agent J): added 'broken' to mirror the Rust enum
// (vct-launcher-core/src/db/models.rs::ModuleStatus::Broken — surfaced by
// the startup reconciler when ~/.vct/modules/<id>/ has gone missing).
// Previously this TS type drifted from the Rust source: the launcher would
// receive `"broken"` strings from `list_installed_modules` and silently
// type-cast them to nothing actionable. Now the catalog tile renders a
// distinct Retry-install + Uninstall pair for both error and broken rows.
export type ModuleStatus = 'installing' | 'installed' | 'running' | 'stopped' | 'error' | 'broken';

export type SecretScope = 'per_project' | 'shared' | 'global';

export type LicenseTier = 'free' | 'pro' | 'mao' | 'enterprise' | 'admin';

export interface ProjectView {
  id: string;
  name: string;
  folder_path: string;
  host: ProjectHost;
  /** URL-friendly slug (lowercase, dashes). Stable across renames only as
   *  long as the name does not change — renaming regenerates the slug.
   *  Use for /p/<slug>/... routes. */
  slug: string;
  created_at: number;
  updated_at: number;
  module_count: number;
}

export interface ModuleInstallRow {
  id: string;
  /**
   * v0.2.49 Stream A: nullable to support `install.scope = "global"`.
   * `null` ⇒ global install (exactly one row per machine for this
   * module; per-project routing happens INSIDE the container). A
   * non-null string ⇒ per-project install (the v0.2.20–v0.2.48
   * behaviour). Stream D's GUI work consumes this nullability to
   * render the right module-tile chrome (the global tile shows once
   * across all projects; the per-project tile shows on each
   * project's Modules tab).
   */
  project_id: string | null;
  module_id: string;
  module_version: string;
  install_path: string;
  status: ModuleStatus;
  enabled: boolean;
  installed_at: number;
  last_started_at: number | null;
  last_error: string | null;
  /**
   * NEW-3 (2026-05-28): resolved container name, populated by the hub
   * supervisor after `podman run -d --name <name>` succeeds. `null` /
   * missing for non-container modules and for container/service modules
   * whose start path hasn't run yet.
   */
  container_name?: string | null;
}

export interface SwitchHostResult {
  project: ProjectView;
  modules_removed: ModuleInstallRow[];
  modules_preserved: ModuleInstallRow[];
}

/**
 * Mirror of Rust `CreateProjectResult` (commands/projects_v2.rs).
 *
 * BLOCKER-2 (2026-05-01): PR 7's signature change wrapped ProjectView in
 * this {project, warnings} envelope, but the TS caller was still using
 * `<ProjectView>` as the invoke generic. Result: every newly-created
 * project landed in the store as the wrapper object, project.id was
 * undefined, and every downstream UI surface that keys off id broke.
 */
export interface CreateProjectResult {
  project: ProjectView;
  /** Non-fatal warnings (env-write failures, stale .env, etc.). */
  warnings: string[];
}

/**
 * Mirror of Rust `RenameProjectResult` (commands/projects_v2.rs).
 *
 * HIGH-7 (2026-05-01): rename mirrors create's warning surface so env
 * refresh failures during rename can be toasted instead of eprintln'd.
 * Also reused as the return type of `set_shared_kg_write_disabled`
 * (MEDIUM-1, refactored 2026-05-01) and its deprecated alias
 * `set_shared_kg_opt_out`.
 */
export interface RenameProjectResult {
  project: ProjectView;
  warnings: string[];
}

/**
 * Mirror of Rust `RenameClassMove` (commands/projects_v2.rs, v0.2.92 W14).
 *
 * ONE class the collection rename would carry. `action` is the whole story
 * for the reader:
 *
 * - `copy`          — `src_count` objects move, WITH their vectors.
 * - `create-empty`  — the source exists but is empty.
 * - `source-absent` — the project is BOUND to a class that does not exist in
 *                     Weaviate. No data is carried; the destination is made so
 *                     the new binding names something real. (Measured on this
 *                     machine: 3 of 5 prefix records name a dead prefix, so
 *                     this row is a normal sight, not an error.)
 * - `resume`        — an interrupted run created this destination already; the
 *                     copy is UUID-preserving, so repeating it re-writes.
 * - `noop`          — this member's name does not change.
 */
export interface RenameClassMove {
  src: string;
  dst: string;
  action: 'copy' | 'create-empty' | 'source-absent' | 'resume' | 'noop';
  src_count: number | null;
  note: string;
}

/** Mirror of Rust `RenameCollectionsPreview` (commands/projects_v2.rs). */
export interface RenameCollectionsPreview {
  project_id: string;
  project_name: string;
  new_name: string;
  old_code_prefix: string;
  new_code_prefix: string;
  moves: RenameClassMove[];
  carried_objects: number;
  /**
   * The previous class names. This operation NEVER drops them — the ledger
   * records the one guarded command that retires them later. UI copy must not
   * imply they were removed.
   */
  retired_classes: string[];
  warnings: string[];
}

/**
 * Mirror of Rust `RenameCollectionsResult` (commands/projects_v2.rs).
 *
 * `refused` carries the engine's machine key (`destination_exists`,
 * `prefix_collision`, `weaviate_unreachable`, …) so the UI can explain the
 * specific precondition instead of a generic failure. A refusal means NOTHING
 * was changed anywhere.
 */
export interface RenameCollectionsResult {
  ok: boolean;
  refused: string | null;
  error: string | null;
  preview: RenameCollectionsPreview | null;
  /** Present only on a performed (non-dry-run) rename. */
  summary: unknown | null;
}

/**
 * Mirror of Rust `UpdateSummary` (commands/projects_v2.rs).
 *
 * PR 5 (2026-05-01): per-action counts produced by the bundle install
 * during an `update_project_v2` run. Drives the toast summary line
 * ("5 files updated, 2 replaced (backup kept)") plus optional
 * detail breakdowns. Field naming mirrors the Rust struct (snake_case).
 */
export interface UpdateSummary {
  /** Newly-shipped orchestrator files that didn't exist before. */
  created: number;
  /** Files whose installed content matched the prior-shipped manifest hash;
   *  now overwritten with the new shipped version. */
  overwritten: number;
  /** User-modified files whose bytes were BACKED UP to
   *  `.claude/backups/bundle-adoptions/<ts>/` before the shipped version was
   *  written (the v0.2.84 adoption policy). This is the common outcome for a
   *  divergent file; `preserved` is the rare fallback. The Rust struct has
   *  always sent this — the toast simply never read it, so an adoption-only
   *  update reported "no changes". */
  adopted: number;
  /** Files where installed content diverged and the backup could NOT be
   *  written, so the user's copy was left in place. Rare. Surfaced via the
   *  `bundle_user_modified_preserved` deferral entry. */
  preserved: number;
  /** Files whose installed content already matches what we'd write. */
  noop: number;
  /** Files unconditionally overwritten (not user-customisable, e.g.
   *  `.claude/hooks/_lib/*`). */
  always_overwritten: number;
  /** First-install only — always 0 in update mode (kept for symmetry). */
  skipped_existing: number;
  /** Number of `errors[]` entries in the JSON envelope (per-file write
   *  failures). Each is also surfaced as a string in `warnings`. */
  errors_count: number;
  /** True iff this run actually WROTE at least one file under `knowledge/**`
   *  or `docs/**` — the Rust side's kg-sync spawn gate (v0.2.71 Piece 5b).
   *  Only the change-CAUSING buckets set it; `noop`/`preserve` do not.
   *
   *  v0.2.92 (review MAJOR-10): the field has always been serialised
   *  (`projects_v2.rs`, `#[serde(default)]`) and was the SECOND field this
   *  interface dropped at the type boundary — the `adopted` fix closed one
   *  instance of the class and left the neighbour open. `types/launcher.parity.test.ts`
   *  now diffs the two declarations so a third instance fails a test instead
   *  of shipping. */
  kg_or_docs_content_changed: boolean;
}

/**
 * Mirror of Rust `UpdateProjectResult` (commands/projects_v2.rs).
 *
 * PR 5 (2026-05-01): structured envelope for the launcher's "Update bundle"
 * action. `warnings` flow as toasts; `summary` drives the one-line summary
 * toast.
 */
export interface UpdateProjectResult {
  project: ProjectView;
  warnings: string[];
  summary: UpdateSummary;
}

/**
 * Mirror of Rust `UpdateAllOptions` (commands/projects_v2.rs).
 *
 * 0.2.x backlog #4 (2026-05-10): drives the "Update all projects" power-
 * user button. `stop_on_error: true` (default) makes the launcher halt at
 * the first project that hard-fails (folder missing, project unregistered)
 * so the user sees the broken project promptly instead of chewing through
 * the remaining N-1 first. `false` continues past failures.
 */
export interface UpdateAllOptions {
  stop_on_error?: boolean;
}

/**
 * Mirror of Rust `UpdateAllProjectEntry` (commands/projects_v2.rs).
 *
 * Per-project outcome of an `update_all_projects` run. `status` is one of:
 *   - "succeeded": `update_project_v2` returned Ok (warnings may still
 *     populate `warnings[]` for soft-fail conditions).
 *   - "failed":    hard failure (project missing on disk / folder gone).
 *     `error` carries the explanatory message.
 *   - "skipped":   `stop_on_error=true` halted iteration before reaching
 *     this project. `error` is null, `summary` is null.
 */
export interface UpdateAllProjectEntry {
  project_id: string;
  project_name: string;
  status: 'succeeded' | 'failed' | 'skipped';
  error: string | null;
  warnings: string[];
  summary: UpdateSummary | null;
}

/**
 * Mirror of Rust `UpdateAllReport` (commands/projects_v2.rs).
 *
 * Aggregate counts after an `update_all_projects` run. The launcher
 * renders these in the progress modal's footer summary
 * ("3 updated, 1 failed, 0 skipped").
 */
export interface UpdateAllReport {
  updated: UpdateAllProjectEntry[];
  total_succeeded: number;
  total_failed: number;
  total_skipped: number;
}

/**
 * Mirror of Rust `UnregisterOptions` (commands/projects_v2.rs).
 *
 * 2026-05-06: drives the per-project "Unregister project" action. Both
 * fields are optional on the Tauri side (`#[serde(default)]`). Sending
 * `null` from the UI maps to backend defaults via `Option<UnregisterOptions>`.
 *
 *   - `purgeLauncherFiles` (default true): surgically remove launcher-
 *     managed files (.claude/hooks, .claude/scripts, .claude/env, infra
 *     compose YAMLs) AND strip canonical env keys from .env / .claude/env
 *     / .claude/settings.json env / .vscode/settings.json claude-code.env.
 *     User content (agents/skills/CONTEXT_STATE/CLAUDE.md/source code/
 *     user-added .env keys) is preserved.
 *   - `purgeCollections` (default false): drop the project's OWN Weaviate
 *     collections (`<Project>_KnowledgeGraph`, `<Project>_Development`).
 *     Shared collections never touched. OFF by default — collections can
 *     always be rebuilt from /knowledge + source code via
 *     install-bundle --update.
 */
export interface UnregisterOptions {
  purgeLauncherFiles?: boolean;
  purgeCollections?: boolean;
}

/**
 * Mirror of Rust `UnregisterReport` (commands/projects_v2.rs).
 *
 * Returned by `delete_project_v2`. The launcher's settings-tab toast uses
 * `filesPurged.length`, `keysPurgedFromEnv.length`, and
 * `collectionsDropped.length` for a one-line summary; `warnings[]` is
 * surfaced as additional error toasts when non-empty.
 */
export interface UnregisterReport {
  projectId: string;
  projectName: string;
  filesPurged: string[];
  keysPurgedFromEnv: string[];
  collectionsDropped: string[];
  warnings: string[];
}

export interface TierCacheView {
  orchestrator_tier: LicenseTier | string;
  module_licenses: Record<string, unknown>;
  last_validated: number;
  last_error: string | null;
  grace_period_remaining_ms: number | null;
}

/**
 * v0.2.32 §D1: row surface for the per-module license section in the
 * orchestrator-license dialog (`ActivationModal.svelte`).
 *
 * Backed by the `get_module_licenses` Tauri command, which flattens
 * `tier_cache.module_licenses` into rows. Mirrors
 * `launcher/src-tauri/src/commands/licensing.rs::ModuleLicenseRow`.
 *
 * Field semantics:
 *   - `module_id`: stable wire id (e.g. `"vct-rl-reranker"`).
 *   - `display_name`: human-readable name from `vct-module.json`; falls
 *     back to `module_id` when no catalog manifest is available.
 *   - `tier`: per-module tier the server granted (`"pro"` / `"mao"` /
 *     etc.). `"unknown"` when the server response was missing the field.
 *   - `activated_at`: optional activation timestamp. Server may send
 *     either ISO-8601 or numeric epoch — backend pre-stringifies both
 *     so the UI renders verbatim.
 */
export interface ModuleLicenseRow {
  module_id: string;
  display_name: string;
  tier: string;
  activated_at: string | null;
}

/**
 * v0.2.36: result shape for the admin-token machine-rebind command.
 *
 * Mirrors `launcher/src-tauri/src/commands/licensing.rs::AdminRebindResult`.
 * The Rust side orchestrates the full rebind (read license key from
 * keychain, compute machine_id_hash, POST to the edge function) so the
 * frontend never touches the secret directly — the value never crosses
 * the IPC boundary.
 *
 * On success: `success=true`, `user` and `rebound_at` populated.
 * On failure: `success=false`, `error` and `detail` describe the cause
 * (network failure, license_invalid, not_an_admin_token, no_license_key,
 * rebind_failed, service_misconfigured, license_key_invalid_format,
 * machine_id_hash_invalid_format).
 *
 * `machine_id_hash` is ALWAYS populated — useful for displaying the
 * "current machine" label in the dialog regardless of outcome.
 */
export interface AdminRebindResult {
  success: boolean;
  user: string | null;
  rebound_at: string | null;
  error: string | null;
  detail: string | null;
  machine_id_hash: string;
}

/**
 * v0.2.40 L1: per-paid-module license key summary surface.
 *
 * Each paid module (RL Reranker, MAO, etc.) owns a row keyed by
 * `module_id`. The reserved value `'__orchestrator__'` identifies the
 * legacy single-key root tier (preserves v0.2.39 single-key UX after
 * upgrade). The raw key value NEVER crosses the IPC boundary —
 * `redacted_key` is the display-only "ends in ..." label.
 *
 * Mirrors `launcher/src-tauri/src/commands/licensing.rs::LicenseKeySummary`.
 */
export interface LicenseKeySummary {
  module_id: string;
  display_name: string;
  redacted_key: string;
  tier: string | null;
  validated_at: number | null;
  last_validation_error: string | null;
  created_at: number;
  updated_at: number;
}

/**
 * v0.2.40 L1: outcome of a per-module validation round-trip.
 *
 * The Rust `validate_module_license` command soft-fails on network
 * errors and surfaces `stale=true` so the GUI can render a warning
 * badge rather than dropping the user to free tier on a transient
 * blip. `tier='free-on-error'` is a client-only synthetic value used
 * when no cached tier exists either.
 *
 * Mirrors `launcher/src-tauri/src/commands/licensing.rs::ModuleLicenseValidationResult`.
 */
export interface ModuleLicenseValidationResult {
  module_id: string;
  tier: string;
  valid: boolean;
  expires_at: string | null;
  http_status: number;
  error: string | null;
  stale: boolean;
}

/**
 * v0.2.40 L1: append-only validation timeline entry.
 *
 * Mirrors `vct_launcher_core::db::license_keys::LicenseKeyValidationRow`.
 * Used by the License Manager modal to show a per-module "last N
 * validations" history without round-tripping to the keychain.
 */
export interface LicenseKeyValidationRow {
  id: number;
  module_id: string;
  validated_at: number;
  tier: string | null;
  http_status: number;
  error_message: string | null;
}

export interface ModuleCatalogEntry {
  id: string;
  name: string;
  version: string;
  description: string;
  category: string;
  tags: string[];
  license_required: boolean;
  license_variant_ids: string[];
  min_orchestrator_tier: string;
  compatibility_hosts: string[];
  is_licensed: boolean;
  manifest_source: string;
  /**
   * Bug 33: optional visibility hint. Public modules are visible to
   * everyone; `private-test` modules are visible only to users with
   * the server-classified `admin` tier. Missing field is treated as
   * `public` for backward compatibility with manifests written before
   * Bug 33.
   */
  visibility?: 'public' | 'private-test';
  /**
   * Bug 16 + Fix 8: render hint.
   *   - 'bundled'      → always-installed, no Install button (e.g. the launcher itself)
   *   - 'available'    → catalog-listed, has Install action
   *   - 'installed'    → installed, can be configured / uninstalled
   *   - 'subcomponent' → ships with parent module, navigate to dashboard CTA
   *   - 'coming_soon'  → announced, not yet shipped. Renders with a Coming Soon
   *                      badge + Learn-more CTA, no Install. Reserved for items
   *                      with a public roadmap commitment; do NOT use for vapor.
   */
  kind:
    | 'bundled'
    | 'available'
    | 'installed'
    | 'update_available'
    | 'broken'
    | 'subcomponent'
    | 'coming_soon';
  parent_id: string;
  cta_route: string;
  /** For `kind === 'coming_soon'`: which tier this will ship under (e.g. 'pro'). */
  coming_soon_tier?: string;
  /** For `kind === 'coming_soon'`: optional public target window (e.g. 'Q3 2026'). */
  coming_soon_target?: string;
  /**
   * v0.2.31 module-deprecation surface (Layer 1, GUI). When `true`, the
   * module card renders an amber `DEPRECATED` badge near the tier chip;
   * a `<DeprecationBanner>` may render at the top of the module's
   * dashboard (when one exists via `cta_route`). The module continues to
   * work normally — this is a "plan ahead for migration" signal, not a
   * hard block. Populated at catalog-build time once the v0.2.32 poller
   * lands; v0.2.31 defaults to `false` so the surface is forward-compatible.
   */
  deprecated?: boolean;
  /** Optional human-readable message rendered in badge tooltip + banner. */
  deprecation_message?: string;
  /** Optional ISO date (YYYY-MM-DD) for the module's end-of-life date. */
  deprecation_eol_date?: string;
  /** Optional URL pointing at the publisher's migration guide. */
  deprecation_migration_url?: string;
  /**
   * v0.2.33 (Agent B, L0a): set on installed module entries whose
   * `module_id` is NOT advertised by the L0 catalog. The renderer
   * shows a "No longer available in catalog" warning badge.
   */
  catalog_warning?: string;
  /**
   * NEW-3 (2026-05-28): the module's `runtime.type` as declared in its
   * manifest. `"container"` or `"service"` for long-running daemon
   * modules. Empty/absent for builtins and L0-only entries.
   */
  runtime_type?: string;
  /**
   * v0.2.49 Stream A (Bug D unblocker): the module's `install.scope`
   * field as declared in its manifest. `"global"` means installed once
   * per host with per-project opt-out via the enable toggle (Stream B);
   * `"per_project"` means each project gets its own install (legacy
   * shape, retained as future-proofing for modules with truly per-
   * project container state). Empty string for legacy payloads from
   * pre-v0.2.49 launchers — the renderer treats `""` ≡ `"per_project"`
   * for back-compat.
   *
   * Drives the per-project badge variant for catalog tiles: a
   * `scope='global'` module that's installed-anywhere renders as
   * `enabled-globally` (not `installed-elsewhere`) since "installed
   * once = available to all" is the contract.
   */
  install_scope?: 'per_project' | 'global' | '';
}

/**
 * v0.2.33 (Agent B, L0a): L0 fetch status. Maps to the Rust `L0Status`
 * enum. Drives the catalog header banner (Agent E's scope).
 */
export type L0Status =
  | { kind: 'ok'; fetched_at: string; modules_count: number }
  | { kind: 'stale'; cached_fetched_at: string; last_error: string }
  | { kind: 'unavailable'; error: string };

/**
 * v0.2.33 (Agent B, L0a): one parse failure surfaced to the renderer
 * for the "1 module manifest couldn't be parsed" banner (Agent E).
 * `source` is either a file path (on-disk manifest) or `L0:<endpoint>`
 * (L0 envelope parse failure).
 */
export interface ManifestParseError {
  module_id: string;
  source: string;
  error: string;
}

/**
 * v0.2.33 (Agent B, L0a, review §10.c): emitted exactly when
 * `<install_root>/paid-modules/` exists AND
 * `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH` is unset AND the user hasn't
 * dismissed the toast. The renderer surfaces this as a one-shot
 * "I see your dev paid-modules — opt in to render them" toast.
 */
export interface DevAffordanceHint {
  paid_modules_path: string;
  env_var_name: string;
}

/**
 * v0.2.33 (Agent B, L0a): the new `list_module_catalog` Tauri command
 * response shape. Replaces the v0.2.32-era bare `ModuleCatalogEntry[]`.
 *
 * The store unwraps `.modules` into the existing `catalog: ModuleCatalogEntry[]`
 * slot. `l0_status` + `parse_errors` + `dev_affordance_hint` flow into
 * Agent E's banner/toast surfaces.
 */
export interface CatalogResponse {
  modules: ModuleCatalogEntry[];
  l0_status: L0Status;
  parse_errors: ManifestParseError[];
  dev_affordance_hint: DevAffordanceHint | null;
}

export interface ModuleStatusView {
  status: string;
  enabled: boolean;
  installed_at: number;
  last_started_at: number | null;
  last_error: string | null;
}

export interface ModuleInstallCompleteEvent {
  project_id: string;
  module_id: string;
  success: boolean;
  error?: string;
}

/**
 * Gap 2: per-project initial code-graph build status.
 *
 * Mirrors `CodeGraphBuildView` in commands::codegraph (Rust). Fired on
 * the `code-graph-build-progress` Tauri event during a build, and
 * returned by `get_code_graph_build_status`.
 */
export type CodeGraphBuildStatus =
  | 'pending'
  | 'running'
  | 'success'
  // v0.2.73 C-11 / RT-3: inserts succeeded but stale-row prune failed
  // (PRUNE_FAILURES=N, N>0). Terminal, treated as a non-alert warning.
  | 'partial'
  | 'failed'
  | 'skipped';

export interface CodeGraphBuildView {
  project_id: string;
  status: CodeGraphBuildStatus;
  /** ISO 8601 (RFC 3339); null until the build starts. */
  started_at_iso: string | null;
  /** ISO 8601; null until the build reaches a terminal state. */
  finished_at_iso: string | null;
  duration_ms: number | null;
  files_analyzed: number;
  /** File-extension tags, e.g. `["py","ts"]`. */
  languages: string[];
  joern_used: boolean;
  error_message: string | null;
  /** Last ~4 KiB of analyzer stdout/stderr — debugging aid. */
  log_tail: string | null;
  /** Live phase indicator on `running` events (e.g. "scan", "analyze"). */
  current_phase: string | null;
}

/**
 * KG auto-sync (2026-05-12): per-project initial `kg-sync --all` status.
 *
 * Mirrors `KgSyncView` in commands::kg_sync (Rust). Fired on the
 * `kg-sync-progress` Tauri event during a sync, and returned by
 * `get_kg_sync_status`. Shape parallels `CodeGraphBuildView` — same
 * lifecycle states, same optional timestamps, same `current_phase`
 * field for live events.
 */
export type KgSyncStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'failed'
  | 'skipped';

export interface KgSyncView {
  project_id: string;
  status: KgSyncStatus;
  /** ISO 8601 (RFC 3339); null until the sync starts. */
  started_at_iso: string | null;
  /** ISO 8601; null until the sync reaches a terminal state. */
  finished_at_iso: string | null;
  duration_ms: number | null;
  /** Total `.md` files in knowledge/ (per the script's "📚 Found N" header). */
  kg_total: number;
  kg_succeeded: number;
  kg_failed: number;
  /** Intentional non-synces (archived / frontmarker / excluded /
   *  embed-skipped) — v0.2.92 WP-B1 / D12. Live-event only: stored rows
   *  report 0 (the DB has no such column). Treat as optional at use sites
   *  so payloads from an older launcher binary still typecheck. */
  kg_skipped?: number;
  /** Total `.md` files in docs/ (per the script's "📚 Found N" header). */
  docs_total: number;
  docs_succeeded: number;
  docs_failed: number;
  /** Docs-side skip count — see `kg_skipped`. */
  docs_skipped?: number;
  error_message: string | null;
  /** Last ~4 KiB of subprocess stdout/stderr — debugging aid. */
  log_tail: string | null;
  /** Live phase indicator on `running` events
   *  ("scan" | "queued" | "embed" | "knowledge" | "docs" | "finalize"). */
  current_phase: string | null;
}

/**
 * KG summary auto-backfill (v0.2.3 / 2026-05-12): per-project initial
 * `generate-kg-summary.py` pass status.
 *
 * Mirrors `KgSummaryView` in commands::kg_summary (Rust). Fired on the
 * `kg-summary-progress` Tauri event during a backfill, and returned by
 * `get_kg_summary_status`. Shape parallels `KgSyncView` — same lifecycle
 * states, same optional timestamps, same `current_phase` field for live
 * events. Adds `backend` (which fallback chain the summariser picked)
 * and per-node counters (succeeded / unchanged / failed / skipped)
 * specific to the per-file invocation pattern.
 */
export type KgSummaryStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'failed'
  | 'skipped';

export interface KgSummaryView {
  project_id: string;
  status: KgSummaryStatus;
  /** ISO 8601 (RFC 3339); null until the backfill starts. */
  started_at_iso: string | null;
  /** ISO 8601; null until the backfill reaches a terminal state. */
  finished_at_iso: string | null;
  duration_ms: number | null;
  /** Total `.md` files discovered under knowledge/. */
  nodes_total: number;
  /** Files where the summariser wrote a new entry. */
  nodes_succeeded: number;
  /** Files where the summariser detected an existing hash-match (no-op). */
  nodes_unchanged: number;
  /** Files where the summariser raised an exception (sub-fatal). */
  nodes_failed: number;
  /** Files where the summariser exited 0 with "no backend" or "no title". */
  nodes_skipped: number;
  /** Backend the summariser picked: "cli" | "ollama" | "api" | "skip" | null.
   *  null on terminal `skipped`/`failed` rows where nothing ran. */
  backend: string | null;
  error_message: string | null;
  /** Last ~4 KiB of aggregated subprocess output — debugging aid. */
  log_tail: string | null;
  /** Live phase indicator on `running` events ("scan" | "summarise"). */
  current_phase: string | null;
}

/**
 * Defect B (v0.2.68): async project-setup lifecycle.
 *
 * `create_project_v2` returns FAST after the synchronous phase (DB row +
 * `.claude/env`); the heavy phase (bootstrap-collections + install-bundle +
 * post-bundle) runs detached. These types mirror `commands::project_setup`
 * (Rust): the `project://setup-progress` event payload (`SetupProgressEvent`)
 * + the `get_project_setup_status` view (`ProjectSetupView`).
 *
 * `deferred` is a terminal INFORMATIONAL state (e.g. cold-Weaviate bootstrap
 * deferred cleanly) — amber in the banner, NO Retry. `failed` is a genuine
 * subprocess failure — red + Retry.
 */
export type ProjectSetupStatus =
  | 'pending'
  | 'running'
  | 'done'
  | 'deferred'
  | 'failed';

/** Coarse phase label carried on non-terminal events. */
export type ProjectSetupPhase = 'bootstrap' | 'bundle' | 'post_bundle';

export type SetupWarningSeverity = 'info' | 'error';

/** One classified warning — F5 severity split: info/amber (deferral,
 *  preserved-files) vs error/red (genuine subprocess failure). */
export interface SetupWarning {
  message: string;
  severity: SetupWarningSeverity;
}

/** `project://setup-progress` event payload. Intermediate (`running`) events
 *  carry `phase` + empty `warnings`; the terminal event carries the full
 *  classified `warnings` list (F5) + `error` on `failed`. */
export interface SetupProgressEvent {
  project_id: string;
  project_name: string;
  status: ProjectSetupStatus;
  phase: ProjectSetupPhase | null;
  warnings: SetupWarning[];
  error: string | null;
}

/** `get_project_setup_status` view — for banner mount / reload (when the
 *  live event stream missed the terminal event). */
export interface ProjectSetupView {
  project_id: string;
  status: ProjectSetupStatus;
  phase: ProjectSetupPhase | null;
  started_at_iso: string | null;
  finished_at_iso: string | null;
  duration_ms: number | null;
  warnings: SetupWarning[];
  error_message: string | null;
}

/** Mirrors `InstallHealth` in commands/installer.rs. Returned by
 *  `check_install_health` once at app startup. When `all_ok` is false the
 *  layout renders `InstallHealthGate.svelte` as a blocking modal. */
export interface InstallHealth {
  /** Resolved install-root path (null = developer mode, no install root
   *  found by walking up from the launcher binary). */
  install_root: string | null;
  has_venv: boolean;
  has_state_dir: boolean;
  has_env_with_kg: boolean;
  mcp_servers_ok: boolean;
  /** True when every signal passes OR when in developer mode. */
  all_ok: boolean;
}

/* ─── Bundle-staleness census (v0.2.92 WP-D GUI half) ──────────────────────
 *
 * Mirrors `commands/bundle_staleness.rs` — the Tauri wrapper around the
 * READ-ONLY `python -m vco_lib.bundle_staleness --json` census.
 *
 * The three-state verdict is load-bearing and must never be collapsed:
 * `unknown` means "VCO could not prove this project's bundle state", which
 * is NOT the same as `current` and NOT the same as `stale`. Likewise a
 * census that could not RUN is reported with `determined: false` and a
 * `summary` of `null` — never a zeroed summary, because "0 stale" and
 * "I have no idea" must be distinguishable by every caller.
 */

/** Per-project verdict. Faithful mirror of the Python census verdicts. */
export type BundleVerdict = 'current' | 'stale' | 'unknown';

/** One project row of the census (Python §5.1 row shape, minus the
 *  display-only `recorded` / `counts` blocks the chip does not use). */
export interface BundleStalenessProject {
  /** Launcher project id (`projects.id`). */
  id: string;
  name: string;
  /** Resolved project folder, as the census saw it. */
  folder: string;
  verdict: BundleVerdict;
  /** Machine reason: `noop` / `files_changed` / `folder_missing` /
   *  `manifest_missing` / `manifest_unparseable` / `engine_error` /
   *  `self_check_failed`. Renders as the chip's tooltip cause. */
  reason: string;
  /** Files a bundle update would change. Non-empty only when `stale`. */
  changed_files: string[];
  /** Count of user-modified files the update would preserve. */
  user_modified: number;
}

/** Population counts. Present ONLY on a determined census. */
export interface BundleStalenessSummary {
  current: number;
  stale: number;
  unknown: number;
}

/** `bundle_staleness_census` command result. */
export interface BundleStalenessCensus {
  /** False when the census could not run at all (no interpreter, no
   *  orchestrator root, non-zero exit, unparseable output, or a registry
   *  the census could not read). When false, `summary` is null and
   *  `projects` is empty — an explicit "could not determine", never
   *  "everything is fine". */
  determined: boolean;
  /** Why the census could not be determined. Null when `determined`. */
  error: string | null;
  /** `launcher.db` on a determined census; the raw Python value (e.g.
   *  `unavailable`) or null otherwise. */
  registry: string | null;
  /** Running orchestrator semver, for display next to the count. */
  running_version: string | null;
  projects: BundleStalenessProject[];
  summary: BundleStalenessSummary | null;
  /** Remedy strings echoed from the census (GUI path / CLI command). */
  remedy_gui: string | null;
  remedy_cli: string | null;
}
